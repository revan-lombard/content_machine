"""Turns a recording + beat tags into finished vertical clips."""
from __future__ import annotations

import html
import json
import random
import shutil
from pathlib import Path

import yaml

from . import captions, detect, ff, formats, render, tracker, writer
from .config import BEATS, ROOT

SFX_DIR = ROOT / "assets" / "sfx"

# How long each beat's on-screen line stays up (None = the whole beat).
TEXT_SECS = {"hook": None, "problem": None, "challenge": None, "build": 3.0,
             "obstacle": 3.5, "reengage": 3.5, "result": 3.0, "end": None}
PUNCH = {"obstacle", "result"}


# ---------------------------------------------------------------- sessions

def load_session(path: str | Path, video: str | None, challenge: str | None) -> dict:
    p = Path(path)
    if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".flv", ".ts"):
        s = {"challenge": challenge or "", "recording": str(p), "tags": []}
        name = p.stem
    else:
        s = json.loads(p.read_text(encoding="utf-8"))
        name = p.name.replace(".beats.json", "").replace(".json", "")
    if video:
        s["recording"] = video
    if challenge:
        s["challenge"] = challenge
    if not s.get("recording") or not Path(s["recording"]).exists():
        raise SystemExit(f"Recording not found ({s.get('recording')}). Pass it with --video PATH.")
    s["tags"] = sorted(s.get("tags", []), key=lambda x: x["t"])
    s["name"] = name
    s["out"] = str(ROOT / "out" / name)
    return s


def db():
    return tracker.connect(ROOT / "data" / "tracker.db")


def clip_id(clip_dir: Path) -> str:
    try:
        return Path(clip_dir).resolve().relative_to((ROOT / "out").resolve()).as_posix()
    except ValueError:
        return Path(clip_dir).resolve().as_posix()


def _save_script(clip_dir: Path, meta: dict) -> None:
    """Write script.yaml and log the clip (with its format + hook style) for performance tracking."""
    cid = clip_id(clip_dir)
    meta = {"clip_id": cid, **meta}
    (clip_dir / "script.yaml").write_text(
        "# Edit any text below, then run:  python -m clipfarm finish " + clip_dir.as_posix() + "\n"
        + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")
    src = meta.get("source") or (meta.get("sources") or [{}])[0]
    with db() as con:
        who = ", ".join(x["login"] for x in meta["sources"]) if meta.get("sources") else src.get("login", "own")
        tracker.register_clip(con, cid, source=who, source_platform=tracker.source_platform_of(meta), fmt=meta.get("format", ""),
                              hook_style=meta.get("hook_style", ""),
                              length=float(meta.get("length") or 0), title=meta.get("title", ""),
                              path=str(clip_dir.resolve()))


def _arms(cfg: dict, dim: str, has_viewer_title: bool = False) -> list[str]:
    exp = cfg["experiments"]
    if dim == "format":
        return [f for f in exp["formats"] if f in formats.SINGLE] or ["payoff_first"]
    styles = [h for h in exp["hook_styles"] if h in writer.HOOK_STYLES]
    if not has_viewer_title:
        styles = [h for h in styles if h != "viewer_title"]
    if cfg["llm"]["provider"] == "none":
        styles = [h for h in styles if h != "ai"]
    return styles or ["curiosity"]


def pick_variant(cfg: dict, con, has_viewer_title: bool = False, rng=None) -> tuple[str, str | None]:
    """Format + hook style for the next clip, from the experiment (or fixed if experiments are off)."""
    if not cfg["experiments"].get("enabled", True):
        return "payoff_first", None
    rng = rng or random.Random()
    fmt = tracker.choose(con, cfg, "format", _arms(cfg, "format"), rng)
    style = tracker.choose(con, cfg, "hook_style", _arms(cfg, "hook_style", has_viewer_title), rng)
    return fmt, style


def _want_countdown(cfg: dict, con, n_moments: int) -> bool:
    exp = cfg["experiments"]
    if not exp.get("enabled", True) or "countdown" not in exp["formats"] or n_moments < 3:
        return False
    return tracker.want_extra(con, cfg, "countdown", _arms(cfg, "format"))


def _sfx(name: str) -> Path | None:
    for ext in (".wav", ".mp3", ".ogg"):
        p = SFX_DIR / f"{name}{ext}"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------- Tier A

def plan_challenge(cfg: dict, s: dict) -> list[dict]:
    """Pick source footage for each group of beats. Returns segments in timeline order."""
    tpl, post = cfg["template"], float(cfg["post_roll"])
    total = ff.duration(s["recording"])
    tags = s["tags"]
    by = lambda beat: [x["t"] for x in tags if x["beat"] == beat]

    results = by("result")
    if not results:
        raise SystemExit("No RESULT tag in this session - a challenge Short needs a payoff. "
                         "Use `moments` instead, or add a result tag to the .beats.json.")
    r_t = results[-1]
    problem = next((t for t in by("problem") if t < r_t), None)
    floor = problem or 0.0
    obstacle = next((t for t in reversed(by("obstacle")) if floor < t < r_t), None)
    retry = next((t for t in reversed(by("retry")) if floor < t < r_t), None)
    attempts = [t for t in by("attempt") if floor < t < (obstacle or r_t)]
    if len(attempts) > 3:
        step = (len(attempts) - 1) / 2
        attempts = [attempts[round(i * step)] for i in range(3)]

    def win(end: float, dur: float) -> tuple[float, float]:
        end = min(total, end)
        start = max(0.0, end - dur)
        return start, end - start

    segs: list[dict] = []
    # 07+08 result + end: one continuous window ending just after the payoff
    res_dur = tpl["result"] + tpl["end"]
    rs, rd = win(r_t + post, res_dur)
    tail = [{"src": (rs, rd), "beats": [("result", tpl["result"]), ("end", tpl["end"])]}]
    # 06 re-engage, 05 obstacle
    mid = []
    if obstacle is not None:
        mid.append({"src": win(obstacle + post, tpl["obstacle"]), "beats": [("obstacle", tpl["obstacle"])]})
    if retry is not None and (obstacle is None or retry > obstacle):
        mid.append({"src": win(retry + post, tpl["reengage"]), "beats": [("reengage", tpl["reengage"])]})
    # 04 build: tagged attempts, otherwise the footage leading into the next segment
    next_start = (mid[0]["src"][0] if mid else rs)
    if attempts:
        each = tpl["build"] / len(attempts)
        build = [{"src": win(t + post, each), "beats": [("build", each)]} for t in attempts]
    else:
        build = [{"src": win(next_start, tpl["build"]), "beats": [("build", tpl["build"])]}]
    # 01-03 setup: one continuous window ending at the problem tag
    setup_dur = tpl["hook"] + tpl["problem"] + tpl["challenge"]
    setup_end = (problem + post) if problem is not None else build[0]["src"][0]
    setup = [{"src": win(max(setup_end, setup_dur), setup_dur),
              "beats": [("hook", tpl["hook"]), ("problem", tpl["problem"]), ("challenge", tpl["challenge"])]}]
    segs = setup + build + mid + tail
    for seg in segs:
        seg["punch"] = any(b in PUNCH for b, _ in seg["beats"])
    return segs


def build_challenge(cfg: dict, s: dict) -> Path:
    clip_dir = Path(s["out"]) / "challenge"
    clip_dir.mkdir(parents=True, exist_ok=True)
    segs = plan_challenge(cfg, s)
    text = writer.challenge_text(cfg, s.get("challenge", ""))
    parts, beats, t = [], [], 0.0
    print(f"Challenge Short: {len(segs)} segments")
    for i, seg in enumerate(segs):
        start, dur = seg["src"]
        part = clip_dir / f"part{i:02d}.mp4"
        render.segment(cfg, s["recording"], start, dur, part, punch=seg["punch"])
        parts.append(part)
        # spread the beats of this segment over its (possibly clamped) real length
        want = sum(d for _, d in seg["beats"])
        k = dur / want if want else 1
        for key, d in seg["beats"]:
            d *= k
            if beats and beats[-1]["key"] == key:  # several build shots = one beat
                beats[-1]["end"] = round(t + d, 2)
            else:
                beats.append({"key": key, "start": round(t, 2), "end": round(t + d, 2),
                              "text": text[key], "source_at": round(start, 2)})
            t += d
    render.concat(parts, clip_dir / "rough.mp4")
    for p in parts:
        p.unlink()
    meta = {"kind": "challenge", "format": "challenge8", "hook_style": "challenge",
            "length": round(t, 2), "challenge": s.get("challenge", ""), "title": text["title"],
            "description": text["description"], "hashtags": cfg["hashtags"], "beats": beats}
    _save_script(clip_dir, meta)
    _write_vo_script(clip_dir, beats)
    missing = [b for b in BEATS if b not in {x["key"] for x in beats}]
    if missing:
        print(f"  no footage tagged for: {', '.join(missing)} (those beats are skipped)")
    print(f"  -> {clip_dir / 'rough.mp4'}  ({t:.0f}s)")
    return clip_dir


def _write_vo_script(clip_dir: Path, beats: list[dict]) -> None:
    lines = ["VOICEOVER SCRIPT - read each line inside its time window.",
             "Record one take from 0:00 (start talking when the video starts), save it as vo.wav",
             "in this folder, then run `python -m clipfarm finish` on this folder.", ""]
    for b in beats:
        lines.append(f"{b['start']:5.1f}s - {b['end']:5.1f}s  {b['key'].upper():<10} {b['text']}")
    (clip_dir / "vo_script.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Tier B

def build_moments(cfg: dict, s: dict) -> list[Path]:
    m = cfg["moments"]
    total = ff.duration(s["recording"])
    # a tag is pressed just after the moment; a loudness spike IS the moment
    peaks = [(x["t"] - 0.5, f"tag:{x['beat']}") for x in s["tags"]
             if x["beat"] in ("moment", "obstacle", "result")]
    picked: list[tuple[float, str]] = []
    for p, why in peaks:
        if all(abs(p - q) >= m["min_gap"] for q, _ in picked):
            picked.append((p, why))
    if m["auto"] and len(picked) < m["max"]:
        for p in detect.spikes(s["recording"], m["max"] - len(picked), m["min_gap"],
                               skip_near=[q for q, _ in picked]):
            picked.append((p, "auto:loud"))
    picked = sorted(picked[: m["max"]])
    out = []
    print(f"Moment clips: {len(picked)}")
    with db() as con:
        for i, (p, why) in enumerate(picked, 1):
            clip_dir = Path(s["out"]) / "moments" / f"{i:02d}"
            fmt, style = pick_variant(cfg, con)
            text = writer.moment_text(cfg, s.get("challenge", ""), i, hook_style=style)
            beats = formats.render_single(cfg, fmt, s["recording"], p, total, clip_dir, text["hook"])
            meta = {"kind": "moment", "format": fmt, "hook_style": text["hook_style"],
                    "length": round(ff.duration(clip_dir / "rough.mp4"), 2),
                    "found_by": why, "source_at": round(p, 2),
                    "title": text["title"], "description": text["description"],
                    "hashtags": cfg["hashtags"], "beats": beats}
            _save_script(clip_dir, meta)
            print(f"  {i:02d}  at {p:7.1f}s  {fmt:<12} {text['hook_style']:<12} ({why})")
            out.append(clip_dir)
        if _want_countdown(cfg, con, len(picked)):
            items = [(s["recording"], p, total) for p, _ in picked[:3]]
            out.append(_countdown(cfg, Path(s["out"]), items, "Top 3 moments this session",
                                  {"hashtags": cfg["hashtags"]}))
    return out


def _countdown(cfg: dict, session_out: Path, items, hook: str, extra: dict, facecam=None) -> Path:
    clip_dir = session_out / "countdown"
    beats = formats.render_countdown(cfg, items, clip_dir, hook, facecam=facecam)
    title = hook if len(hook) <= 90 else hook[:87] + "..."
    meta = {"kind": "moment", "format": "countdown", "hook_style": "countdown",
            "length": round(ff.duration(clip_dir / "rough.mp4"), 2),
            "title": title, "description": "The best moments, ranked.", **extra, "beats": beats}
    _save_script(clip_dir, meta)
    print(f"  countdown of the top {min(3, len(items))} -> {clip_dir}")
    return clip_dir


def render_moment(cfg: dict, src: str | Path, peak: float, total: float, clip_dir: Path,
                  length: float | None = None, facecam: list[float] | None = None) -> None:
    """The original payoff-first moment clip (kept for compatibility)."""
    formats.render_single(cfg, "payoff_first", src, peak, total, clip_dir, "", length, facecam)


# ---------------------------------------------------------------- Twitch

def build_twitch(cfg: dict, src: dict, vod_id: str | None = None, chat_file: str | None = None,
                 max_n: int | None = None, helix=None, rng=None) -> tuple[Path, list[Path]]:
    """Best moments of an approved streamer's VOD -> clips in the formats under test,
    with credit + campaign hashtags."""
    from . import twitch
    m = cfg["moments"]
    rules = src.get("rules") or {}
    h = helix or twitch.Helix()
    uid = h.user_id(src["login"])
    if vod_id:
        vod = h.vod(vod_id)
    else:
        vods = h.vods(uid, 1)
        if not vods:
            raise SystemExit(f"{src['login']} has no saved VODs (VODs may be turned off on the channel)")
        vod = vods[0]
    vod_len = twitch.parse_duration(vod["duration"])
    session_out = ROOT / "out" / f"{src['name']}-{vod['id']}"
    session_out.mkdir(parents=True, exist_ok=True)
    print(f"{src['name']}: VOD {vod['id']} \"{vod.get('title', '')}\" ({vod['duration']})")

    clips = h.clips(uid, vod["created_at"], twitch.vod_end_iso(vod))
    clip_ms = twitch.moments_from_clips(clips, vod["id"])
    print(f"  viewer clips: {len(clips)} -> {len(clip_ms)} distinct moments")
    chat_path = chat_file or twitch.download_chat(vod["id"], session_out / "chat.json")
    chat_ms = twitch.moments_from_chat(twitch.load_chat(chat_path)) if chat_path else []
    print(f"  chat spikes: {len(chat_ms)}" + ("" if chat_path else " (no chat replay - install TwitchDownloaderCLI)"))
    picked = twitch.combine(clip_ms, chat_ms, m["min_gap"], max_n or m["max"])
    if not picked:
        raise SystemExit("  no moments found: nobody clipped this VOD and there's no chat replay")

    hf = float(m["hook_flash"])
    length = float(m["length"])
    length = max(float(rules.get("min_length", 0)) - hf, length)
    length = min(float(rules.get("max_length", 600)) - hf, length)
    credit = rules.get("credit") or f"@{src['login']}"
    tags = rules.get("hashtags") or cfg["hashtags"]
    facecam = src.get("facecam")
    source_meta = lambda peak: {
        "platform": "twitch", "login": src["login"], "vod": vod["id"],
        "url": f"https://www.twitch.tv/videos/{vod['id']}?t={int(peak // 60)}m{int(peak % 60)}s",
        "permission": (src.get("permission") or {}).get("type")}
    common = {"credit": credit, "layout": "split" if facecam else "single", "hashtags": tags}

    out, pieces = [], []
    with db() as con:
        for i, mo in enumerate(picked, 1):
            sec_start = max(0.0, mo.peak + 2.0 - length - 3.0)
            sec_end = min(vod_len, mo.peak + 5.0) if vod_len else mo.peak + 5.0
            piece = session_out / "sources" / f"{i:02d}.mp4"
            piece.parent.mkdir(exist_ok=True)
            try:
                twitch.download_section(vod["id"], sec_start, sec_end, piece)
            except RuntimeError as e:
                print(f"  {i:02d}  skipped: {e}")
                continue
            local_peak, total = mo.peak - sec_start, ff.duration(piece)
            pieces.append((mo, piece, local_peak, total))
            titles = [t for t in mo.clip_titles if t.strip() and t.strip() != vod.get("title", "").strip()]
            has_title = any(8 <= len(t.strip()) <= 60 for t in titles)
            fmt, style = pick_variant(cfg, con, has_title, rng)
            text = writer.moment_text(cfg, vod.get("title", ""), i, context=titles,
                                      streamer=src["login"], hook_style=style)
            clip_dir = session_out / "moments" / f"{i:02d}"
            beats = formats.render_single(cfg, fmt, piece, local_peak, total, clip_dir, text["hook"],
                                          length=length, facecam=facecam)
            meta = {"kind": "moment", "format": fmt, "hook_style": text["hook_style"],
                    "length": round(ff.duration(clip_dir / "rough.mp4"), 2),
                    "found_by": "; ".join(mo.reasons), "source_at": round(mo.peak, 2),
                    "source": source_meta(mo.peak), **common,
                    "title": text["title"], "description": text["description"], "beats": beats}
            _save_script(clip_dir, meta)
            print(f"  {i:02d}  {mo.peak / 60:6.1f} min  {fmt:<12} {text['hook_style']:<12} "
                  f"score {mo.score:4.1f} ({'; '.join(mo.reasons)})")
            out.append(clip_dir)
        if _want_countdown(cfg, con, len(pieces)):
            best = sorted(pieces, key=lambda x: -x[0].score)[:3]
            items = [(p, lp, t) for _, p, lp, t in best]
            out.append(_countdown(cfg, session_out, items, f"Top 3 moments from {src['login']}'s stream",
                                  {**common, "source": source_meta(best[0][0].peak)}, facecam=facecam))
    return session_out, out


# ---------------------------------------------------------------- YouTube

def build_youtube(cfg: dict, src: dict, video: str | None = None, max_n: int | None = None,
                  rng=None) -> tuple[Path, list[Path]]:
    """Best moments of an approved YouTube streamer's latest (or given) past livestream."""
    from . import detect, twitch, ytlive
    m, yc = cfg["moments"], cfg.get("youtube_live") or {}
    rules = src.get("rules") or {}
    if video:
        vid = ytlive.video_id(video)
        if not vid:
            raise SystemExit(f"'{video}' is not a YouTube video link or id")
    else:
        streams = ytlive.recent_streams(cfg, src["login"], 1)
        if not streams:
            raise SystemExit(f"{src['login']} has no finished livestreams on their Live tab")
        vid = streams[0]["id"]
    inf = ytlive.info(cfg, vid)
    session_out = ROOT / "out" / f"{src['name']}-yt-{vid}"
    session_out.mkdir(parents=True, exist_ok=True)
    print(f"{src['name']} (YouTube): \"{inf['title']}\" ({inf['duration'] / 3600:.1f}h)")
    chat = ytlive.chat(cfg, vid, session_out) if yc.get("use_chat", True) else None
    chat_ms = twitch.moments_from_chat(chat) if chat else []
    heat_ms = ytlive.moments_from_heatmap(inf["heatmap"]) if yc.get("use_heatmap", True) else []
    print(f"  chat spikes: {len(chat_ms)}" + ("" if chat else " (no chat replay)")
          + f" | most-replayed peaks: {len(heat_ms)}" + ("" if inf["heatmap"] else " (no heatmap yet)"))
    spans = {round(h.peak, 2): h.span for h in heat_ms}
    picked = twitch.combine(chat_ms, heat_ms, m["min_gap"], max_n or m["max"])
    if not picked:
        raise SystemExit("  no moments found: no chat replay and no most-replayed data for this stream yet "
                         "(the heatmap appears once a video has enough views - try again later)")
    hf = float(m["hook_flash"])
    length = float(m["length"])
    length = max(float(rules.get("min_length", 0)) - hf, length)
    length = min(float(rules.get("max_length", 600)) - hf, length)
    credit = rules.get("credit") or f"@{src['login'].lstrip('@')}"
    tags = rules.get("hashtags") or cfg["hashtags"]
    facecam = src.get("facecam")
    common = {"credit": credit, "layout": "split" if facecam else "single", "hashtags": tags}
    source_meta = lambda peak: {"platform": "youtube", "login": src["login"], "vod": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}&t={int(peak)}s",
                                "permission": (src.get("permission") or {}).get("type")}
    dur = inf["duration"] or 10 ** 9
    out, pieces = [], []
    with db() as con:
        for i, mo in enumerate(picked, 1):
            span = spans.get(round(mo.peak, 2)) if not any("chat" in r for r in mo.reasons) else None
            if span:  # coarse most-replayed stretch: grab all of it, then find its loudest point
                sec_start = max(0.0, span[0] - length)
                sec_end = min(dur, span[1] + 5.0)
            else:
                sec_start = max(0.0, mo.peak + 2.0 - length - 3.0)
                sec_end = min(dur, mo.peak + 5.0)
            piece = session_out / "sources" / f"{i:02d}.mp4"
            piece.parent.mkdir(exist_ok=True)
            try:
                ytlive.download_section(cfg, vid, sec_start, sec_end, piece)
            except RuntimeError as e:
                print(f"  {i:02d}  skipped: {str(e)[:200]}")
                continue
            total = ff.duration(piece)
            local_peak = mo.peak - sec_start
            if span:
                lo, hi = span[0] - sec_start, span[1] - sec_start
                loud = [t for t in detect.spikes(str(piece), 3, 5.0) if lo - 2 <= t <= hi + 2]
                if loud:
                    local_peak = loud[0]
                    mo.reasons.append("loudest point in it")
            local_peak = min(max(0.5, local_peak), max(0.5, total - 0.5))
            abs_peak = sec_start + local_peak
            pieces.append((mo, piece, local_peak, total))
            fmt, style = pick_variant(cfg, con, False, rng)
            text = writer.moment_text(cfg, inf["title"], i, streamer=src["login"].lstrip("@"), hook_style=style)
            clip_dir = session_out / "moments" / f"{i:02d}"
            beats = formats.render_single(cfg, fmt, piece, local_peak, total, clip_dir, text["hook"],
                                          length=length, facecam=facecam)
            meta = {"kind": "moment", "format": fmt, "hook_style": text["hook_style"],
                    "length": round(ff.duration(clip_dir / "rough.mp4"), 2),
                    "found_by": "; ".join(mo.reasons), "source_at": round(abs_peak, 2),
                    "source": source_meta(abs_peak), **common,
                    "title": text["title"], "description": text["description"], "beats": beats}
            _save_script(clip_dir, meta)
            print(f"  {i:02d}  {abs_peak / 60:6.1f} min  {fmt:<12} {text['hook_style']:<12} ({'; '.join(mo.reasons)})")
            out.append(clip_dir)
        if _want_countdown(cfg, con, len(pieces)):
            best = sorted(pieces, key=lambda x: -x[0].score)[:3]
            out.append(_countdown(cfg, session_out, [(p, lp, t) for _, p, lp, t in best],
                                  f"Top 3 moments from {src['login'].lstrip('@')}'s stream",
                                  {**common, "source": source_meta(best[0][0].peak)}, facecam=facecam))
    return session_out, out


# ---------------------------------------------------------------- Kick

def build_kick(cfg: dict, src: dict, hours: float | None = None, max_n: int | None = None,
               get=None, rng=None, now=None) -> tuple[Path, list[Path]]:
    """Most-viewed recent viewer clips of an approved Kick streamer -> clips in the formats under
    test. GTA categories only; gambling categories always skipped."""
    from datetime import datetime, timezone

    from . import kick
    m, kc = cfg["moments"], cfg.get("kick") or {}
    rules = src.get("rules") or {}
    hours = float(hours or kc.get("hours", 48))
    wanted = rules.get("categories", kc.get("categories", []))
    blocked = list(kc.get("block_categories", [])) + list(rules.get("block_categories", []))
    all_clips = kick.clips(src["login"], hours=hours, get=get, now=now)
    usable = [c for c in all_clips if kick.allowed(c, wanted, blocked)]
    skipped = len(all_clips) - len(usable)
    print(f"{src['name']} (Kick): {len(all_clips)} clips in the last {hours:.0f}h, {len(usable)} usable"
          + (f" ({skipped} outside {wanted or 'any'} or in a blocked category)" if skipped else ""))
    picked = kick.moments(usable, max_n or m["max"])
    if not picked:
        raise SystemExit("  no usable clips: nobody clipped a GTA moment in that window "
                         "(try --hours 168, or check the streamer's categories)")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    session_out = ROOT / "out" / f"{src['name']}-kick-{stamp}"
    session_out.mkdir(parents=True, exist_ok=True)
    hf = float(m["hook_flash"])
    length = float(m["length"])
    length = max(float(rules.get("min_length", 0)) - hf, length)
    length = min(float(rules.get("max_length", 600)) - hf, length)
    credit = rules.get("credit") or f"@{src['login']}"
    tags = rules.get("hashtags") or cfg["hashtags"]
    facecam = src.get("facecam")
    common = {"credit": credit, "layout": "split" if facecam else "single", "hashtags": tags}
    source_meta = lambda c: {"platform": "kick", "login": src["login"], "clip": c["id"], "url": c["page"],
                             "category": c["category"], "permission": (src.get("permission") or {}).get("type")}
    out, pieces = [], []
    with db() as con:
        for i, c in enumerate(picked, 1):
            piece = session_out / "sources" / f"{i:02d}.mp4"
            piece.parent.mkdir(exist_ok=True)
            try:
                kick.download(c, piece)
            except RuntimeError as e:
                print(f"  {i:02d}  skipped: {str(e)[:200]}")
                continue
            total = ff.duration(piece)
            peak = max(0.5, total - 4.0)  # people clip right after the moment: the payoff is near the end
            pieces.append((c, piece, peak, total))
            titles = c["titles"]
            has_title = any(8 <= len(t) <= 60 for t in titles)
            fmt, style = pick_variant(cfg, con, has_title, rng)
            text = writer.moment_text(cfg, c["category"] or "GTA", i, context=titles,
                                      streamer=src["login"], hook_style=style)
            clip_dir = session_out / "moments" / f"{i:02d}"
            beats = formats.render_single(cfg, fmt, piece, peak, total, clip_dir, text["hook"],
                                          length=length, facecam=facecam)
            meta = {"kind": "moment", "format": fmt, "hook_style": text["hook_style"],
                    "length": round(ff.duration(clip_dir / "rough.mp4"), 2),
                    "found_by": "; ".join(c["reasons"]), "source_at": 0,
                    "source": source_meta(c), **common,
                    "title": text["title"], "description": text["description"], "beats": beats}
            _save_script(clip_dir, meta)
            print(f"  {i:02d}  {fmt:<12} {text['hook_style']:<12} score {c['score']:4.1f} ({c['reasons'][0]})")
            out.append(clip_dir)
        if _want_countdown(cfg, con, len(pieces)):
            best = sorted(pieces, key=lambda x: -x[0]["score"])[:3]
            out.append(_countdown(cfg, session_out, [(p, pk, t) for _, p, pk, t in best],
                                  f"Top 3 moments from {src['login']}'s stream",
                                  {**common, "source": source_meta(best[0][0])}, facecam=facecam))
    return session_out, out


# ---------------------------------------------------------------- multi-streamer reactions

def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "reaction"


def find_react(cfg: dict, keywords: list[str], names: list[str], helix=None, vods_back: int = 5,
               plan_dir: Path | None = None, kick_get=None) -> Path:
    """Search approved streamers' recent viewer clips for the keywords; draft a reaction plan."""
    from . import sources, twitch
    h = helix
    words = [k.lower() for k in keywords]
    lines, found = [], 0
    for name in names:
        src = sources.get(name)  # refuses streamers without permission
        if src.get("platform") == "youtube":  # YouTube has no searchable clips
            lines.append(f"  # - source: {src['name']}   # YouTube: add vod: <video link>&t=<time> by hand")
            continue
        if src.get("platform") == "kick":
            from . import kick
            kc = cfg.get("kick") or {}
            ks = [c for c in kick.clips(src["login"], hours=24 * 2 * vods_back, get=kick_get)
                  if kick.allowed(c, (src.get("rules") or {}).get("categories", kc.get("categories", [])),
                                  kc.get("block_categories", []))
                  and any(w in c["title"].lower() for w in words)]
            ks.sort(key=lambda c: -c["views"])
            if not ks:
                lines.append(f"  # - source: {src['name']}   # no Kick clip matched {keywords} - add clip: <link> by hand")
                continue
            found += 1
            lines.append(f"  - source: {src['name']}\n    clip: {ks[0]['page']}   # \"{ks[0]['title']}\" ({ks[0]['views']} views)")
            for c in ks[1:3]:
                lines.append(f"    # other match: {c['page']}  \"{c['title']}\" ({c['views']} views)")
            continue
        h = h or twitch.Helix()
        uid = h.user_id(src["login"])
        hits = []
        for vod in h.vods(uid, vods_back):
            for c in h.clips(uid, vod["created_at"], twitch.vod_end_iso(vod)):
                title = (c.get("title") or "").lower()
                if c.get("vod_offset") is None or str(c.get("video_id")) != str(vod["id"]):
                    continue
                if any(w in title for w in words):
                    peak = float(c["vod_offset"]) + max(1.0, float(c.get("duration", 30)) - 4.0)
                    hits.append((int(c.get("view_count", 0)), vod["id"], peak, c.get("title", "")))
        hits.sort(reverse=True)
        if not hits:
            lines.append(f"  # - source: {src['name']}   # no viewer clip matched {keywords} - add vod + at by hand")
            continue
        found += 1
        views, vod_id, peak, title = hits[0]
        t = f"{int(peak // 3600)}h{int(peak % 3600 // 60)}m{int(peak % 60)}s"
        lines.append(f"  - source: {src['name']}\n    vod: https://www.twitch.tv/videos/{vod_id}?t={t}"
                     f"   # \"{title}\" ({views} views)")
        for v2, vid2, p2, t2 in hits[1:3]:
            lines.append(f"    # other match: https://www.twitch.tv/videos/{vid2}?t={int(p2)}s  \"{t2}\" ({v2} views)")
    moment = " ".join(keywords)
    plan_dir = plan_dir or (ROOT / "plans")
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{_slug(moment)}.yaml"
    path.write_text(
        f"# Multi-streamer reaction plan. Check each timestamp: it should be the instant the moment\n"
        f"# happens (open the link). Order = play order: put the strongest reaction LAST.\n"
        f"# Then run:  python -m clipfarm react build {path.as_posix()}\n"
        f"title: \"{len(names)} streamers react to {moment}\"\n"
        f"moment: \"{moment}\"\n"
        f"layout: sequence      # sequence | grid (grid needs a facecam box for every streamer)\n"
        f"align: true           # fine-tune sync from shared game audio when it matches\n"
        f"entries:\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"Matched {found} of {len(names)} streamers. Plan: {path}")
    return path


def build_react(cfg: dict, plan_path: str | Path, sources_file=None) -> Path:
    """Build a multi-streamer reaction clip from a plan. Every streamer must be approved."""
    from . import react, sources, twitch
    plan = react.load_plan(plan_path, lambda n: sources.get(n, sources_file))
    entries = plan["entries"]
    n = len(entries)
    grid = plan["layout"] == "grid"
    if grid and not all(e["source"].get("facecam") for e in entries):
        missing = [e["source"]["name"] for e in entries if not e["source"].get("facecam")]
        print(f"  grid needs a facecam box for {', '.join(missing)} - using sequence instead")
        grid = False
    session_out = ROOT / "out" / f"react-{_slug(plan.get('title') or plan['moment'])}"
    pre_dl, post_dl = 14.0, 10.0
    items = []
    for i, e in enumerate(entries):
        piece = session_out / "sources" / f"{i:02d}-{e['source']['name']}.mp4"
        piece.parent.mkdir(parents=True, exist_ok=True)
        if e.get("clip"):
            from . import kick
            kick.download(e["clip"], piece)
            t = e["at"] if e["at"] is not None else max(0.5, ff.duration(piece) - 4.0)
            items.append({"file": piece, "t": t, "label": e["label"], "facecam": e["source"].get("facecam"),
                          "src": e["source"], "vod": None, "clip": e["clip"], "at": t})
            continue
        start = max(0.0, e["at"] - pre_dl)
        if e["source"].get("platform") == "youtube":
            from . import ytlive
            ytlive.download_section(cfg, e["vod"], start, e["at"] + post_dl, piece)
        else:
            twitch.download_section(e["vod"], start, e["at"] + post_dl, piece)
        items.append({"file": piece, "t": e["at"] - start, "label": e["label"],
                      "facecam": e["source"].get("facecam"), "src": e["source"], "vod": e["vod"], "at": e["at"]})
    if plan.get("align"):
        synced = react.align([(it["file"], it["t"]) for it in items])
        for it, (t, conf) in zip(items, synced):
            moved = t - it["t"]
            it["t"], it["sync"] = t, conf
            if conf is not None:
                state = f"shifted {moved:+.2f}s" if abs(moved) > 0.01 else "kept"
                print(f"  sync {it['src']['name']}: {state} (match {conf:.2f})")
    rules = [e["source"].get("rules") or {} for e in entries]
    hook = plan.get("hook") or plan.get("title") or f"{n} streamers react to {plan['moment']}"
    clip_dir = session_out / "react"
    labels = []
    if grid:
        beats, labels = react.render_grid(cfg, items, clip_dir, hook)
    else:
        max_len = min([float(r["max_length"]) for r in rules if r.get("max_length")] or [0]) or None
        beats = react.render_sequence(cfg, items, clip_dir, hook, plan.get("setup_text") or plan["moment"],
                                      max_length=max_len)
    # every streamer is credited; hashtags merged; platforms limited to what ALL campaigns allow
    credit = " · ".join(e["label"] for e in entries)
    tags: list[str] = []
    for r in rules:
        for t in r.get("hashtags") or []:
            if t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
    tags = (tags or list(cfg["hashtags"]))[:8]
    meta = {"kind": "moment", "format": "multi_react", "hook_style": "react",
            "length": round(ff.duration(clip_dir / "rough.mp4"), 2),
            "title": hook[:90], "description": f"{n} streamers, one moment: {plan['moment']}.",
            "credit": credit if len(credit) <= 60 and not grid else "",
            "credits_all": credit, "layout": "split" if any(it["facecam"] for it in items) else "single",
            "labels": [list(x) for x in labels], "hashtags": tags,
            "sources": [{"platform": it["src"].get("platform", "twitch"), "login": it["src"]["login"],
                         "vod": it["vod"],
                         "url": it.get("clip") or (
                             f"https://www.youtube.com/watch?v={it['vod']}&t={int(it['at'])}s"
                             if it["src"].get("platform") == "youtube" else
                             f"https://www.twitch.tv/videos/{it['vod']}?t={int(it['at'])}s"),
                         "permission": (it["src"].get("permission") or {}).get("type"),
                         "sync": it.get("sync")} for it in items],
            "beats": beats}
    _save_script(clip_dir, meta)
    print(f"  {'grid' if grid else 'sequence'} of {n} streamers -> {clip_dir}  ({meta['length']:.0f}s)")
    return clip_dir


# ---------------------------------------------------------------- finish

def finish(cfg: dict, clip_dir: Path, vo: str | None = None, use_captions: bool | None = None) -> Path:
    clip_dir = Path(clip_dir)
    meta = yaml.safe_load((clip_dir / "script.yaml").read_text(encoding="utf-8"))
    vo_path = Path(vo) if vo else next((clip_dir / f"vo{e}" for e in (".wav", ".mp3", ".m4a")
                                         if (clip_dir / f"vo{e}").exists()), None)
    words = []
    if use_captions if use_captions is not None else cfg["captions"]["enabled"]:
        src = vo_path or clip_dir / "rough.mp4"
        cache = clip_dir / ("words_vo.json" if vo_path else "words.json")
        words = captions.transcribe(src, cfg["captions"]["model"], cache, cfg)
    beats = []
    for b in meta["beats"]:
        hold = TEXT_SECS.get(b["key"]) if meta["kind"] == "challenge" else None
        end = min(b["end"], b["start"] + hold) if hold else b["end"]
        beats.append({**b, "end": end})
    captions.write_ass(cfg, clip_dir / "overlay.ass", beats, words,
                       credit=meta.get("credit", ""), duration=ff.duration(clip_dir / "rough.mp4"),
                       split=meta.get("layout") == "split",
                       labels=[tuple(x) for x in meta.get("labels") or []])
    sfx = []
    for b in meta["beats"]:
        p = _sfx(b["key"])
        if p:
            sfx.append((p, b["start"]))
    if meta.get("format", "payoff_first") == "payoff_first" and meta["kind"] == "moment" and _sfx("rewind"):
        sfx.append((_sfx("rewind"), float(cfg["moments"]["hook_flash"])))
    out = render.finish(cfg, clip_dir, "overlay.ass", vo_path, sfx)
    print(f"  done -> {out}" + ("  (with voiceover)" if vo_path else ""))
    return out


# ---------------------------------------------------------------- post kit

def _platform_text(meta: dict) -> dict:
    tags = meta.get("hashtags") or []
    title = meta["title"].strip()
    desc = meta.get("description", "").strip()
    yt_tags = tags if "#shorts" in [t.lower() for t in tags] else tags + ["#shorts"]
    credit = (meta.get("credits_all") or meta.get("credit", "")).strip()
    cr = f"\n\nClip from {credit}" if credit else ""
    crs = f" | {credit}" if credit else ""
    return {
        "youtube_title": f"{title[:100 - len(crs)]}{crs}" if credit else title[:100],
        "youtube_description": f"{desc}{cr}\n\n{' '.join(yt_tags)}",
        "tiktok": f"{title}{crs} {' '.join(tags[:5])}"[:2200],
        "instagram": f"{title}\n\n{desc}{cr}\n\n{' '.join(tags)}",
        "facebook": f"{title}\n\n{desc}{cr}",
        "x": f"{title}{crs} {' '.join(tags[:2])}"[:280],
    }


def kit(session_out: Path) -> Path:
    session_out = Path(session_out)
    post = session_out / "post"
    post.mkdir(exist_ok=True)
    items = []
    clips = (sorted(session_out.glob("challenge/final.mp4")) + sorted(session_out.glob("countdown/final.mp4"))
             + sorted(session_out.glob("react/final.mp4"))
             + sorted(session_out.glob("moments/*/final.mp4")))
    for n, final in enumerate(clips, 1):
        meta = yaml.safe_load((final.parent / "script.yaml").read_text(encoding="utf-8"))
        name = f"{n:02d}-{meta['kind']}.mp4"
        shutil.copy2(final, post / name)
        items.append({"file": name, "kind": meta["kind"], "clip_id": meta.get("clip_id", ""),
                      "format": meta.get("format", ""), "hook_style": meta.get("hook_style", ""),
                      **_platform_text(meta)})
    (post / "posts.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    (post / "review.html").write_text(_review_html(session_out.name, items), encoding="utf-8")
    print(f"Post kit: {len(items)} clips -> {post / 'review.html'}")
    return post


def _review_html(name: str, items: list[dict]) -> str:
    fields = [("youtube_title", "YouTube title"), ("youtube_description", "YouTube description"),
              ("tiktok", "TikTok caption"), ("instagram", "Instagram / Reels"),
              ("facebook", "Facebook"), ("x", "X")]
    cards = []
    for it in items:
        rows = "".join(
            f'<div class="f"><div class="h"><span>{label}</span>'
            f'<button onclick="cp(this)">Copy</button></div><pre>{html.escape(it[k])}</pre></div>'
            for k, label in fields)
        cards.append(
            f'<section data-id="{html.escape(name)}/{it["file"]}"><video src="{it["file"]}" controls '
            f'preload="metadata" playsinline></video><div class="m"><div class="t"><b>{it["file"]}</b>'
            f'<label><input type="checkbox" onchange="mark(this)"> posted</label></div>{rows}</div></section>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review</title><style>
:root{{--bg:#111318;--card:#1a1d24;--line:#2b2f38;--ink:#e8eaee;--mute:#9aa0aa;--acc:#3fb6a0}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1100px;margin:auto;padding:24px 16px}} h1{{font-size:20px;margin:0 0 4px}} p{{color:var(--mute);margin:0 0 20px}}
section{{display:grid;grid-template-columns:240px 1fr;gap:18px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}}
section.done{{opacity:.45}} video{{width:240px;aspect-ratio:9/16;background:#000;border-radius:6px}}
.t{{display:flex;justify-content:space-between;margin-bottom:8px}} .f{{border-top:1px solid var(--line);padding:6px 0}}
.h{{display:flex;justify-content:space-between;align-items:center;color:var(--mute);font-size:12px}}
pre{{white-space:pre-wrap;margin:4px 0 0;font:13px/1.4 system-ui,sans-serif}}
button{{background:var(--acc);color:#06201b;border:0;border-radius:5px;padding:3px 10px;font-weight:600;cursor:pointer}}
@media(max-width:640px){{section{{grid-template-columns:1fr}} video{{width:100%}}}}
</style></head><body><main><h1>{html.escape(name)}: {len(items)} clips</h1>
<p>Watch, delete the ones you don't want, copy the caption for each platform, tick posted.</p>
{''.join(cards)}</main><script>
function cp(b){{navigator.clipboard.writeText(b.closest('.f').querySelector('pre').textContent);b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1200)}}
function key(s){{return 'posted:'+s.dataset.id}}
function mark(c){{const s=c.closest('section');s.classList.toggle('done',c.checked);try{{localStorage.setItem(key(s),c.checked?'1':'')}}catch(e){{}}}}
document.querySelectorAll('section').forEach(s=>{{let v='';try{{v=localStorage.getItem(key(s))}}catch(e){{}}if(v){{s.classList.add('done');s.querySelector('input').checked=true}}}});
</script></body></html>"""
