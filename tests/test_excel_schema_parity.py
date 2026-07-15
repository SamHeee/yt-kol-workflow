from pathlib import Path

import pandas as pd

from export.excel_exporter import (
    BatchExcelExporter,
    ExcelExporter,
    INFLUENCER_COLUMNS,
    INFLUENCER_VIDEO_COLUMNS,
    SEARCH_TASK_COLUMNS,
    SEARCH_VIDEO_COLUMNS,
)


TABLE_FILES = {
    "search_tasks.xlsx": list(SEARCH_TASK_COLUMNS.values()),
    "search_videos.xlsx": list(SEARCH_VIDEO_COLUMNS.values()),
    "influencers.xlsx": list(INFLUENCER_COLUMNS.values()),
    "influencer_videos.xlsx": list(INFLUENCER_VIDEO_COLUMNS.values()),
}


def test_keyword_exporter_always_creates_four_schema_complete_files(tmp_path):
    exporter = ExcelExporter(
        str(tmp_path),
        keyword="camping review",
        timestamp="20260714_120000",
    )

    paths = exporter.ensure_all_files()

    assert len(paths) == 4
    for filename, expected_headers in TABLE_FILES.items():
        path = Path(exporter.get_output_dir()) / filename
        assert path.exists()
        frame = pd.read_excel(path)
        assert list(frame.columns) == expected_headers
        assert frame.empty


def test_search_video_export_uses_composite_key_and_typed_created_time(tmp_path):
    exporter = ExcelExporter(
        str(tmp_path),
        keyword="camping review",
        timestamp="20260714_120001",
    )
    path = exporter.export_search_videos(
        [{
            "video_id": "video-1",
            "video_url": "https://www.youtube.com/watch?v=video-1",
            "published_at": "2026-07-10T12:30:00Z",
            "video_record_date": 1783699200000,
            "view_count": 1234,
            "is_qualified": True,
        }],
        "camping review",
    )

    frame = pd.read_excel(path)
    assert list(frame.columns) == list(SEARCH_VIDEO_COLUMNS.values())
    assert frame.loc[0, "唯一键"] == "camping review|video-1"
    assert pd.api.types.is_datetime64_any_dtype(frame["视频记录日期"])
    assert frame.loc[0, "Views"] == 1234
    assert bool(frame.loc[0, "是否通过筛选"]) is True
    assert pd.isna(frame.loc[0, "多行文本"])


def test_batch_exporter_always_creates_four_summary_files(tmp_path):
    exporter = BatchExcelExporter(str(tmp_path))
    paths = exporter.export_summary()

    assert {Path(path).name for path in paths} == {
        "search_tasks_all.xlsx",
        "search_videos_all.xlsx",
        "influencers_all.xlsx",
        "influencer_videos_all.xlsx",
    }
