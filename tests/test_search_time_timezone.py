from datetime import datetime, timedelta, timezone

from export.excel_exporter import _excel_datetime
from feishu.schema import _ts, format_search_task_record
from feishu.value_utils import timestamp_ms


def test_aware_search_time_preserves_instant_across_schema_and_excel():
    shanghai = datetime(2026, 10, 20, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    expected = int(shanghai.timestamp() * 1000)
    record = format_search_task_record({"search_time": shanghai.isoformat()})
    assert record["fields"]["搜索时间"] == expected
    exported = _excel_datetime(shanghai)
    assert exported == datetime(2026, 10, 20, 12, 0)
    assert timestamp_ms(exported) == expected


def test_legacy_youtube_timestamps_keep_their_utc_meaning():
    expected = int(datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _ts("2026-10-20 12:00:00") == expected
    assert _ts("2026-10-20T12:00:00Z") == expected
    assert timestamp_ms("2026-10-20 12:00:00") == expected
