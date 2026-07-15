import pandas as pd

from export.excel_exporter import ExcelExporter
from export.summary_builder import (
    METADATA_COLUMNS,
    PROVENANCE_SHEET,
    TABLE_ORDER,
    TABLE_SPECS,
    _dedupe_rows,
    _normalize_source_row,
    build_summary_workbook,
)
from feishu.schema import PRIMARY_FIELD_NAME


def test_influencer_dedupe_merges_new_enrichment_fields():
    rows = [
        {
            "Channel ID": "UC1",
            "KOL Name": "手动确认",
            "最新发布日期": None,
            "断更评估": None,
            "频道初步判断": None,
        },
        {
            "Channel ID": "UC1",
            "KOL Name": "Sarah",
            "最新发布日期": "2026-07-01 00:00:00",
            "断更评估": "持续更新",
            "频道初步判断": "领域=户外/露营; 内容=产品测评; 主体=个人创作者; 自有品牌=未发现",
        },
    ]

    deduped, stats = _dedupe_rows(rows, "Channel ID", merge_nonblank=True)

    assert stats["duplicate_rows_removed"] == 1
    assert deduped[0]["KOL Name"] == "Sarah"
    assert deduped[0]["断更评估"] == "持续更新"
    assert deduped[0]["频道初步判断"].startswith("领域=户外/露营")


def test_influencer_dedupe_never_downgrades_confirmed_name():
    rows = [
        {"Channel ID": "UC1", "KOL Name": "Sarah"},
        {"Channel ID": "UC1", "KOL Name": "手动确认"},
    ]

    deduped, _ = _dedupe_rows(rows, "Channel ID", merge_nonblank=True)

    assert deduped[0]["KOL Name"] == "Sarah"


def test_summary_contract_has_four_schema_sheets_and_separate_provenance():
    assert TABLE_ORDER == [
        "search_tasks",
        "search_videos",
        "influencers",
        "influencer_videos",
    ]
    for table_key in TABLE_ORDER:
        headers = TABLE_SPECS[table_key]["expected_headers"]
        assert headers[0] == PRIMARY_FIELD_NAME
        assert not set(METADATA_COLUMNS) & set(headers)


def test_summary_normalizes_legacy_headers_and_rebuilds_search_video_key():
    row = _normalize_source_row(
        "search_videos",
        {
            "搜索关键词": "camping review",
            "Video ID": "video-1",
            "频道ID": "channel-1",
            "视频标题": "Legacy title",
        },
    )

    assert row["唯一键"] == "camping review|video-1"
    assert row["Channel ID"] == "channel-1"
    assert row["Video Title"] == "Legacy title"
    assert "频道ID" not in row


def test_build_summary_writes_four_business_sheets_and_provenance(tmp_path):
    source_root = tmp_path / "source"
    exporter = ExcelExporter(
        str(source_root),
        keyword="camping review",
        timestamp="20260714_120000",
    )
    exporter.export_search_tasks([
        {
            "task_key": "camping review",
            "keyword": "camping review",
            "status": "成功",
        }
    ])
    exporter.ensure_all_files()

    output = tmp_path / "summary.xlsx"
    payload = build_summary_workbook([str(source_root)], str(output))

    workbook = pd.ExcelFile(output)
    assert workbook.sheet_names == [*TABLE_ORDER, PROVENANCE_SHEET]
    for table_key in TABLE_ORDER:
        headers = list(pd.read_excel(output, sheet_name=table_key, nrows=0).columns)
        assert headers == TABLE_SPECS[table_key]["expected_headers"]
        assert not set(METADATA_COLUMNS) & set(headers)

    provenance = pd.read_excel(output, sheet_name=PROVENANCE_SHEET)
    assert set(METADATA_COLUMNS) <= set(provenance.columns)
    assert len(provenance) == 4
    assert payload["provenance_rows"] == 4
