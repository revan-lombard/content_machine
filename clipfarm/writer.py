"""Writes the on-screen beat lines, titles and descriptions.

provider "none": built-in templates (free, offline, always works).
provider "ollama": a free local model via https://ollama.com (better, varied writing).
"""
from __future__ import annotations

import json
import random
import urllib.request

STRUCTURE = """You write on-screen text for 60-second GTA 6 YouTube Shorts / TikToks.
Every video follows this beat structure (a "Can I do X in GTA 6?" challenge):
01 hook (0-3s): create a question the viewer needs answered. Usually "Can I X in GTA 6?"
02 problem (3-8s): show why it's hard. The viewer instantly feels the pain point.
03 challenge (8-12s): the rules and stakes. Give them a reason to stay. "Will it work?"
04 build (12-30s): the attempt. Short progress line.
05 obstacle (30-40s): something breaks. "That's not supposed to happen..."
06 reengage (40-48s): pull attention back right before the payoff. "One more thing to try..."
07 result (48-57s): the payoff. An obvious transformation.
08 end (57-60s): one-line kicker, then the video cuts abruptly.
Each line is on screen for a few seconds: max 8 words, punchy, no emojis, no hashtags."""


def _ollama(cfg: dict, prompt: str) -> dict | None:
    llm = cfg["llm"]
    body = json.dumps({"model": llm["model"], "prompt": prompt, "format": "json",
                       "stream": False, "options": {"temperature": 0.9}}).encode()
    req = urllib.request.Request(f"{llm['url'].rstrip('/')}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(json.loads(r.read())["response"])
    except Exception as e:
        print(f"  ollama unavailable ({e}); using templates")
        return None


def _clean(d: dict, keys: list[str]) -> dict:
    return {k: str(d.get(k, "")).strip() for k in keys}


def challenge_text(cfg: dict, challenge: str) -> dict:
    """Beat lines + title/description for a Tier A challenge Short."""
    c = challenge.strip() or "pull this off"
    base = {
        "hook": f"Can I {c} in GTA 6?",
        "problem": "This should be impossible",
        "challenge": "No cheats. One attempt. Will it work?",
        "build": "Here's the plan",
        "obstacle": "That's NOT supposed to happen...",
        "reengage": "One more thing to try...",
        "result": "IT ACTUALLY WORKED",
        "end": f"And that's how you {c}.",
        "title": f"Can I {c} in GTA 6?",
        "description": f"Trying to {c} in GTA 6 with no cheats. Did it work?",
    }
    keys = list(base)
    if cfg["llm"]["provider"] == "ollama":
        out = _ollama(cfg, f"{STRUCTURE}\n\nChallenge: {c}\n\nReturn JSON with keys: "
                           f"{', '.join(keys)}. 'title' max 70 characters and makes people click. "
                           "'description' is 1-2 sentences.")
        if out:
            merged = {**base, **{k: v for k, v in _clean(out, keys).items() if v}}
            return merged
    return base


MOMENT_HOOKS = [
    "Wait for it...", "GTA 6 physics are unreal", "I did NOT expect that",
    "Watch the end", "This shouldn't be possible", "Rockstar, explain this",
    "Nobody talks about this", "That went wrong FAST", "Worst timing ever",
    "How did this happen?", "This is why I love GTA 6", "I can't believe that worked",
]


def moment_text(cfg: dict, challenge: str, index: int, transcript: str = "") -> dict:
    """Hook + title for a short moment clip."""
    rng = random.Random(f"{challenge}-{index}")
    hook = rng.choice(MOMENT_HOOKS)
    base = {"hook": hook, "title": f"{hook.rstrip('.')} | GTA 6",
            "description": "GTA 6 moment. Follow for a new clip every day."}
    if cfg["llm"]["provider"] == "ollama":
        out = _ollama(cfg, "You write hooks for 15-25 second GTA 6 gameplay clips. The clip opens on "
                           "the payoff for 1.5s, then rewinds to the build-up.\n"
                           f"Session theme: {challenge or 'general gameplay'}\n"
                           f"What was said in the clip: {transcript[:400] or '(unknown)'}\n"
                           "Return JSON with keys: hook (max 7 words, creates curiosity, "
                           "no emojis), title (max 70 chars), description (1 sentence).")
        if out:
            return {**base, **{k: v for k, v in _clean(out, list(base)).items() if v}}
    return base
