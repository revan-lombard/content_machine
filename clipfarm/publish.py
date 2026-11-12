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

from . import meta_api, net, sources, tiktok_api, tracker, x_api, youtube_api
from .config import ROOT

AUTO = ["youtube", "instagram", "facebook", "tiktok", "x"]
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
        if platform == "tiktok":
            k = self.sec.get("tiktok") or {}
            if not (k.get("client_key") and k.get("client_secret")):
                return "add tiktok client_key/client_secret to secrets.yaml"
            mode = (self.cfg["publish"].get("tiktok") or {}).get("mode", "inbox")
            scope = tiktok_api.POST_SCOPES.get(mode)
            if not scope:
                return f"publish.tiktok.mode must be inbox or direct, not '{mode}'"
            if not tiktok_api.has_scope(t / "tiktok.json", scope):
                return f"run: python -m clipfarm connect tiktok  (to grant {scope})"
            return None
        if platform == "x":
            k = self.sec.get("x") or {}
            if not k.get("client_id"):
                return "add x client_id (and client_secret) to secrets.yaml"
            if not x_api.has_scopes(t / "x.json"):
                return "run: python -m clipfarm connect x"
            return None
        return "not supported for auto-posting"

    def post(self, platform: str, video: Path, texts: dict, tags: list[str]) -> tuple[str, str]:
        t = token_dir(self.root)
        if platform == "youtube":
            y = self.sec["youtube_oauth"]
            tok = youtube_api.access_token(y["client_id"], y["client_secret"], t / "youtube.json")
            return youtube_api.upload(tok, video, texts["youtube_title"], texts["youtube_description"], tags,
                                      self.cfg["publish"]["youtube"].get("privacy", "public"))
        if platform == "x":
            k = self.sec["x"]
            tok = x_api.access_token(k["client_id"], k.get("client_secret") or None, t / "x.json")
            return x_api.post(tok, video, texts["x"])
        if platform == "tiktok":
            k = self.sec["tiktok"]
            tok = tiktok_api.access_token(k["client_key"], k["client_secret"], t / "tiktok.json")
            tc = self.cfg["publish"].get("tiktok") or {}
            if tc.get("mode", "inbox") == "direct":
                return tiktok_api.direct_post(tok, video, texts["tiktok"], tc.get("privacy", "PUBLIC_TO_EVERYONE"))
            return tiktok_api.send_to_inbox(tok, video), None  # None = a draft, not a post yet
        conf = meta_api.load(t / "meta.json")
        if platform == "instagram":
            return meta_api.post_instagram(conf, video, texts["instagram"], self.ver)
        return meta_api.post_facebook(conf, video, texts["facebook"], self.ver)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _allowed_by_source(meta: dict, platform: str, sources_file=None) -> str | None:
    """None if this clip may go to this platform, else why not."""
    srcs = [x for x in (meta.get("sources") or [meta.get("source") or {}]) if x.get("platform") in ("twitch", "kick", "youtube")]
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
          AND NOT EXISTS (SELECT 1 FROM pending_posts q WHERE q.clip_id = c.id AND q.platform = ?)
          AND (SELECT COUNT(*) FROM publish_attempts a WHERE a.clip_id = c.id AND a.platform = ?) < ?
        ORDER BY c.created""", (platform, platform, platform, MAX_ATTEMPTS)).fetchall()


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
        since = _iso(now - timedelta(hours=24))
        recent = [r[0] for r in con.execute(
            "SELECT posted_at FROM posts WHERE platform=? AND posted_at >= ?",
            (platform, since))]
        # TikTok inbox drafts count too (TikTok allows at most 5 pending drafts per 24h)
        recent += [r[0] for r in con.execute(
            "SELECT sent_at FROM pending_posts WHERE platform=? AND sent_at >= ?", (platform, since))]
        cap = int((pub.get(platform) or {}).get("daily_max", 5))
        if len(recent) >= cap:
            results.append({"platform": platform, "skipped": f"daily max {cap} reached"})
            continue
        gap = timedelta(minutes=float(pub.get("min_gap_minutes", 90)))
        if recent and now - max(datetime.strptime(r, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                for r in recent) < gap:
            results.append({"platform": platform, "skipped": "waiting for the gap between posts"})
            continue
        if platform == "x":  # X bills per post: stop at the monthly limit
            limit = int((cfg.get("x") or {}).get("monthly_post_limit", 150))
            used = con.execute("SELECT COUNT(*) FROM api_spend WHERE platform='x' AND kind='post' "
                               "AND substr(at,1,7)=?", (now.strftime("%Y-%m"),)).fetchone()[0]
            if used >= limit:
                results.append({"platform": platform, "skipped": f"monthly X post limit {limit} reached "
                                                                 "(x.monthly_post_limit in config.yaml)"})
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
            if url is None:  # sent to the TikTok inbox: you finish it in the app
                con.execute("INSERT OR REPLACE INTO pending_posts VALUES (?,?,?,?,?)",
                            (clip["id"], platform, ext_id, _iso(now), _platform_text(meta)["tiktok"]))
                con.commit()
                results.append({"platform": platform, "clip": clip["id"], "inbox": True})
            else:
                tracker.record_post(con, clip["id"], platform, url, posted_at=_iso(now), external_id=ext_id)
                if platform == "x":
                    con.execute("INSERT INTO api_spend VALUES ('x', ?, 'post')", (_iso(now),))
                    con.commit()
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



def _norm(text: str) -> str:
    """Caption text for matching: no hashtags/mentions/punctuation, lowercase, single spaces."""
    import re
    t = re.sub(r"[#@]\w+", " ", text or "")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return " ".join(t.split())


def claim_tiktok(cfg: dict, con, root: Path, now: datetime | None = None, list_fn=None) -> int:
    """Find TikTok inbox drafts you've since posted from the app, and log them as posts (with their
    link) so their stats are tracked. Matches on the caption you pasted; if you edited it, a lone
    new video posted after a lone pending draft is matched too. Returns how many were logged."""
    pend = con.execute("SELECT * FROM pending_posts WHERE platform='tiktok' ORDER BY sent_at").fetchall()
    if not pend:
        return 0
    now = now or datetime.now(timezone.utc)
    to_ts = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    since = min(to_ts(p["sent_at"]) for p in pend) - 60
    if list_fn is None:
        k = _secrets().get("tiktok") or {}
        try:
            tok = tiktok_api.access_token(k["client_key"], k["client_secret"], token_dir(root) / "tiktok.json")
            vids = tiktok_api.list_videos(tok, since)
        except (net.HttpError, RuntimeError, KeyError) as e:
            print(f"  TikTok: couldn't check for posted drafts: {str(e)[:150]}")
            return 0
    else:
        vids = list_fn(since)
    known = {r[0] for r in con.execute("SELECT external_id FROM posts WHERE platform='tiktok'")}
    vids = sorted((v for v in vids if str(v["id"]) not in known), key=lambda v: v.get("create_time", 0))
    logged = 0
    for p in pend:
        want = _norm(p["caption"])[:40]
        cands = [v for v in vids if v.get("create_time", 0) >= to_ts(p["sent_at"]) - 60]
        hit = next((v for v in cands if want and want in _norm(v.get("video_description") or v.get("title") or "")), None)
        if hit is None and len(pend) == 1 and len(cands) == 1:
            hit = cands[0]
        if hit is None:
            continue
        url = hit.get("share_url") or f"https://www.tiktok.com/video/{hit['id']}"
        posted = datetime.fromtimestamp(hit.get("create_time", now.timestamp()), timezone.utc)
        tracker.record_post(con, p["clip_id"], "tiktok", url, posted_at=_iso(posted), external_id=str(hit["id"]))
        vids.remove(hit)
        logged += 1
    return logged
