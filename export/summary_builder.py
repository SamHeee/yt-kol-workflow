#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a merged summary workbook from one or more workflow output folders."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from export import excel_exporter
from feishu import schema as feishu_schema
from feishu.value_utils import clean_text, is_blank


# Kept as a public compatibility constant.  These columns now live only in the
# dedicated provenance sheet instead of being repeated on every business row.
METADATA_COLUMNS = ["来源批次", "来源关键词目录", "来源文件"]
PROVENANCE_SHEET = "来源信息"
PROVENANCE_COLUMNS = [
    "数据表",
    *METADATA_COLUMNS,
    "来源记录数",
    "读取状态",
    "错误信息",
]

# Match the canonical Feishu/schema order.  ``来源信息`` is written last.
TABLE_ORDER = ["search_tasks", "search_videos", "influencers", "influencer_videos"]


def _schema_headers(field_definitions: Iterable[Tuple[str, int]]) -> List[str]:
    """Return the physical Excel header order for a Feishu table.

    Older schema modules did not explicitly list Feishu's mandatory default
    primary field.  Keep this fallback so historical branches can still build
    summaries while new exporters expose it directly.
    """
    primary_field = getattr(feishu_schema, "PRIMARY_FIELD_NAME", "多行文本")
    headers = [name for name, _field_type in field_definitions]
    if primary_field not in headers:
        headers.insert(0, primary_field)
    return headers


def _export_headers(columns_name: str, schema_name: str) -> List[str]:
    columns = getattr(excel_exporter, columns_name, None)
    if columns is not None:
        return list(columns.values())
    return _schema_headers(getattr(feishu_schema, schema_name))


TABLE_SPECS: Dict[str, Dict[str, Any]] = {
    "search_tasks": {
        "filename": "search_tasks.xlsx",
        "dedupe_key": "唯一键",
        "expected_headers": _export_headers("SEARCH_TASK_COLUMNS", "SEARCH_TASKS_FIELDS"),
    },
    "search_videos": {
        "filename": "search_videos.xlsx",
        "dedupe_key": "唯一键",
        "expected_headers": _export_headers("SEARCH_VIDEO_COLUMNS", "SEARCH_VIDEOS_FIELDS"),
    },
    "influencers": {
        "filename": "influencers.xlsx",
        "dedupe_key": "Channel ID",
        "expected_headers": _export_headers("INFLUENCER_COLUMNS", "INFLUENCERS_FIELDS"),
    },
    "influencer_videos": {
        "filename": "influencer_videos.xlsx",
        "dedupe_key": "唯一键",
        "expected_headers": _export_headers("INFLUENCER_VIDEO_COLUMNS", "INFLUENCER_VIDEOS_FIELDS"),
    },
}


# Historical local exports used friendly Chinese headers.  New workbooks use
# exact Feishu names, but these aliases keep old output folders mergeable.
LEGACY_HEADER_ALIASES: Dict[str, Dict[str, str]] = {
    "search_tasks": {},
    "search_videos": {
        "频道ID": "Channel ID",
        "频道名称": "Channel Name",
        "视频标题": "Video Title",
        "发布时间": "Publish Time",
        "播放量": "Views",
        "点赞数": "Likes",
        "评论数": "Comments",
        "时长(秒)": "Duration (sec)",
        "时长": "Duration (H:M:S)",
        "标签": "Tags",
        "有字幕": "Has Subtitles",
        "通过筛选": "是否通过筛选",
    },
    "influencers": {
        "频道ID": "Channel ID",
        "频道名称": "Channel Name",
        "代表视频互动率(%)": "代表视频互动率",
    },
    "influencer_videos": {
        "频道ID": "Channel ID",
        "频道名称": "Channel Name",
        "视频标题": "Video Title",
        "发布时间": "Publish Time",
        "播放量": "Views",
        "点赞数": "Likes",
        "评论数": "Comments",
        "时长(秒)": "Duration (sec)",
        "时长": "Duration (H:M:S)",
        "标签": "Tags",
    },
}


def _iter_source_files(batch_dir: Path, filename: str) -> Iterable[Path]:
    search_root = batch_dir / "per_keyword" if (batch_dir / "per_keyword").exists() else batch_dir
    for path in sorted(search_root.rglob(filename)):
        if path.name.startswith("~$"):
            continue
        yield path


def _read_excel_rows(path: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    df = pd.read_excel(path, dtype=object)
    df = df.where(pd.notna(df), None)
    headers = [str(column) for column in df.columns]
    return headers, df.to_dict(orient="records")


def _normalize_source_row(table_key: str, source_row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a current or legacy row to the exact Feishu Excel contract."""
    expected_headers = TABLE_SPECS[table_key]["expected_headers"]
    aliases = LEGACY_HEADER_ALIASES.get(table_key, {})
    normalized = {header: source_row.get(header) for header in expected_headers}

    # Exact/new headers take precedence.  A legacy alias only fills a blank
    # canonical cell, preventing old/new columns from surviving side-by-side.
    for legacy_header, canonical_header in aliases.items():
        legacy_value = source_row.get(legacy_header)
        if canonical_header in normalized and is_blank(normalized.get(canonical_header)):
            if not is_blank(legacy_value):
                normalized[canonical_header] = legacy_value

    # Historical workbooks predate the persisted unique-key columns.  Rebuild
    # the same keys used by the workflow formatters before dedupe and sync.
    if table_key == "search_tasks" and is_blank(normalized.get("唯一键")):
        keyword = clean_text(normalized.get("搜索关键词"))
        if keyword:
            normalized["唯一键"] = keyword
    elif table_key == "search_videos" and is_blank(normalized.get("唯一键")):
        video_id = clean_text(normalized.get("Video ID"))
        if video_id:
            normalized["唯一键"] = f"{clean_text(normalized.get('搜索关键词'))}|{video_id}"
    elif table_key == "influencer_videos" and is_blank(normalized.get("唯一键")):
        video_id = clean_text(normalized.get("Video ID"))
        if video_id:
            normalized["唯一键"] = video_id

    return normalized


def _collect_table_rows(
    table_key: str,
    batch_dirs: List[Path],
) -> Tuple[List[str], List[Dict[str, Any]], Dict[str, Any]]:
    spec = TABLE_SPECS[table_key]
    filename = spec["filename"]
    headers = list(spec.get("expected_headers", []))
    rows: List[Dict[str, Any]] = []
    files: List[str] = []
    failed_files: List[Dict[str, str]] = []
    provenance_rows: List[Dict[str, Any]] = []
    ignored_headers = set()

    for batch_dir in batch_dirs:
        for source_file in _iter_source_files(batch_dir, filename):
            keyword_dir = source_file.parent.name
            try:
                source_headers, source_rows = _read_excel_rows(source_file)
            except Exception as exc:
                failed_files.append({"file": str(source_file), "error": str(exc)})
                provenance_rows.append({
                    "数据表": table_key,
                    "来源批次": batch_dir.name,
                    "来源关键词目录": keyword_dir,
                    "来源文件": str(source_file.resolve()),
                    "来源记录数": 0,
                    "读取状态": "失败",
                    "错误信息": str(exc),
                })
                continue

            files.append(str(source_file.resolve()))
            recognized_headers = set(headers) | set(LEGACY_HEADER_ALIASES.get(table_key, {}))
            ignored_headers.update(header for header in source_headers if header not in recognized_headers)
            for source_row in source_rows:
                rows.append(_normalize_source_row(table_key, source_row))
            provenance_rows.append({
                "数据表": table_key,
                "来源批次": batch_dir.name,
                "来源关键词目录": keyword_dir,
                "来源文件": str(source_file.resolve()),
                "来源记录数": len(source_rows),
                "读取状态": "成功",
                "错误信息": "",
            })

    stats = {
        "table_key": table_key,
        "source_files": len(files),
        "source_rows": len(rows),
        "failed_files": failed_files,
        "ignored_source_headers": sorted(ignored_headers),
        "provenance_rows": provenance_rows,
    }
    return headers, rows, stats


def _dedupe_rows(
    rows: List[Dict[str, Any]],
    key_column: str,
    *,
    merge_nonblank: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    seen: Dict[str, Dict[str, Any]] = {}
    deduped: List[Dict[str, Any]] = []
    duplicate_rows = 0
    blank_key_rows = 0

    for row in rows:
        key = clean_text(row.get(key_column))
        if not key:
            blank_key_rows += 1
            deduped.append(row)
            continue
        if key in seen:
            duplicate_rows += 1
            if merge_nonblank:
                _merge_influencer_row(seen[key], row)
            continue
        seen[key] = row
        deduped.append(row)

    return deduped, {
        "dedupe_key": key_column,
        "unique_keys": len(seen),
        "duplicate_rows_removed": duplicate_rows,
        "blank_key_rows_kept": blank_key_rows,
    }


def _merge_influencer_row(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    """Merge a newer duplicate without letting legacy blank columns win.

    The summary builder frequently combines old workbooks (which predate the
    enrichment columns) with new ones.  Keep the first row for provenance, but
    fill its blank cells from later rows and prefer a confirmed KOL name over
    the ``手动确认`` placeholder.
    """
    current_name = clean_text(target.get("KOL Name"))
    incoming_name = clean_text(incoming.get("KOL Name"))
    if incoming_name and incoming_name != "手动确认" and current_name in {"", "手动确认"}:
        target["KOL Name"] = incoming.get("KOL Name")

    current_latest = pd.to_datetime(target.get("最新发布日期"), errors="coerce", utc=True)
    incoming_latest = pd.to_datetime(incoming.get("最新发布日期"), errors="coerce", utc=True)
    if pd.notna(incoming_latest) and (pd.isna(current_latest) or incoming_latest > current_latest):
        target["最新发布日期"] = incoming.get("最新发布日期")
        if not is_blank(incoming.get("断更评估")):
            target["断更评估"] = incoming.get("断更评估")

    for column, value in incoming.items():
        if column == "KOL Name":
            continue
        if is_blank(target.get(column)) and not is_blank(value):
            target[column] = value


def _normalize_cell(value: Any, field_type: Optional[int] = None) -> Any:
    if is_blank(value):
        return None
    coercer = getattr(excel_exporter, "_coerce_excel_value", None)
    if coercer is not None:
        value = coercer(value, field_type)
        if is_blank(value):
            return None
    return value


def _write_workbook(
    output_path: Path,
    tables: Dict[str, Dict[str, Any]],
    provenance_rows: Optional[List[Dict[str, Any]]] = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        for table_key in TABLE_ORDER:
            table = tables[table_key]
            headers = table["headers"]
            field_types = getattr(excel_exporter, "TABLE_FIELD_TYPES", {}).get(table_key, {})
            matrix = [
                {
                    header: _normalize_cell(row.get(header), field_types.get(header))
                    for header in headers
                }
                for row in table["rows"]
            ]
            df = pd.DataFrame(matrix, columns=headers)
            df.to_excel(writer, index=False, sheet_name=table_key)
            excel_exporter._style_worksheet(writer.sheets[table_key], field_types)

        provenance_matrix = [
            {header: _normalize_cell(row.get(header)) for header in PROVENANCE_COLUMNS}
            for row in (provenance_rows or [])
        ]
        provenance_df = pd.DataFrame(provenance_matrix, columns=PROVENANCE_COLUMNS)
        provenance_df.to_excel(writer, index=False, sheet_name=PROVENANCE_SHEET)
        excel_exporter._style_worksheet(writer.sheets[PROVENANCE_SHEET], {})


def build_summary_workbook(
    batch_dirs: List[str],
    output_path: str,
    *,
    dedupe: bool = True,
    stats_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge workflow output folders into four business sheets plus provenance."""
    resolved_batch_dirs = [Path(path).resolve() for path in batch_dirs]
    missing = [str(path) for path in resolved_batch_dirs if not path.exists()]
    if missing:
        raise FileNotFoundError(f"输出目录不存在: {missing}")

    output = Path(output_path).resolve()
    tables: Dict[str, Dict[str, Any]] = {}
    table_stats: Dict[str, Any] = {}
    provenance_rows: List[Dict[str, Any]] = []

    for table_key in TABLE_ORDER:
        headers, rows, stats = _collect_table_rows(table_key, resolved_batch_dirs)
        provenance_rows.extend(stats.pop("provenance_rows", []))
        spec = TABLE_SPECS[table_key]
        key_column = spec["dedupe_key"]
        if dedupe and key_column in headers:
            rows, dedupe_stats = _dedupe_rows(
                rows,
                key_column,
                merge_nonblank=(table_key == "influencers"),
            )
            stats.update(dedupe_stats)
        elif dedupe:
            stats.update({
                "dedupe_key": key_column,
                "duplicate_rows_removed": 0,
                "blank_key_rows_kept": 0,
                "missing_dedupe_key": True,
            })

        stats["output_rows"] = len(rows)
        tables[table_key] = {"headers": headers, "rows": rows}
        table_stats[table_key] = stats

    _write_workbook(output, tables, provenance_rows)

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "batch_dirs": [str(path) for path in resolved_batch_dirs],
        "output_path": str(output),
        "dedupe": dedupe,
        "tables": table_stats,
        "provenance_rows": len(provenance_rows),
    }

    stats_output = Path(stats_path).resolve() if stats_path else output.with_suffix(".stats.json")
    stats_output.parent.mkdir(parents=True, exist_ok=True)
    stats_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["stats_path"] = str(stats_output)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并 KOL 输出目录，生成四张业务表和来源信息表")
    parser.add_argument("batch_dirs", nargs="+", help="一个或多个批次输出目录")
    parser.add_argument("--output", "-o", default="", help="输出 xlsx 路径")
    parser.add_argument("--no-dedupe", action="store_true", help="不执行默认去重")
    parser.add_argument("--stats", default="", help="统计 JSON 输出路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output
    if not output:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = str(Path("output") / f"summary_{stamp}" / "kol_summary_tables.xlsx")

    result = build_summary_workbook(
        args.batch_dirs,
        output,
        dedupe=not args.no_dedupe,
        stats_path=args.stats or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
