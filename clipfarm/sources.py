"""Approved sources. The pipeline only processes channels listed in sources.yaml with permission."""
from __future__ import annotations

from pathlib import Path

import yaml

from .config import ROOT

PERMISSION_TYPES = {
    "campaign": "a paid clipping campaign you joined (proof = the campaign link)",
    "written": "the streamer/team said yes to you directly (proof = screenshot path or message link)",
    "public": "the streamer publicly allows anyone to clip (proof = link to where they say so)",
}


class NotApproved(SystemExit):
    pass


def load_all(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else ROOT / "sources.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("sources") or []


def problems(src: dict) -> list[str]:
    """Why a source can't be used. Empty list = approved."""
    out = []
    if not src.get("enabled", True):
        out.append("enabled is false")
    if src.get("platform", "twitch") != "twitch":
        out.append(f"platform '{src.get('platform')}' is not supported yet (twitch only)")
    if not src.get("login"):
        out.append("missing login (the channel name in the twitch.tv URL)")
    perm = src.get("permission") or {}
    if perm.get("type") not in PERMISSION_TYPES:
        out.append(f"permission.type must be one of: {', '.join(PERMISSION_TYPES)}")
    if not str(perm.get("proof") or "").strip():
        out.append("permission.proof is empty (link or screenshot path showing you may clip them)")
    cam = src.get("facecam")
    if cam is not None and not (isinstance(cam, list) and len(cam) == 4
                                and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in cam)):
        out.append("facecam must be [x, y, w, h] as fractions of the frame (0-1), or null")
    return out


def get(name: str, path: str | Path | None = None) -> dict:
    """The approved source called `name`, or exit with the reason it can't be used."""
    for src in load_all(path):
        if name.lower() in (str(src.get("name", "")).lower(), str(src.get("login", "")).lower()):
            bad = problems(src)
            if bad:
                raise NotApproved(f"Source '{name}' can't be used yet:\n  - " + "\n  - ".join(bad))
            src.setdefault("rules", {})
            src["name"] = src.get("name") or src["login"]
            return src
    raise NotApproved(f"'{name}' is not in sources.yaml. Only channels you have permission to clip "
                      "can be processed - add it there with its permission proof first.")


def report(path: str | Path | None = None) -> None:
    srcs = load_all(path)
    if not srcs:
        print("No sources yet. Copy the example in sources.yaml and fill it in.")
    for s in srcs:
        bad = problems(s)
        status = "OK " if not bad else "OFF"
        print(f"[{status}] {s.get('name') or s.get('login')}  ({(s.get('permission') or {}).get('type', '?')})")
        for b in bad:
            print(f"        - {b}")
