#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send direct Feishu messages through an authenticated lark-cli user.

The project deliberately keeps Feishu operations behind ``lark-cli``.  This
adapter is small but strict: it never invokes a shell, always selects the user
identity explicitly, validates the CLI's 50-character idempotency limit, and
distinguishes an explicit rejection from an outcome that cannot be known
(notably a local timeout after the request may already have reached Feishu).
"""

from __future__ import annotations

import json
import hmac
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Union


Runner = Callable[..., Any]
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
MAX_DIAGNOSTIC_LENGTH = 500


class FeishuCliNotificationError(RuntimeError):
    """A message was explicitly rejected or could not be attempted."""


class FeishuCliAmbiguousDeliveryError(FeishuCliNotificationError):
    """The caller cannot safely infer whether Feishu accepted the message."""


@dataclass(frozen=True)
class NotificationReceipt:
    """Minimal, non-sensitive result returned after a confirmed send."""

    message_id: str = ""
    idempotency_key: str = ""


class FeishuCliNotifier:
    """CLI-only direct-message transport.

    ``runner`` follows :func:`subprocess.run` and is injectable for tests.
    Message content is sent to lark-cli's raw API transport on stdin, never in
    the process argument list where local process inspection could expose it.
    No shell is involved.
    """

    def __init__(
        self,
        *,
        cli_path: Union[str, os.PathLike[str]] = "lark-cli",
        profile: str = "kol-workflow",
        timeout: float = 60,
        runner: Optional[Runner] = None,
    ) -> None:
        if not profile or not str(profile).strip():
            raise ValueError("lark-cli profile cannot be empty")
        if timeout <= 0:
            raise ValueError("notification timeout must be greater than zero")
        requested_cli_path = os.fspath(cli_path)
        self.cli_path = shutil.which(requested_cli_path) or requested_cli_path
        self.profile = str(profile).strip()
        self.timeout = float(timeout)
        self._runner = runner or subprocess.run
        self._locked_user_id = ""

    def send_markdown(
        self,
        *,
        user_id: str,
        markdown: str,
        idempotency_key: str,
    ) -> NotificationReceipt:
        """Send one markdown direct message as the authenticated CLI user."""
        markdown_text = str(markdown or "").strip()
        if not markdown_text:
            raise ValueError("Feishu reminder markdown cannot be empty")
        content = json.dumps(
            {
                "schema": "2.0",
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": markdown_text},
                    ]
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._send_message(
            user_id=user_id,
            content=content,
            content_flag="--content",
            content_description="markdown",
            idempotency_key=idempotency_key,
            msg_type="interactive",
            accept_empty_success=True,
        )

    def send_card(
        self,
        *,
        user_id: str,
        card: Mapping[str, Any],
        idempotency_key: str,
    ) -> NotificationReceipt:
        """Send one interactive message card as the authenticated CLI user."""
        if not isinstance(card, Mapping) or not card:
            raise ValueError("Feishu reminder card must be a non-empty object")
        try:
            content = json.dumps(
                card,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Feishu reminder card must be JSON-serializable") from exc
        return self._send_message(
            user_id=user_id,
            content=content,
            content_flag="--content",
            content_description="card",
            idempotency_key=idempotency_key,
            msg_type="interactive",
            accept_empty_success=True,
        )

    def _send_message(
        self,
        *,
        user_id: str,
        content: str,
        content_flag: str,
        content_description: str,
        idempotency_key: str,
        msg_type: str = "",
        accept_empty_success: bool = False,
    ) -> NotificationReceipt:
        recipient = str(user_id or "").strip()
        payload = str(content or "").strip()
        key = str(idempotency_key or "").strip()
        if not recipient:
            raise ValueError("Feishu reminder user OpenID cannot be empty")
        if not payload:
            raise ValueError(
                f"Feishu reminder {content_description} cannot be empty"
            )
        if not key:
            raise ValueError("message idempotency key cannot be empty")
        if len(key) > 50:
            raise ValueError("message idempotency key cannot exceed 50 characters")
        if IDEMPOTENCY_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError(
                "message idempotency key may contain only letters, numbers, '.', '_', ':', '-'"
            )

        # The approved destination is the user authenticated by this profile.
        # Verify immediately before the mutating command so a mistyped .env
        # OpenID can never direct operational data to another person.
        self.assert_current_user(recipient)

        effective_type = msg_type or "text"
        request_body = json.dumps(
            {
                "receive_id": recipient,
                "msg_type": effective_type,
                "content": payload,
                "uuid": key,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        command = [
            self.cli_path,
            "--profile",
            self.profile,
            "api",
            "POST",
            "/open-apis/im/v1/messages",
            "--as",
            "user",
            "--params",
            '{"receive_id_type":"open_id"}',
            "--data",
            "-",
            "--format",
            "json",
        ]
        try:
            completed = self._runner(
                command,
                input=request_body,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise FeishuCliAmbiguousDeliveryError(
                "lark-cli message send timed out; delivery outcome is unknown and requires manual confirmation"
            ) from None
        except FileNotFoundError as exc:
            raise FeishuCliNotificationError(
                f"lark-cli executable was not found: {self._sanitize(self.cli_path)}"
            ) from exc
        except OSError as exc:
            raise FeishuCliNotificationError(
                f"unable to start lark-cli message send: {self._sanitize(str(exc))}"
            ) from exc

        stdout = self._to_text(getattr(completed, "stdout", ""))
        stderr = self._to_text(getattr(completed, "stderr", ""))
        return_code = int(getattr(completed, "returncode", 0) or 0)
        # Some lark-cli releases successfully send interactive cards but emit no
        # receipt on stdout.  This behavior was verified by reading the sent
        # interactive messages back from the same P2P chat.  Keep markdown and
        # any non-zero/noisy result conservative.
        if (
            accept_empty_success
            and return_code == 0
            and not stdout.strip()
            and not stderr.strip()
        ):
            return NotificationReceipt(idempotency_key=key)
        try:
            envelope = self._parse_json(stdout)
        except ValueError as exc:
            diagnostic = self._sanitize(stderr or "non-JSON CLI output")
            if return_code == 0:
                raise FeishuCliAmbiguousDeliveryError(
                    "lark-cli exited successfully but its delivery receipt was unreadable; "
                    "delivery outcome is unknown and requires manual confirmation"
                ) from exc
            raise FeishuCliAmbiguousDeliveryError(
                f"lark-cli message send ended without a verifiable response: {diagnostic}; "
                "delivery outcome is unknown and requires manual confirmation"
            ) from exc

        payload = self._confirmed_payload(envelope, return_code=return_code)
        message_id = self._find_message_id(payload)
        return NotificationReceipt(message_id=message_id, idempotency_key=key)

    def current_user_id(self) -> str:
        """Return the verified OpenID of the CLI profile's current user."""
        command = [
            self.cli_path,
            "--profile",
            self.profile,
            "auth",
            "status",
            "--verify",
            "--json",
        ]
        try:
            completed = self._runner(
                command,
                input=None,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise FeishuCliNotificationError(
                "lark-cli current-user verification timed out; no message was sent"
            ) from None
        except FileNotFoundError as exc:
            raise FeishuCliNotificationError(
                f"lark-cli executable was not found: {self._sanitize(self.cli_path)}"
            ) from exc
        except OSError as exc:
            raise FeishuCliNotificationError(
                f"unable to start lark-cli current-user verification: {self._sanitize(str(exc))}"
            ) from exc

        stdout = self._to_text(getattr(completed, "stdout", ""))
        stderr = self._to_text(getattr(completed, "stderr", ""))
        return_code = int(getattr(completed, "returncode", 0) or 0)
        try:
            envelope = self._parse_json(stdout)
        except ValueError as exc:
            raise FeishuCliNotificationError(
                "lark-cli current-user verification returned unreadable JSON; no message was sent"
            ) from exc
        if return_code != 0:
            detail = self._error_detail(
                envelope.get("error") if isinstance(envelope, Mapping) else stderr
            )
            raise FeishuCliNotificationError(
                f"lark-cli current-user verification failed (exit={return_code}): {detail}"
            )
        if isinstance(envelope, Mapping) and "ok" in envelope:
            if envelope.get("ok") is not True:
                raise FeishuCliNotificationError(
                    "lark-cli current-user verification was rejected: "
                    f"{self._error_detail(envelope.get('error'))}"
                )
            payload = envelope.get("data")
        else:
            payload = envelope
        open_ids = sorted(set(self._find_user_open_ids(payload)))
        if not open_ids:
            # Compatibility fallback for older CLI responses that returned a
            # single top-level openId without an identities.user wrapper.
            open_ids = sorted(set(self._find_open_ids(payload)))
        if len(open_ids) != 1:
            raise FeishuCliNotificationError(
                "lark-cli current-user verification did not return exactly one user OpenID; no message was sent"
            )
        return open_ids[0]

    def assert_current_user(self, user_id: str) -> str:
        """Require ``user_id`` to be the profile's verified current OpenID."""
        expected = str(user_id or "").strip()
        if not expected:
            raise ValueError("Feishu reminder user OpenID cannot be empty")
        if self._locked_user_id:
            if not hmac.compare_digest(self._locked_user_id, expected):
                raise FeishuCliNotificationError(
                    "configured reminder recipient differs from the user already locked for this process; no message was sent"
                )
            return self._locked_user_id
        current = self.current_user_id()
        if not hmac.compare_digest(current, expected):
            raise FeishuCliNotificationError(
                "configured reminder recipient is not the current authenticated lark-cli user; no message was sent"
            )
        self._locked_user_id = current
        return current

    def _confirmed_payload(self, envelope: Any, *, return_code: int) -> Any:
        if not isinstance(envelope, Mapping):
            raise FeishuCliAmbiguousDeliveryError(
                "lark-cli returned a non-object delivery receipt; delivery outcome is unknown"
            )

        if "ok" in envelope:
            if envelope.get("ok") is not True:
                detail = self._error_detail(envelope.get("error"))
                raise FeishuCliNotificationError(
                    f"Feishu rejected the reminder message: {detail}"
                )
            payload = envelope.get("data")
        else:
            payload = envelope

        if return_code != 0:
            # Structured output with an explicit non-zero result is a confirmed
            # failure.  Unstructured failures were classified as ambiguous
            # earlier so they cannot be retried under a different aggregate key.
            detail = self._error_detail(payload)
            raise FeishuCliNotificationError(
                f"lark-cli message send failed (exit={return_code}): {detail}"
            )

        if isinstance(payload, Mapping):
            code = payload.get("code")
            if code not in (None, 0, "0"):
                detail = self._sanitize(str(payload.get("msg") or "OpenAPI error"))
                raise FeishuCliNotificationError(
                    f"Feishu message API rejected the reminder (code={code}): {detail}"
                )
            if "data" in payload and code in (0, "0"):
                return payload.get("data") or {}
        return payload if payload is not None else {}

    def _error_detail(self, value: Any) -> str:
        if isinstance(value, Mapping):
            code = value.get("code")
            message = value.get("message") or value.get("msg") or "CLI error"
            detail = self._sanitize(str(message))
            return f"{detail} (code={self._sanitize(str(code))})" if code else detail
        return self._sanitize(str(value or "unknown CLI error"))

    @classmethod
    def _find_message_id(cls, value: Any) -> str:
        if isinstance(value, Mapping):
            for name in ("message_id", "messageId", "id"):
                candidate = value.get(name)
                if candidate:
                    return str(candidate)
            for child in value.values():
                found = cls._find_message_id(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_message_id(child)
                if found:
                    return found
        return ""

    @classmethod
    def _find_open_ids(cls, value: Any):
        if isinstance(value, Mapping):
            for name, child in value.items():
                normalized_name = re.sub(r"[^a-z]", "", str(name).lower())
                if normalized_name in {"openid", "useropenid"}:
                    candidate = str(child or "").strip()
                    if re.fullmatch(r"ou_[A-Za-z0-9_-]+", candidate):
                        yield candidate
                yield from cls._find_open_ids(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._find_open_ids(child)

    @classmethod
    def _find_user_open_ids(cls, value: Any):
        if isinstance(value, Mapping):
            for name, child in value.items():
                normalized_name = re.sub(r"[^a-z]", "", str(name).lower())
                if normalized_name in {"user", "currentuser", "useridentity"}:
                    yield from cls._find_open_ids(child)
                else:
                    yield from cls._find_user_open_ids(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._find_user_open_ids(child)

    @staticmethod
    def _parse_json(text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty JSON output")
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, character in enumerate(stripped):
                if character not in "{[":
                    continue
                try:
                    value, _ = decoder.raw_decode(stripped[index:])
                except json.JSONDecodeError:
                    continue
                return value
            raise ValueError("no JSON value found")

    @staticmethod
    def _to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _sanitize(value: str) -> str:
        safe = " ".join(str(value or "").split())
        secret_names = (
            r"device[_-]?code|access[_-]?token|refresh[_-]?token|"
            r"tenant[_-]?access[_-]?token|app[_-]?secret|authorization"
        )
        safe = re.sub(
            rf"(?i)([\"']?(?:{secret_names})[\"']?\s*[:=]\s*[\"']?)"
            r"[^\s\"',}\]]+",
            r"\1<redacted>",
            safe,
        )
        safe = re.sub(r"(?i)\bBearer\s+[^\s\"',}\]]+", "Bearer <redacted>", safe)
        safe = re.sub(r"\bou_[A-Za-z0-9_-]+\b", "<redacted-user-id>", safe)
        return safe[:MAX_DIAGNOSTIC_LENGTH] or "no diagnostic output"


__all__ = [
    "FeishuCliAmbiguousDeliveryError",
    "FeishuCliNotificationError",
    "FeishuCliNotifier",
    "NotificationReceipt",
]
