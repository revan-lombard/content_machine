"""Fetch view counts for posted clips.

YouTube: automatic with a free API key (public view/like/comment counts).
TikTok: automatic once connected (fetch_tiktok_stats, Display API).
Instagram / Facebook: automatic via the Meta connection (publish.py).
X: typed in on the dashboard.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

from .config import ROOT
from . import net, tracker

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


def fetch_tiktok_stats(cfg: dict, con, root, days: int = 30, now=None) -> dict:
    """Views, likes, comments and shares for TikTok posts logged with a full tiktok.com link.
    Same schedule as Instagram/Facebook: hourly under 72h old, daily after."""
    from datetime import datetime, timedelta, timezone

    from . import tiktok_api
    from .publish import _due, _iso, _secrets, token_dir
    res = {"updated": 0, "matched": 0, "unmatched": 0, "failed": 0}
    tok_file = token_dir(root) / "tiktok.json"
    t = _secrets().get("tiktok") or {}
    if not tok_file.exists() or not (t.get("client_key") and t.get("client_secret")):
        return res
    now = now or datetime.now(timezone.utc)
    posts = con.execute("""SELECT p.id, p.url, p.external_id, p.posted_at,
                                  (SELECT MAX(fetched_at) FROM stats s WHERE s.post_id = p.id
                                   AND s.origin = 'tiktok-api') AS last_fetch
                           FROM posts p WHERE p.platform = 'tiktok' AND p.posted_at >= ?""",
                        (_iso(now - timedelta(days=days)),)).fetchall()
    due: dict[str, int] = {}
    for p in posts:
        vid = p["external_id"] or tiktok_api.video_id(p["url"])
        if not vid:
            res["unmatched"] += 1
            continue
        if not p["external_id"]:
            con.execute("UPDATE posts SET external_id=? WHERE id=?", (vid, p["id"]))
            res["matched"] += 1
        if _due(p["posted_at"], p["last_fetch"], now):
            due[vid] = p["id"]
    con.commit()
    if not due:
        return res
    try:
        token = tiktok_api.access_token(t["client_key"], t["client_secret"], tok_file)
        found = tiktok_api.query(token, list(due))
    except (net.HttpError, RuntimeError) as e:
        print(f"  TikTok stats failed: {str(e)[:200]}")
        res["failed"] += len(due)
        return res
    for vid, post_id in due.items():
        s = found.get(vid)
        if s is None:  # not a video of the connected account (or deleted)
            res["failed"] += 1
            continue
        tracker.add_stats(con, post_id, s["views"], s["likes"], s["comments"], s["shares"],
                          origin="tiktok-api", fetched_at=_iso(now))
        res["updated"] += 1
    return res


def _x_due(posted_at: str, fetches: list[str], checkpoints: list[float], now) -> bool:
    """X reads cost money, so read once at each checkpoint (hours after posting), not hourly.
    A checkpoint is due once the post is that old and no read has been taken since then."""
    from datetime import datetime, timedelta, timezone
    posted = datetime.strptime(posted_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    reads = [datetime.strptime(f, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for f in fetches]
    for c in checkpoints:
        at = posted + timedelta(hours=float(c))
        if now >= at and not any(r >= at for r in reads):
            return True
    return False


def fetch_x_stats(cfg: dict, con, root, days: int = 30, now=None) -> dict:
    """Views (impressions), likes, replies and reposts for X posts logged with their link.
    Reads happen at cfg x.checkpoints (default 48h and 7 days) and stop at x.monthly_read_limit."""
    from datetime import datetime, timedelta, timezone

    from . import x_api
    from .publish import _iso, _secrets
    res = {"updated": 0, "matched": 0, "unmatched": 0, "failed": 0, "capped": 0}
    bearer = (_secrets().get("x") or {}).get("bearer_token")
    if not bearer:
        return res
    xc = cfg.get("x") or {}
    checkpoints = xc.get("checkpoints") or [48, 168]
    limit = int(xc.get("monthly_read_limit", 1000))
    now = now or datetime.now(timezone.utc)
    posts = con.execute("SELECT id, url, external_id, posted_at FROM posts WHERE platform='x' AND posted_at >= ?",
                        (_iso(now - timedelta(days=days)),)).fetchall()
    due: dict[str, int] = {}
    for p in posts:
        pid = p["external_id"] or x_api.post_id(p["url"])
        if not pid:
            res["unmatched"] += 1
            continue
        if not p["external_id"]:
            con.execute("UPDATE posts SET external_id=? WHERE id=?", (pid, p["id"]))
            res["matched"] += 1
        fetches = [r[0] for r in con.execute(
            "SELECT fetched_at FROM stats WHERE post_id=? AND origin='x-api'", (p["id"],))]
        if _x_due(p["posted_at"], fetches, checkpoints, now):
            due[pid] = p["id"]
    con.commit()
    month = now.strftime("%Y-%m")
    used = con.execute("SELECT COUNT(*) FROM stats WHERE origin='x-api' AND substr(fetched_at,1,7)=?",
                       (month,)).fetchone()[0]
    room = max(0, limit - used)
    if len(due) > room:
        res["capped"] = len(due) - room
        print(f"  X: monthly read limit ({limit}) reached - skipping {res['capped']} read(s). "
              f"Raise x.monthly_read_limit in config.yaml if you want more.")
        due = dict(list(due.items())[:room])
    if not due:
        return res
    try:
        found = x_api.metrics(bearer, list(due))
    except (net.HttpError, RuntimeError) as e:
        hint = " (no credits on your X developer account?)" if getattr(e, "status", 0) in (402, 403) else ""
        print(f"  X stats failed{hint}: {str(e)[:200]}")
        res["failed"] += len(due)
        return res
    for pid, post_id in due.items():
        s = found.get(pid)
        if s is None:
            res["failed"] += 1  # deleted or protected
            continue
        tracker.add_stats(con, post_id, s["views"], s["likes"], s["comments"], s["shares"],
                          origin="x-api", fetched_at=_iso(now))
        res["updated"] += 1
    return res
