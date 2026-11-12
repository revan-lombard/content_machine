"""Streamer discovery, against fake Twitch / Kick / YouTube APIs. Run:  python -m pytest -q"""
import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest
import yaml

from clipfarm import config, discover, net, pipeline, server, sources, tracker
from clipfarm.net import Response

NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


class FakeHelix:
    cid, token = "CID", "TOK"

    def __init__(self, streams):
        self.streams, self.calls = streams, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "games":
            return {"data": [{"id": "G6"}]} if params["name"] == "Grand Theft Auto VI" else {"data": []}
        if path == "streams":
            return {"data": self.streams, "pagination": {}}
        if path == "clips":
            n = {"u1": 12, "u2": 0}.get(params["broadcaster_id"], 1)
            return {"data": [{"view_count": 100}] * n}
        raise AssertionError(path)


def tw(login, uid, viewers, lang="en", title="GTA 6 day 3"):
    return {"user_login": login, "user_name": login.title(), "user_id": uid, "viewer_count": viewers,
            "title": title, "language": lang, "game_name": "Grand Theft Auto VI"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


@pytest.fixture()
def con(tmp_path):
    c = tracker.connect(tmp_path / "t.db")
    discover.setup(c)
    return c


def test_twitch_scan_records_bios_and_clip_activity(con, monkeypatch):
    helix = FakeHelix([tw("midsize", "u1", 900), tw("tiny", "u2", 20), tw("huge", "u3", 60000),
                       tw("espanol", "u4", 500, lang="es")])
    monkeypatch.setattr(discover, "_users", lambda h, ids: {"u1": "GTA RP daily | clippers welcome, DM on Discord"})
    res = discover.run(config.load(), con, helix=helix, platforms=["twitch"], now=NOW)
    assert res == {"twitch": 3}                                    # Spanish stream filtered out (languages: en)
    row = con.execute("SELECT * FROM prospects WHERE login='midsize'").fetchone()
    assert row["clips_7d"] == 12 and row["clip_views_7d"] == 1200 and "clippers" in row["bio"]
    checked = [p["broadcaster_id"] for path, p in helix.calls if path == "clips"]
    assert checked == ["u1"]                                       # only in-range streamers get the extra call


def test_rank_prefers_consistent_mid_size_clippable(con):
    cfg = config.load()
    def seen(login, days, viewers, bio="", plat="twitch"):
        for d in range(days):
            discover.record(con, plat, [{"login": login, "viewers": viewers, "title": "GTA", "bio": bio,
                                         "url": f"https://x/{login}"}], NOW - timedelta(days=d))
    seen("steady", 8, 700, bio="clips welcome")
    seen("oneoff", 1, 900)
    seen("massive", 8, 50000)
    seen("tiny", 8, 15)
    seen("already", 8, 700)
    con.execute("UPDATE prospects SET clip_views_7d=5000 WHERE login='steady'")
    ranked = discover.rank(con, cfg, {("twitch", "already")}, NOW)
    order = [d["login"] for d in ranked]
    assert order[0] == "steady"
    assert order.index("massive") > order.index("steady") and order.index("tiny") > order.index("massive")
    by = {d["login"]: d for d in ranked}
    assert by["steady"]["hint"] and by["steady"]["days_seen"] == 8 and by["massive"]["size_fit"] == "big"
    assert by["already"]["in_sources"] and not by["steady"]["in_sources"]
    # sightings older than 14 days don't count
    discover.record(con, "twitch", [{"login": "ancient", "viewers": 500}], NOW - timedelta(days=30))
    assert "ancient" not in [d["login"] for d in discover.rank(con, cfg, set(), NOW)]


def test_kick_scan_falls_back_on_sort_value(con, monkeypatch):
    calls = []

    def fake(method, url, headers, data, timeout):
        calls.append(url)
        if "oauth/token" in url:
            return J({"access_token": "KT"})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        if "/categories" in url:
            return J({"data": [{"id": 15, "name": "Grand Theft Auto V"}, {"id": 99, "name": "Grand Strategy"}]})
        if "/livestreams" in url:
            if q.get("sort") == "viewer_count":
                return J({"message": "Invalid request"}, status=400)
            assert q["category_id"] == "15" and headers["Authorization"] == "Bearer KT"
            return J({"data": [{"slug": "kicker", "viewer_count": 1200, "stream_title": "GTA RP", "language": "en"},
                               {"channel": {"slug": "nested"}, "viewer_count": 300, "language": "en"}]})
        return J({}, 404)

    monkeypatch.setattr(net, "transport", fake)
    res = discover.run(config.load(), con, kick_creds={"client_id": "a", "client_secret": "b"},
                       platforms=["kick"], now=NOW)
    assert res == {"kick": 2}
    assert {r[0] for r in con.execute("SELECT login FROM prospects WHERE platform='kick'")} == {"kicker", "nested"}


def test_youtube_scan(con, monkeypatch):
    def fake(method, url, headers, data, timeout):
        if "/search" in url:
            return J({"items": [{"id": {"videoId": "v1"}, "snippet": {}}, {"id": {"videoId": "v2"}, "snippet": {}}]})
        return J({"items": [
            {"id": "v1", "snippet": {"channelId": "UC1", "channelTitle": "RP Guy", "title": "GTA 6 RP live"},
             "liveStreamingDetails": {"concurrentViewers": "2300"}},
            {"id": "v2", "snippet": {"channelId": "UC1", "channelTitle": "RP Guy"}, "liveStreamingDetails": {}}]})
    monkeypatch.setattr(net, "transport", fake)
    res = discover.run(config.load(), con, yt_key="K", platforms=["youtube"], now=NOW)
    assert res == {"youtube": 1}                                  # one channel even with two live videos
    row = con.execute("SELECT * FROM prospects WHERE platform='youtube'").fetchone()
    assert row["url"] == "https://www.youtube.com/channel/UC1"


def test_failures_dont_stop_other_platforms(con, monkeypatch):
    def boom(method, url, headers, data, timeout):
        return J({"error": "down"}, 503)
    monkeypatch.setattr(net, "transport", boom)
    res = discover.run(config.load(), con, kick_creds={"client_id": "a", "client_secret": "b"}, yt_key="K",
                       platforms=["kick", "youtube"], now=NOW)
    assert res["kick"].startswith("failed") and res["youtube"].startswith("failed")


def test_dm_and_sources_entry_stay_locked_until_yes():
    d = {"platform": "kick", "login": "kicker", "name": "Kicker"}
    assert "permission" in discover.dm(d) and "Kick" in discover.dm(d)
    entry = yaml.safe_load("sources:\n" + discover.sources_entry(d))["sources"][0]
    assert entry["login"] == "kicker" and entry["platform"] == "kick"
    probs = sources.problems(entry)
    assert "enabled is false" in probs and any("proof" in p for p in probs)   # can't be used until filled in


def test_discover_tab_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(sources, "load_all", lambda path=None: [])
    con = pipeline.db()
    discover.setup(con)
    discover.record(con, "twitch", [{"login": "steady", "name": "Steady", "viewers": 700, "title": "GTA RP",
                                     "url": "https://www.twitch.tv/steady"}], datetime.now(timezone.utc))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config.load()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/discover").read().decode()
        assert "Steady" in page and "Copy message" in page and "Copy sources.yaml entry" in page
        urllib.request.urlopen(urllib.request.Request(base + "/api/prospect", data=json.dumps(
            {"platform": "twitch", "login": "steady", "status": "no"}).encode(), headers={"Content-Type": "application/json"}))
        assert "Steady" not in urllib.request.urlopen(base + "/discover").read().decode()   # said no -> hidden
        assert "Steady" in urllib.request.urlopen(base + "/discover?all=1").read().decode()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(base + "/api/prospect", data=json.dumps(
                {"platform": "twitch", "login": "steady", "status": "maybe"}).encode()))
    finally:
        srv.shutdown()
