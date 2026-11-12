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
    srcs = [x for x in (meta.get("sources") or [meta.get("source") or {}]) if x.get("platform") == "twitch"]
    for src in srcs:  # multi-streamer clips need every streamer's permission and campaign OK
        try:
            s = sources.get(src["login"], sources_file)
        except SystemExit as e:
            return f"{src['login']}: source no longer approved ({str(e).splitlines()[0]})"
        allowed = (s.get("rules") or {}).get("platforms")
        if allowed and platform not in allowed:
            return f"{src['login']}'s campaign rules allow only {', '.join(allowed)}"
    return None  # your own gameplay has no sources


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


def _due(posted_at: str, last_fetch: str | None, now: datetime) -> bool:
    """Fetch hourly while a post is young (its 48h score is being decided), daily after that.
    Keeps well under Meta's API rate limits even with hundreds of posts."""
    if not last_fetch:
        return True
    age = now - datetime.strptime(posted_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    since = now - datetime.strptime(last_fetch, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return since >= (timedelta(minutes=55) if age <= timedelta(hours=72) else timedelta(hours=23))


def _fetch_meta_stats(cfg: dict, con, root: Path, platform: str, resolve, get_stats, days: int,
                      now: datetime | None) -> dict:
    """Shared by Instagram and Facebook: link hand-logged posts to their ids (resolve), then
    snapshot stats for every post that's due (hourly under 72h old, daily after)."""
    res = {"updated": 0, "matched": 0, "unmatched": 0, "failed": 0}
    f = token_dir(root) / "meta.json"
    if not f.exists():
        return res
    conf = meta_api.load(f)
    ver = cfg["publish"].get("meta_api_version", "v23.0")
    now = now or datetime.now(timezone.utc)
    origin = f"{platform}-api"
    posts = con.execute("""SELECT p.id, p.url, p.external_id, p.posted_at,
                                  (SELECT MAX(fetched_at) FROM stats s WHERE s.post_id = p.id
                                   AND s.origin = ?) AS last_fetch
                           FROM posts p WHERE p.platform = ? AND p.posted_at >= ?""",
                        (origin, platform, _iso(now - timedelta(days=days)))).fetchall()
    ids: dict[int, str] = {p["id"]: p["external_id"] for p in posts if p["external_id"]}
    missing = [p for p in posts if not p["external_id"]]
    if missing:
        found = resolve(conf, ver, missing)
        for p in missing:
            if found.get(p["id"]):
                con.execute("UPDATE posts SET external_id=? WHERE id=?", (found[p["id"]], p["id"]))
                ids[p["id"]] = found[p["id"]]
                res["matched"] += 1
            else:
                res["unmatched"] += 1
        con.commit()
    for p in posts:
        if p["id"] not in ids or not _due(p["posted_at"], p["last_fetch"], now):
            continue
        try:
            s = get_stats(conf, ids[p["id"]], ver)
        except (net.HttpError, RuntimeError) as e:
            print(f"  {platform} stats failed for {p['url']}: {str(e)[:150]}")
            res["failed"] += 1
            continue
        if "views" in s:
            tracker.add_stats(con, p["id"], s["views"], s.get("likes", 0), s.get("comments", 0),
                              s.get("shares", 0), origin=origin, fetched_at=_iso(now))
            res["updated"] += 1
    return res


def fetch_instagram_stats(cfg: dict, con, root: Path, days: int = 30, now: datetime | None = None) -> dict:
    """Views, likes, comments and shares for your Instagram posts - auto-posted ones and ones you
    posted by hand and logged with their link (matched to your account by the link's code)."""
    f = token_dir(root) / "meta.json"
    if f.exists() and not meta_api.load(f).get("ig_user_id"):
        return {"updated": 0, "matched": 0, "unmatched": 0, "failed": 0}

    def resolve(conf, ver, missing):
        try:
            by_code = meta_api.recent_media(conf, ver)
        except (net.HttpError, RuntimeError) as e:
            print(f"  Instagram: couldn't list your posts: {str(e)[:150]}")
            return {}
        return {p["id"]: by_code.get(meta_api.shortcode(p["url"]) or "") for p in missing}

    return _fetch_meta_stats(cfg, con, root, "instagram", resolve, meta_api.instagram_stats, days, now)


def fetch_facebook_stats(cfg: dict, con, root: Path, days: int = 30, now: datetime | None = None) -> dict:
    """Views (plays incl. replays), reactions, comments and shares for Reels on the connected Page -
    auto-posted ones and hand-posted ones logged with a facebook.com/reel/... link."""
    def resolve(conf, ver, missing):  # the video id is in the link itself
        return {p["id"]: meta_api.facebook_video_id(p["url"]) for p in missing}

    return _fetch_meta_stats(cfg, con, root, "facebook", resolve, meta_api.facebook_stats, days, now)
