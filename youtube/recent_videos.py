"""Read up to ten accessible uploads, retaining playlist order across pages."""

from .client import api_request
from .playlists import YOUTUBE_PLAYLIST_ITEMS_URL, channel_id_to_uploads_playlist
from .videos import fetch_video_details


def collect_recent_videos(api_key, channel_id, *, uploads_playlist_id="", count=10, quota_tracker=None):
    if not 1 <= count <= 50:
        raise ValueError("recent video count must be between 1 and 50")
    videos, seen_ids, seen_tokens = [], set(), set()
    token = ""
    # Bound pathological playlists and preserve old metrics on incomplete reads.
    for _ in range(100):
        if token in seen_tokens:
            return [], "Uploads pagination token repeated"
        seen_tokens.add(token)
        if quota_tracker and quota_tracker.remaining() < 1:
            return [], "Local quota budget exhausted"
        params = {"part": "contentDetails", "playlistId": uploads_playlist_id or channel_id_to_uploads_playlist(channel_id),
                  "maxResults": count, "key": api_key}
        if token:
            params["pageToken"] = token
        ok, data, error = api_request(YOUTUBE_PLAYLIST_ITEMS_URL, params=params,
                                     quota_tracker=quota_tracker, api_name="playlistItems.list")
        if not ok:
            return [], error
        if not isinstance(data.get("items"), list):
            return [], "Malformed uploads response"
        ids = []
        for item in data["items"]:
            vid = (item.get("contentDetails") or {}).get("videoId")
            if vid and vid not in seen_ids:
                ids.append(vid)
                seen_ids.add(vid)
        if ids:
            if quota_tracker and quota_tracker.remaining() < 1:
                return [], "Local quota budget exhausted"
            details, _, error = fetch_video_details(api_key, ids, quota_tracker=quota_tracker)
            if error:
                return [], error
            by_id = {}
            for video in details:
                vid = video.get("video_id")
                if vid not in ids or vid in by_id or video.get("channel_id") != channel_id:
                    return [], "Video ID or channel ownership mismatch"
                by_id[vid] = video
            videos.extend(by_id[vid] for vid in ids if vid in by_id)
        if len(videos) >= count:
            return videos[:count], ""
        token = data.get("nextPageToken", "")
        if not token:
            return videos, ""
        if not isinstance(token, str):
            return [], "Malformed uploads pagination token"
    return [], "Uploads pagination limit reached; sample incomplete"
