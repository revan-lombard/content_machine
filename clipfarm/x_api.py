"""X (Twitter) stats via the official API v2 (pay-per-use since 2026: ~$0.005 per post read).

Needs only a Bearer token (developer.x.com -> your app -> Keys and tokens) in secrets.yaml.
Because every read costs money, posts are read at a few checkpoints (48h, 7 days) instead of
hourly, and a monthly read cap protects against surprise bills.
"""
from __future__ import annotations

import re

from . import net

LOOKUP = "https://api.x.com/2/tweets"
POST_ID = re.compile(r"(?:x|twitter)\.com/[^/]+/status(?:es)?/(\d{5,})")


def post_id(url: str) -> str | None:
    m = POST_ID.search(url or "")
    return m.group(1) if m else None


def metrics(bearer: str, ids: list[str]) -> dict[str, dict]:
    """id -> {views, likes, comments, shares}. Deleted / protected posts are absent.
    views = the post's impression count (what X shows as "Views"); if a video's own view count is
    present and larger, that's used. shares = reposts + quotes. Up to 100 ids per request."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        r = net.request("GET", LOOKUP, params={
            "ids": ",".join(ids[i:i + 100]), "tweet.fields": "public_metrics",
            "expansions": "attachments.media_keys", "media.fields": "public_metrics"},
            headers={"Authorization": f"Bearer {bearer}"}).json()
        media = {m["media_key"]: (m.get("public_metrics") or {}).get("view_count", 0)
                 for m in (r.get("includes") or {}).get("media", [])}
        for t in r.get("data", []):
            pm = t.get("public_metrics") or {}
            vid_views = max([media.get(k, 0) or 0 for k in (t.get("attachments") or {}).get("media_keys", [])] or [0])
            out[str(t["id"])] = {
                "views": int(max(pm.get("impression_count", 0) or 0, vid_views)),
                "likes": int(pm.get("like_count", 0) or 0),
                "comments": int(pm.get("reply_count", 0) or 0),
                "shares": int((pm.get("retweet_count", 0) or 0) + (pm.get("quote_count", 0) or 0)),
            }
    return out
