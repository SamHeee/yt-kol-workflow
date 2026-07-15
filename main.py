#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube网红开发工作流系统 - CLI入口

用法:
  python main.py search --keyword "knix leak proof review" --feishu-app-token TOKEN
  python main.py batch --keywords-file keywords.txt --feishu-app-token TOKEN
  python main.py search --resume temp/state_xxx.json
  python main.py exclusion --add-brand "Knix"
  python main.py exclusion --list
  python main.py quota
"""

import sys
import os
import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Set

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    WorkflowConfig, YouTubeConfig, FeishuConfig, BrandExclusion,
    parse_keywords_file, KeywordTask, PROJECT_ROOT,
)
from utils.logger import setup_logger, _to_rfc3339_boundary
from youtube.quota import (
    QUOTA_COSTS,
    QuotaTracker,
    assert_budget_available,
    print_quota_estimate,
)
from feishu.bitable import BitableClient
from feishu.client_factory import (
    FeishuClientContext,
    create_bitable_client_from_config,
    initialize_created_base_schema,
    mask_app_token,
)
from feishu.preflight import run_feishu_preflight
from feishu.schema import (
    SchemaManager,
    SEARCH_TASKS_TABLE, SEARCH_VIDEOS_TABLE,
    INFLUENCERS_TABLE, INFLUENCER_VIDEOS_TABLE,
    format_search_task_record, format_search_video_record,
    format_influencer_record, format_influencer_video_record,
)
from workflow.phase_a_search import run_phase_a
from workflow.phase_b_filter import run_phase_b
from filter.channel_dedup import deduplicate_channels as run_phase_c
from workflow.phase_d_detail import run_phase_d
from workflow.seen_channels import SeenChannelStore
from workflow.state import WorkflowState
from export.excel_exporter import ExcelExporter, BatchExcelExporter


logger = logging.getLogger("kol_workflow")


YOUTUBE_SEARCH_ORDER_CHOICES = [
    "relevance",
    "viewCount",
    "date",
    "rating",
    "title",
]


def _feishu_progress(message: str) -> None:
    print(f"[飞书] {message}")


def _create_feishu_context(
    config: FeishuConfig,
    *,
    create_base_if_missing: bool = True,
) -> FeishuClientContext:
    """Prepare CLI/app auth and resolve or create the Base target once."""
    context = create_bitable_client_from_config(
        config,
        create_base_if_missing=create_base_if_missing,
        progress=_feishu_progress,
    )
    config.app_token = context.app_token
    if context.created_base:
        print(f"[飞书] 已自动创建多维表格: {context.base_name}")
    if context.base_url:
        print(f"[飞书] 多维表格: {context.base_url}")
    else:
        print(f"[飞书] Base token: {mask_app_token(context.app_token)}（已保存供后续复用）")
    return context


def _ensure_created_base_schema(context: FeishuClientContext) -> None:
    """Initialize the four business tables only for a newly created Base."""
    initialize_created_base_schema(context)


# ============================================================================
# Interactive prompts
# ============================================================================

def ask_sort_order() -> str:
    """Ask user to choose sort order."""
    print("\n排序策略:")
    print("  [1] relevance - 相关性最高 (适合精准查找测评视频)")
    print("  [2] viewCount - 播放量最高 (适合找头部网红)")
    print("  [3] date      - 最新发布 (适合发现新兴创作者)")
    print("  [4] rating    - 评分最高")
    print("  [5] title     - 按标题字母顺序")
    while True:
        choice = input("请选择 [1/2/3/4/5] (默认1): ").strip() or "1"
        if choice == "1":
            return "relevance"
        elif choice == "2":
            return "viewCount"
        elif choice == "3":
            return "date"
        elif choice == "4":
            return "rating"
        elif choice == "5":
            return "title"
        print("  无效输入，请输入 1, 2, 3, 4 或 5")


def ask_video_fetch_method(channel_count: int, quota_tracker: QuotaTracker) -> bool:
    """Ask user to choose video fetch method. Returns True for playlist."""
    playlist_cost = channel_count * 2  # playlistItems + videos.list
    search_cost = channel_count * 101  # search.list + videos.list

    print(f"\n获取网红最近视频的方式 (共 {channel_count} 个频道):")
    print(f"  [1] playlistItems (推荐, ~{playlist_cost} units, 按时间排序)")
    print(f"  [2] search        (~{search_cost} units, 可按播放量排序)")
    print(f"  当前剩余配额: {quota_tracker.remaining()} units")

    while True:
        choice = input("请选择 [1/2] (默认1): ").strip() or "1"
        if choice == "1":
            return True
        elif choice == "2":
            return False
        print("  无效输入，请输入 1 或 2")


def ask_confirm(message: str = "确认执行?") -> bool:
    choice = input(f"\n{message} [Y/n]: ").strip().lower()
    return choice in ("", "y", "yes")


def _is_quota_exceeded(error: str) -> bool:
    error = (error or "").lower()
    return (
        "quotaexceeded" in error
        or "dailylimitexceeded" in error
        or "配额已耗尽" in error
    )


def _mark_quota_interrupted(summary: dict, state: WorkflowState, error: str):
    summary["status"] = "配额中断"
    summary["error"] = error
    summary["state_file"] = str(state.state_file)
    state.update_stats(last_error=error, interrupted_reason="quotaExceeded")
    state.save()
    print("\nYouTube API 配额已耗尽，任务已暂停。")
    print(f"进度已保存: {state.state_file}")
    print(f"续传命令: python main.py search --resume {state.state_file}")


def _print_quota_budget(label: str, estimated: int, remaining: int):
    print(f"\n配额预算: {label}预计消耗 ~{estimated} units，本地预算剩余 {remaining} units")


def _estimate_remaining_cost(
    config: WorkflowConfig,
    state: WorkflowState,
    use_playlist: bool = None,
    no_detail: bool = False,
) -> int:
    """Estimate quota for the phases that have not completed yet."""
    cost = 0
    max_results = config.max_results

    if not state.is_phase_complete("a"):
        search_pages = (max_results + 49) // 50
        cost += search_pages * QUOTA_COSTS["search.list"]
        video_count = max_results
    else:
        video_count = len(state.data.get("search_results", [])) or state.data.get("search_results_count", max_results)

    if not state.is_phase_complete("b") and video_count:
        video_batches = (video_count + 49) // 50
        cost += video_batches * QUOTA_COSTS["videos.list"]

    if no_detail or state.is_phase_complete("d"):
        return cost

    if state.is_phase_complete("c"):
        channel_ids = [
            c.get("channel_id", "")
            for c in state.data.get("new_channels", [])
            if c.get("channel_id")
        ]
        channel_count = len(state.get_remaining_channels(channel_ids))
    else:
        channel_count = config.estimated_channels_per_keyword

    if channel_count <= 0:
        return cost

    channel_batches = (channel_count + 49) // 50
    cost += channel_batches * QUOTA_COSTS["channels.list"]

    if use_playlist is False:
        cost += channel_count * QUOTA_COSTS["search.list"]
    else:
        cost += channel_count * QUOTA_COSTS["playlistItems.list"]

    recent_video_batches = (channel_count * 10 + 49) // 50
    cost += recent_video_batches * QUOTA_COSTS["videos.list"]
    return cost


def _populate_created_times(
    feishu_client: BitableClient,
    table_id: str,
    items: list,
    *,
    source_key,
    feishu_key_field: str,
    created_time_field: str,
    output_key: str,
):
    """Read Feishu CreatedTime values back into local export objects.

    The values must come from Feishu after the upsert.  In particular, using a
    local timestamp here would incorrectly replace the first-created time for
    records that already existed and were merely updated.
    """
    for item in items:
        item[output_key] = ""
    if not items:
        return
    if not feishu_client:
        logger.warning(f"飞书未启用，{created_time_field}将在本地 Excel 中留空")
        return

    key_pairs = []
    for item in items:
        value = str(source_key(item) or "").strip()
        if value:
            key_pairs.append((item, value))

    try:
        records_by_key = feishu_client.get_records_by_field_values(
            table_id,
            feishu_key_field,
            [value for _, value in key_pairs],
            automatic_fields=True,
        )
    except Exception as exc:
        logger.warning(f"回读飞书{created_time_field}失败，本地 Excel 对应日期留空: {exc}")
        return

    populated = 0
    for item, key in key_pairs:
        record = records_by_key.get(key, {})
        # Prefer the named CreatedTime cell; the top-level automatic audit
        # field is a safe fallback and represents the same physical record.
        created_time = (record.get("fields") or {}).get(created_time_field)
        if not created_time:
            created_time = record.get("created_time")
        if created_time:
            item[output_key] = created_time
            populated += 1

    missing = len(items) - populated
    if missing:
        logger.warning(
            f"回读飞书{created_time_field}不完整: {populated}/{len(items)}，"
            f"其余 {missing} 条在本地 Excel 中留空"
        )


def _write_influencer_outputs(
    feishu_client: BitableClient,
    schema_mgr: SchemaManager,
    excel_exporter: ExcelExporter,
    influencer_details: list,
    influencer_videos: list,
    new_channels: list,
    keyword: str,
):
    """Write Phase D outputs idempotently before success or interruption."""
    if not influencer_details and not influencer_videos:
        return

    for influencer in influencer_details:
        influencer["influencer_record_date"] = ""

    if feishu_client and schema_mgr:
        if influencer_details:
            table_id = schema_mgr.get_table_id(INFLUENCERS_TABLE)
            records = []
            ch_to_new = {c["channel_id"]: c for c in new_channels}
            for inf in influencer_details:
                rep = ch_to_new.get(inf["channel_id"], {}).get("representative_video", {})
                records.append(format_influencer_record(inf, rep, keyword))
            result = feishu_client.upsert_records(
                table_id,
                records,
                key_field="Channel ID",
            )
            logger.info(f"飞书幂等写入网红详情表: 新增 {result['created']} 条, 更新 {result['updated']} 条")
            _populate_created_times(
                feishu_client,
                table_id,
                influencer_details,
                source_key=lambda item: item.get("channel_id", ""),
                feishu_key_field="Channel ID",
                created_time_field="网红记录日期",
                output_key="influencer_record_date",
            )

        if influencer_videos:
            table_id = schema_mgr.get_table_id(INFLUENCER_VIDEOS_TABLE)
            records = [format_influencer_video_record(v) for v in influencer_videos]
            result = feishu_client.upsert_records(
                table_id,
                records,
                fallback_key_fields=["Video ID"],
            )
            logger.info(f"飞书幂等写入网红视频表: 新增 {result['created']} 条, 更新 {result['updated']} 条")
    elif influencer_details:
        logger.warning("飞书未启用，网红记录日期将在本地 Excel 中留空")

    if excel_exporter:
        if influencer_details:
            excel_exporter.export_influencers(influencer_details)
        if influencer_videos:
            excel_exporter.export_influencer_videos(influencer_videos)


# ============================================================================
# Single keyword workflow
# ============================================================================

def run_single_keyword(
    keyword: str,
    config: WorkflowConfig,
    sort_order: str = "",
    use_playlist: bool = None,
    no_detail: bool = False,
    feishu_client: BitableClient = None,
    schema_mgr: SchemaManager = None,
    quota_tracker: QuotaTracker = None,
    excel_exporter: ExcelExporter = None,
    existing_channel_ids: Set[str] = None,
    state: WorkflowState = None,
    collect_results: bool = False,
) -> dict:
    """Run the full 4-phase workflow for a single keyword.

    Returns a summary dict.
    """
    if not quota_tracker:
        quota_tracker = QuotaTracker(config.youtube.daily_quota)

    if not state:
        state = WorkflowState(keyword, os.path.join(config.output_dir, "temp"))

    existing_channel_ids = set(existing_channel_ids or set())
    seen_store = SeenChannelStore(config.seen_channels_file)
    seen_channel_ids = seen_store.ids()
    if seen_channel_ids:
        existing_channel_ids.update(seen_channel_ids)
        logger.info(f"本地持久频道库已有频道: {len(seen_channel_ids)} 个")

    summary = {
        "keyword": keyword,
        "sort_order": sort_order,
        "search_results": 0,
        "qualified_count": 0,
        "unique_channels": 0,
        "new_channels": 0,
        "influencers_fetched": 0,
        "influencer_videos": 0,
        "quota_used_start": quota_tracker.used,
        "status": "进行中",
        "error": "",
    }

    start_time = datetime.now()
    search_results = []
    qualified = []
    all_videos = []
    new_channels = []
    influencer_details = []
    influencer_videos = []
    task_output_done = False

    def _finalize_search_task_output() -> dict:
        """Persist one search-task row for this keyword exactly once."""
        nonlocal task_output_done
        if task_output_done:
            return summary
        task_output_done = True

        summary["quota_used"] = quota_tracker.used - summary["quota_used_start"]
        task_data = {
            "task_key": keyword,
            "keyword": keyword,
            "sort_order": sort_order,
            "region": config.region,
            "search_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "result_count": summary["search_results"],
            "qualified_count": summary["qualified_count"],
            "unique_channels": summary["unique_channels"],
            "new_channels": summary["new_channels"],
            "quota_used": summary["quota_used"],
            "status": summary["status"],
            "note": summary.get("error", ""),
        }

        if feishu_client and schema_mgr:
            try:
                table_id = schema_mgr.get_table_id(SEARCH_TASKS_TABLE)
                feishu_client.upsert_records(
                    table_id,
                    [format_search_task_record(task_data)],
                    fallback_key_fields=["搜索关键词"],
                )
            except Exception as exc:
                logger.warning(f"写入飞书搜索任务表失败，本地搜索任务仍将保留: {exc}")

        if excel_exporter:
            try:
                excel_exporter.export_search_tasks([task_data])
            except Exception as exc:
                logger.warning(f"导出本地搜索任务表失败: {exc}")
            try:
                # A keyword run always has the same four-table local contract.
                # Empty result groups still get a header-only workbook, while
                # files already written by earlier phases are left untouched.
                excel_exporter.ensure_all_files()
            except Exception as exc:
                logger.warning(f"补齐四张本地 Excel 文件失败: {exc}")

        if collect_results:
            summary["_search_tasks"] = [task_data]
        return summary

    if sort_order:
        state.update_stats(
            sort_order=sort_order,
            region=config.region,
            lang=config.lang,
            filter_mode=config.filter_mode,
            min_subscribers=config.min_subscribers,
        )

    estimated_cost = _estimate_remaining_cost(
        config=config,
        state=state,
        use_playlist=use_playlist,
        no_detail=no_detail,
    )
    _print_quota_budget(keyword, estimated_cost, quota_tracker.remaining())
    try:
        assert_budget_available(estimated_cost, quota_tracker.remaining(), label=f"关键词 '{keyword}'")
    except RuntimeError as exc:
        summary["status"] = "预算不足"
        summary["error"] = str(exc)
        summary["state_file"] = str(state.state_file)
        state.update_stats(last_error=str(exc), interrupted_reason="quotaBudget")
        print(f"[FAIL] {exc}")
        return _finalize_search_task_output()

    try:
        # ====== Phase A: Search ======
        if not state.is_phase_complete("a"):
            search_results, error = run_phase_a(
                api_key=config.youtube.api_key,
                keyword=keyword,
                sort_order=sort_order,
                max_results=config.max_results,
                region_code=config.region,
                relevance_language=config.lang,
                search_filters=config.search_filters,
                quota_tracker=quota_tracker,
            )
            if error:
                if _is_quota_exceeded(error):
                    _mark_quota_interrupted(summary, state, error)
                    return _finalize_search_task_output()
                summary["status"] = "失败"
                summary["error"] = error
                return _finalize_search_task_output()

            summary["search_results"] = len(search_results)
            state.update_stats(
                search_results_count=len(search_results),
                search_results=search_results,
            )
            state.mark_phase("a")
        else:
            logger.info("阶段A已完成，跳过")
            search_results = state.data.get("search_results", [])
            summary["search_results"] = state.data.get("search_results_count", len(search_results))

        # ====== Phase B: Video details + filter ======
        if not state.is_phase_complete("b") and search_results:
            qualified, all_videos, missing_ids, error = run_phase_b(
                api_key=config.youtube.api_key,
                search_results=search_results,
                brand_exclusion=config.brand_exclusion,
                min_views=config.min_views,
                min_engagement=config.min_engagement,
                filter_mode=config.filter_mode,
                quota_tracker=quota_tracker,
            )
            if error:
                if _is_quota_exceeded(error):
                    _mark_quota_interrupted(summary, state, error)
                    return _finalize_search_task_output()
                summary["status"] = "部分成功"
                summary["error"] = error

            summary["qualified_count"] = len(qualified)
            state.update_stats(
                qualified_count=len(qualified),
                qualified_videos=qualified,
                all_videos=all_videos,
                missing_video_ids=missing_ids,
            )
            state.mark_phase("b")

            # Write to Feishu: search videos
            for video in all_videos:
                video["video_record_date"] = ""
            if feishu_client and schema_mgr:
                table_id = schema_mgr.get_table_id(SEARCH_VIDEOS_TABLE)
                records = [format_search_video_record(v, keyword) for v in all_videos]
                if records:
                    result = feishu_client.upsert_records(
                        table_id,
                        records,
                        fallback_key_fields=["搜索关键词", "Video ID"],
                    )
                    logger.info(f"飞书幂等写入视频数据表: 新增 {result['created']} 条, 更新 {result['updated']} 条")
                    _populate_created_times(
                        feishu_client,
                        table_id,
                        all_videos,
                        source_key=lambda item: f"{keyword}|{item.get('video_id', '')}",
                        feishu_key_field="唯一键",
                        created_time_field="视频记录日期",
                        output_key="video_record_date",
                    )
            elif all_videos:
                logger.warning("飞书未启用，视频记录日期将在本地 Excel 中留空")

            # Export Excel
            if excel_exporter:
                excel_exporter.export_search_videos(all_videos, keyword)
        else:
            if state.is_phase_complete("b"):
                logger.info("阶段B已完成，跳过")
                qualified = state.data.get("qualified_videos", [])
                all_videos = state.data.get("all_videos", [])
                summary["qualified_count"] = state.data.get("qualified_count", len(qualified))
            else:
                qualified = []
                all_videos = []

        # ====== Phase C: Channel dedup ======
        if not state.is_phase_complete("c") and qualified:
            # Get existing channel IDs from Feishu for incremental check
            if feishu_client and schema_mgr and not existing_channel_ids:
                try:
                    table_id = schema_mgr.get_table_id(INFLUENCERS_TABLE)
                    existing_channel_ids = feishu_client.get_existing_values(
                        table_id, "Channel ID"
                    )
                    logger.info(f"飞书已有网红: {len(existing_channel_ids)} 个")
                except Exception as e:
                    logger.warning(f"读取已有网红失败: {e}")

            new_channels, existing_channels = run_phase_c(
                qualified_videos=qualified,
                existing_channel_ids=existing_channel_ids,
                source_keyword=keyword,
            )

            summary["unique_channels"] = len(new_channels) + len(existing_channels)
            summary["new_channels"] = len(new_channels)
            summary["_new_channel_ids"] = [c.get("channel_id", "") for c in new_channels if c.get("channel_id")]
            state.update_stats(
                new_channels_count=len(new_channels),
                new_channels=new_channels,
                existing_channels=existing_channels,
                phase_d_progress={"total_channels": len(new_channels), "completed_channels": 0, "completed_channel_ids": []},
            )
            state.mark_phase("c")
        else:
            if state.is_phase_complete("c"):
                logger.info("阶段C已完成，跳过")
                new_channels = state.data.get("new_channels", [])
                existing_channels = state.data.get("existing_channels", [])
                summary["unique_channels"] = len(new_channels) + len(existing_channels)
                summary["new_channels"] = len(new_channels)
                summary["_new_channel_ids"] = [
                    c.get("channel_id", "") for c in new_channels if c.get("channel_id")
                ]
            else:
                new_channels = []
                summary["_new_channel_ids"] = []

        # ====== Phase D: Influencer details ======
        if not no_detail and new_channels:
            if not state.is_phase_complete("d"):
                # Ask for video fetch method if not decided
                if use_playlist is None:
                    use_playlist = ask_video_fetch_method(len(new_channels), quota_tracker)

                influencer_details, influencer_videos, error = run_phase_d(
                    api_key=config.youtube.api_key,
                    new_channels=new_channels,
                    use_playlist=use_playlist,
                    min_subscribers=config.min_subscribers,
                    quota_tracker=quota_tracker,
                    state=state,
                )

                summary["influencers_fetched"] = len(influencer_details)
                summary["influencer_videos"] = len(influencer_videos)

                if error:
                    _write_influencer_outputs(
                        feishu_client=feishu_client,
                        schema_mgr=schema_mgr,
                        excel_exporter=excel_exporter,
                        influencer_details=influencer_details,
                        influencer_videos=influencer_videos,
                        new_channels=new_channels,
                        keyword=keyword,
                    )
                    state.update_stats(
                        partial_influencer_details=influencer_details,
                        partial_influencer_videos=influencer_videos,
                    )
                    if _is_quota_exceeded(error):
                        if collect_results:
                            summary["_search_videos"] = all_videos
                            summary["_influencers"] = influencer_details
                            summary["_influencer_videos"] = influencer_videos
                        _mark_quota_interrupted(summary, state, error)
                        return _finalize_search_task_output()
                    summary["status"] = "部分成功"
                    summary["error"] = error
                else:
                    state.update_stats(
                        influencer_details=influencer_details,
                        influencer_videos=influencer_videos,
                    )
                    state.mark_phase("d")

                    _write_influencer_outputs(
                        feishu_client=feishu_client,
                        schema_mgr=schema_mgr,
                        excel_exporter=excel_exporter,
                        influencer_details=influencer_details,
                        influencer_videos=influencer_videos,
                        new_channels=new_channels,
                        keyword=keyword,
                    )
            else:
                logger.info("阶段D已完成，跳过")
                influencer_details = state.data.get("influencer_details", [])
                influencer_videos = state.data.get("influencer_videos", [])
                summary["influencers_fetched"] = len(influencer_details)
                summary["influencer_videos"] = len(influencer_videos)
        else:
            influencer_details = []
            influencer_videos = []
            if no_detail:
                logger.info("跳过阶段D (--no-detail)")

        if new_channels and summary["status"] not in ("配额中断", "预算不足", "失败", "异常", "部分成功"):
            seen_store.mark_channels(
                influencer_details if influencer_details else new_channels,
                source_keyword=keyword,
            )

        # ====== Write search task record ======
        if summary["status"] == "进行中":
            summary["status"] = "成功"

        elapsed = (datetime.now() - start_time).total_seconds()

        if collect_results:
            summary["_search_videos"] = all_videos
            summary["_influencers"] = influencer_details
            summary["_influencer_videos"] = influencer_videos

        _finalize_search_task_output()

        # Print summary
        _print_summary(summary, elapsed)

        return summary

    except KeyboardInterrupt:
        logger.warning("用户中断，保存进度...")
        state.save()
        summary["status"] = "中断"
        print(f"\n进度已保存: {state.state_file}")
        print(f"续传命令: python main.py search --resume {state.state_file}")
        return _finalize_search_task_output()

    except Exception as e:
        logger.error(f"工作流异常: {e}", exc_info=True)
        state.save()
        summary["status"] = "异常"
        summary["error"] = str(e)
        return _finalize_search_task_output()


def _print_summary(summary: dict, elapsed: float):
    """Print execution summary."""
    status_icon = {"成功": "[OK]", "部分成功": "[WARN]", "失败": "[FAIL]"}.get(summary["status"], "[?]")

    print(f"""
========================================
YouTube网红开发任务完成
========================================
关键词: {summary['keyword']}
排序策略: {summary['sort_order']}
处理时长: {elapsed:.1f}秒

搜索结果: {summary['search_results']} 个视频
筛选通过: {summary['qualified_count']} 个
独立频道: {summary['unique_channels']} 个
新增网红: {summary['new_channels']} 个
网红详情: {summary['influencers_fetched']} 个
网红视频: {summary['influencer_videos']} 条
配额消耗: {summary['quota_used']} units

执行状态: {status_icon} {summary['status']}
{"错误信息: " + summary['error'] if summary['error'] else ""}
========================================""")


# ============================================================================
# Batch mode
# ============================================================================

def run_batch(
    tasks: list,  # List[KeywordTask]
    config: WorkflowConfig,
    default_sort_order: str = "",
    use_playlist: bool = None,
    no_detail: bool = False,
    skip_confirm: bool = False,
    write_excel: bool = True,
    write_feishu: bool = True,
):
    """Run multiple keyword tasks in batch."""
    quota_tracker = QuotaTracker(config.youtube.daily_quota)

    # Feishu setup
    feishu_client = None
    schema_mgr = None
    if write_feishu:
        try:
            context = _create_feishu_context(config.feishu)
            _ensure_created_base_schema(context)
            feishu_client, schema_mgr = run_feishu_preflight(
                client=context.client,
            )
            logger.info("飞书多维表格初始化完成")
        except Exception as e:
            logger.error(f"飞书预检失败: {e}")
            if not write_excel:
                if ask_confirm("飞书预检失败，是否改为本地Excel继续?"):
                    write_excel = True
                else:
                    print("任务已停止")
                    return
            elif not ask_confirm("飞书不可用，是否仅使用本地Excel继续?"):
                return

    # Batch Excel exporter
    batch_exporter = BatchExcelExporter(config.output_dir) if write_excel else None

    # Quota estimation
    keywords = [t.keyword for t in tasks]
    max_results_list = [t.max_results or config.max_results for t in tasks]
    total_estimated = print_quota_estimate(
        keywords, max_results_list,
        estimated_channels_per_kw=config.estimated_channels_per_keyword,
        use_playlist=use_playlist if use_playlist is not None else True,
        daily_limit=config.youtube.daily_quota,
        include_detail=not no_detail,
    )
    try:
        assert_budget_available(total_estimated, quota_tracker.remaining(), label="批量任务")
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        print("请减少关键词数量、降低 --max-results，或等配额恢复后续跑。")
        return

    if not skip_confirm and not ask_confirm("确认执行批量任务?"):
        print("已取消")
        return

    # Ask for playlist method once for all (only if not set via CLI)
    if use_playlist is None and not no_detail:
        # Default to True (playlistItems) for efficiency, unless quota is very low
        if quota_tracker.remaining() < quota_tracker.daily_limit * 0.1:
            use_playlist = False  # Switch to search if quota < 10%
        else:
            use_playlist = True

    # Track all existing channel IDs across keywords
    existing_channel_ids: Set[str] = set()
    if feishu_client and schema_mgr:
        try:
            table_id = schema_mgr.get_table_id(INFLUENCERS_TABLE)
            existing_channel_ids = feishu_client.get_existing_values(table_id, "Channel ID")
        except Exception:
            pass

    results = []
    for i, task in enumerate(tasks):
        print(f"\n{'='*60}")
        print(f"批量任务 [{i+1}/{len(tasks)}]: {task.keyword}")
        print(f"{'='*60}")

        sort_order = task.sort_order or default_sort_order
        if not sort_order:
            if skip_confirm:
                sort_order = "relevance"  # skip_confirm 模式默认使用相关性排序
            else:
                print(f"\n关键词: {task.keyword}")
                sort_order = ask_sort_order()

        max_results = task.max_results or config.max_results
        config_copy = WorkflowConfig(
            youtube=config.youtube,
            feishu=config.feishu,
            brand_exclusion=config.brand_exclusion,
            output_dir=config.output_dir,
            log_level=config.log_level,
            region=config.region,
            lang=config.lang,
            max_results=max_results,
            min_views=config.min_views,
            min_engagement=config.min_engagement,
            filter_mode=config.filter_mode,
            min_subscribers=config.min_subscribers,
            estimated_channels_per_keyword=config.estimated_channels_per_keyword,
            seen_channels_file=config.seen_channels_file,
            search_filters=config.search_filters,
        )

        kw_exporter = batch_exporter.get_keyword_exporter(task.keyword) if batch_exporter else None

        summary = run_single_keyword(
            keyword=task.keyword,
            config=config_copy,
            sort_order=sort_order,
            use_playlist=use_playlist,
            no_detail=no_detail,
            feishu_client=feishu_client,
            schema_mgr=schema_mgr,
            quota_tracker=quota_tracker,
            excel_exporter=kw_exporter,
            existing_channel_ids=existing_channel_ids,
            collect_results=bool(batch_exporter),
        )

        if batch_exporter:
            batch_exporter.accumulate(
                search_tasks=summary.pop("_search_tasks", []),
                search_videos=summary.pop("_search_videos", []),
                influencers=summary.pop("_influencers", []),
                influencer_videos=summary.pop("_influencer_videos", []),
            )

        results.append(summary)
        existing_channel_ids.update(summary.pop("_new_channel_ids", []))

        if summary.get("status") in ("配额中断", "预算不足"):
            print("\n批量任务已暂停。")
            if summary.get("state_file"):
                print(f"可从该状态文件续跑当前关键词: {summary['state_file']}")
            break

        # Update cross-keyword dedup set
        if summary.get("new_channels", 0) > 0 and feishu_client and schema_mgr:
            try:
                table_id = schema_mgr.get_table_id(INFLUENCERS_TABLE)
                existing_channel_ids = feishu_client.get_existing_values(table_id, "Channel ID")
            except Exception:
                pass

        # Check quota
        quota_tracker.warn_if_low(0.15)
        if not quota_tracker.can_afford("search.list", 2):
            logger.warning("配额不足，停止后续关键词")
            break

    # Export batch summary
    if batch_exporter:
        batch_exporter.export_summary()

    # Print batch summary
    print(f"\n{'='*60}")
    print("批量任务汇总")
    print(f"{'='*60}")
    total_new = sum(r.get("new_channels", 0) for r in results)
    total_videos = sum(r.get("influencer_videos", 0) for r in results)
    print(f"执行关键词: {len(results)}/{len(tasks)}")
    print(f"新增网红合计: {total_new}")
    print(f"网红视频合计: {total_videos}")
    print(f"\n{quota_tracker.summary()}")
    if batch_exporter:
        print(f"\n本地文件: {batch_exporter.get_output_dir()}")
    print(f"{'='*60}")


# ============================================================================
# CLI
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTube网红开发工作流系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ---- search ----
    sp_search = subparsers.add_parser("search", help="搜索单个关键词")
    sp_search.add_argument("--keyword", "-k", required=False, help="搜索关键词")
    sp_search.add_argument("--resume", help="断点续传: 状态文件路径")
    sp_search.add_argument("--sort-order", choices=YOUTUBE_SEARCH_ORDER_CHOICES, help="排序策略(不指定则交互询问)")
    sp_search.add_argument("--max-results", type=int, default=100, help="最大搜索结果数 (默认100)")
    sp_search.add_argument("--no-detail", action="store_true", help="仅搜索+筛选，不抓取网红详情")
    _add_common_args(sp_search)

    # ---- batch ----
    sp_batch = subparsers.add_parser("batch", help="批量搜索多个关键词")
    sp_batch.add_argument("--keywords-file", "-f", required=True, help="关键词文件 (txt或csv)")
    sp_batch.add_argument("--sort-order", choices=YOUTUBE_SEARCH_ORDER_CHOICES, help="统一排序策略")
    sp_batch.add_argument("--max-results", type=int, default=100, help="默认最大搜索结果数")
    sp_batch.add_argument("--no-detail", action="store_true", help="仅搜索+筛选")
    sp_batch.add_argument("--yes", "-y", action="store_true", help="跳过确认直接执行")
    sp_batch.add_argument("--playlist", action="store_true", default=True,
                         help="使用 playlistItems 获取最近视频 (默认开启)")
    sp_batch.add_argument("--search", dest="playlist", action="store_false",
                         help="使用 search.list 获取最近视频 (消耗更多配额)")
    _add_common_args(sp_batch)

    # ---- exclusion ----
    sp_excl = subparsers.add_parser("exclusion", help="管理品牌排除名单")
    sp_excl.add_argument("--add-brand", help="添加品牌名称")
    sp_excl.add_argument("--add-channel", help="添加频道ID")
    sp_excl.add_argument("--add-keyword", help="添加频道名称关键词")
    sp_excl.add_argument("--list", action="store_true", help="显示当前排除名单")
    sp_excl.add_argument("--import-file", help="从CSV导入排除名单")
    sp_excl.add_argument("--config",
                        default=os.environ.get("BRAND_EXCLUSION_FILE", "brand_exclusions.json"),
                        help="排除名单文件路径 (或设置 BRAND_EXCLUSION_FILE 环境变量)")

    # ---- quota ----
    subparsers.add_parser("quota", help="查看配额说明")

    # ---- feishu-setup ----
    sp_feishu_setup = subparsers.add_parser(
        "feishu-setup",
        help="自动安装飞书 CLI、打开浏览器授权并准备多维表格",
    )
    sp_feishu_setup.add_argument(
        "--feishu-app-token",
        default=os.environ.get("FEISHU_APP_TOKEN", ""),
        help="已有飞书 Base URL/app_token；不填则自动创建新的多维表格",
    )
    sp_feishu_setup.add_argument(
        "--no-create-base",
        action="store_true",
        help="只完成 CLI 授权，不创建新的多维表格",
    )
    _add_feishu_auth_args(sp_feishu_setup)

    # ---- merge-output ----
    sp_merge = subparsers.add_parser(
        "merge-output",
        help="合并多个输出目录生成四张业务表和来源信息表",
    )
    sp_merge.add_argument("batch_dirs", nargs="+", help="一个或多个批次输出目录")
    sp_merge.add_argument("--output", "-o", default="", help="输出 xlsx 路径")
    sp_merge.add_argument("--no-dedupe", action="store_true", help="不执行默认去重")
    sp_merge.add_argument("--stats", default="", help="统计 JSON 输出路径")

    # ---- sync-workbook ----
    sp_sync = subparsers.add_parser("sync-workbook", help="将汇总工作簿同步到飞书多维表格")
    sp_sync.add_argument("--workbook", required=True, help="汇总 xlsx 文件")
    sp_sync.add_argument(
        "--feishu-app-token",
        default=os.environ.get("FEISHU_APP_TOKEN", ""),
        help="飞书 app_token 或 Base URL；不填则复用或自动创建多维表格",
    )
    _add_feishu_auth_args(sp_sync)
    sp_sync.add_argument(
        "--table",
        choices=["all", "search_tasks", "search_videos", "influencers", "influencer_videos"],
        default="all",
    )
    sp_sync.add_argument("--dry-run", action="store_true", help="只生成计划，不写入")
    sp_sync.add_argument("--skip-test", action="store_true", help="跳过 10 条测试，直接执行")
    sp_sync.add_argument("--test-only", action="store_true", help="只执行测试写入")
    sp_sync.add_argument("--test-limit", type=int, default=10, help="测试行数")
    sp_sync.add_argument("--cleanup-empty-rows", action="store_true", help="同步后删除全空行")
    sp_sync.add_argument("--clear-primary", action="store_true", help="同步前清空 A 列主字段")
    sp_sync.add_argument("--fill-channel-descriptions", action="store_true", help="同步 influencers 时调用 YouTube API 补空频道描述")
    sp_sync.add_argument("--description-only", action="store_true", help="只更新 influencers 表的频道描述字段")
    sp_sync.add_argument("--youtube-api-key", default="", help="YouTube API Key；不填则读取环境变量")
    sp_sync.add_argument("--stats", default="", help="统计 JSON 输出路径")

    # ---- clean-feishu-empty ----
    sp_clean = subparsers.add_parser("clean-feishu-empty", help="删除飞书多维表格中的全空行")
    sp_clean.add_argument(
        "--feishu-app-token",
        default=os.environ.get("FEISHU_APP_TOKEN", ""),
        help="飞书 app_token 或 Base URL；不填则复用或自动创建多维表格",
    )
    _add_feishu_auth_args(sp_clean)
    sp_clean.add_argument("--table", action="append", default=[], help="只清理指定表名，可重复传入")
    sp_clean.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    sp_clean.add_argument("--stats", default="", help="统计 JSON 输出路径")

    # ---- refresh-influencers ----
    sp_refresh = subparsers.add_parser(
        "refresh-influencers",
        help="回填或刷新飞书中已有网红的姓名、活跃度、频道判断和代表视频标题",
    )
    sp_refresh.add_argument(
        "--feishu-app-token",
        default=os.environ.get("FEISHU_APP_TOKEN", ""),
        help="飞书 app_token 或 Base URL（可读取 FEISHU_APP_TOKEN）",
    )
    sp_refresh.add_argument(
        "--youtube-api-key",
        default=os.environ.get("YOUTUBE_API_KEY", ""),
        help="YouTube API Key（可读取 YOUTUBE_API_KEY）",
    )
    sp_refresh.add_argument(
        "--fields",
        choices=["all", "name", "activity", "assessment", "rep-title"],
        default="all",
        help="只刷新指定字段组（默认 all）",
    )
    sp_refresh.add_argument("--channel-id", action="append", default=[], help="只刷新指定 Channel ID，可重复")
    sp_refresh.add_argument("--limit", type=int, default=None, help="最多处理记录数")
    sp_refresh.add_argument("--dry-run", action="store_true", help="只生成更新计划，不写记录")
    sp_refresh.add_argument(
        "--replace-kol-names",
        action="store_true",
        help="重新计算并覆盖现有 KOL Name；仅用于已确认当前值均为程序生成的迁移批次",
    )
    sp_refresh.add_argument(
        "--ensure-schema",
        action="store_true",
        help="先补齐网红详情表字段；该操作即使配合 --dry-run 也会修改表结构",
    )
    sp_refresh.add_argument("--stats", default="", help="统计 JSON 输出路径")
    _add_feishu_auth_args(sp_refresh)

    return parser


def _add_common_args(parser):
    parser.add_argument("--feishu-app-token", default=os.environ.get("FEISHU_APP_TOKEN", ""),
                        help="飞书多维表格app_token或URL (或设置 FEISHU_APP_TOKEN 环境变量)")
    parser.add_argument("--output-dir", "-o", default=os.environ.get("OUTPUT_DIR", ""),
                        help="输出目录 (默认项目目录下 output, 或设置 OUTPUT_DIR 环境变量)")
    parser.add_argument("--region", default=os.environ.get("REGION", "US"),
                        help="搜索地区 (默认US, 或设置 REGION 环境变量)")
    parser.add_argument("--lang", default=os.environ.get("LANG_CODE", "en"),
                        help="搜索语言 (默认en, 或设置 LANG_CODE 环境变量)")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                        choices=["DEBUG", "INFO", "WARNING"])
    parser.add_argument("--no-excel", action="store_true", help="跳过本地Excel输出")
    parser.add_argument("--no-feishu", action="store_true", help="跳过飞书写入")
    _add_feishu_auth_args(parser)
    parser.add_argument("--min-views", type=int, default=10000,
                        help="筛选阈值: 播放量必须大于该值 (默认10000)")
    parser.add_argument("--min-engagement", type=float, default=3.0,
                        help="筛选阈值: 互动率必须大于该百分比 (默认3.0)")
    parser.add_argument("--filter-mode", choices=["or", "and", "weighted"], default="or",
                        help="视频筛选逻辑: or=任一达标, and=双达标, weighted=均衡加权 (默认or)")
    parser.add_argument("--min-subscribers", type=int, default=0,
                        help="频道级筛选: 最低订阅数 (默认0, 不限制)")
    parser.add_argument("--estimated-channels", type=int, default=15,
                        help="配额预算: 每个关键词预计新增频道数 (默认15)")
    parser.add_argument("--seen-channels-file", default=os.environ.get("SEEN_CHANNELS_FILE", ""),
                        help="跨运行持久频道库路径 (默认项目目录 seen_channels.json)")
    parser.add_argument("--video-duration", choices=["any", "short", "medium", "long"], default="any",
                        help="YouTube搜索过滤: 视频时长")
    parser.add_argument("--video-caption", choices=["any", "closedCaption", "none"], default="any",
                        help="YouTube搜索过滤: 字幕状态")
    parser.add_argument("--video-definition", choices=["any", "high", "standard"], default="any",
                        help="YouTube搜索过滤: 清晰度")
    parser.add_argument("--published-after", default="",
                        help="YouTube搜索过滤: 发布时间不早于 YYYY-MM-DD")
    parser.add_argument("--published-before", default="",
                        help="YouTube搜索过滤: 发布时间不晚于 YYYY-MM-DD")


def _add_feishu_auth_args(parser):
    defaults = FeishuConfig()
    parser.add_argument(
        "--feishu-auth-mode",
        choices=["auto", "cli", "app"],
        default=defaults.auth_mode,
        help="飞书认证模式：auto 自动选择、cli 浏览器授权、app 静态应用凭证",
    )
    parser.add_argument(
        "--feishu-cli-profile",
        default=defaults.cli_profile,
        help="lark-cli 独立 Profile 名称",
    )
    parser.add_argument(
        "--feishu-cli-path",
        default=defaults.cli_path,
        help="lark-cli 可执行文件路径；默认自动检测",
    )
    parser.add_argument(
        "--feishu-auth-timeout",
        type=int,
        default=defaults.auth_timeout_seconds,
        help="等待浏览器授权的最长秒数",
    )
    parser.add_argument(
        "--feishu-base-name",
        default=defaults.base_name,
        help="未提供目标时自动创建的多维表格名称",
    )
    parser.add_argument(
        "--no-feishu-auto-setup",
        action="store_true",
        help="禁止自动创建应用或发起浏览器授权",
    )
    parser.add_argument(
        "--no-feishu-auto-install",
        action="store_true",
        help="禁止自动安装或升级 lark-cli",
    )
    parser.add_argument(
        "--no-feishu-browser",
        action="store_true",
        help="不自动打开浏览器，仅在终端显示授权链接",
    )


def _feishu_config_from_args(args) -> FeishuConfig:
    """Build FeishuConfig consistently for every CLI entry point."""
    config = FeishuConfig()
    token_or_url = getattr(args, "feishu_app_token", "")
    if token_or_url:
        config.app_token = FeishuConfig.extract_app_token(token_or_url)
    config.auth_mode = getattr(args, "feishu_auth_mode", config.auth_mode)
    config.cli_profile = getattr(args, "feishu_cli_profile", config.cli_profile)
    config.cli_path = getattr(args, "feishu_cli_path", config.cli_path)
    config.auth_timeout_seconds = getattr(
        args,
        "feishu_auth_timeout",
        config.auth_timeout_seconds,
    )
    config.base_name = getattr(args, "feishu_base_name", config.base_name)
    if getattr(args, "no_feishu_auto_setup", False):
        config.auto_setup = False
    if getattr(args, "no_feishu_auto_install", False):
        config.auto_install = False
    if getattr(args, "no_feishu_browser", False):
        config.open_browser = False
    return config


def build_search_filters_from_args(args) -> dict:
    """Build YouTube search.list filters from CLI arguments."""
    existing = getattr(args, "search_filters", None)
    if existing is not None:
        return dict(existing)

    filters = {}
    if getattr(args, "video_duration", "any") != "any":
        filters["videoDuration"] = args.video_duration
    if getattr(args, "video_caption", "any") != "any":
        filters["videoCaption"] = args.video_caption
    if getattr(args, "video_definition", "any") != "any":
        filters["videoDefinition"] = args.video_definition
    if getattr(args, "published_after", ""):
        filters["publishedAfter"] = _to_rfc3339_boundary(args.published_after)
    if getattr(args, "published_before", ""):
        filters["publishedBefore"] = _to_rfc3339_boundary(args.published_before, end_of_day=True)
    return filters


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        print("用法: python main.py batch --keywords-file keywords.txt")
        print("查看帮助: python main.py --help")
        sys.exit(1)

    # ---- exclusion command ----
    if args.command == "exclusion":
        handle_exclusion(args)
        return

    # ---- quota command ----
    if args.command == "quota":
        print("YouTube Data API 每日配额: 10,000 units (默认)")
        print("主要消耗:")
        print("  search.list:        100 units/次")
        print("  videos.list:          1 unit/次 (最多50个ID)")
        print("  channels.list:        1 unit/次 (最多50个ID)")
        print("  playlistItems.list:   1 unit/次")
        print("\n典型场景 (1关键词, 100结果, 15个新网红):")
        print("  方案1 (playlistItems): ~221 units → 每日可处理 ~45 关键词")
        print("  方案2 (search):       ~1706 units → 每日可处理 ~5 关键词")
        return

    if args.command == "feishu-setup":
        handle_feishu_setup(args)
        return

    # ---- maintenance commands ----
    if args.command == "merge-output":
        handle_merge_output(args)
        return
    if args.command == "sync-workbook":
        handle_sync_workbook(args)
        return
    if args.command == "clean-feishu-empty":
        handle_clean_feishu_empty(args)
        return
    if args.command == "refresh-influencers":
        handle_refresh_influencers(args)
        return

    # ---- search / batch ----
    # Build config
    config = WorkflowConfig(
        output_dir=args.output_dir,
        log_level=args.log_level,
        region=args.region,
        lang=args.lang,
        max_results=args.max_results,
        min_views=getattr(args, "min_views", 10000),
        min_engagement=getattr(args, "min_engagement", 3.0),
        filter_mode=getattr(args, "filter_mode", "or"),
        min_subscribers=getattr(args, "min_subscribers", 0),
        estimated_channels_per_keyword=getattr(args, "estimated_channels", 15),
        seen_channels_file=getattr(args, "seen_channels_file", ""),
        search_filters=build_search_filters_from_args(args),
    )

    if not args.no_feishu:
        config.feishu = _feishu_config_from_args(args)

    # Brand exclusions
    exclusion_file = os.environ.get("BRAND_EXCLUSION_FILE", "brand_exclusions.json")
    config.brand_exclusion = BrandExclusion.load(exclusion_file)

    # Validate
    require_feishu = not getattr(args, "no_feishu", False)
    errors = config.validate(require_youtube=True, require_feishu=require_feishu)
    if errors:
        for err in errors:
            print(f"❌ {err}")
        sys.exit(1)

    # Setup logger
    secrets = [config.youtube.api_key] if config.youtube.api_key else []
    if config.feishu.app_secret:
        secrets.append(config.feishu.app_secret)
    setup_logger(
        log_dir=os.path.join(config.output_dir, "logs"),
        log_level=config.log_level,
        secrets=secrets,
    )

    if args.command == "search":
        handle_search(args, config)
    elif args.command == "batch":
        handle_batch(args, config)


def handle_search(args, config: WorkflowConfig):
    """Handle single keyword search."""
    if args.resume:
        state = WorkflowState.load(args.resume)
        keyword = state.keyword
        logger.info(f"断点续传: {keyword}")
    elif args.keyword:
        keyword = args.keyword
        state = None
    else:
        print("❌ 请指定 --keyword 或 --resume")
        sys.exit(1)

    sort_order = args.sort_order or (state.data.get("sort_order", "") if state else "")
    if not sort_order:
        sort_order = ask_sort_order()

    # Feishu init
    feishu_client = None
    schema_mgr = None
    if not args.no_feishu:
        try:
            context = _create_feishu_context(config.feishu)
            _ensure_created_base_schema(context)
            feishu_client, schema_mgr = run_feishu_preflight(
                client=context.client,
            )
        except Exception as e:
            logger.error(f"飞书预检失败: {e}")
            if args.no_excel:
                if ask_confirm("飞书预检失败，是否改为本地Excel继续?"):
                    args.no_excel = False
                else:
                    print("任务已停止")
                    return
            elif not ask_confirm("飞书不可用，是否仅使用本地Excel继续?"):
                return

    # Excel exporter
    excel_exporter = None
    if not args.no_excel:
        excel_exporter = ExcelExporter(config.output_dir, keyword)

    quota_tracker = QuotaTracker(config.youtube.daily_quota)

    run_single_keyword(
        keyword=keyword,
        config=config,
        sort_order=sort_order,
        no_detail=args.no_detail,
        feishu_client=feishu_client,
        schema_mgr=schema_mgr,
        quota_tracker=quota_tracker,
        excel_exporter=excel_exporter,
        state=state,
    )

    if excel_exporter:
        print(f"本地文件: {excel_exporter.get_output_dir()}")

    print(f"\n{quota_tracker.summary()}")


def handle_batch(args, config: WorkflowConfig):
    """Handle batch keyword search."""
    tasks = parse_keywords_file(args.keywords_file, config.max_results)
    if not tasks:
        print("[FAIL] 关键词文件为空或格式错误")
        sys.exit(1)

    print(f"加载 {len(tasks)} 个关键词:")
    for i, t in enumerate(tasks):
        sr = t.sort_order or args.sort_order or "(待询问)"
        print(f"  {i+1}. {t.keyword} [排序={sr}, 数量={t.max_results or config.max_results}]")

    run_batch(
        tasks=tasks,
        config=config,
        default_sort_order=args.sort_order or "",
        no_detail=args.no_detail,
        skip_confirm=args.yes,
        use_playlist=args.playlist,
        write_excel=not args.no_excel,
        write_feishu=not args.no_feishu,
    )


def handle_merge_output(args):
    """Handle output folder merge."""
    from export.summary_builder import build_summary_workbook

    output = args.output
    if not output:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = str(PROJECT_ROOT / "output" / f"summary_{stamp}" / "kol_summary_tables.xlsx")

    result = build_summary_workbook(
        args.batch_dirs,
        output,
        dedupe=not args.no_dedupe,
        stats_path=args.stats or None,
    )
    print("\n汇总表已生成:")
    print(f"  文件: {result['output_path']}")
    print(f"  统计: {result['stats_path']}")
    for table_key, stats in result["tables"].items():
        removed = stats.get("duplicate_rows_removed", 0)
        print(f"  {table_key}: {stats['output_rows']} 行 (去重 {removed} 行)")


def handle_feishu_setup(args):
    """Install/authenticate lark-cli and optionally prepare the four tables."""
    feishu_cfg = _feishu_config_from_args(args)
    if args.no_create_base and not feishu_cfg.app_token:
        if feishu_cfg.auth_mode == "app":
            raise RuntimeError("App 模式只授权不建 Base 时仍需提供 --feishu-app-token")
        from feishu.lark_cli import LarkCliManager

        manager = LarkCliManager(
            profile=feishu_cfg.cli_profile,
            cli_path=feishu_cfg.cli_path or None,
            timeout=feishu_cfg.auth_timeout_seconds,
            open_browser=feishu_cfg.open_browser,
            auto_install=feishu_cfg.auto_install,
            progress=_feishu_progress,
        )
        if not feishu_cfg.auto_setup:
            manager.ensure_cli()
            if not manager.profile_configured() or not manager.authorization_valid():
                raise RuntimeError("飞书 CLI 尚未授权，且自动设置已禁用")
        else:
            manager.ensure_ready()
        print(f"飞书 CLI 授权完成，Profile: {feishu_cfg.cli_profile}")
        return

    context = _create_feishu_context(
        feishu_cfg,
        create_base_if_missing=not args.no_create_base,
    )
    _ensure_created_base_schema(context)
    _, schema_mgr = run_feishu_preflight(client=context.client)
    print("\n飞书设置完成:")
    print(f"  认证模式: {context.auth_mode}")
    print(f"  Profile: {feishu_cfg.cli_profile if context.auth_mode == 'cli' else '-'}")
    print(f"  Base: {context.base_url or mask_app_token(context.app_token)}")
    print(f"  业务表: {len(schema_mgr.table_ids)} 张")


def handle_sync_workbook(args):
    """Handle workbook-to-Feishu sync."""
    from feishu.workbook_sync import sync_workbook_to_feishu

    feishu_cfg = _feishu_config_from_args(args)
    context = _create_feishu_context(
        feishu_cfg,
        create_base_if_missing=not args.dry_run,
    )
    _ensure_created_base_schema(context)
    stats_path = args.stats or str(Path(args.workbook).with_suffix(".sync_stats.json"))
    result = sync_workbook_to_feishu(
        args.workbook,
        app_token=context.app_token,
        app_id=feishu_cfg.app_id,
        app_secret=feishu_cfg.app_secret,
        client=context.client,
        table=args.table,
        dry_run=args.dry_run,
        skip_test=args.skip_test,
        test_only=args.test_only,
        test_limit=args.test_limit,
        cleanup_empty_rows=args.cleanup_empty_rows,
        clear_primary=args.clear_primary,
        fill_channel_descriptions=args.fill_channel_descriptions,
        description_only=args.description_only,
        youtube_api_key=args.youtube_api_key,
        stats_path=stats_path,
    )

    print("\n飞书同步完成:")
    print(f"  工作簿: {result['workbook']}")
    print(f"  统计: {result.get('stats_path', stats_path)}")
    for phase in result["phases"]:
        if "sync_results" not in phase:
            continue
        print(f"  {phase['phase']}:")
        for item in phase["sync_results"]:
            print(
                "    "
                f"{item['table_name']}: 更新 {item.get('actual_updates', 0)}, "
                f"新增 {item.get('actual_creates', 0)}, "
                f"重复源数据 {item.get('duplicate_source_keys', 0)}"
            )


def handle_clean_feishu_empty(args):
    """Handle Feishu empty-row cleanup."""
    from feishu.cleanup import cleanup_empty_records, mask_token

    feishu_cfg = _feishu_config_from_args(args)
    context = _create_feishu_context(
        feishu_cfg,
        create_base_if_missing=not args.dry_run,
    )
    _ensure_created_base_schema(context)
    client = context.client
    started_at = datetime.now().isoformat(timespec="seconds")
    results = cleanup_empty_records(
        client,
        table_names=args.table or None,
        dry_run=args.dry_run,
    )

    payload = {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "app_token": mask_token(context.app_token),
        "results": results,
    }
    stats_path = args.stats
    if not stats_path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stats_path = str(PROJECT_ROOT / "output" / f"feishu_empty_cleanup_{stamp}.json")
    Path(stats_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stats_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n飞书空行清理完成:")
    for item in results:
        print(f"  {item['table_name']}: 删除 {item['deleted_empty_rows']} 行，剩余空行 {item['empty_rows_after']}")
    print(f"  统计: {stats_path}")


def handle_refresh_influencers(args):
    """Backfill/refresh the five influencer enrichment fields."""
    from feishu.schema import INFLUENCERS_FIELDS, ensure_influencer_field_options
    from workflow.refresh_influencers import refresh_influencers

    feishu_cfg = _feishu_config_from_args(args)
    youtube_cfg = YouTubeConfig(api_key=args.youtube_api_key)
    missing_config = []
    if not youtube_cfg.api_key:
        missing_config.append("YOUTUBE_API_KEY")
    if missing_config:
        raise RuntimeError("刷新网红详情缺少配置: " + ", ".join(missing_config))

    context = _create_feishu_context(
        feishu_cfg,
        create_base_if_missing=not args.dry_run,
    )
    _ensure_created_base_schema(context)
    client = context.client
    table_id = client.find_table_by_name(INFLUENCERS_TABLE)
    if not table_id:
        raise RuntimeError(f"飞书数据表不存在: {INFLUENCERS_TABLE}")

    if args.ensure_schema:
        client.ensure_fields(table_id, INFLUENCERS_FIELDS)
        ensure_influencer_field_options(client, table_id)
    else:
        existing_fields = {
            item.get("field_name", "")
            for item in client.list_fields(table_id)
        }
        required = {"KOL Name", "最新发布日期", "断更评估", "频道初步判断", "代表视频标题"}
        missing_fields = sorted(required - existing_fields)
        if missing_fields:
            raise RuntimeError(
                f"网红详情表缺少新增字段: {missing_fields}；请先运行一次 --ensure-schema"
            )

    quota_tracker = QuotaTracker(youtube_cfg.daily_quota)
    result = refresh_influencers(
        api_key=youtube_cfg.api_key,
        client=client,
        table_id=table_id,
        fields=args.fields,
        channel_ids=args.channel_id,
        limit=args.limit,
        dry_run=args.dry_run,
        quota_tracker=quota_tracker,
        replace_kol_names=args.replace_kol_names,
    )
    result["quota_used"] = quota_tracker.used
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")

    stats_path = args.stats
    if not stats_path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stats_path = str(PROJECT_ROOT / "output" / f"refresh_influencers_{stamp}.json")
    Path(stats_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stats_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n网红详情刷新完成:")
    print(f"  源记录: {result['source_records']}")
    print(f"  预计配额: {result.get('estimated_quota', 0)}")
    print(f"  计划更新: {result['planned_updates']}")
    print(f"  实际更新: {result['actual_updates']}")
    print(f"  KOL Name 手动确认: {result['manual_confirmation_count']}")
    print(f"  断更评估: {result['status_counts']}")
    print(f"  API/解析错误: {len(result['errors'])}")
    print(f"  配额消耗: {result['quota_used']}")
    print(f"  统计: {stats_path}")


def handle_exclusion(args):
    """Handle brand exclusion management."""
    excl = BrandExclusion.load(args.config)

    if args.add_brand:
        excl.add_brand(args.add_brand)
        excl.save(args.config)
        print(f"✅ 添加品牌: {args.add_brand}")

    if args.add_channel:
        excl.add_channel_id(args.add_channel)
        excl.save(args.config)
        print(f"✅ 添加频道ID: {args.add_channel}")

    if args.add_keyword:
        excl.add_keyword(args.add_keyword)
        excl.save(args.config)
        print(f"✅ 添加频道名称关键词: {args.add_keyword}")

    if args.import_file:
        import csv
        with open(args.import_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("brand_name"):
                    excl.add_brand(row["brand_name"])
                if row.get("channel_id"):
                    excl.add_channel_id(row["channel_id"])
                if row.get("channel_keyword"):
                    excl.add_keyword(row["channel_keyword"])
        excl.save(args.config)
        print(f"✅ 导入完成")

    if args.list or not any([args.add_brand, args.add_channel, args.add_keyword, args.import_file]):
        print(f"\n品牌排除名单 ({args.config}):")
        print(f"  品牌名称 ({len(excl.brand_names)}): {excl.brand_names}")
        print(f"  频道ID ({len(excl.channel_ids)}): {excl.channel_ids}")
        print(f"  频道关键词 ({len(excl.channel_name_keywords)}): {excl.channel_name_keywords}")


if __name__ == "__main__":
    main()
