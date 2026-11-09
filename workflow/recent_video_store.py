"""Read legacy video rows and persist verifiable fresh samples before metrics."""

from feishu.schema import format_influencer_video_record
from feishu.value_utils import field_to_text, number_value


def table_videos(records):
    return [{"video_id": field_to_text(f.get("Video ID")),
             "channel_id": field_to_text(f.get("Channel ID")),
             "published_at": f.get("Publish Time"),
             # Bitable list returns numeric strings while batch_get returns
             # native numbers. Normalize only at this external-data boundary.
             "view_count": None if isinstance(f.get("Views"), bool) else number_value(f.get("Views")),
             "view_count_available": f.get("Views") is not None}
            for f in (r.get("fields") or {} for r in records)]


def save_sample(client, table_id, videos, existing_records=None):
    """Serial upsert by Video ID, preserving manual subtitles and other fields."""
    if not videos:
        return 0
    wanted = {v["video_id"]: v for v in videos}
    existing = {}
    inventory = client.get_all_records(table_id) if existing_records is None else existing_records
    for record in inventory:
        fields = record.get("fields") or {}
        vid = field_to_text(fields.get("Video ID"))
        if vid not in wanted:
            continue
        if field_to_text(fields.get("Channel ID")) != wanted[vid]["channel_id"]:
            raise ValueError("已有视频所属频道冲突")
        existing.setdefault(vid, []).append(record)
    updates, creates = [], []
    for vid, video in wanted.items():
        fields = format_influencer_video_record(video)["fields"]
        fields.pop("字幕内容", None)
        if vid in existing:
            for row in existing[vid]:
                updates.append({"record_id": row["record_id"], "fields": fields})
        else:
            creates.append({"fields": fields})
    updated_ids = [r["record_id"] for r in updates]
    if updates:
        latest = client.batch_get_records(table_id, updated_ids)
        if len(latest) != len(updated_ids):
            raise RuntimeError("保存视频前记录已变更")
        expected_by_id = {r["record_id"]: r["fields"] for r in updates}
        for row in latest:
            expected = expected_by_id[row["record_id"]]
            actual = row.get("fields") or {}
            if any(field_to_text(actual.get(k)) != expected[k] for k in ("Video ID", "Channel ID")):
                raise RuntimeError("保存视频前标识已变更")
        if client.batch_update_records(table_id, updates) != len(updates):
            raise RuntimeError("视频更新数量不一致")
    created = client.batch_create_records_with_ids(table_id, creates) if creates else []
    if len(created) != len(creates):
        raise RuntimeError("视频创建数量不一致")
    if existing_records is not None:
        existing_records.extend(created)
    ids = updated_ids + [r["record_id"] for r in created]
    readback = client.batch_get_records(table_id, ids)
    if len(readback) != len(ids):
        raise RuntimeError("视频明细读回数量不一致")
    for row in readback:
        actual = row.get("fields") or {}
        video = wanted.get(field_to_text(actual.get("Video ID")))
        if not video or field_to_text(actual.get("Channel ID")) != video["channel_id"]:
            raise RuntimeError("视频明细读回标识不一致")
        expected = format_influencer_video_record(video)["fields"]["Views"]
        if actual.get("Views") != expected:
            raise RuntimeError("视频播放量读回不一致，未回填频道统计")
    return len(ids)
