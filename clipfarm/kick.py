"""Kick: find a streamer's most-viewed recent clips and download them.

Kick has no official API for clips or VODs, so this uses the endpoint kick.com's own clip
pages use, behind Cloudflare - requests go through curl_cffi impersonating Chrome. Kick VOD
downloads broke in a 2026 platform change, so the pipeline works from viewer CLIPS (up to 3 min
each), which already contain the moment. Expect this to need fixes when Kick changes things:
`pip install -U yt-dlp curl_cffi` is the first thing to try.

Safety: clips are filtered to GTA categories, and gambling categories are always blocked
(promoting gambling gets clip accounts banned).
"""
from __future__ import annotations

import math
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLIPS = "https://kick.com/api/v2/channels/{slug}/clips"


def _session():
    try:
        from curl_cffi import requests
    except ImportError:
        raise SystemExit("Kick needs the curl_cffi package: pip install -r requirements.txt")
    return requests.Session(impersonate="chrome")


def _http_get(url: str, params: dict) -> dict:
    r = _session().get(url, params=params, timeout=30)
    if r.status_code == 404:
        raise SystemExit(f"Kick channel not found ({url.split('/channels/')[1].split('/')[0]})")
    if r.status_code != 200:
        raise RuntimeError(f"Kick returned HTTP {r.status_code} (Cloudflare block or API change). "
                           "Try: pip install -U curl_cffi yt-dlp")
    return r.json()


def _ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def normalize(c: dict, slug: str) -> dict:
    """Kick's clip objects aren't documented and have changed before; read fields defensively."""
    cat = c.get("category")
    cat_name = (cat.get("name") if isinstance(cat, dict) else cat) or c.get("category_name") or ""
    cid = str(c.get("id") or c.get("clip_id") or "")
    ls = c.get("livestream")
    stream = c.get("livestream_id") or (ls.get("id") if isinstance(ls, dict) else "") or ""
    return {
        "id": cid,
        "title": (c.get("title") or "").strip(),
        "views": int(c.get("views") or c.get("view_count") or 0),
        "likes": int(c.get("likes") or c.get("likes_count") or 0),
        "duration": float(c.get("duration") or 0),
        "created": _ts(c.get("created_at")),
        "stream": str(stream),
        "category": cat_name,
        "media": c.get("clip_url") or c.get("video_url") or "",
        "page": f"https://kick.com/{slug}/clips/{cid}",
    }


def clips(slug: str, hours: float = 48, max_pages: int = 6, get=None, now: datetime | None = None) -> list[dict]:
    """Clips made in the last `hours`, newest first."""
    get = get or _http_get
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    out, cursor = [], "0"
    for _ in range(max_pages):
        d = get(CLIPS.format(slug=slug), {"cursor": cursor, "sort": "date", "time": "all"})
        items = d.get("clips") or d.get("data") or []
        page = [normalize(c, slug) for c in items]
        out += [c for c in page if c["created"] is None or c["created"] >= since]
        cursor = d.get("nextCursor") or d.get("next_cursor") or d.get("cursor")
        oldest = min((c["created"] for c in page if c["created"]), default=None)
        if not cursor or not items or (oldest and oldest < since):
            break
    return out


def allowed(clip: dict, wanted: list[str], blocked: list[str]) -> bool:
    cat = clip["category"].lower()
    if any(b.lower() in cat for b in blocked):
        return False
    return not wanted or any(w.lower() in cat for w in wanted)


def moments(clip_list: list[dict], max_n: int) -> list[dict]:
    """Group clips of the same moment (same stream, made within 90s of each other), keep the
    most-viewed clip of each group, score like Twitch (views + how many people clipped it)."""
    groups: list[list[dict]] = []
    for c in sorted(clip_list, key=lambda c: c["created"] or datetime.min.replace(tzinfo=timezone.utc)):
        g = next((g for g in groups if g[0]["stream"] == c["stream"] and c["created"] and g[-1]["created"]
                  and (c["created"] - g[-1]["created"]).total_seconds() <= 90), None)
        (g.append(c) if g is not None else groups.append([c]))
    out = []
    for g in groups:
        best = max(g, key=lambda c: c["views"])
        views = sum(c["views"] for c in g)
        out.append({**best, "clippers": len(g), "total_views": views,
                    "titles": [c["title"] for c in sorted(g, key=lambda c: -c["views"]) if c["title"]],
                    "score": math.log10(1 + views) * 2 + len(g) * 1.5,
                    "reasons": [f"{len(g)} viewer clip(s), {views} views"]})
    out.sort(key=lambda m: -m["score"])
    return out[:max_n]


def download(clip_or_url, out: Path) -> Path:
    """Download a clip: its media URL with ffmpeg first, then yt-dlp impersonating Chrome."""
    if out.exists() and out.stat().st_size > 0:
        return out
    media = clip_or_url.get("media") if isinstance(clip_or_url, dict) else None
    page = clip_or_url.get("page") if isinstance(clip_or_url, dict) else str(clip_or_url)
    errors = []
    if media:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", media, "-c", "copy",
                            "-bsf:a", "aac_adtstoasc", str(out)], capture_output=True, text=True)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
        errors.append(r.stderr[-300:])
    exe = shutil.which("yt-dlp")
    cmd = [exe] if exe else ["python", "-m", "yt_dlp"]
    r = subprocess.run([*cmd, "--quiet", "--no-warnings", "--impersonate", "chrome", "-f", "best",
                        "-o", str(out), page], capture_output=True, text=True)
    if r.returncode == 0 and out.exists():
        return out
    errors.append(r.stderr[-500:])
    raise RuntimeError(f"couldn't download Kick clip {page}: {' | '.join(e for e in errors if e)}")
