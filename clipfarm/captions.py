"""Spoken-word captions and the .ass overlay file with beat text.

Two engines:
  faster-whisper - pip package; uses NVIDIA GPUs, otherwise the CPU.
  whisper-cpp    - whisper.cpp's Vulkan build; uses AMD GPUs like the RX 580.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from . import ff


def transcribe(audio_path: Path, model_name: str, cache: Path, cfg: dict | None = None) -> list[dict]:
    """Word timings: [{"w": "word", "s": start, "e": end}, ...]. Cached next to the clip."""
    if cache.exists() and cache.stat().st_mtime >= audio_path.stat().st_mtime:
        return json.loads(cache.read_text(encoding="utf-8"))
    c = (cfg or {}).get("captions", {})
    if c.get("engine") == "whisper-cpp":
        words = _whisper_cpp(audio_path, c)
        if words is not None:
            cache.write_text(json.dumps(words), encoding="utf-8")
            return words
        print("  falling back to faster-whisper on the CPU")
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


def _whisper_cpp(audio_path: Path, c: dict) -> list[dict] | None:
    exe, model = c.get("whisper_cpp_exe"), c.get("whisper_cpp_model")
    if not exe or not Path(exe).exists() or not model or not Path(model).exists():
        print("  whisper-cpp: set captions.whisper_cpp_exe and whisper_cpp_model in config.yaml")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        ff.run(["-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
        base = Path(tmp) / "out"
        # -ml 1 -sow: one word per segment, split on word boundaries; -oj: JSON output
        res = subprocess.run([exe, "-m", model, "-f", str(wav), "-ml", "1", "-sow", "-oj",
                              "-of", str(base), "-np"], capture_output=True, text=True)
        out = base.with_suffix(".json")
        if res.returncode != 0 or not out.exists():
            print(f"  whisper-cpp failed: {res.stderr[-300:]}")
            return None
        return parse_whisper_cpp(json.loads(out.read_text(encoding="utf-8")))


def parse_whisper_cpp(data: dict) -> list[dict]:
    words = []
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        off = seg.get("offsets") or {}
        if text and not (text.startswith("[") and text.endswith("]")):  # skip [MUSIC] etc.
            words.append({"w": text, "s": round(off.get("from", 0) / 1000, 2),
                          "e": round(off.get("to", 0) / 1000, 2)})
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


def write_ass(cfg: dict, path: Path, beats: list[dict], words: list[dict],
              credit: str = "", duration: float = 0.0, split: bool = False) -> None:
    v, t = cfg["video"], cfg["text"]
    W, H = v["width"], v["height"]
    font = t["font"]
    # split layout: beat text sits just under the face cam instead of over it
    beat_top = int(H * float(v.get("facecam_height", 0.38))) + 30 if split else int(H * 0.12)
    # colours are &HAABBGGRR
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Beat,{font},{t['beat_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,8,70,70,{beat_top},1
Style: Credit,{font},46,&H30FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,1,2,60,60,{int(H * 0.07)},1
Style: Cap,{font},{t['caption_size']},&H0000E5FF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,2,80,80,{int(H * 0.22)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if credit and duration > 0:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{_esc(credit)}")
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
