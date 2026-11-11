"""Auto-posting of APPROVED clips to YouTube Shorts, Instagram Reels and Facebook Reels.

Rules, every run:
- only clips you approved on the review page (or `python -m clipfarm approve ...`)
- the streamer must still be approved in sources.yaml, and the campaign's `platforms`
  rule (if set) must allow the platform
- at most one post per platform per run, `min_gap_minutes` apart, within `hours`,
  and no more than `daily_max` per platform per 24h
- a clip that fails 3 times on a platform is skipped there (the error shows on the dashboard)
Every successful post is logged with its link, so its views are tracked automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import meta_api, net, sources, tracker, youtube_api
from .config import ROOT

AUTO = ["youtube", "instagram", "facebook"]
MAX_ATTEMPTS = 3


def _secrets() -> dict:
    p = ROOT / "secrets.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def token_dir(root: Path) -> Path:
    return root / "data" / "tokens"


class Clients:
    """Credentials, loaded lazily so a platform that isn't set up is just skipped."""

    def __init__(self, cfg: dict, root: Path):
        self.cfg, self.root, self.sec = cfg, root, _secrets()
        self.ver = cfg["publish"].get("meta_api_version", "v23.0")

    def ready(self, platform: str) -> str | None:
        """None if the platform can post, else why not."""
        t = token_dir(self.root)
        if platform == "youtube":
            y = self.sec.get("youtube_oauth") or {}
            if not (y.get("client_id") and y.get("client_secret")):
                return "add youtube_oauth client_id/client_secret to secrets.yaml"
            if not (t / "youtube.json").exists():
                return "run: python -m clipfarm connect youtube"
            return None
        if platform in ("instagram", "facebook"):
            if not (t / "meta.json").exists():
                return "run: python -m clipfarm connect meta --token ..."
            if platform == "instagram" and not meta_api.load(t / "meta.json").get("ig_user_id"):
                return "no Instagram account is linked to the connected Facebook Page"
            return None
        return "not supported for auto-posting"

    def post(self, platform: str, video: Path, texts: dict, tags: list[str]) -> tuple[str, str]:
        t = token_dir(self.root)
        if platform == "youtube":
            y = self.sec["youtube_oauth"]
            tok = youtube_api.access_token(y["client_id"], y["client_secret"], t / "youtube.json")
            return youtube_api.upload(tok, video, texts["youtube_title"], texts["youtube_description"], tags,
                                      self.cfg["publish"]["youtube"].get("privacy", "public"))
        conf = meta_api.load(t / "meta.json")
        if platform == "instagram":
            return meta_api.post_instagram(conf, video, texts["instagram"], self.ver)
        return meta_api.post_facebook(conf, video, texts["facebook"], self.ver)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _allowed_by_source(meta: dict, platform: str, sources_file=None) -> str | None:
    """None if this clip may go to this platform, else why not."""
    src = meta.get("source") or {}
    if src.get("platform") != "twitch":
        return None  # your own gameplay
    try:
        s = sources.get(src["login"], sources_file)
    except SystemExit as e:
        return f"source no longer approved ({str(e).splitlines()[0]})"
    allowed = (s.get("rules") or {}).get("platforms")
    if allowed and platform not in allowed:
        return f"campaign rules allow only {', '.join(allowed)}"
    return None


def queue(con, platform: str) -> list:
    """Approved clips not yet on this platform, oldest first, under the retry limit."""
    return con.execute("""
        SELECT c.* FROM clips c
        WHERE c.status = 'approved'
          AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id = c.id AND p.platform = ?)
          AND (SELECT COUNT(*) FROM publish_attempts a WHERE a.clip_id = c.id AND a.platform = ?) < ?
        ORDER BY c.created""", (platform, platform, MAX_ATTEMPTS)).fetchall()


def run(cfg: dict, con, root: Path, clients=None, platforms: list[str] | None = None,
        dry_run: bool = False, now: datetime | None = None, sources_file=None) -> list[dict]:
    from .pipeline import _platform_text
    pub = cfg["publish"]
    now = now or datetime.now(timezone.utc)
    clients = clients or Clients(cfg, root)
    local_hour = now.astimezone().hour
    lo, hi = pub.get("hours", [0, 24])
    results = []
    for platform in platforms or pub["platforms"]:
        if platform not in AUTO:
            results.append({"platform": platform, "skipped": "manual only (see README)"})
            continue
        why = clients.ready(platform)
        if why:
            results.append({"platform": platform, "skipped": why})
            continue
        if not (lo <= local_hour < hi):
            results.append({"platform": platform, "skipped": f"outside posting hours {lo}:00-{hi}:00"})
            continue
        recent = [r["posted_at"] for r in con.execute(
            "SELECT posted_at FROM posts WHERE platform=? AND posted_at >= ?",
            (platform, _iso(now - timedelta(hours=24))))]
        cap = int((pub.get(platform) or {}).get("daily_max", 5))
        if len(recent) >= cap:
            results.append({"platform": platform, "skipped": f"daily max {cap} reached"})
            continue
        gap = timedelta(minutes=float(pub.get("min_gap_minutes", 90)))
        if recent and now - max(datetime.strptime(r, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                for r in recent) < gap:
            results.append({"platform": platform, "skipped": "waiting for the gap between posts"})
            continue
        posted = False
        for clip in queue(con, platform):
            d = Path(clip["path"])
            video = d / "final.mp4"
            if not video.exists():
                continue
            meta = yaml.safe_load((d / "script.yaml").read_text(encoding="utf-8"))
            blocked = _allowed_by_source(meta, platform, sources_file)
            if blocked:
                results.append({"platform": platform, "clip": clip["id"], "skipped": blocked})
                continue
            if dry_run:
                results.append({"platform": platform, "clip": clip["id"], "would_post": True})
                posted = True
                break
            try:
                ext_id, url = clients.post(platform, video, _platform_text(meta), meta.get("hashtags") or [])
            except (net.HttpError, RuntimeError, KeyError, OSError) as e:
                con.execute("INSERT INTO publish_attempts VALUES (?,?,?,?)",
                            (clip["id"], platform, _iso(now), str(e)[:1000]))
                con.commit()
                results.append({"platform": platform, "clip": clip["id"], "error": str(e)[:300]})
                break  # don't hammer a platform that's failing; next run retries
            tracker.record_post(con, clip["id"], platform, url, posted_at=_iso(now), external_id=ext_id)
            results.append({"platform": platform, "clip": clip["id"], "url": url})
            posted = True
            break
        if not posted and not any(r.get("platform") == platform for r in results):
            results.append({"platform": platform, "skipped": "nothing approved left to post"})
    return results


def fetch_instagram_stats(cfg: dict, con, root: Path, days: int = 30) -> int:
    """Auto stats for Instagram posts made by the publisher (we know their media ids)."""
    f = token_dir(root) / "meta.json"
    if not f.exists():
        return 0
    conf = meta_api.load(f)
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    n = 0
    for p in con.execute("""SELECT id, external_id FROM posts WHERE platform='instagram'
                            AND external_id IS NOT NULL AND posted_at >= ?""", (cutoff,)).fetchall():
        try:
            s = meta_api.instagram_stats(conf, p["external_id"], cfg["publish"].get("meta_api_version", "v23.0"))
        except (net.HttpError, RuntimeError) as e:
            print(f"  Instagram stats failed for post {p['id']}: {str(e)[:150]}")
            continue
        if "views" in s:
            tracker.add_stats(con, p["id"], s["views"], s.get("likes", 0), s.get("comments", 0),
                              s.get("shares", 0), origin="instagram-api")
            n += 1
    return n
