"""Multi-streamer reactions: several streamers hitting the same moment, in one clip.

    python -m clipfarm react find "dam jump" --sources a b c   # drafts a plan from viewer clips
    (check / fix the timestamps in the plan file)
    python -m clipfarm react build plans/dam-jump.yaml          # builds, finishes, adds to review

Every streamer in a plan must be approved in sources.yaml - this format needs permission
from each of them.

Layouts
  sequence  hook (flash of the last, best reaction) -> the moment's setup (first streamer's
            gameplay) -> each streamer's reaction in turn, name on screen -> hard cut.
            Order = plan order, so put the strongest reaction last.
  grid      gameplay on top, up to 4 face cams below reacting at the same instant.
            Needs a facecam box for every streamer and good sync.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import yaml

from . import ff, render

TS = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")


# ---------------------------------------------------------------- plans

def parse_time(v) -> float:
    """3723 / "3723" / "1h2m3s" / "1:02:03" / "62:03" -> seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    if ":" in s:
        total = 0.0
        for part in s.split(":"):
            total = total * 60 + float(part)
        return total
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    m = TS.fullmatch(s)
    if not m or not any(m.groups()):
        raise ValueError(f"can't read the time '{v}' (use 1h2m3s, 1:02:03 or seconds)")
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)


def parse_vod(vod, at=None) -> tuple[str, float]:
    """A VOD id or URL (optionally with ?t=1h2m3s) + an optional separate time -> (vod_id, seconds)."""
    s = str(vod).strip()
    m = re.search(r"videos/(\d+)", s)
    vod_id = m.group(1) if m else s
    if not vod_id.isdigit():
        raise ValueError(f"'{vod}' is not a Twitch VOD id or link")
    t = None
    if at is not None:
        t = parse_time(at)
    else:
        tm = re.search(r"[?&]t=([0-9hms:.]+)", s)
        if tm:
            t = parse_time(tm.group(1))
    if t is None:
        raise ValueError(f"no timestamp for VOD {vod_id}: add ?t=1h2m3s to the link or an 'at:' line")
    return vod_id, t


def load_plan(path: str | Path, get_source) -> dict:
    """Read a plan and check every streamer's permission (get_source raises if not approved)."""
    plan = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = plan.get("entries") or []
    if len(entries) < 2:
        raise SystemExit("A reaction plan needs at least 2 streamers under 'entries'")
    out = []
    for e in entries:
        src = get_source(e["source"])
        label = e.get("label") or f"@{src['login']}"
        if e.get("clip"):  # a clip link (Kick clips, where VODs can't be downloaded reliably)
            at = parse_time(e["at"]) if e.get("at") is not None else None  # None = near the clip's end
            out.append({"source": src, "clip": str(e["clip"]).strip(), "vod": None, "at": at, "label": label})
            continue
        if not e.get("vod"):
            raise SystemExit(f"{e['source']}: give either vod: (+ time) or clip: (a clip link)")
        vod, at = parse_vod(e["vod"], e.get("at"))
        out.append({"source": src, "vod": vod, "at": at, "label": label})
    plan["entries"] = out
    plan.setdefault("layout", "sequence")
    plan.setdefault("align", True)
    plan.setdefault("moment", plan.get("title", "this"))
    if plan["layout"] not in ("sequence", "grid"):
        raise SystemExit("layout must be sequence or grid")
    return plan


# ---------------------------------------------------------------- sync

ENV_RATE = 50  # envelope samples per second (20ms)


def envelope(path: str | Path, start: float, dur: float) -> np.ndarray:
    """Loudness envelope (log RMS per 20ms) of a stretch of a file's audio."""
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, start):.3f}",
                          "-t", f"{dur:.3f}", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = 8000 // ENV_RATE
    x = x[: len(x) // n * n].reshape(-1, n)
    e = np.log(np.sqrt((x ** 2).mean(axis=1)) + 1e-4) if len(x) else np.zeros(0)
    return e


def slide(ref: np.ndarray, other: np.ndarray) -> tuple[int, float]:
    """Slide `ref` along the longer `other`. Returns (offset in samples where it fits best,
    Pearson correlation there, -1..1)."""
    L = len(ref)
    if L < ENV_RATE * 3 or len(other) < L:
        return 0, -1.0
    r = (ref - ref.mean()) / (ref.std() or 1)
    best, best_c = 0, -1.0
    for k in range(len(other) - L + 1):
        w = other[k:k + L]
        sd = w.std()
        if sd == 0:
            continue
        c = float(np.dot(r, (w - w.mean()) / sd) / L)
        if c > best_c:
            best, best_c = k, c
    return best, best_c


def align(pieces: list[tuple[Path, float]], window: float = 12.0, max_shift: float = 6.0,
          min_corr: float = 0.45) -> list[tuple[float, float | None]]:
    """pieces: (file, local time of the moment as planned). The first is the reference.
    Returns per piece (corrected time, confidence). A shift is only applied when the audio
    matches with correlation >= min_corr (shared game audio, e.g. a cutscene); otherwise the
    planned time is kept."""
    ref_file, ref_t = pieces[0]
    ref = envelope(ref_file, ref_t - window / 2, window)
    out: list[tuple[float, float | None]] = [(ref_t, None)]
    for f, t in pieces[1:]:
        start = max(0.0, t - window / 2 - max_shift)
        oth = envelope(f, start, window + 2 * max_shift)
        k, c = slide(ref, oth)
        moved = start + k / ENV_RATE + window / 2  # where the reference's centre lands in `other`
        out.append((moved, round(c, 2)) if c >= min_corr else (t, round(c, 2)))
    return out


# ---------------------------------------------------------------- rendering

def render_sequence(cfg: dict, items: list[dict], clip_dir: Path, hook: str, setup_text: str,
                    setup_secs: float = 6.0, react_pre: float = 1.0, react_post: float = 4.0,
                    max_length: float | None = None) -> list[dict]:
    """items: {"file", "t" (local moment time), "label", "facecam"} in play order."""
    clip_dir.mkdir(parents=True, exist_ok=True)
    parts, beats, t = [], [], 0.0
    n = len(items)
    each = react_pre + react_post
    if max_length:  # shrink the per-streamer slot to fit the campaign's max length
        room = max_length - 2.0 - setup_secs
        each = max(2.5, min(each, room / n))
        react_post = each - react_pre

    def seg(it, start, dur, punch=False, facecam="same"):
        p = clip_dir / f"p{len(parts)}.mp4"
        fc = it.get("facecam") if facecam == "same" else facecam
        render.segment(cfg, str(it["file"]), start, dur, p, punch=punch, facecam=fc)
        parts.append(p)
        return ff.duration(p)

    last = items[-1]
    d = seg(last, last["t"] - 0.3, 2.0, punch=True)  # hook: flash of the best reaction
    beats.append({"key": "hook", "start": 0.0, "end": round(d + 1.0, 2), "text": hook})
    t += d
    first = items[0]
    d = seg(first, first["t"] - setup_secs, setup_secs, facecam=None)  # the moment, full gameplay
    beats.append({"key": "problem", "start": round(t + 1.0, 2), "end": round(t + d, 2), "text": setup_text})
    t += d
    for i, it in enumerate(items, 1):
        d = seg(it, it["t"] - react_pre, react_pre + react_post, punch=True)
        beats.append({"key": f"react{i}", "start": round(t, 2), "end": round(t + d, 2), "text": it["label"]})
        t += d
    render.concat(parts, clip_dir / "rough.mp4")
    for p in parts:
        p.unlink()
    return beats


def render_grid(cfg: dict, items: list[dict], clip_dir: Path, hook: str, pre: float = 6.0,
                post: float = 5.0) -> tuple[list[dict], list[tuple[str, int, int]]]:
    """Gameplay (first streamer) on top, up to 4 face cams in a 2x2 grid below, in sync.
    Returns beats and tile labels (text, x, y)."""
    v = cfg["video"]
    W, H, fps = v["width"], v["height"], v["fps"]
    items = items[:4]
    clip_dir.mkdir(parents=True, exist_ok=True)
    top_h = H // 2 // 2 * 2
    tile_w, tile_h = W // 2 // 2 * 2, (H - top_h) // 2 // 2 * 2
    dur = pre + post
    args, chains, audio = [], [], []
    for i, it in enumerate(items):
        args += ["-ss", f"{max(0.0, it['t'] - pre):.3f}", "-t", f"{dur:.3f}", "-i", str(it["file"])]
    g = items[0]
    gx = f"(iw-ow)*{float(v['crop_x'])}"
    empty = 4 - len(items)
    split_n = 2 + empty
    outs = "[g0][c0in]" + "".join(f"[f{k}]" for k in range(empty))
    chains.append(f"[0:v]split={split_n}{outs};[g0]scale=-2:{top_h},crop={W}:{top_h}:{gx}:0,setsar=1[top]")
    for i, it in enumerate(items):
        x, y, w, h = it.get("facecam") or [0, 0, 1, 1]
        src = "[c0in]" if i == 0 else f"[{i}:v]"
        chains.append(f"{src}crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={tile_w}:{tile_h}:"
                      f"force_original_aspect_ratio=increase,crop={tile_w}:{tile_h},setsar=1[t{i}]")
        audio.append(f"[{i}:a]volume={1.0 if i == 0 else 0.6}[a{i}]")
    for k in range(empty):  # fewer than 4 streamers: fill with dimmed, blurred gameplay
        chains.append(f"[f{k}]scale={tile_w}:{tile_h}:force_original_aspect_ratio=increase,"
                      f"crop={tile_w}:{tile_h},gblur=sigma=25,eq=brightness=-0.25,setsar=1[t{len(items) + k}]")
    chains.append(f"[t0][t1][t2][t3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[bottom]")
    chains.append(f"[top][bottom]vstack,fps={fps},format=yuv420p[v]")
    chains += audio
    chains.append("".join(f"[a{i}]" for i in range(len(items))) +
                  f"amix=inputs={len(items)}:normalize=0,alimiter=limit=0.95,aresample=48000[a]")
    out = clip_dir / "rough.mp4"
    render._encode(cfg, [*args, "-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]"],
                   ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-t", f"{dur:.3f}",
                    "-movflags", "+faststart", str(out)])
    labels = [(it["label"], 24 + (i % 2) * tile_w, top_h + 20 + (i // 2) * tile_h) for i, it in enumerate(items)]
    beats = [{"key": "hook", "start": 0.0, "end": 3.0, "text": hook}]
    return beats, labels
