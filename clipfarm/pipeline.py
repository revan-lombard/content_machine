"""Turns a recording + beat tags into finished vertical clips."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

import yaml

from . import captions, detect, ff, render, writer
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


def _save_script(clip_dir: Path, meta: dict) -> None:
    (clip_dir / "script.yaml").write_text(
        "# Edit any text below, then run:  python -m clipfarm finish " + clip_dir.as_posix() + "\n"
        + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")


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
    meta = {"kind": "challenge", "challenge": s.get("challenge", ""), "title": text["title"],
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
    m, post = cfg["moments"], float(cfg["post_roll"])
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
    for i, (p, why) in enumerate(picked, 1):
        clip_dir = Path(s["out"]) / "moments" / f"{i:02d}"
        clip_dir.mkdir(parents=True, exist_ok=True)
        hf = float(m["hook_flash"])
        f_start = max(0.0, p - hf / 2)
        main_end = min(total, p + 2.0)
        main_start = max(0.0, main_end - m["length"])
        flash, main = clip_dir / "p0.mp4", clip_dir / "p1.mp4"
        render.segment(cfg, s["recording"], f_start, hf, flash, punch=True)
        render.segment(cfg, s["recording"], main_start, main_end - main_start, main)
        render.concat([flash, main], clip_dir / "rough.mp4")
        flash.unlink()
        main.unlink()
        text = writer.moment_text(cfg, s.get("challenge", ""), i)
        meta = {"kind": "moment", "found_by": why, "source_at": round(p, 2),
                "title": text["title"], "description": text["description"], "hashtags": cfg["hashtags"],
                "beats": [{"key": "hook", "start": 0.0, "end": round(hf + 2.0, 2), "text": text["hook"]}]}
        _save_script(clip_dir, meta)
        print(f"  {i:02d}  at {p:7.1f}s  ({why})  -> {clip_dir}")
        out.append(clip_dir)
    return out


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
        words = captions.transcribe(src, cfg["captions"]["model"], cache)
    beats = []
    for b in meta["beats"]:
        hold = TEXT_SECS.get(b["key"]) if meta["kind"] == "challenge" else None
        end = min(b["end"], b["start"] + hold) if hold else b["end"]
        beats.append({**b, "end": end})
    captions.write_ass(cfg, clip_dir / "overlay.ass", beats, words)
    sfx = []
    for b in meta["beats"]:
        p = _sfx(b["key"])
        if p:
            sfx.append((p, b["start"]))
    if meta["kind"] == "moment" and _sfx("rewind"):
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
    return {
        "youtube_title": title[:100],
        "youtube_description": f"{desc}\n\n{' '.join(yt_tags)}",
        "tiktok": f"{title} {' '.join(tags[:5])}"[:2200],
        "instagram": f"{title}\n\n{desc}\n\n{' '.join(tags)}",
        "facebook": f"{title}\n\n{desc}",
        "x": f"{title} {' '.join(tags[:2])}"[:280],
    }


def kit(session_out: Path) -> Path:
    session_out = Path(session_out)
    post = session_out / "post"
    post.mkdir(exist_ok=True)
    items = []
    clips = sorted(session_out.glob("challenge/final.mp4")) + sorted(session_out.glob("moments/*/final.mp4"))
    for n, final in enumerate(clips, 1):
        meta = yaml.safe_load((final.parent / "script.yaml").read_text(encoding="utf-8"))
        name = f"{n:02d}-{meta['kind']}.mp4"
        shutil.copy2(final, post / name)
        items.append({"file": name, "kind": meta["kind"], **_platform_text(meta)})
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
