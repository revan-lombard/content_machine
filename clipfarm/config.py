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
              "crop_x": 0.5, "crf": 20, "preset": "veryfast", "punch_in": True,
              "encoder": "libx264", "facecam_height": 0.38},
    "text": {"font": "Arial Black", "beat_size": 84, "caption_size": 70},
    "captions": {"enabled": True, "engine": "faster-whisper", "model": "small", "words_per_line": 3,
                 "whisper_cpp_exe": "", "whisper_cpp_model": ""},
    "audio": {"game_volume": 1.0, "game_volume_under_vo": 0.3},
    "moments": {"length": 18, "hook_flash": 1.5, "max": 10, "min_gap": 20, "auto": True},
    "llm": {"provider": "none", "model": "llama3.1", "url": "http://localhost:11434"},
    "experiments": {"enabled": True,
                    "formats": ["payoff_first", "straight", "beat_story", "loop", "countdown", "multi_react"],
                    "hook_styles": ["viewer_title", "question", "curiosity", "ai"],
                    "min_scored": 15, "explore": 0.2, "score_hours": 48},
    "publish": {"platforms": ["youtube", "instagram", "facebook", "tiktok", "x"], "min_gap_minutes": 90,
                "hours": [9, 23], "meta_api_version": "v23.0",
                "youtube": {"daily_max": 5, "privacy": "public"},
                "instagram": {"daily_max": 10}, "facebook": {"daily_max": 10},
                "tiktok": {"daily_max": 5, "mode": "inbox", "privacy": "PUBLIC_TO_EVERYONE"},
                "x": {"daily_max": 8}},
    "tiktok": {"port": 8899},
    "backup": {"dir": "", "keep": 14},
    "outreach": {"your_name": "", "your_channel": "", "daily_max": 10, "min_gap_minutes": 10,
                 "hours": [10, 20], "followup": True, "followup_days": 5},
    "discovery": {"games": ["Grand Theft Auto VI", "Grand Theft Auto V"], "languages": ["en"],
                  "viewers": [100, 8000], "youtube_queries": ["GTA 6 live", "GTA V RP live"]},
    "youtube_live": {"use_chat": True, "use_heatmap": True, "cookies_from_browser": ""},
    "kick": {"hours": 48, "categories": ["Grand Theft Auto"],
             "block_categories": ["Slots", "Casino", "Gambling", "Betting"]},
    "x": {"checkpoints": [48, 168], "monthly_read_limit": 1000, "monthly_post_limit": 150, "port": 8898},
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
