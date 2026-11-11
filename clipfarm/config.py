"""Load config.yaml on top of built-in defaults."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

BEATS = ["hook", "problem", "challenge", "build", "obstacle", "reengage", "result", "end"]

DEFAULTS: dict = {
    "template": {"hook": 3, "problem": 5, "challenge": 4, "build": 18,
                 "obstacle": 10, "reengage": 8, "result": 9, "end": 3},
    "hotkeys": {"problem": "f5", "attempt": "f6", "obstacle": "f7", "retry": "f8",
                "result": "f9", "moment": "f10", "undo": "f11", "record": "f12"},
    "post_roll": 1.5,
    "obs": {"use_websocket": True, "host": "localhost", "port": 4455, "password": ""},
    "video": {"width": 1080, "height": 1920, "fps": 30, "mode": "blur", "fg_aspect": 1.25,
              "crop_x": 0.5, "crf": 20, "preset": "veryfast", "punch_in": True},
    "text": {"font": "Arial Black", "beat_size": 84, "caption_size": 70},
    "captions": {"enabled": True, "model": "small", "words_per_line": 3},
    "audio": {"game_volume": 1.0, "game_volume_under_vo": 0.3},
    "moments": {"length": 18, "hook_flash": 1.5, "max": 10, "min_gap": 20, "auto": True},
    "llm": {"provider": "none", "model": "llama3.1", "url": "http://localhost:11434"},
    "hashtags": ["#gta6", "#gta", "#gtavi", "#gaming", "#shorts"],
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if p.exists():
        user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return _merge(DEFAULTS, user)
