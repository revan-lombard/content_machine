"""Thin ffmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def require() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found. Install ffmpeg and make sure it is on your PATH "
                             "(Windows: `winget install Gyan.FFmpeg`, then open a new terminal).")


def run(args: list[str], cwd: str | Path | None = None) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{' '.join(cmd)}\n{res.stderr.strip()[-2000:]}")


def probe(path: str | Path) -> dict:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def duration(path: str | Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_audio(path: str | Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path)["streams"])


def video_size(path: str | Path) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise ValueError(f"no video stream in {path}")
