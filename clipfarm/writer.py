"""Writes the on-screen beat lines, titles and descriptions.

provider "none": built-in templates (free, offline, always works).
provider "lmstudio": a free local model in LM Studio (works on AMD cards like the RX 580).
provider "ollama": a free local model via https://ollama.com (newer GPUs).
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


def _openai_compatible(cfg: dict, prompt: str) -> dict | None:
    """LM Studio (and other OpenAI-compatible local servers). LM Studio runs on AMD GPUs via Vulkan."""
    llm = cfg["llm"]
    body = json.dumps({"model": llm["model"], "temperature": 0.9,
                       "messages": [{"role": "system", "content": "Reply with one JSON object only."},
                                    {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{llm['url'].rstrip('/')}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            text = json.loads(r.read())["choices"][0]["message"]["content"]
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        print(f"  local AI unavailable ({e}); using templates")
        return None


def _ask(cfg: dict, prompt: str) -> dict | None:
    provider = cfg["llm"]["provider"]
    if provider == "ollama":
        return _ollama(cfg, prompt)
    if provider in ("lmstudio", "openai-compatible"):
        return _openai_compatible(cfg, prompt)
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
    if cfg["llm"]["provider"] != "none":
        out = _ask(cfg, f"{STRUCTURE}\n\nChallenge: {c}\n\nReturn JSON with keys: "
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
QUESTION_HOOKS = [
    "Did that really just happen?", "How is this even possible?", "Would you have survived this?",
    "What would you have done here?", "Craziest GTA 6 moment yet?", "How did this actually work?",
    "Is GTA 6 physics broken?", "Who saw that coming?",
]
# Hook styles under test (see tracker.py). "viewer_title" falls back to "curiosity" when no
# usable viewer clip title exists, and "ai" falls back to "curiosity" without a local AI.
HOOK_STYLES = ["viewer_title", "question", "curiosity", "ai"]


def moment_text(cfg: dict, challenge: str, index: int, transcript: str = "",
                context: list[str] | None = None, streamer: str = "",
                hook_style: str | None = None) -> dict:
    """Hook + title for a short moment clip. `context` = titles viewers gave their Twitch clips.
    Returns hook/title/description plus the hook_style actually used."""
    rng = random.Random(f"{challenge}-{index}-{streamer}")
    good = [t.strip() for t in (context or []) if 8 <= len(t.strip()) <= 60]
    style = hook_style or ("viewer_title" if good else "curiosity")
    if style == "viewer_title" and not good:
        style = "curiosity"
    if style == "ai" and cfg["llm"]["provider"] == "none":
        style = "curiosity"
    if style == "viewer_title":
        hook = good[0]  # a viewer's clip title is often the best hook there is
    elif style == "question":
        hook = rng.choice(QUESTION_HOOKS)
    else:
        hook = rng.choice(MOMENT_HOOKS)
    # the streamer's credit is added per platform when posting, so the title stays clean
    base = {"hook": hook, "title": f"{hook.rstrip('.')} | GTA 6"[:90],
            "description": "GTA 6 moment. Follow for a new clip every day."}
    if style == "ai":
        out = _ask(cfg, "You write hooks for 15-25 second GTA 6 clips.\n"
                        f"Streamer: {streamer or 'me'}\nStream / session title: {challenge or 'GTA 6'}\n"
                        f"Titles viewers gave their clips of this moment: {'; '.join(context or []) or '(none)'}\n"
                        f"What was said: {transcript[:400] or '(unknown)'}\n"
                        "Return JSON with keys: hook (max 7 words, creates curiosity, "
                        "no emojis), title (max 70 chars), description (1 sentence). Don't invent "
                        "events that the clip titles don't support.")
        if out:
            base = {**base, **{k: v for k, v in _clean(out, list(base)).items() if v}}
        else:
            style = "curiosity"
    return {**base, "hook_style": style}
