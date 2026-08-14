import copy
import json
import subprocess
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from feishu.sample_logistics import (
    CARRIER_FIELD,
    CONTACT_EMAIL_FIELD,
    DELIVERED_DATE_FIELD,
    KOL_NAME_FIELD,
    LAST_QUERIED_AT_FIELD,
    LATEST_EVENT_AT_FIELD,
    LATEST_PROGRESS_FIELD,
    LOGISTICS_STATUS_FIELD,
    REMINDER_ERROR_FIELD,
    REMINDER_EVENT_KEY_FIELD,
    REMINDER_SENT_AT_FIELD,
    REMINDER_STATUS_FIELD,
    REMINDER_TYPE_FIELD,
    SAMPLE_RECORD_ID_FIELD,
    SAMPLE_STATUS_FIELD,
    SOURCE_SYNCED_AT_FIELD,
    STAGNATION_STARTED_AT_FIELD,
    SYNCED_SHIPMENT_KEY_FIELD,
    TRACKING_NUMBER_FIELD,
)
from notifications.feishu_cli import (
    FeishuCliAmbiguousDeliveryError,
    FeishuCliNotificationError,
    FeishuCliNotifier,
    NotificationReceipt,
)
from workflow.sample_reminders import (
    BEIJING_TZ,
    DELIVERY_REMINDER,
    REMINDER_FAILED,
    REMINDER_MANUAL,
    REMINDER_SENDING,
    REMINDER_SENT,
    STAGNATION_REMINDER,
    ReminderDeliveryAmbiguousError,
    SampleReminderError,
    build_daily_reminder_card,
    build_daily_reminder_markdown,
    build_digest_idempotency_key,
    dispatch_sample_reminders,
    plan_sample_reminders,
)


UTC = timezone.utc


def utc_ms(value):
    return int(value.astimezone(UTC).timestamp() * 1000)


class FakeClient:
    def __init__(self, records):
        self.records = {
            record["record_id"]: copy.deepcopy(record)
            for record in records
        }
        self.update_calls = []

    def get_all_records(self, table_id):
        assert table_id == "tbl-samples"
        return copy.deepcopy(list(self.records.values()))

    def batch_get_records(self, table_id, record_ids):
        assert table_id == "tbl-samples"
        return [
            copy.deepcopy(self.records[record_id])
            for record_id in record_ids
            if record_id in self.records
        ]

    def batch_update_records(self, table_id, updates):
        assert table_id == "tbl-samples"
        self.update_calls.append(copy.deepcopy(updates))
        for update in updates:
            self.records[update["record_id"]]["fields"].update(update["fields"])
        return len(updates)


class FakeNotifier:
    def __init__(self, *, client=None, error=None):
        self.client = client
        self.error = error
        self.calls = []
        self.assert_calls = []

    def assert_current_user(self, user_id):
        self.assert_calls.append(user_id)
        return user_id

    def send_card(self, **kwargs):
        if self.client is not None:
            assert all(
                record["fields"].get(REMINDER_STATUS_FIELD) == REMINDER_SENDING
                for record in self.client.records.values()
            )
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return NotificationReceipt(message_id="om_test", idempotency_key=kwargs["idempotency_key"])


def card_visible_text(card):
    values = []

    def visit(value):
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, str):
                values.append(content)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(card)
    return "\n".join(values)


def delivered_record(delivered_at, *, reminder_status="", reminder_key=""):
    return {
        "record_id": "rec-delivered",
        "fields": {
            SAMPLE_RECORD_ID_FIELD: "SMP-000001",
            KOL_NAME_FIELD: "Creator One",
            CONTACT_EMAIL_FIELD: [{"text": "creator_one@example.com"}],
            SAMPLE_STATUS_FIELD: "已签收",
            LOGISTICS_STATUS_FIELD: "已签收",
            DELIVERED_DATE_FIELD: utc_ms(delivered_at),
            LATEST_EVENT_AT_FIELD: utc_ms(delivered_at),
            LATEST_PROGRESS_FIELD: "Delivered, In/At Mailbox",
            CARRIER_FIELD: "DHL",
            TRACKING_NUMBER_FIELD: "TRACK-001",
            SYNCED_SHIPMENT_KEY_FIELD: "shipment-one",
            REMINDER_STATUS_FIELD: reminder_status,
            REMINDER_EVENT_KEY_FIELD: reminder_key,
        },
    }


def stagnant_record(started_at, source_synced_at, *, record_id="rec-stagnant"):
    return {
        "record_id": record_id,
        "fields": {
            SAMPLE_RECORD_ID_FIELD: "SMP-000002",
            KOL_NAME_FIELD: "Creator Two",
            CONTACT_EMAIL_FIELD: [{"text": "creator_two@example.com"}],
            SAMPLE_STATUS_FIELD: "已寄出",
            LOGISTICS_STATUS_FIELD: "运输中",
            STAGNATION_STARTED_AT_FIELD: utc_ms(started_at),
            LATEST_EVENT_AT_FIELD: utc_ms(started_at),
            SOURCE_SYNCED_AT_FIELD: utc_ms(source_synced_at),
            LAST_QUERIED_AT_FIELD: utc_ms(source_synced_at),
            LATEST_PROGRESS_FIELD: "Departed origin facility",
            CARRIER_FIELD: "UPS",
            TRACKING_NUMBER_FIELD: "TRACK-002",
            SYNCED_SHIPMENT_KEY_FIELD: "shipment-two",
        },
    }


def test_delivery_becomes_due_on_next_beijing_calendar_day():
    delivered_at = datetime(2026, 8, 3, 23, 55, tzinfo=BEIJING_TZ)
    record = delivered_record(delivered_at)

    same_day, skipped = plan_sample_reminders(
        [record],
        now=datetime(2026, 8, 3, 23, 59, tzinfo=BEIJING_TZ),
    )
    assert same_day == []
    assert skipped["delivery_same_day"] == 1

    next_day, _ = plan_sample_reminders(
        [record],
        now=datetime(2026, 8, 4, 0, 1, tzinfo=BEIJING_TZ),
    )
    assert len(next_day) == 1
    assert next_day[0].reminder_type == DELIVERY_REMINDER


def test_cancelled_sample_never_emits_stale_delivery_reminder():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = delivered_record(now - timedelta(days=1))
    record["fields"][SAMPLE_STATUS_FIELD] = "已取消"

    candidates, skipped = plan_sample_reminders([record], now=now)

    assert candidates == []
    assert skipped["sample_status_ineligible"] == 1


def test_stagnation_requires_72_hours_and_fresh_provider_data():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    almost = stagnant_record(now - timedelta(hours=71, minutes=59), now - timedelta(hours=1))
    due = stagnant_record(
        now - timedelta(hours=72),
        now - timedelta(hours=1),
        record_id="rec-due",
    )
    stale = stagnant_record(
        now - timedelta(hours=100),
        now - timedelta(hours=37),
        record_id="rec-stale",
    )

    candidates, skipped = plan_sample_reminders([almost, due, stale], now=now)

    assert [candidate.record_id for candidate in candidates] == ["rec-due"]
    assert candidates[0].reminder_type == STAGNATION_REMINDER
    assert skipped["stagnation_not_due"] == 1
    assert skipped["provider_data_stale"] == 1


def test_same_event_is_not_planned_after_sent_or_ambiguous_outcome():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = delivered_record(now - timedelta(days=1))
    first, _ = plan_sample_reminders([record], now=now)
    assert len(first) == 1

    for status, reason in [
        (REMINDER_SENT, "already_sent"),
        (REMINDER_SENDING, "sending_in_progress"),
        (REMINDER_MANUAL, "manual_confirmation_required"),
    ]:
        duplicate = copy.deepcopy(record)
        duplicate["fields"][REMINDER_EVENT_KEY_FIELD] = first[0].event_key
        duplicate["fields"][REMINDER_STATUS_FIELD] = status
        planned, skipped = plan_sample_reminders([duplicate], now=now)
        assert planned == []
        assert skipped[reason] == 1


def test_dry_run_plans_both_types_without_writes_or_messages():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    records = [
        delivered_record(now - timedelta(days=1)),
        stagnant_record(now - timedelta(hours=80), now - timedelta(hours=2)),
    ]
    records[0]["fields"]["收件信息"] = "Sensitive Recipient, 123 Example Road"
    client = FakeClient(records)
    notifier = FakeNotifier()

    result = dispatch_sample_reminders(
        client=client,
        table_id="tbl-samples",
        notifier=notifier,
        now=now,
        dry_run=True,
    )

    assert result["planned_delivery_reminders"] == 1
    assert result["planned_stagnation_reminders"] == 1
    assert result["messages_sent"] == 0
    assert client.update_calls == []
    assert notifier.calls == []
    assert notifier.assert_calls == []


def test_dispatch_marks_sending_before_one_interactive_card_then_marks_sent():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    records = [
        delivered_record(now - timedelta(days=1)),
        stagnant_record(now - timedelta(hours=80), now - timedelta(hours=2)),
    ]
    client = FakeClient(records)
    notifier = FakeNotifier(client=client)

    result = dispatch_sample_reminders(
        client=client,
        table_id="tbl-samples",
        notifier=notifier,
        recipient_user_id="ou_recipient",
        base_url="https://example.feishu.cn/wiki/fake_test_base_token",
        now=now,
    )

    assert len(client.update_calls) == 2
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    card = call["card"]
    visible_text = card_visible_text(card)
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "purple"
    assert card["header"]["title"]["content"] == "样品物流每日提醒｜2026-08-04"
    assert "已送达待邮件确认（1）" in visible_text
    assert "连续 72 小时无进展（1）" in visible_text
    assert "KOL Name：** Creator One" in visible_text
    assert r"联系邮箱：** creator\_one@example.com" in visible_text
    assert "物流状态：** 已签收" in visible_text
    assert "最新物流进度：** Delivered, In/At Mailbox" in visible_text
    assert "物流：** DHL｜TRACK-001" in visible_text
    assert "KOL Name：** Creator Two" in visible_text
    assert r"联系邮箱：** creator\_two@example.com" in visible_text
    assert "物流状态：** 运输中" in visible_text
    assert "最新物流进度：** Departed origin facility" in visible_text
    assert "物流：** UPS｜TRACK-002" in visible_text
    button = next(
        element
        for element in card["body"]["elements"]
        if element["tag"] == "button"
    )
    assert button["text"]["content"] == "查看样品跟踪表"
    assert button["type"] == "primary"
    assert button["behaviors"] == [
        {
            "type": "open_url",
            "default_url": "https://example.feishu.cn/wiki/fake_test_base_token?table=tbl-samples",
        }
    ]
    assert "Sensitive Recipient" not in visible_text
    assert "收件信息" not in visible_text
    assert len(call["idempotency_key"]) <= 50
    assert notifier.assert_calls == ["ou_recipient"]
    assert result["messages_sent"] == 1
    assert result["sent"] == 2
    for record in client.records.values():
        assert record["fields"][REMINDER_STATUS_FIELD] == REMINDER_SENT
        assert record["fields"][REMINDER_SENT_AT_FIELD] == utc_ms(now)
        assert record["fields"][REMINDER_ERROR_FIELD] == ""


@pytest.mark.parametrize(
    ("notifier_error", "expected_exception", "expected_status"),
    [
        (
            FeishuCliNotificationError(
                "recipient was rejected: TRACK-001 creator_one@example.com "
                "Delivered, In/At Mailbox"
            ),
            SampleReminderError,
            REMINDER_FAILED,
        ),
        (
            FeishuCliAmbiguousDeliveryError(
                "timeout; delivery outcome is unknown: TRACK-001 "
                "creator_one@example.com Delivered, In/At Mailbox"
            ),
            ReminderDeliveryAmbiguousError,
            REMINDER_MANUAL,
        ),
    ],
)
def test_failure_and_ambiguous_delivery_have_distinct_persisted_states(
    notifier_error,
    expected_exception,
    expected_status,
):
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    client = FakeClient([delivered_record(now - timedelta(days=1))])
    notifier = FakeNotifier(error=notifier_error)

    with pytest.raises(expected_exception) as captured:
        dispatch_sample_reminders(
            client=client,
            table_id="tbl-samples",
            notifier=notifier,
            recipient_user_id="ou_recipient",
            now=now,
        )

    fields = client.records["rec-delivered"]["fields"]
    assert fields[REMINDER_STATUS_FIELD] == expected_status
    assert fields[REMINDER_ERROR_FIELD]
    assert fields[REMINDER_SENT_AT_FIELD] is None
    traceback_text = "".join(
        traceback.format_exception(
            captured.type,
            captured.value,
            captured.value.__traceback__,
        )
    )
    for sensitive_value in (
        "TRACK-001",
        "creator_one@example.com",
        "Delivered, In/At Mailbox",
    ):
        assert sensitive_value not in fields[REMINDER_ERROR_FIELD]
        assert sensitive_value not in traceback_text
    assert captured.value.__cause__ is None


def test_cli_notifier_uses_user_identity_markdown_and_short_idempotency_key():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if "auth" in command:
            payload = {
                "ok": True,
                "data": {
                    "identities": {
                        "bot": {"openId": "ou_bot_identity"},
                        "user": {"openId": "ou_recipient"},
                    }
                },
            }
        else:
            payload = {"ok": True, "data": {"message_id": "om_confirmed"}}
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    notifier = FeishuCliNotifier(
        cli_path=r"C:\tools\lark-cli.exe",
        profile="kol-test",
        timeout=17,
        runner=runner,
    )
    receipt = notifier.send_markdown(
        user_id="ou_recipient",
        markdown="## Reminder",
        idempotency_key="sample-rem-123",
    )

    assert receipt.message_id == "om_confirmed"
    assert calls[0][0][-4:] == ["auth", "status", "--verify", "--json"]
    command, kwargs = calls[1]
    assert command[:6] == [
        r"C:\tools\lark-cli.exe",
        "--profile",
        "kol-test",
        "api",
        "POST",
        "/open-apis/im/v1/messages",
    ]
    assert command[command.index("--as") + 1] == "user"
    assert command[command.index("--params") + 1] == (
        '{"receive_id_type":"open_id"}'
    )
    assert command[command.index("--data") + 1] == "-"
    assert "ou_recipient" not in command
    assert "## Reminder" not in command
    assert "sample-rem-123" not in command
    body = json.loads(kwargs["input"])
    assert body["receive_id"] == "ou_recipient"
    assert body["msg_type"] == "interactive"
    assert body["uuid"] == "sample-rem-123"
    markdown_card = json.loads(body["content"])
    assert markdown_card["body"]["elements"] == [
        {"tag": "markdown", "content": "## Reminder"}
    ]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 17


def test_cli_notifier_sends_interactive_card_json():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if "auth" in command:
            payload = {
                "ok": True,
                "data": {"user": {"open_id": "ou_recipient"}},
            }
        else:
            payload = {"ok": True, "data": {"message_id": "om_card"}}
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
        )

    notifier = FeishuCliNotifier(runner=runner)
    card = {
        "schema": "2.0",
        "header": {
            "template": "purple",
            "title": {"tag": "plain_text", "content": "Reminder"},
        },
        "body": {"elements": []},
    }
    receipt = notifier.send_card(
        user_id="ou_recipient",
        card=card,
        idempotency_key="sample-rem-card",
    )

    command, kwargs = calls[1]
    request_body = json.loads(kwargs["input"])
    assert request_body["msg_type"] == "interactive"
    assert json.loads(request_body["content"]) == card
    assert request_body["receive_id"] == "ou_recipient"
    assert request_body["uuid"] == "sample-rem-card"
    assert "Reminder" not in command
    assert "ou_recipient" not in command
    assert "sample-rem-card" not in command
    assert command[command.index("--data") + 1] == "-"
    assert kwargs["shell"] is False
    assert receipt.message_id == "om_card"


def test_cli_interactive_card_and_wrapped_markdown_accept_verified_empty_success():
    def runner(command, **kwargs):
        if "auth" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"ok": True, "data": {"user": {"open_id": "ou_recipient"}}}
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    card_notifier = FeishuCliNotifier(runner=runner)
    receipt = card_notifier.send_card(
        user_id="ou_recipient",
        card={"schema": "2.0", "body": {"elements": []}},
        idempotency_key="sample-card-empty-receipt",
    )
    assert receipt.message_id == ""
    assert receipt.idempotency_key == "sample-card-empty-receipt"

    markdown_notifier = FeishuCliNotifier(runner=runner)
    markdown_receipt = markdown_notifier.send_markdown(
        user_id="ou_recipient",
        markdown="test",
        idempotency_key="sample-markdown-empty-receipt",
    )
    assert markdown_receipt.message_id == ""
    assert markdown_receipt.idempotency_key == "sample-markdown-empty-receipt"


def test_cli_timeout_is_explicitly_ambiguous_and_key_length_is_validated():
    call_count = 0

    def timeout_runner(command, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"ok": True, "data": {"user": {"open_id": "ou_recipient"}}}
                ),
                stderr="",
            )
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    notifier = FeishuCliNotifier(runner=timeout_runner)
    sensitive_markdown = "TRACK-SECRET creator@example.com logistics progress"
    with pytest.raises(
        FeishuCliAmbiguousDeliveryError,
        match="outcome is unknown",
    ) as captured:
        notifier.send_markdown(
            user_id="ou_recipient",
            markdown=sensitive_markdown,
            idempotency_key="sample-rem-timeout",
        )
    traceback_text = "".join(
        traceback.format_exception(
            captured.type,
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert sensitive_markdown not in traceback_text
    assert captured.value.__cause__ is None
    with pytest.raises(ValueError, match="50"):
        notifier.send_markdown(
            user_id="ou_recipient",
            markdown="test",
            idempotency_key="x" * 51,
        )


def test_current_cli_user_must_equal_configured_recipient_before_send():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"ok": True, "data": {"identity": {"user_open_id": "ou_current"}}}
            ),
            stderr="",
        )

    notifier = FeishuCliNotifier(runner=runner)
    assert notifier.current_user_id() == "ou_current"
    with pytest.raises(FeishuCliNotificationError, match="current authenticated"):
        notifier.assert_current_user("ou_someone_else")
    assert all("+messages-send" not in command for command in calls)


def test_targeted_sample_ids_and_limit_are_applied_before_dry_run_plan():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    first = delivered_record(now - timedelta(days=1))
    second = copy.deepcopy(first)
    second["record_id"] = "rec-second"
    second["fields"][SAMPLE_RECORD_ID_FIELD] = "SMP-000099"
    second["fields"][SYNCED_SHIPMENT_KEY_FIELD] = "shipment-99"
    client = FakeClient([first, second])

    only_second = dispatch_sample_reminders(
        client=client,
        table_id="tbl-samples",
        notifier=FakeNotifier(),
        sample_ids=["SMP-000099"],
        limit=1,
        now=now,
        dry_run=True,
    )
    assert only_second["planned_events"] == 1
    expected, _ = plan_sample_reminders([second], now=now)
    assert only_second["event_keys"] == [expected[0].event_key]


def test_digest_key_is_deterministic_and_below_cli_limit():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    candidates, _ = plan_sample_reminders(
        [
            delivered_record(now - timedelta(days=1)),
            stagnant_record(now - timedelta(hours=80), now - timedelta(hours=1)),
        ],
        now=now,
    )
    assert build_digest_idempotency_key(candidates) == build_digest_idempotency_key(
        list(reversed(candidates))
    )
    assert len(build_digest_idempotency_key(candidates)) <= 50


def test_process_crash_leaves_timestamped_digest_lease_before_delivery():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    client = FakeClient([delivered_record(now - timedelta(days=1))])

    class CrashBeforeDelivery(FakeNotifier):
        def send_card(self, **kwargs):
            self.calls.append(kwargs)
            raise SystemExit("simulated process crash")

    notifier = CrashBeforeDelivery()
    with pytest.raises(SystemExit, match="simulated process crash"):
        dispatch_sample_reminders(
            client=client,
            table_id="tbl-samples",
            notifier=notifier,
            recipient_user_id="ou_recipient",
            now=now,
        )

    fields = client.records["rec-delivered"]["fields"]
    assert fields[REMINDER_STATUS_FIELD] == REMINDER_SENDING
    lease_text = fields[REMINDER_ERROR_FIELD]
    assert lease_text.startswith("__KOL_REMINDER_LEASE_V1__:")
    lease = json.loads(lease_text.split(":", 1)[1])
    assert lease["attempted_at"] == "2026-08-04T01:00:00Z"
    assert lease["idempotency_key"] == notifier.calls[0]["idempotency_key"]
    assert lease["event_count"] == 1


def test_recent_crash_lease_is_skipped_without_calling_notifier():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    client = FakeClient([delivered_record(now - timedelta(days=1))])

    class CrashNotifier(FakeNotifier):
        def send_card(self, **kwargs):
            raise SystemExit

    with pytest.raises(SystemExit):
        dispatch_sample_reminders(
            client=client,
            table_id="tbl-samples",
            notifier=CrashNotifier(),
            recipient_user_id="ou_recipient",
            now=now,
        )

    retry_notifier = FakeNotifier()
    result = dispatch_sample_reminders(
        client=client,
        table_id="tbl-samples",
        notifier=retry_notifier,
        recipient_user_id="ou_recipient",
        now=now + timedelta(minutes=10),
    )
    assert result["messages_sent"] == 0
    assert result["skipped_counts"]["sending_lease_in_progress"] == 1
    assert retry_notifier.assert_calls == []
    assert retry_notifier.calls == []


def test_stale_lease_reuses_original_key_and_excludes_new_candidates():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    original = delivered_record(now - timedelta(days=1))
    client = FakeClient([original])

    class CrashNotifier(FakeNotifier):
        def send_card(self, **kwargs):
            self.calls.append(kwargs)
            raise SystemExit

    crashed = CrashNotifier()
    with pytest.raises(SystemExit):
        dispatch_sample_reminders(
            client=client,
            table_id="tbl-samples",
            notifier=crashed,
            recipient_user_id="ou_recipient",
            now=now,
        )
    original_key = crashed.calls[0]["idempotency_key"]

    new_record = delivered_record(now - timedelta(days=2))
    new_record["record_id"] = "rec-new-due"
    new_record["fields"][SAMPLE_RECORD_ID_FIELD] = "SMP-NEW-DUE"
    new_record["fields"][SYNCED_SHIPMENT_KEY_FIELD] = "shipment-new"
    client.records[new_record["record_id"]] = copy.deepcopy(new_record)

    retry_notifier = FakeNotifier()
    result = dispatch_sample_reminders(
        client=client,
        table_id="tbl-samples",
        notifier=retry_notifier,
        recipient_user_id="ou_recipient",
        now=now + timedelta(minutes=31),
    )

    assert result["idempotency_key"] == original_key
    assert retry_notifier.calls[0]["idempotency_key"] == original_key
    assert result["sent"] == 1
    assert "SMP-000001" in card_visible_text(retry_notifier.calls[0]["card"])
    assert "SMP-NEW-DUE" not in card_visible_text(retry_notifier.calls[0]["card"])
    assert client.records["rec-new-due"]["fields"].get(REMINDER_STATUS_FIELD, "") == ""


def test_unparseable_stale_sending_lease_requires_manual_confirmation():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = delivered_record(now - timedelta(days=1))
    expected, _ = plan_sample_reminders([record], now=now)
    record["fields"].update(
        {
            REMINDER_STATUS_FIELD: REMINDER_SENDING,
            REMINDER_EVENT_KEY_FIELD: expected[0].event_key,
            REMINDER_ERROR_FIELD: "legacy sending state without lease metadata",
        }
    )
    client = FakeClient([record])
    notifier = FakeNotifier()

    result = dispatch_sample_reminders(
        client=client,
        table_id="tbl-samples",
        notifier=notifier,
        recipient_user_id="ou_recipient",
        now=now,
    )

    assert result["messages_sent"] == 0
    assert result["skipped_counts"][
        "invalid_sending_lease_manual_confirmation"
    ] == 1
    assert client.records["rec-delivered"]["fields"][REMINDER_STATUS_FIELD] == REMINDER_MANUAL
    assert notifier.calls == []


def test_full_short_tracking_number_is_rendered_and_table_link_is_sanitized():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = delivered_record(now - timedelta(days=1))
    record["fields"][TRACKING_NUMBER_FIELD] = "ABC"
    candidates, _ = plan_sample_reminders([record], now=now)

    markdown = build_daily_reminder_markdown(
        candidates,
        now=now,
        base_url="https://my.feishu.cn/wiki/safeToken?secret=must-not-survive#fragment",
        table_id="tbl-samples",
    )
    assert "物流：DHL｜ABC" in markdown
    assert r"\*\*\*" not in markdown
    assert "secret" not in markdown
    assert "#fragment" not in markdown
    assert "https://my.feishu.cn/wiki/safeToken?table=tbl-samples" in markdown

    unsafe_markdown = build_daily_reminder_markdown(
        candidates,
        now=now,
        base_url="https://my.feishu.cn.evil.example/wiki/safeToken",
        table_id="tbl-samples",
    )
    assert "打开样品跟踪表" not in unsafe_markdown


def test_full_tracking_number_is_not_truncated_by_long_carrier_text():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = delivered_record(now - timedelta(days=1))
    full_tracking_number = "9" * 240
    record["fields"][CARRIER_FIELD] = "C" * 240
    record["fields"][TRACKING_NUMBER_FIELD] = full_tracking_number
    candidates, _ = plan_sample_reminders([record], now=now)

    markdown = build_daily_reminder_markdown(candidates, now=now)
    card = build_daily_reminder_card(candidates, now=now)

    assert full_tracking_number in markdown
    assert full_tracking_number in card_visible_text(card)


def test_tracking_number_and_progress_variants_are_not_redacted():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = stagnant_record(
        now - timedelta(hours=80),
        now - timedelta(hours=1),
    )
    record["fields"][TRACKING_NUMBER_FIELD] = "TRACK-001"
    record["fields"][LATEST_PROGRESS_FIELD] = (
        "carrier echoed track-001, TRACK 001, T.R.A.C.K_0/0:1, "
        "TRACK–001 and TRACK#001"
    )
    candidates, _ = plan_sample_reminders([record], now=now)

    markdown = build_daily_reminder_markdown(
        candidates,
        now=now,
        base_url="https://my.feishu.cn/wiki/safeToken",
        table_id="tbl-samples",
    )

    assert "物流：UPS｜TRACK-001" in markdown
    assert (
        r"最新物流进度：carrier echoed track-001, TRACK 001, "
        r"T.R.A.C.K\_0/0:1, TRACK–001 and TRACK#001"
    ) in markdown
    assert r"\*\*\*\*\*-001" not in markdown
    card = build_daily_reminder_card(candidates, now=now)
    card_text = card_visible_text(card)
    assert "物流：** UPS｜TRACK-001" in card_text
    assert "TRACK 001" in card_text


def test_reminder_common_fields_use_explicit_fallbacks():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    record = delivered_record(now - timedelta(days=1))
    candidates, _ = plan_sample_reminders([record], now=now)
    candidate = replace(
        candidates[0],
        sample_record_id="",
        kol_name="",
        contact_email="",
        carrier="",
        tracking_number="",
        logistics_status="",
        progress="",
    )

    markdown = build_daily_reminder_markdown([candidate], now=now)
    card = build_daily_reminder_card([candidate], now=now)
    card_text = card_visible_text(card)

    assert "**未编号**" in markdown
    assert "KOL Name：未填写 KOL" in markdown
    assert "联系邮箱：未填写" in markdown
    assert "物流状态：暂无轨迹" in markdown
    assert "最新物流进度：未返回物流轨迹" in markdown
    assert "物流：承运商未填写｜运单号未填写" in markdown
    assert "KOL Name：** 未填写 KOL" in card_text
    assert "联系邮箱：** 未填写" in card_text
    assert "物流状态：** 暂无轨迹" in card_text
    assert "最新物流进度：** 未返回物流轨迹" in card_text
    assert "物流：** 承运商未填写｜运单号未填写" in card_text


def test_stagnation_event_key_changes_when_node_changes_at_same_timestamp():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    original = stagnant_record(
        now - timedelta(hours=80),
        now - timedelta(hours=1),
    )
    changed = copy.deepcopy(original)
    changed["fields"][LATEST_PROGRESS_FIELD] = "Arrived at destination facility"

    original_candidates, _ = plan_sample_reminders([original], now=now)
    changed_candidates, _ = plan_sample_reminders([changed], now=now)

    assert original_candidates[0].event_key != changed_candidates[0].event_key


def test_equivalent_no_trajectory_statuses_share_one_event_key():
    now = datetime(2026, 8, 4, 9, 0, tzinfo=BEIJING_TZ)
    pending = stagnant_record(
        now - timedelta(hours=80),
        now - timedelta(hours=1),
    )
    pending["fields"][LOGISTICS_STATUS_FIELD] = "待查询"
    pending["fields"][LATEST_PROGRESS_FIELD] = ""
    pending["fields"][LATEST_EVENT_AT_FIELD] = None
    no_track = copy.deepcopy(pending)
    no_track["fields"][LOGISTICS_STATUS_FIELD] = "暂无轨迹"

    pending_candidates, _ = plan_sample_reminders([pending], now=now)
    no_track_candidates, _ = plan_sample_reminders([no_track], now=now)

    assert pending_candidates[0].event_key == no_track_candidates[0].event_key
