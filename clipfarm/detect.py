"""Find clip-worthy moments you didn't tag: loudness spikes (explosions, crashes, yelling)."""
from __future__ import annotations

import subprocess

import numpy as np

RATE = 8000
WIN = 0.5  # seconds per loudness window


def loudness(path: str) -> np.ndarray:
    """dB loudness per WIN-second window of the file's audio."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(RATE * WIN)
    if len(x) < n:
        return np.zeros(0)
    x = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((x ** 2).mean(axis=1)) + 1e-6
    return 20 * np.log10(rms)


def spikes(path: str, max_n: int, min_gap: float, skip_near: list[float] | None = None) -> list[float]:
    """Times (seconds) of the loudest moments relative to their surroundings."""
    db = loudness(path)
    if db.size == 0:
        return []
    # compare each window with the typical level over the surrounding ~60s
    k = max(1, int(60 / WIN))
    pad = np.pad(db, (k // 2, k // 2), mode="edge")
    base = np.array([np.median(pad[i:i + k]) for i in range(len(db))])
    score = db - base
    # a moment is a sustained spike: smooth over 1.5s
    s = max(1, int(1.5 / WIN))
    score = np.convolve(score, np.ones(s) / s, mode="same")
    order = np.argsort(score)[::-1]
    picked: list[float] = []
    taken = list(skip_near or [])
    for i in order:
        if score[i] < 6:  # less than 6 dB above its surroundings: not a moment
            break
        t = float((i + 1) * WIN)
        if all(abs(t - u) >= min_gap for u in taken):
            picked.append(round(float(t), 2))
            taken.append(t)
        if len(picked) >= max_n:
            break
    return sorted(picked)
