"""Auto-posting, against fake YouTube / Meta servers. Run:  python -m pytest -q"""
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from clipfarm import config, meta_api, net, publish, tracker, youtube_api
from clipfarm.net import Response

NOW = datetime.now(timezone.utc).replace(microsecond=0, minute=0).replace(hour=15)


class FakeNet:
    """Records requests and answers them from a route table: (method, url-substring) -> Response."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        for (m, frag), resp in self.routes:
            if m == method and frag in url:
                r = resp(url, headers, data) if callable(resp) else resp
                return r
        return Response(404, {}, b'{"error":"no route"}')


def J(obj, status=200, headers=None):
    return Response(status, headers or {}, json.dumps(obj).encode())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


@pytest.fixture()
def video(tmp_path):
    v = tmp_path / "final.mp4"
    v.write_bytes(b"\x00" * 1234)
    return v


# ---------------------------------------------------------------- YouTube protocol

def test_youtube_refresh_and_resumable_upload(tmp_path, video, monkeypatch):
    tok = tmp_path / "yt.json"
    tok.write_text(json.dumps({"refresh_token": "R", "access_token": "old", "expires_at": 0}))
    fake = FakeNet([
        (("POST", "oauth2.googleapis.com/token"), J({"access_token": "NEW", "expires_in": 3600})),
        (("POST", "upload/youtube/v3/videos"), J({}, headers={"location": "https://upload.test/session1"})),
        (("PUT", "upload.test/session1"), J({"id": "abcDEF12345"})),
    ])
    monkeypatch.setattr(net, "transport", fake)
    access = youtube_api.access_token("cid", "sec", tok)
    assert access == "NEW" and json.loads(tok.read_text())["access_token"] == "NEW"
    vid, url = youtube_api.upload(access, video, "T" * 150, "desc", ["#gta6", "#shorts"], "public")
    assert (vid, url) == ("abcDEF12345", "https://youtube.com/shorts/abcDEF12345")
    start, put = fake.calls[1], fake.calls[2]
    meta = json.loads(start["data"])
    assert len(meta["snippet"]["title"]) == 100 and meta["snippet"]["tags"] == ["gta6", "shorts"]
    assert meta["status"]["privacyStatus"] == "public"
    assert start["headers"]["X-Upload-Content-Length"] == "1234"
    assert put["data"] == video.read_bytes() and put["headers"]["Authorization"] == "Bearer NEW"


# ---------------------------------------------------------------- Meta protocol

def test_meta_connect_picks_page_and_instagram(tmp_path, monkeypatch):
    fake = FakeNet([
        (("GET", "/oauth/access_token"), J({"access_token": "LONG"})),
        (("GET", "/me/accounts"), J({"data": [{"id": "P1", "name": "My Clips", "access_token": "PAGETOK",
                                               "instagram_business_account": {"id": "IG1"}}]})),
    ])
    monkeypatch.setattr(net, "transport", fake)
    conf = meta_api.connect("app", "secret", "SHORT", "v23.0", tmp_path / "meta.json")
    assert conf == {"page_id": "P1", "page_name": "My Clips", "page_token": "PAGETOK", "ig_user_id": "IG1"}
    assert "LONG" in fake.calls[1]["url"]


def test_instagram_reel_upload_waits_then_publishes(video, monkeypatch):
    states = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    fake = FakeNet([
        (("POST", "/IG1/media_publish"), J({"id": "MEDIA9"})),
        (("POST", "/IG1/media"), J({"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/C1"})),
        (("POST", "rupload.facebook.com"), J({"success": True})),
        (("GET", "/C1"), lambda u, h, d: J({"status_code": next(states)})),
        (("GET", "/MEDIA9"), J({"permalink": "https://www.instagram.com/reel/XYZ/"})),
    ])
    monkeypatch.setattr(net, "transport", fake)
    conf = {"page_token": "PT", "ig_user_id": "IG1", "page_id": "P1"}
    mid, link = meta_api.post_instagram(conf, video, "caption #gta6", "v23.0")
    assert (mid, link) == ("MEDIA9", "https://www.instagram.com/reel/XYZ/")
    create = urllib.parse.parse_qs(fake.calls[0]["data"].decode())
    assert create["media_type"] == ["REELS"] and create["upload_type"] == ["resumable"]
    up = fake.calls[1]
    assert up["headers"]["Authorization"] == "OAuth PT" and up["headers"]["file_size"] == "1234"
    assert up["data"] == video.read_bytes()


def test_instagram_processing_error_raises(video, monkeypatch):
    fake = FakeNet([
        (("POST", "/IG1/media"), J({"id": "C1"})),
        (("POST", "rupload.facebook.com"), J({"success": True})),
        (("GET", "/C1"), J({"status_code": "ERROR", "status": "bad codec"})),
    ])
    monkeypatch.setattr(net, "transport", fake)
    with pytest.raises(RuntimeError, match="bad codec"):
        meta_api.post_instagram({"page_token": "PT", "ig_user_id": "IG1"}, video, "c", "v23.0")


def test_facebook_reel_three_phases(video, monkeypatch):
    fake = FakeNet([
        (("POST", "/P1/video_reels"), lambda u, h, d: J(
            {"video_id": "V7", "upload_url": "https://rupload.facebook.com/video-upload/v23.0/V7"}
            if b"upload_phase=start" in d else {"success": True})),
        (("POST", "rupload.facebook.com/video-upload"), J({"success": True})),
    ])
    monkeypatch.setattr(net, "transport", fake)
    vid, url = meta_api.post_facebook({"page_token": "PT", "page_id": "P1"}, video, "cap", "v23.0")
    assert (vid, url) == ("V7", "https://www.facebook.com/reel/V7")
    finish = urllib.parse.parse_qs(fake.calls[2]["data"].decode())
    assert finish["upload_phase"] == ["finish"] and finish["video_state"] == ["PUBLISHED"]
    assert finish["description"] == ["cap"]


def test_http_errors_hide_tokens(monkeypatch):
    monkeypatch.setattr(net, "transport", FakeNet([(("GET", "x.test"), J({"error": "nope"}, status=400))]))
    with pytest.raises(net.HttpError) as e:
        net.request("GET", "https://x.test/a", params={"access_token": "SECRET"})
    assert "SECRET" not in str(e.value)


# ---------------------------------------------------------------- publishing rules

class FakeClients:
    def __init__(self, fail=()):
        self.posted, self.fail = [], set(fail)

    def ready(self, platform):
        return None

    def post(self, platform, video, texts, tags):
        if platform in self.fail:
            raise RuntimeError("server said no")
        self.posted.append((platform, video.parent.name))
        return f"id-{platform}-{video.parent.name}", f"https://{platform}.test/{video.parent.name}"


@pytest.fixture()
def setup(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    cfg = config.load()
    cfg["publish"]["hours"] = [0, 24]
    src_file = tmp_path / "sources.yaml"
    src_file.write_text(yaml.safe_dump({"sources": [
        {"name": "ok", "login": "ok_streamer", "permission": {"type": "campaign", "proof": "x"},
         "rules": {"platforms": ["youtube", "instagram"]}}]}))

    def clip(name, status="approved", login="ok_streamer", created=None):
        d = tmp_path / "out" / name
        d.mkdir(parents=True)
        (d / "final.mp4").write_bytes(b"v")
        meta = {"kind": "moment", "title": f"T {name}", "description": "d", "hashtags": ["#gta6"],
                "credit": "@x", "source": {"platform": "twitch", "login": login}}
        (d / "script.yaml").write_text(yaml.safe_dump(meta))
        tracker.register_clip(con, name, path=str(d), title=meta["title"])
        con.execute("UPDATE clips SET status=?, created=? WHERE id=?", (status, created or name, name))
        con.commit()
    return con, cfg, clip, src_file


def run(cfg, con, clients, src_file, now=NOW, **kw):
    return publish.run(cfg, con, Path("."), clients=clients, now=now, sources_file=src_file, **kw)


def test_only_approved_oldest_first_one_per_platform(setup):
    con, cfg, clip, sf = setup
    clip("b1")
    clip("a1")
    clip("c1", status="new")
    clip("d1", status="rejected")
    fc = FakeClients()
    res = run(cfg, con, fc, sf, platforms=["youtube"])
    assert fc.posted == [("youtube", "a1")]
    assert res[0]["url"] == "https://youtube.test/a1"
    post = con.execute("SELECT * FROM posts").fetchone()
    assert (post["clip_id"], post["platform"], post["external_id"]) == ("a1", "youtube", "id-youtube-a1")


def test_campaign_platform_rule_and_revoked_source(setup):
    con, cfg, clip, sf = setup
    clip("a1")
    clip("b2", login="not_listed")
    fc = FakeClients()
    res = run(cfg, con, fc, sf, platforms=["facebook"])
    assert fc.posted == []  # a1: campaign allows only youtube+instagram; b2: no permission
    reasons = " ".join(r.get("skipped", "") for r in res)
    assert "campaign rules allow only" in reasons and "no longer approved" in reasons


def test_gap_daily_cap_and_hours(setup):
    con, cfg, clip, sf = setup
    for n in ("a", "b", "c", "d"):
        clip(n)
    cfg["publish"]["min_gap_minutes"] = 60
    cfg["publish"]["youtube"]["daily_max"] = 2
    fc = FakeClients()
    run(cfg, con, fc, sf, platforms=["youtube"])
    assert "gap" in run(cfg, con, fc, sf, now=NOW + timedelta(minutes=30), platforms=["youtube"])[0]["skipped"]
    run(cfg, con, fc, sf, now=NOW + timedelta(minutes=61), platforms=["youtube"])
    assert "daily max" in run(cfg, con, fc, sf, now=NOW + timedelta(minutes=130), platforms=["youtube"])[0]["skipped"]
    assert [p[1] for p in fc.posted] == ["a", "b"]
    run(cfg, con, fc, sf, now=NOW + timedelta(hours=25), platforms=["youtube"])
    assert [p[1] for p in fc.posted] == ["a", "b", "c"]
    local_hour = NOW.astimezone().hour
    cfg["publish"]["hours"] = [(local_hour + 1) % 24, (local_hour + 2) % 24 or 24]
    assert "posting hours" in run(cfg, con, fc, sf, now=NOW + timedelta(hours=50), platforms=["youtube"])[0]["skipped"]


def test_failures_retry_then_give_up(setup):
    con, cfg, clip, sf = setup
    clip("a1")
    fc = FakeClients(fail={"instagram"})
    for i in range(4):
        res = run(cfg, con, fc, sf, now=NOW + timedelta(hours=3 * i), platforms=["instagram"])
    assert con.execute("SELECT COUNT(*) FROM publish_attempts").fetchone()[0] == 3
    assert res[0]["skipped"] == "nothing approved left to post"


def test_dry_run_posts_nothing(setup):
    con, cfg, clip, sf = setup
    clip("a1")
    fc = FakeClients()
    res = run(cfg, con, fc, sf, platforms=["youtube", "tiktok"], dry_run=True)
    assert fc.posted == [] and res[0]["would_post"] and "manual" in res[1]["skipped"]
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


def test_status_validation(setup):
    con, cfg, clip, sf = setup
    clip("a1", status="new")
    tracker.set_status(con, "a1", "approved")
    with pytest.raises(ValueError):
        tracker.set_status(con, "a1", "maybe")
    with pytest.raises(ValueError):
        tracker.set_status(con, "zzz", "approved")
