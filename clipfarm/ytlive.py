"""YouTube streamers: find the best moments of a past livestream and download only those parts.

YouTube has no clips API, so moments come from:
  1. Live chat replay - message bursts / laugh + hype emotes (same scoring as Twitch chat).
  2. "Most replayed" - YouTube's heatmap of what viewers rewatch. It's coarse on long streams
     (100 segments), so each replayed stretch is narrowed to its loudest moment.
Everything goes through yt-dlp, so no API key is needed. If YouTube asks yt-dlp to "confirm
you're not a bot", set youtube_live.cookies_from_browser in config.yaml (e.g. chrome).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import twitch

VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})|^([A-Za-z0-9_-]{11})$")


def video_id(s: str) -> str | None:
    m = VIDEO_ID.search(str(s).strip())
    return (m.group(1) or m.group(2)) if m else None


def channel_url(login: str) -> str:
    login = login.strip()
    if login.startswith("http"):
        return login.rstrip("/")
    if login.startswith("UC") and len(login) == 24:
        return f"https://www.youtube.com/channel/{login}"
    return f"https://www.youtube.com/@{login.lstrip('@')}"


def _base(cfg: dict) -> list[str]:
    exe = shutil.which("yt-dlp")
    cmd = [exe] if exe else ["python", "-m", "yt_dlp"]
    cookies = (cfg.get("youtube_live") or {}).get("cookies_from_browser")
    if cookies:
        cmd += ["--cookies-from-browser", str(cookies)]
    return cmd + ["--no-warnings"]


def run(cfg: dict, args: list[str]) -> subprocess.CompletedProcess:
    """One yt-dlp call (tests replace this)."""
    r = subprocess.run([*_base(cfg), *args], capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()[-600:]
        hint = (" - set youtube_live.cookies_from_browser in config.yaml (e.g. chrome)"
                if "not a bot" in err or "Sign in" in err else "")
        raise RuntimeError(f"yt-dlp failed{hint}: {err}")
    return r


def recent_streams(cfg: dict, login: str, n: int = 3) -> list[dict]:
    """The channel's latest finished livestreams (from its Live tab), newest first."""
    r = run(cfg, ["-J", "--flat-playlist", "--playlist-end", str(n), channel_url(login) + "/streams"])
    entries = json.loads(r.stdout).get("entries") or []
    return [{"id": e["id"], "title": e.get("title", "")} for e in entries
            if e.get("id") and e.get("live_status") not in ("is_live", "is_upcoming")]


def info(cfg: dict, vid: str) -> dict:
    r = run(cfg, ["-J", "--skip-download", f"https://www.youtube.com/watch?v={vid}"])
    d = json.loads(r.stdout)
    return {"id": vid, "title": d.get("title", ""), "duration": float(d.get("duration") or 0),
            "heatmap": d.get("heatmap") or [], "live_status": d.get("live_status")}


def chat(cfg: dict, vid: str, out_dir: Path) -> list[dict] | None:
    """Live chat replay as Twitch-style comments ({content_offset_seconds, message:{body}})."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{vid}.live_chat.json"
    if not target.exists():
        try:
            run(cfg, ["--skip-download", "--write-subs", "--sub-langs", "live_chat",
                      "-o", str(out_dir / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"])
        except RuntimeError as e:
            print(f"  no chat replay: {str(e)[:150]}")
            return None
    if not target.exists():
        return None
    return parse_chat(target.read_text(encoding="utf-8"))


def parse_chat(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        rep = d.get("replayChatItemAction") or {}
        try:
            offset = int(rep.get("videoOffsetTimeMsec", "")) / 1000
        except ValueError:
            continue
        for act in rep.get("actions") or []:
            item = (act.get("addChatItemAction") or {}).get("item") or {}
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            body = "".join(run_.get("text") or (run_.get("emoji") or {}).get("emojiId", "")
                           for run_ in (r.get("message") or {}).get("runs", []))
            out.append({"content_offset_seconds": offset, "message": {"body": body}})
    return out


def moments_from_heatmap(heatmap: list[dict], max_n: int = 10) -> list[twitch.Moment]:
    """Most-replayed stretches: local maxima clearly above the video's typical replay level."""
    if len(heatmap) < 5:
        return []
    vals = [float(h.get("value") or 0) for h in heatmap]
    typical = sorted(vals)[len(vals) // 2]
    out = []
    for i, h in enumerate(heatmap):
        v = vals[i]
        if v < max(0.35, typical + 0.2):
            continue
        if (i > 0 and vals[i - 1] > v) or (i + 1 < len(vals) and vals[i + 1] >= v):
            continue  # not a local peak
        s, e = float(h["start_time"]), float(h["end_time"])
        m = twitch.Moment(peak=(s + e) / 2, score=v * 6, reasons=[f"most replayed ({v:.2f})"])
        m.span = (s, e)  # refined to the loudest point once downloaded
        out.append(m)
    out.sort(key=lambda m: -m.score)
    return out[:max_n]


def download_section(cfg: dict, vid: str, start: float, end: float, out: Path) -> Path:
    if out.exists() and out.stat().st_size > 0:
        return out
    run(cfg, ["--quiet", "--no-part", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
              "--merge-output-format", "mp4", "--download-sections", f"*{start:.1f}-{end:.1f}",
              "--force-keyframes-at-cuts", "-o", str(out), f"https://www.youtube.com/watch?v={vid}"])
    if not out.exists():
        raise RuntimeError(f"yt-dlp didn't produce {out.name}")
    return out
