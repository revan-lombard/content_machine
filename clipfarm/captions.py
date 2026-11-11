"""Spoken-word captions (faster-whisper) and the .ass overlay file with beat text."""
from __future__ import annotations

import json
from pathlib import Path


def transcribe(audio_path: Path, model_name: str, cache: Path) -> list[dict]:
    """Word timings: [{"w": "word", "s": start, "e": end}, ...]. Cached next to the clip."""
    if cache.exists() and cache.stat().st_mtime >= audio_path.stat().st_mtime:
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  captions skipped: faster-whisper not installed (pip install -r requirements.txt)")
        return []
    try:
        model = WhisperModel(model_name, device="auto", compute_type="int8")
        segs, _ = model.transcribe(str(audio_path), word_timestamps=True, vad_filter=True)
        words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                 for seg in segs for w in (seg.words or []) if w.word.strip()]
    except Exception as e:  # model download blocked, bad audio, ...
        print(f"  captions skipped: {type(e).__name__}: {str(e)[:200]}")
        return []
    cache.write_text(json.dumps(words), encoding="utf-8")
    return words


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def write_ass(cfg: dict, path: Path, beats: list[dict], words: list[dict]) -> None:
    v, t = cfg["video"], cfg["text"]
    W, H = v["width"], v["height"]
    font = t["font"]
    # colours are &HAABBGGRR
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Beat,{font},{t['beat_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,8,70,70,{int(H * 0.12)},1
Style: Cap,{font},{t['caption_size']},&H0000E5FF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,2,80,80,{int(H * 0.22)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for b in beats:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        # pop in: start at 115% and settle to 100% in 120ms
        pop = r"{\fscx115\fscy115\t(0,120,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 1,{_ts(b['start'])},{_ts(b['end'])},Beat,,0,0,0,,{pop}{_esc(text.upper())}")

    n = max(1, int(cfg["captions"]["words_per_line"]))
    for i in range(0, len(words), n):
        chunk = words[i:i + n]
        start = chunk[0]["s"]
        end = words[i + n]["s"] if i + n < len(words) else chunk[-1]["e"] + 0.3
        end = min(end, chunk[-1]["e"] + 0.6)
        text = " ".join(w["w"] for w in chunk).upper()
        lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{_esc(text)}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
