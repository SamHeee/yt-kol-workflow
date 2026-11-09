from openpyxl import load_workbook

from export.excel_exporter import ExcelExporter, read_snapshot_metadata
from export.summary_builder import _dedupe_rows, build_summary_workbook
from feishu.workbook_sync import build_fields, TABLE_CONFIGS, load_source_rows
from workflow.recent_video_metrics import calculate_recent_metrics, MEAN_FIELD, MEDIAN_FIELD


def row(time, mean, median, status=True):
    value = {"Channel ID": "UC1", MEAN_FIELD: mean, MEDIAN_FIELD: median, "__observation_time__": time}
    if status:
        value["__recent_snapshot__"] = {"status": "success"}
    return value


def test_merge_keeps_mean_and_median_from_newest_success_as_one_pair():
    items = [row(20, 3, 2), row(10, 100, 90), row(30, None, None, False)]
    result, _ = _dedupe_rows(items, "Channel ID", merge_nonblank=True)
    assert (result[0][MEAN_FIELD], result[0][MEDIAN_FIELD]) == (3, 2)


def test_newer_empty_success_is_not_filled_with_stale_values():
    result, _ = _dedupe_rows([row(10, 100, 50), row(20, None, None), row(5, 200, 100)], "Channel ID", merge_nonblank=True)
    assert result[0][MEAN_FIELD] is None and result[0][MEDIAN_FIELD] is None


def test_export_merge_sync_retains_empty_success_and_number_formats(tmp_path):
    exporter = ExcelExporter(str(tmp_path / "batch"), "test", timestamp="20261030_120000")
    snapshot = calculate_recent_metrics([], channel_id="UC1")
    exporter.export_influencers([{"channel_id": "UC1", "subscriber_count": 12345, **snapshot}])
    path = tmp_path / "summary.xlsx"
    build_summary_workbook([str(tmp_path / "batch")], str(path))
    rows = load_source_rows(path, "influencers")
    assert rows[0]["__recent_snapshot__"]["status"] == "success"
    fields = build_fields(rows[0], TABLE_CONFIGS["influencers"], {
        MEAN_FIELD: {"type": 2}, MEDIAN_FIELD: {"type": 2}}, {MEAN_FIELD: 500, MEDIAN_FIELD: 400})
    assert fields == {MEAN_FIELD: None, MEDIAN_FIELD: None}
    wb = load_workbook(path)
    ws = wb["influencers"]
    headers = [c.value for c in ws[1]]
    assert ws.cell(2, headers.index("订阅数")+1).number_format == "#,##0"
    assert ws.cell(2, headers.index(MEAN_FIELD)+1).number_format == "#,##0.00"
    assert "__recent_snapshot__" not in headers
    wb.close()


def test_old_workbook_blank_or_partial_metrics_do_not_clear_existing_values():
    meta = {MEAN_FIELD: {"type": 2}, MEDIAN_FIELD: {"type": 2}}
    for source in ({}, {MEAN_FIELD: 10}):
        assert build_fields(source, TABLE_CONFIGS["influencers"], meta,
                            {MEAN_FIELD: 500, MEDIAN_FIELD: 400}) == {}


def test_apify_email_manual_value_is_protected_from_workbook_import():
    result = build_fields({"apify邮箱": "new@example.com"}, TABLE_CONFIGS["influencers"],
                          {"apify邮箱": {"type": 1}}, {"apify邮箱": "manual@example.com"})
    assert "apify邮箱" not in result
