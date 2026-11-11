"""Fetch view counts for posted clips.

YouTube: automatic with a free API key (public view/like/comment counts).
TikTok / Instagram / Facebook / X: typed in on the dashboard for now (their stats APIs need
app reviews or business accounts - next phase).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

from .config import ROOT
from . import tracker

YT_ID = re.compile(r"(?:youtube\.com/(?:shorts/|watch\?v=|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    m = YT_ID.search(url or "")
    return m.group(1) if m else None


def _youtube_key() -> str | None:
    key = os.environ.get("YOUTUBE_API_KEY")
    p = ROOT / "secrets.yaml"
    if not key and p.exists():
        import yaml
        key = ((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("youtube") or {}).get("api_key")
    return key or None


def fetch_youtube(con, api_key: str | None = None, days: int = 30, fetch=None) -> int:
    """Snapshot stats for YouTube posts from the last `days`. Returns how many were updated."""
    key = api_key or _youtube_key()
    if not key:
        print("YouTube stats skipped: no API key (see secrets.example.yaml)")
        return 0
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = con.execute("SELECT id, url FROM posts WHERE platform='youtube' AND posted_at >= ?",
                       (cutoff,)).fetchall()
    ids = {youtube_id(r["url"]): r["id"] for r in rows if youtube_id(r["url"])}
    fetch = fetch or _get
    n = 0
    keys = list(ids)
    for i in range(0, len(keys), 50):
        batch = keys[i:i + 50]
        q = urllib.parse.urlencode({"part": "statistics", "id": ",".join(batch), "key": key})
        data = fetch(f"https://www.googleapis.com/youtube/v3/videos?{q}")
        for item in data.get("items", []):
            s = item.get("statistics", {})
            tracker.add_stats(con, ids[item["id"]], int(s.get("viewCount", 0)), int(s.get("likeCount", 0)),
                              int(s.get("commentCount", 0)), 0, origin="youtube-api")
            n += 1
    return n


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())
