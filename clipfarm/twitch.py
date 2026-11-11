"""Twitch: find the best moments of a VOD and download only those parts.

Moment signals, strongest first:
  1. Viewer clips  - Twitch's API lists the clips viewers made, with their VOD offset and views.
                     Many people clipping the same moment is the best signal there is.
  2. Chat activity - (optional) a chat replay JSON from TwitchDownloaderCLI. Message bursts and
                     laugh/hype emotes mark moments nobody clipped.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT

API = "https://api.twitch.tv/helix"
CHAT_DELAY = 6.0  # seconds between something happening on stream and chat reacting

HYPE = re.compile(r"\b(lul|lmao+|kekw|omegalul|icant|lol+|xd+|pog\w*|w+|no ?way|clip (it|that)|"
                  r"wtf|holy|bro+|nah+)\b|💀|😂|🤣", re.I)


# ---------------------------------------------------------------- API

def _credentials() -> tuple[str, str]:
    cid, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    p = ROOT / "secrets.yaml"
    if (not cid or not secret) and p.exists():
        import yaml
        s = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("twitch") or {}
        cid, secret = cid or s.get("client_id"), secret or s.get("client_secret")
    if not cid or not secret:
        raise SystemExit("Twitch API keys missing. Create a free app at https://dev.twitch.tv/console/apps "
                         "(OAuth redirect: http://localhost, category: Application Integration), then put "
                         "the Client ID and a new Client Secret in secrets.yaml (see secrets.example.yaml).")
    return cid, secret


class Helix:
    def __init__(self):
        self.cid, secret = _credentials()
        body = urllib.parse.urlencode({"client_id": self.cid, "client_secret": secret,
                                       "grant_type": "client_credentials"}).encode()
        with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", data=body, timeout=20) as r:
            self.token = json.loads(r.read())["access_token"]

    def get(self, path: str, **params) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{API}/{path}?{q}", headers={
            "Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
        return {}

    def user_id(self, login: str) -> str:
        data = self.get("users", login=login)["data"]
        if not data:
            raise SystemExit(f"Twitch user '{login}' not found")
        return data[0]["id"]

    def vods(self, user_id: str, n: int = 5) -> list[dict]:
        return self.get("videos", user_id=user_id, type="archive", first=n)["data"]

    def vod(self, vod_id: str) -> dict:
        data = self.get("videos", id=vod_id)["data"]
        if not data:
            raise SystemExit(f"VOD {vod_id} not found (deleted, or subscriber-only?)")
        return data[0]

    def clips(self, user_id: str, started_at: str, ended_at: str, limit: int = 500) -> list[dict]:
        out, cursor = [], None
        while len(out) < limit:
            page = self.get("clips", broadcaster_id=user_id, started_at=started_at,
                            ended_at=ended_at, first=100, after=cursor)
            out += page.get("data", [])
            cursor = (page.get("pagination") or {}).get("cursor")
            if not cursor or not page.get("data"):
                break
        return out


def parse_duration(s: str) -> float:
    """Twitch VOD durations look like '3h2m1s'."""
    total = 0.0
    for num, unit in re.findall(r"(\d+)([hms])", s or ""):
        total += int(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def vod_end_iso(vod: dict) -> str:
    from datetime import datetime, timedelta
    start = datetime.strptime(vod["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    # clips can be made a while after the stream ends, from the VOD
    end = start + timedelta(seconds=parse_duration(vod["duration"])) + timedelta(days=7)
    return end.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- moments

@dataclass
class Moment:
    peak: float                    # VOD seconds where the payoff happens
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    clip_titles: list[str] = field(default_factory=list)
    clip_views: int = 0
    clippers: int = 0


def moments_from_clips(clips: list[dict], vod_id: str) -> list[Moment]:
    """Group viewer clips of the same moment. Score grows with views AND with how many people clipped it."""
    ms: list[Moment] = []
    usable = sorted((c for c in clips if str(c.get("video_id")) == str(vod_id)
                     and c.get("vod_offset") is not None), key=lambda c: c["vod_offset"])
    for c in usable:
        # people clip right after the moment, so the payoff sits near the clip's end
        peak = float(c["vod_offset"]) + max(1.0, float(c.get("duration", 30)) - 4.0)
        near = next((m for m in ms if abs(m.peak - peak) < 20), None)
        if near is None:
            near = Moment(peak=peak)
            ms.append(near)
        near.clippers += 1
        near.clip_views += int(c.get("view_count", 0))
        if c.get("title"):
            near.clip_titles.append(c["title"])
    for m in ms:
        m.score = math.log10(1 + m.clip_views) * 2 + m.clippers * 1.5
        m.reasons.append(f"{m.clippers} viewer clip(s), {m.clip_views} views")
    return ms


def chat_rates(comments: list[dict], bucket: float = 5.0) -> tuple[list[float], list[float]]:
    """Messages per bucket, and hype-weighted messages per bucket."""
    if not comments:
        return [], []
    end = max(float(c.get("content_offset_seconds", 0)) for c in comments)
    n = int(end // bucket) + 1
    rate, hype = [0.0] * n, [0.0] * n
    for c in comments:
        i = int(float(c.get("content_offset_seconds", 0)) // bucket)
        body = (c.get("message") or {}).get("body", "") if isinstance(c.get("message"), dict) else str(c.get("message", ""))
        rate[i] += 1
        hype[i] += 1 + 2 * len(HYPE.findall(body))
    return rate, hype


def moments_from_chat(comments: list[dict], max_n: int = 30, bucket: float = 5.0) -> list[Moment]:
    """Chat bursts compared with the surrounding 5 minutes."""
    _, hype = chat_rates(comments, bucket)
    if len(hype) < 3:
        return []
    k = max(3, int(300 / bucket))
    out = []
    for i, v in enumerate(hype):
        window = hype[max(0, i - k // 2): i + k // 2 + 1]
        mean = sum(window) / len(window)
        sd = math.sqrt(sum((x - mean) ** 2 for x in window) / len(window)) or 1.0
        z = (v - mean) / sd
        if z >= 2.5 and v >= 5:
            out.append(Moment(peak=max(0.0, i * bucket + bucket / 2 - CHAT_DELAY), score=z,
                              reasons=[f"chat spike z={z:.1f}"]))
    out.sort(key=lambda m: -m.score)
    return out[:max_n]


def combine(clip_ms: list[Moment], chat_ms: list[Moment], min_gap: float, max_n: int) -> list[Moment]:
    """Merge both signals; a moment backed by both gets both scores."""
    merged = [Moment(m.peak, m.score, list(m.reasons), list(m.clip_titles), m.clip_views, m.clippers)
              for m in clip_ms]
    for c in chat_ms:
        hit = next((m for m in merged if abs(m.peak - c.peak) < 15), None)
        if hit:
            hit.score += c.score
            hit.reasons += c.reasons
        else:
            merged.append(Moment(c.peak, c.score * 0.8, list(c.reasons)))
    merged.sort(key=lambda m: -m.score)
    picked: list[Moment] = []
    for m in merged:
        if all(abs(m.peak - p.peak) >= min_gap for p in picked):
            picked.append(m)
        if len(picked) >= max_n:
            break
    return sorted(picked, key=lambda m: m.peak)


def load_chat(path: str | Path) -> list[dict]:
    """Chat JSON from TwitchDownloaderCLI (`chatdownload`), or a plain list of comments."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("comments", []) if isinstance(data, dict) else data


# ---------------------------------------------------------------- downloads

def _ytdlp() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else ["python", "-m", "yt_dlp"]


def download_section(vod_id: str, start: float, end: float, out: Path, max_height: int = 1080) -> Path:
    """Download only [start, end] of a VOD - no need to pull a 6-hour stream."""
    if out.exists() and out.stat().st_size > 0:
        return out
    cmd = [*_ytdlp(), "--quiet", "--no-warnings", "--no-part",
           "-f", f"best[height<={max_height}]/best",
           "--download-sections", f"*{start:.1f}-{end:.1f}", "--force-keyframes-at-cuts",
           "-o", str(out), f"https://www.twitch.tv/videos/{vod_id}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"yt-dlp failed for VOD {vod_id} [{start:.0f}-{end:.0f}]:\n{res.stderr[-1500:]}")
    return out


def download_chat(vod_id: str, out: Path) -> Path | None:
    """Chat replay via TwitchDownloaderCLI, if it's installed. Returns None otherwise."""
    exe = shutil.which("TwitchDownloaderCLI") or shutil.which("twitchdownloadercli")
    local = ROOT / "tools" / ("TwitchDownloaderCLI.exe" if os.name == "nt" else "TwitchDownloaderCLI")
    exe = exe or (str(local) if local.exists() else None)
    if not exe:
        return None
    if out.exists():
        return out
    res = subprocess.run([exe, "chatdownload", "--id", str(vod_id), "-o", str(out)],
                         capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        print(f"  chat download failed, continuing with viewer clips only:\n  {res.stderr[-300:]}")
        return None
    return out
