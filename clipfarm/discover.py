"""Streamer discovery: who streams GTA, how consistently, at what size, and how clippable.

Each run records who is live in the GTA categories right now (Twitch, Kick, YouTube). Over days
that builds a picture: a streamer seen on 6 of the last 14 days at ~800 viewers, whose viewers
make lots of clips and whose bio mentions clippers, is a great person to ask.

Discovery never grants permission. It drafts the message asking for it, and a ready
sources.yaml entry you complete with their "yes" as proof.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from . import net

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
  platform TEXT, login TEXT, name TEXT, seen_at TEXT, viewers INTEGER, title TEXT, language TEXT, game TEXT);
CREATE TABLE IF NOT EXISTS prospects (
  platform TEXT, login TEXT, name TEXT, url TEXT, bio TEXT, status TEXT DEFAULT 'new', notes TEXT,
  clips_7d INTEGER, clip_views_7d INTEGER, checked_at TEXT, PRIMARY KEY (platform, login));
"""
STATUSES = ["new", "contacted", "yes", "no", "ignore"]
CLIP_HINT = re.compile(r"\b(clip(s|per|pers|ping)?|editors?|tiktok|shorts)\b", re.I)


def setup(con) -> None:
    con.executescript(SCHEMA)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(con, platform: str, streams: list[dict], now: datetime) -> None:
    """streams: {login, name, viewers, title, language, game, url, bio?}"""
    for s in streams:
        con.execute("INSERT INTO sightings VALUES (?,?,?,?,?,?,?,?)",
                    (platform, s["login"].lower(), s.get("name") or s["login"], _iso(now), int(s.get("viewers") or 0),
                     s.get("title", ""), s.get("language", ""), s.get("game", "")))
        con.execute("""INSERT INTO prospects (platform, login, name, url, bio) VALUES (?,?,?,?,?)
                       ON CONFLICT(platform, login) DO UPDATE SET name=excluded.name, url=excluded.url,
                       bio=COALESCE(NULLIF(excluded.bio, ''), prospects.bio)""",
                    (platform, s["login"].lower(), s.get("name") or s["login"], s.get("url", ""), s.get("bio", "")))
    con.commit()


# ---------------------------------------------------------------- scanners

def scan_twitch(helix, games: list[str], languages: list[str], max_streams: int = 300) -> list[dict]:
    ids = []
    for g in games:
        ids += [x["id"] for x in helix.get("games", name=g).get("data", [])]
    out, seen = [], set()
    for gid in ids:
        cursor = None
        while len([s for s in out if s["game_id"] == gid]) < max_streams:
            params = {"game_id": gid, "first": 100, "after": cursor}
            if languages:
                params["language"] = languages[0] if len(languages) == 1 else None
            page = helix.get("streams", **params)
            for s in page.get("data", []):
                if s["user_login"] in seen or (languages and s.get("language") not in languages):
                    continue
                seen.add(s["user_login"])
                out.append({"login": s["user_login"], "name": s.get("user_name"), "viewers": s.get("viewer_count", 0),
                            "title": s.get("title", ""), "language": s.get("language", ""), "game": s.get("game_name", ""),
                            "game_id": gid, "user_id": s["user_id"], "url": f"https://www.twitch.tv/{s['user_login']}"})
            cursor = (page.get("pagination") or {}).get("cursor")
            if not cursor or not page.get("data"):
                break
    # bios (100 users per call): mentions of clipping / Discord are hints worth showing
    for i in range(0, len(out), 100):
        batch = out[i:i + 100]
        users = _users(helix, [s["user_id"] for s in batch])
        for s in batch:
            s["bio"] = users.get(s["user_id"], "")
    return out


def _users(helix, ids: list[str]) -> dict[str, str]:
    """Helix /users with repeated id= params."""
    import urllib.parse
    q = "&".join("id=" + urllib.parse.quote(i) for i in ids)
    r = net.request("GET", f"https://api.twitch.tv/helix/users?{q}",
                    headers={"Client-Id": helix.cid, "Authorization": f"Bearer {helix.token}"}).json()
    return {u["id"]: u.get("description", "") for u in r.get("data", [])}


def twitch_clip_activity(helix, user_id: str, now: datetime) -> tuple[int, int]:
    r = helix.get("clips", broadcaster_id=user_id, started_at=_iso(now - timedelta(days=7)),
                  ended_at=_iso(now), first=100)
    clips = r.get("data", [])
    return len(clips), sum(int(c.get("view_count", 0)) for c in clips)


KICK_API = "https://api.kick.com/public/v1"


def kick_token(client_id: str, client_secret: str) -> str:
    r = net.request("POST", "https://id.kick.com/oauth/token", form={
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}).json()
    return r["access_token"]


def scan_kick(token: str, games: list[str], languages: list[str], max_streams: int = 300) -> list[dict]:
    """Kick's official public API. Its docs are thin, so field names are read defensively and
    the sort value falls back if one is refused."""
    auth = {"Authorization": f"Bearer {token}"}
    cats = []
    for g in games:
        d = net.request("GET", f"{KICK_API}/categories", params={"q": g}, headers=auth).json()
        cats += [c for c in d.get("data", []) if "grand theft auto" in str(c.get("name", "")).lower()]
    out, seen = [], set()
    for c in cats:
        cursor = None
        for _ in range(max(1, max_streams // 100)):
            page = None
            for sort in ("viewer_count", "viewer_count_desc"):
                params = {"category_id": c["id"], "limit": 100, "sort": sort}
                if cursor:
                    params["after"] = cursor
                if len(languages) == 1:
                    params["language"] = languages[0]
                try:
                    page = net.request("GET", f"{KICK_API}/livestreams", params=params, headers=auth).json()
                    break
                except net.HttpError as e:
                    if e.status != 400:
                        raise
            if not page:
                break
            for s in page.get("data", []):
                slug = s.get("slug") or (s.get("channel") or {}).get("slug") or s.get("channel_slug")
                if not slug or slug in seen or (languages and s.get("language") and s["language"] not in languages):
                    continue
                seen.add(slug)
                out.append({"login": slug, "name": slug, "viewers": int(s.get("viewer_count") or 0),
                            "title": s.get("stream_title") or s.get("title", ""), "language": s.get("language", ""),
                            "game": c.get("name", ""), "url": f"https://kick.com/{slug}"})
            cursor = page.get("next_cursor") or (page.get("pagination") or {}).get("next_cursor")
            if not cursor:
                break
    return out


def scan_youtube(api_key: str, queries: list[str], languages: list[str]) -> list[dict]:
    """YouTube live GTA streams (search costs 100 of the free 10,000 daily quota units per query)."""
    vids, chans = {}, {}
    for q in queries:
        params = {"part": "snippet", "eventType": "live", "type": "video", "q": q, "maxResults": 50, "key": api_key}
        if languages:
            params["relevanceLanguage"] = languages[0]
        d = net.request("GET", "https://www.googleapis.com/youtube/v3/search", params=params).json()
        for it in d.get("items", []):
            vids[it["id"]["videoId"]] = it["snippet"]
    if not vids:
        return []
    out = []
    ids = list(vids)
    for i in range(0, len(ids), 50):
        d = net.request("GET", "https://www.googleapis.com/youtube/v3/videos", params={
            "part": "liveStreamingDetails,snippet", "id": ",".join(ids[i:i + 50]), "key": api_key}).json()
        for v in d.get("items", []):
            sn = v.get("snippet", {})
            cid = sn.get("channelId")
            if not cid or cid in chans:
                continue
            chans[cid] = True
            out.append({"login": cid, "name": sn.get("channelTitle", cid),
                        "viewers": int((v.get("liveStreamingDetails") or {}).get("concurrentViewers") or 0),
                        "title": sn.get("title", ""), "language": sn.get("defaultAudioLanguage", ""),
                        "game": "GTA", "url": f"https://www.youtube.com/channel/{cid}"})
    # channel descriptions (1 quota unit per 50): where business emails usually are
    for i in range(0, len(out), 50):
        batch = out[i:i + 50]
        d = net.request("GET", "https://www.googleapis.com/youtube/v3/channels", params={
            "part": "snippet", "id": ",".join(s["login"] for s in batch), "key": api_key}).json()
        desc = {c["id"]: (c.get("snippet") or {}).get("description", "") for c in d.get("items", [])}
        for s in batch:
            s["bio"] = desc.get(s["login"], "")
    return out


# ---------------------------------------------------------------- ranking

def rank(con, cfg: dict, approved: set[tuple[str, str]], now: datetime, days: int = 14) -> list[dict]:
    """Prospects worth asking, best first. approved = {(platform, login)} already in sources.yaml."""
    dc = cfg.get("discovery") or {}
    lo, hi = dc.get("viewers", [100, 8000])
    since = _iso(now - timedelta(days=days))
    rows = con.execute("""SELECT p.*, COUNT(DISTINCT substr(s.seen_at, 1, 10)) AS days_seen,
                                 AVG(s.viewers) AS avg_viewers, MAX(s.viewers) AS peak, MAX(s.seen_at) AS last_seen,
                                 (SELECT title FROM sightings t WHERE t.platform=p.platform AND t.login=p.login
                                  ORDER BY seen_at DESC LIMIT 1) AS last_title
                          FROM prospects p JOIN sightings s ON s.platform=p.platform AND s.login=p.login
                          WHERE s.seen_at >= ? GROUP BY p.platform, p.login""", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        avg = d["avg_viewers"] or 0
        size = 1.0 if lo <= avg <= hi else (0.5 if avg > hi else 0.3)
        clips = d.get("clip_views_7d") or 0
        hint = bool(CLIP_HINT.search(f"{d.get('bio') or ''} {d.get('last_title') or ''}"))
        d["hint"] = hint
        d["in_sources"] = (d["platform"], d["login"]) in approved
        d["score"] = round((1 + d["days_seen"]) * size * (1 + math.log10(1 + clips) / 2) * (1.5 if hint else 1.0), 2)
        d["size_fit"] = "in range" if size == 1.0 else ("big" if avg > hi else "small")
        out.append(d)
    out.sort(key=lambda d: -d["score"])
    return out


def dm(d: dict) -> str:
    where = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}.get(d["platform"], "")
    return (f"Hey {d['name']}! I run a GTA clip channel and love your {where} streams. Could I have your "
            f"permission to cut your best moments into Shorts/TikToks/Reels? Every clip credits you on screen "
            f"and in the caption, and I'm happy to follow any rules you have (hashtags, platforms, length).")


def sources_entry(d: dict) -> str:
    login = d["login"]
    return (f"  - name: {re.sub(r'[^a-z0-9_]+', '_', d['name'].lower()).strip('_') or d['login']}\n"
            f"    enabled: false              # set true once they've said yes\n"
            f"    platform: {d['platform']}\n"
            f"    login: {login}\n"
            f"    permission:\n"
            f"      type: written\n"
            f"      proof: \"\"                # screenshot path or link to their yes\n"
            f"    rules:\n"
            f"      credit: \"@{d['name']}\"\n"
            f"    facecam: null\n")


def run(cfg: dict, con, helix=None, kick_creds: dict | None = None, yt_key: str | None = None,
        platforms: list[str] | None = None, now: datetime | None = None, clip_checks: int = 40) -> dict:
    """One discovery pass. Returns counts per platform (or the error that stopped it)."""
    setup(con)
    dc = cfg.get("discovery") or {}
    games = dc.get("games", ["Grand Theft Auto VI", "Grand Theft Auto V"])
    langs = dc.get("languages", ["en"])
    now = now or datetime.now(timezone.utc)
    want = platforms or ["twitch", "kick", "youtube"]
    res = {}
    if "twitch" in want and helix is not None:
        try:
            streams = scan_twitch(helix, games, langs)
            record(con, "twitch", streams, now)
            # clip activity for the most promising few (one API call each)
            lo, hi = dc.get("viewers", [100, 8000])
            for s in sorted([s for s in streams if lo <= s["viewers"] <= hi], key=lambda s: -s["viewers"])[:clip_checks]:
                n, v = twitch_clip_activity(helix, s["user_id"], now)
                con.execute("UPDATE prospects SET clips_7d=?, clip_views_7d=?, checked_at=? WHERE platform='twitch' AND login=?",
                            (n, v, _iso(now), s["login"].lower()))
            con.commit()
            res["twitch"] = len(streams)
        except (net.HttpError, RuntimeError, KeyError) as e:
            res["twitch"] = f"failed: {str(e)[:150]}"
    if "kick" in want and kick_creds:
        try:
            tok = kick_token(kick_creds["client_id"], kick_creds["client_secret"])
            streams = scan_kick(tok, games, langs)
            record(con, "kick", streams, now)
            res["kick"] = len(streams)
        except (net.HttpError, RuntimeError, KeyError) as e:
            res["kick"] = f"failed: {str(e)[:150]}"
    if "youtube" in want and yt_key:
        try:
            streams = scan_youtube(yt_key, dc.get("youtube_queries", ["GTA 6 live", "GTA V RP live"]), langs)
            record(con, "youtube", streams, now)
            res["youtube"] = len(streams)
        except (net.HttpError, RuntimeError, KeyError) as e:
            res["youtube"] = f"failed: {str(e)[:150]}"
    return res
