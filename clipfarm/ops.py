"""Housekeeping that used to be checklist steps: scheduled tasks, backups, health check, updates."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from .config import ROOT

# ---------------------------------------------------------------- scheduled tasks (Windows)

TASKS = [
    # name, clipfarm arguments, schedule arguments for schtasks
    ("ClipFarm - daily clips", "clip --all", ["/SC", "DAILY", "/ST", "08:15"]),
    ("ClipFarm - discover 1", "discover", ["/SC", "DAILY", "/ST", "13:05"]),
    ("ClipFarm - discover 2", "discover", ["/SC", "DAILY", "/ST", "18:05"]),
    ("ClipFarm - discover 3", "discover", ["/SC", "DAILY", "/ST", "22:05"]),
    ("ClipFarm - publisher", "publish --watch", ["/SC", "ONLOGON"]),
    ("ClipFarm - backup", "backup", ["/SC", "DAILY", "/ST", "23:40"]),
]


def task_command(root: Path, args: str) -> str:
    """cmd line a scheduled task runs: the project's own Python, output appended to a log."""
    py = root / ".venv" / "Scripts" / "python.exe"
    log = root / "data" / "logs" / f"{args.split()[0]}.log"
    return f'cmd /c cd /d "{root}" && "{py}" -m clipfarm {args} >> "{log}" 2>&1'


def schedule(root: Path = ROOT, remove: bool = False, run=subprocess.run) -> list[str]:
    """Create (or remove) the Windows scheduled tasks. Returns one line per task."""
    if os.name != "nt" and run is subprocess.run:
        raise SystemExit("Scheduled tasks are created with Windows Task Scheduler - run this on your PC.")
    (root / "data" / "logs").mkdir(parents=True, exist_ok=True)
    out = []
    for name, args, when in TASKS:
        if remove:
            cmd = ["schtasks", "/Delete", "/TN", name, "/F"]
        else:
            cmd = ["schtasks", "/Create", "/TN", name, "/TR", task_command(root, args), *when, "/F"]
        r = run(cmd, capture_output=True, text=True)
        ok = r.returncode == 0
        out.append(f"{'removed' if remove else 'scheduled'}: {name}" if ok else
                   f"FAILED: {name}: {(r.stderr or r.stdout).strip()[:200]}")
    return out


def scheduled(run=subprocess.run) -> dict[str, bool]:
    res = {}
    for name, _, _ in TASKS:
        try:
            r = run(["schtasks", "/Query", "/TN", name], capture_output=True, text=True)
            res[name] = r.returncode == 0
        except (OSError, FileNotFoundError):
            res[name] = False
    return res


# ---------------------------------------------------------------- backups

def backup_dir(cfg: dict, root: Path = ROOT) -> Path:
    custom = (cfg.get("backup") or {}).get("dir")
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))
    one = os.environ.get("OneDrive")
    return Path(one) / "ClipFarm backups" if one and Path(one).exists() else root / "backups"


def backup(cfg: dict, root: Path = ROOT, now: datetime | None = None) -> Path:
    """Zip the results database (a consistent copy, even while it's in use), keys, sources and config.
    Keeps the newest `backup.keep` zips (default 14)."""
    dest = backup_dir(cfg, root)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M")
    target = dest / f"clipfarm-{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        db = root / "data" / "tracker.db"
        if db.exists():
            snap = root / "data" / "tracker-backup.db"
            src, dst = sqlite3.connect(str(db)), sqlite3.connect(str(snap))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            z.write(snap, "data/tracker.db")
            snap.unlink()
        for name in ("secrets.yaml", "sources.yaml", "config.yaml"):
            if (root / name).exists():
                z.write(root / name, name)
        tokens = root / "data" / "tokens"
        if tokens.exists():
            for f in tokens.glob("*.json"):
                z.write(f, f"data/tokens/{f.name}")
    keep = int((cfg.get("backup") or {}).get("keep", 14))
    for old in sorted(dest.glob("clipfarm-*.zip"))[:-keep]:
        old.unlink()
    return target


# ---------------------------------------------------------------- update

def update(root: Path = ROOT, run=subprocess.run) -> list[str]:
    """git pull + install any new packages (what 'git pull, then setup.bat' used to be)."""
    out = []
    r = run(["git", "-C", str(root), "pull", "--ff-only"], capture_output=True, text=True)
    out.append((r.stdout or r.stderr).strip() or "git pull done")
    if r.returncode != 0:
        out.append("Update stopped: git pull failed (local changes to code files? see the message above).")
        return out
    r = run([sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
            capture_output=True, text=True)
    out.append("packages up to date" if r.returncode == 0 else f"pip failed: {r.stderr.strip()[-300:]}")
    return out


# ---------------------------------------------------------------- doctor

def _mod(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def checks(cfg: dict, root: Path = ROOT, run=subprocess.run) -> list[tuple[str, str, str, str]]:
    """(area, item, status ok|missing|optional, how to fix) for everything setup involves."""
    import yaml

    from . import sources
    sec_file = root / "secrets.yaml"
    sec = (yaml.safe_load(sec_file.read_text(encoding="utf-8")) or {}) if sec_file.exists() else {}
    tok = root / "data" / "tokens"
    has = lambda *path: _get(sec, path)
    out = []

    def add(area, item, ok, fix, optional=False):
        out.append((area, item, "ok" if ok else ("optional" if optional else "missing"), "" if ok else fix))

    add("Install", f"Python {sys.version_info.major}.{sys.version_info.minor}", sys.version_info >= (3, 10),
        "install Python 3.10+ (install.bat does it)")
    add("Install", "ffmpeg", bool(shutil.which("ffmpeg")), "run install.bat (or: winget install Gyan.FFmpeg)")
    for mod, what in (("yaml", "pyyaml"), ("numpy", "numpy"), ("yt_dlp", "yt-dlp"), ("faster_whisper", "faster-whisper"),
                      ("curl_cffi", "curl_cffi (Kick)")):
        add("Install", what, _mod(mod), "run setup.bat (or: python -m clipfarm update)")
    add("Install", "git", bool(shutil.which("git")), "run install.bat (or: winget install Git.Git)")

    o = cfg.get("outreach") or {}
    add("You", "your_name / your_channel in config.yaml", bool(o.get("your_name") and o.get("your_channel")),
        "python -m clipfarm wizard")
    add("Sources", "Twitch API keys", bool(has("twitch", "client_id") and has("twitch", "client_secret")),
        "python -m clipfarm wizard")
    approved = [s for s in sources.load_all(root / "sources.yaml") if not sources.problems(s)]
    add("Sources", f"approved streamers ({len(approved)})", bool(approved),
        "add a streamer with their permission proof (python -m clipfarm wizard)")

    add("Posting", "Instagram + Facebook connected", (tok / "meta.json").exists(),
        "python -m clipfarm wizard  (Meta step)", optional=True)
    add("Posting", "YouTube connected", (tok / "youtube.json").exists(), "python -m clipfarm wizard  (YouTube step)",
        optional=True)
    add("Posting", "TikTok connected", (tok / "tiktok.json").exists(), "python -m clipfarm wizard  (TikTok step)",
        optional=True)
    add("Posting", "X connected", (tok / "x.json").exists(), "python -m clipfarm wizard  (X step)", optional=True)
    add("Stats", "YouTube API key", bool(has("youtube", "api_key")), "python -m clipfarm wizard  (YouTube step)",
        optional=True)
    add("Outreach", "email (SMTP)", bool(has("outreach_email", "username") and has("outreach_email", "password")),
        "python -m clipfarm wizard  (email step)", optional=True)
    add("Outreach", "Twitch whispers", (tok / "twitch_user.json").exists(), "python -m clipfarm connect twitch",
        optional=True)

    if os.name == "nt":
        for name, ok in scheduled(run).items():
            add("Automation", f"scheduled task: {name}", ok, "python -m clipfarm schedule")
    newest = max((p.stat().st_mtime for p in backup_dir(cfg, root).glob("clipfarm-*.zip")), default=0) \
        if backup_dir(cfg, root).exists() else 0
    add("Automation", "backup in the last 2 days", newest and (datetime.now().timestamp() - newest) < 2 * 86400,
        "python -m clipfarm backup  (or schedule it)")
    return out


def _get(d, path):
    for p in path:
        d = (d or {}).get(p) if isinstance(d, dict) else None
    return d
