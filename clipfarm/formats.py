"""Clip formats under test. Each turns moment(s) of footage into rough.mp4 + its on-screen beats.

Single-moment formats:
  payoff_first - 1.5s flash of the payoff, then the build-up into it (the original moment clip)
  straight     - plain chronological build-up -> payoff, hook text up front (the control)
  beat_story   - your structure, compressed: question hook -> build -> "Wait for it..." -> payoff
                 with a punch-in -> hard cut
  loop         - ~9s ending right on the payoff, cut so it loops
Multi-moment:
  countdown    - top 3 moments of one stream, #3 -> #2 -> #1
"""
from __future__ import annotations

from pathlib import Path

from . import ff, render

SINGLE = ["payoff_first", "straight", "beat_story", "loop"]
MULTI = ["countdown", "multi_react"]  # multi_react: made on request from a plan (react.py)
ALL = SINGLE + MULTI


def _win(peak: float, before: float, after: float, total: float) -> tuple[float, float]:
    end = min(total, peak + after)
    start = max(0.0, end - before - after)
    return start, end - start


def render_single(cfg: dict, fmt: str, src: str | Path, peak: float, total: float, clip_dir: Path,
                  hook: str, length: float | None = None, facecam=None) -> list[dict]:
    """Render one moment in `fmt`. Returns the beats (on-screen text with timings)."""
    clip_dir.mkdir(parents=True, exist_ok=True)
    m = cfg["moments"]
    length = float(length or m["length"])
    src = str(src)
    parts: list[Path] = []
    beats: list[dict] = []

    def seg(start, dur, punch=False):
        p = clip_dir / f"p{len(parts)}.mp4"
        render.segment(cfg, src, start, dur, p, punch=punch, facecam=facecam)
        parts.append(p)
        return dur

    if fmt == "payoff_first":
        hf = float(m["hook_flash"])
        seg(max(0.0, peak - hf / 2), hf, punch=True)
        s, d = _win(peak, length - 2.0, 2.0, total)
        seg(s, d)
        beats = [{"key": "hook", "start": 0.0, "end": round(hf + 2.0, 2), "text": hook}]
    elif fmt == "straight":
        s, d = _win(peak, length - 2.0, 2.0, total)
        seg(s, d)
        beats = [{"key": "hook", "start": 0.0, "end": 3.0, "text": hook}]
    elif fmt == "beat_story":
        s, d = _win(peak - 0.3, length - 1.8, 0.0, total)
        d1 = seg(s, d)
        d2 = seg(peak - 0.3, min(1.8, max(0.5, total - (peak - 0.3))), punch=True)  # hard cut after payoff
        beats = [{"key": "hook", "start": 0.0, "end": 3.0, "text": hook},
                 {"key": "reengage", "start": round(max(3.0, d1 - 3.5), 2), "end": round(d1, 2),
                  "text": "Wait for it..."}]
        _ = d2
    elif fmt == "loop":
        s, d = _win(peak, 8.0, 1.0, total)
        seg(s, d)
        beats = [{"key": "hook", "start": 0.0, "end": 2.0, "text": hook}]
    else:
        raise ValueError(f"unknown single-moment format: {fmt}")

    if len(parts) == 1:
        parts[0].replace(clip_dir / "rough.mp4")
    else:
        render.concat(parts, clip_dir / "rough.mp4")
        for p in parts:
            p.unlink()
    return beats


def render_countdown(cfg: dict, items: list[tuple[str | Path, float, float]], clip_dir: Path,
                     hook: str, facecam=None) -> list[dict]:
    """items: (src, peak, total) best-first. Plays them #3 -> #1, ~10s each."""
    clip_dir.mkdir(parents=True, exist_ok=True)
    top = items[:3][::-1]
    parts, beats, t = [], [], 0.0
    for i, (src, peak, total) in enumerate(top):
        rank = len(top) - i
        s, d = _win(peak, 8.5, 1.5, total)
        p = clip_dir / f"p{i}.mp4"
        render.segment(cfg, str(src), s, d, p, punch=True, facecam=facecam)
        parts.append(p)
        label_start = t + (2.5 if i == 0 else 0.0)
        beats.append({"key": f"rank{rank}", "start": round(label_start, 2),
                      "end": round(min(t + d, label_start + 2.0), 2), "text": f"#{rank}"})
        t += ff.duration(p)
    beats.insert(0, {"key": "hook", "start": 0.0, "end": 2.5, "text": hook})
    render.concat(parts, clip_dir / "rough.mp4")
    for p in parts:
        p.unlink()
    return beats
