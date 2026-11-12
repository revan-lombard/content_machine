"""Performance tracking + format testing.

Every clip is logged with the format and hook style it was made with. Every post is logged
with its URL; view counts come from the YouTube API or are typed in. A post "wins" when its
views 48h after posting beat the median of your recent posts on the same platform, so the
test stays fair as your accounts grow and works across platforms with different reach.

The next clip's format is picked by Thompson sampling: formats that win more get picked
more, but every format keeps getting some clips (warm-up rotation, then an exploration share)
so a lucky early streak can't lock in a winner.
"""
from __future__ import annotations

import math
import random
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLATFORMS = ["youtube", "tiktok", "instagram", "facebook", "x"]
DIMENSIONS = {"format": "format", "hook_style": "hook_style"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY, created TEXT, source TEXT, format TEXT, hook_style TEXT,
  length REAL, title TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT REFERENCES clips(id), platform TEXT,
  url TEXT, posted_at TEXT, UNIQUE(clip_id, platform));
CREATE TABLE IF NOT EXISTS pending_posts (
  clip_id TEXT, platform TEXT, ref TEXT, sent_at TEXT, caption TEXT, PRIMARY KEY (clip_id, platform));
CREATE TABLE IF NOT EXISTS publish_attempts (
  clip_id TEXT, platform TEXT, at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS stats (
  post_id INTEGER REFERENCES posts(id), fetched_at TEXT, views INTEGER, likes INTEGER,
  comments INTEGER, shares INTEGER, origin TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # columns added after the first release
    cols = {r[1] for r in con.execute("PRAGMA table_info(clips)")}
    if "status" not in cols:
        con.execute("ALTER TABLE clips ADD COLUMN status TEXT DEFAULT 'new'")  # new | approved | rejected
    if "external_id" not in {r[1] for r in con.execute("PRAGMA table_info(posts)")}:
        con.execute("ALTER TABLE posts ADD COLUMN external_id TEXT")
    con.commit()
    return con


def set_status(con, clip_id: str, status: str) -> None:
    if status not in ("new", "approved", "rejected"):
        raise ValueError("status must be new, approved or rejected")
    if not con.execute("UPDATE clips SET status=? WHERE id=?", (status, clip_id)).rowcount:
        raise ValueError(f"unknown clip {clip_id}")
    con.commit()


# ---------------------------------------------------------------- logging

def register_clip(con, clip_id: str, *, source: str = "", fmt: str = "", hook_style: str = "",
                  length: float = 0.0, title: str = "", path: str = "") -> None:
    con.execute("""INSERT INTO clips (id, created, source, format, hook_style, length, title, path)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET format=excluded.format, hook_style=excluded.hook_style,
                   length=excluded.length, title=excluded.title, path=excluded.path""",
                (clip_id, now_iso(), source, fmt, hook_style, length, title, path))
    con.commit()


def record_post(con, clip_id: str, platform: str, url: str, posted_at: str | None = None,
                external_id: str | None = None) -> int:
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    if not con.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone():
        raise ValueError(f"unknown clip {clip_id}")
    con.execute("""INSERT INTO posts (clip_id, platform, url, posted_at, external_id) VALUES (?,?,?,?,?)
                   ON CONFLICT(clip_id, platform) DO UPDATE SET url=excluded.url,
                   external_id=COALESCE(excluded.external_id, posts.external_id)""",
                (clip_id, platform, url.strip(), posted_at or now_iso(), external_id))
    # a draft sent to the TikTok inbox is now a real post
    con.execute("DELETE FROM pending_posts WHERE clip_id=? AND platform=?", (clip_id, platform))
    con.commit()
    return con.execute("SELECT id FROM posts WHERE clip_id=? AND platform=?", (clip_id, platform)).fetchone()[0]


def add_stats(con, post_id: int, views: int, likes: int = 0, comments: int = 0, shares: int = 0,
              origin: str = "manual", fetched_at: str | None = None) -> None:
    con.execute("INSERT INTO stats VALUES (?,?,?,?,?,?,?)",
                (post_id, fetched_at or now_iso(), int(views), int(likes or 0), int(comments or 0),
                 int(shares or 0), origin))
    con.commit()


# ---------------------------------------------------------------- scoring

def post_scores(con, hours: float = 48, now: datetime | None = None) -> list[dict]:
    """One row per post: views at ~`hours` after posting, and whether it beat its platform's median.
    status: 'scored' | 'pending' (too young) | 'needs_stats' (old enough, no numbers yet)."""
    now = now or datetime.now(timezone.utc)
    rows = con.execute("""SELECT p.*, c.format, c.hook_style, c.title, c.source
                          FROM posts p JOIN clips c ON c.id = p.clip_id ORDER BY p.posted_at""").fetchall()
    out = []
    for r in rows:
        posted = _dt(r["posted_at"])
        target = posted + timedelta(hours=hours)
        snaps = con.execute("SELECT * FROM stats WHERE post_id=? ORDER BY fetched_at", (r["id"],)).fetchall()
        d = dict(r)
        latest = snaps[-1] if snaps else None
        d["latest_views"] = latest["views"] if latest else None
        if now < target:
            d["status"] = "pending"
        else:
            # closest snapshot to the target time, ignoring ones taken before half the window
            ok = [s for s in snaps if _dt(s["fetched_at"]) >= posted + timedelta(hours=hours / 2)]
            if not ok:
                d["status"] = "needs_stats"
            else:
                s = min(ok, key=lambda s: abs((_dt(s["fetched_at"]) - target).total_seconds()))
                d.update(status="scored", views=s["views"], likes=s["likes"], comments=s["comments"],
                         shares=s["shares"])
                d["engagement"] = ((s["likes"] + s["comments"] + s["shares"]) / s["views"]) if s["views"] else 0.0
        out.append(d)
    # win/loss against the median of the previous 30 scored posts on the same platform
    for plat in PLATFORMS:
        hist: list[float] = []
        for d in (x for x in out if x["platform"] == plat and x["status"] == "scored"):
            v = math.log1p(d["views"])
            # the first post on a platform has nothing to beat yet: it counts as average
            base = statistics.median(hist[-30:]) if hist else v
            d["baseline_views"] = round(math.expm1(base))
            d["win"] = 1.0 if v > base else 0.5 if v == base else 0.0
            hist.append(v)
    return out


def arm_stats(con, dim: str, arms: list[str], hours: float = 48) -> dict[str, dict]:
    col = DIMENSIONS[dim]
    res = {a: {"clips": 0, "scored": 0, "wins": 0.0, "views": [], "engagement": []} for a in arms}
    for row in con.execute(f"SELECT {col} AS arm, COUNT(*) AS n FROM clips GROUP BY {col}"):
        if row["arm"] in res:
            res[row["arm"]]["clips"] = row["n"]
    per_clip: dict[str, list[dict]] = {}
    for d in post_scores(con, hours):
        if d["status"] == "scored":
            per_clip.setdefault(d["clip_id"], []).append(d)
    for clip_id, posts in per_clip.items():
        arm = posts[0][col]
        if arm not in res:
            continue
        r = res[arm]
        r["scored"] += 1
        r["wins"] += sum(p["win"] for p in posts) / len(posts)
        r["views"] += [p["views"] for p in posts]
        r["engagement"] += [p["engagement"] for p in posts]
    for r in res.values():
        r["win_rate"] = r["wins"] / r["scored"] if r["scored"] else None
        r["median_views"] = statistics.median(r["views"]) if r["views"] else None
        r["engagement_rate"] = statistics.mean(r["engagement"]) if r["engagement"] else None
    return res


# ---------------------------------------------------------------- choosing

def _policy(stats: dict[str, dict], arms: list[str], exp: dict, rng: random.Random) -> str:
    warm = int(exp.get("min_scored", 15))
    if any(stats[a]["scored"] < warm for a in arms):
        # warm-up: rotate evenly (by clips made, since scores arrive 48h later)
        fewest = min(stats[a]["clips"] for a in arms)
        return rng.choice([a for a in arms if stats[a]["clips"] == fewest])
    if rng.random() < float(exp.get("explore", 0.2)):
        return rng.choice(arms)
    return max(arms, key=lambda a: rng.betavariate(1 + stats[a]["wins"], 1 + stats[a]["scored"] - stats[a]["wins"]))


def choose(con, cfg: dict, dim: str, arms: list[str], rng: random.Random | None = None) -> str:
    exp = cfg["experiments"]
    return _policy(arm_stats(con, dim, arms, exp.get("score_hours", 48)), arms, exp, rng or random.Random())


def want_extra(con, cfg: dict, arm: str, others: list[str], rng: random.Random | None = None) -> bool:
    """For formats made IN ADDITION to the regular clips (countdown: one per stream).
    Warm-up: always make it. Then: make it when its sampled win rate beats a typical regular
    format's, or on an exploration draw."""
    exp = cfg["experiments"]
    rng = rng or random.Random()
    stats = arm_stats(con, "format", [arm] + others, exp.get("score_hours", 48))
    if stats[arm]["scored"] < int(exp.get("min_scored", 15)):
        return True
    if rng.random() < float(exp.get("explore", 0.2)):
        return True
    beta = lambda a: rng.betavariate(1 + stats[a]["wins"], 1 + stats[a]["scored"] - stats[a]["wins"])
    return beta(arm) >= statistics.median([beta(o) for o in others])


def mix(con, cfg: dict, dim: str, arms: list[str], draws: int = 4000) -> tuple[dict[str, float], bool]:
    """Expected share of upcoming clips per arm, and whether we're still warming up."""
    exp = cfg["experiments"]
    stats = arm_stats(con, dim, arms, exp.get("score_hours", 48))
    warming = any(stats[a]["scored"] < int(exp.get("min_scored", 15)) for a in arms)
    if warming:
        return {a: 1 / len(arms) for a in arms}, True
    rng = random.Random(7)
    counts = {a: 0 for a in arms}
    for _ in range(draws):
        counts[_policy(stats, arms, exp, rng)] += 1
    return {a: counts[a] / draws for a in arms}, False


def todo(con, hours: float = 48, now: datetime | None = None) -> list[dict]:
    """Posts that are old enough to score but have no numbers (TikTok, Instagram, ... typed in by hand)."""
    return [d for d in post_scores(con, hours, now) if d["status"] == "needs_stats"]
