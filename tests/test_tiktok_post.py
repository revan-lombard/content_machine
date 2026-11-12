"""TikTok auto-posting (inbox drafts + direct), against a fake TikTok API. Run:  python -m pytest -q"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from clipfarm import config, net, publish, server, tiktok_api, tracker
from clipfarm.net import Response

MB = 1024 * 1024
NOW = datetime.now(timezone.utc).replace(microsecond=0, minute=0).replace(hour=15)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")


def J(data=None, code="ok", status=200):
    return Response(status, {}, json.dumps({"data": data or {}, "error": {"code": code, "message": code}}).encode())


class FakeTikTok:
    def __init__(self, privacy=("PUBLIC_TO_EVERYONE", "SELF_ONLY"), statuses=("PROCESSING_UPLOAD", "PUBLISH_COMPLETE"),
                 init_error=None):
        self.calls, self.chunks, self.privacy = [], [], list(privacy)
        self.statuses, self.init_error = iter(statuses), init_error

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        if method == "PUT":
            self.chunks.append((headers["Content-Range"], len(data)))
            return Response(206, {}, b"")
        body = json.loads(data or b"{}")
        if "inbox/video/init" in url or "/publish/video/init" in url:
            if self.init_error:
                return J(code=self.init_error)
            return J({"publish_id": "v_pub_1", "upload_url": "https://upload.tiktok.test/x"})
        if "creator_info" in url:
            return J({"creator_username": "myclips", "privacy_level_options": self.privacy})
        if "status/fetch" in url:
            st = next(self.statuses)
            d = {"status": st}
            if st == "PUBLISH_COMPLETE":
                d["publicaly_available_post_id"] = [7300000000000000001]
            if st == "FAILED":
                d["fail_reason"] = "duration_check"
            return J(d)
        return J(code="not_found")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


def test_chunk_plan():
    assert tiktok_api.chunk_plan(3 * MB) == (3 * MB, 1)
    assert tiktok_api.chunk_plan(25 * MB) == (10 * MB, 2)   # last chunk carries the remainder (15 MB)
    assert tiktok_api.chunk_plan(30 * MB) == (10 * MB, 3)


def test_send_to_inbox_uploads_every_byte(tmp_path, monkeypatch):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"\x01" * (25 * MB + 123))
    fake = FakeTikTok()
    monkeypatch.setattr(net, "transport", fake)
    assert tiktok_api.send_to_inbox("TOK", v) == "v_pub_1"
    init = json.loads(fake.calls[0][3])["source_info"]
    assert init == {"source": "FILE_UPLOAD", "video_size": 25 * MB + 123, "chunk_size": 10 * MB, "total_chunk_count": 2}
    size = 25 * MB + 123
    assert fake.chunks == [(f"bytes 0-{10 * MB - 1}/{size}", 10 * MB),
                           (f"bytes {10 * MB}-{size - 1}/{size}", size - 10 * MB)]
    assert fake.calls[0][2]["Authorization"] == "Bearer TOK"


def test_direct_post_public_and_private(tmp_path, monkeypatch):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 1000)
    monkeypatch.setattr(net, "transport", FakeTikTok())
    pid, url = tiktok_api.direct_post("TOK", v, "caption #gta6")
    assert (pid, url) == ("7300000000000000001", "https://www.tiktok.com/@myclips/video/7300000000000000001")
    fake = FakeTikTok(privacy=("SELF_ONLY",))  # unaudited app: only private allowed
    monkeypatch.setattr(net, "transport", fake)
    tiktok_api.direct_post("TOK", v, "c")
    post_info = next(json.loads(c[3])["post_info"] for c in fake.calls if "publish/video/init" in c[1])
    assert post_info["privacy_level"] == "SELF_ONLY"
    monkeypatch.setattr(net, "transport", FakeTikTok(statuses=("FAILED",)))
    with pytest.raises(RuntimeError, match="duration_check"):
        tiktok_api.direct_post("TOK", v, "c")


def test_too_many_pending_drafts_is_an_error(tmp_path, monkeypatch):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    monkeypatch.setattr(net, "transport", FakeTikTok(init_error="spam_risk_too_many_pending_share"))
    with pytest.raises(RuntimeError, match="too_many_pending"):
        tiktok_api.send_to_inbox("TOK", v)


def test_ready_needs_the_posting_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_secrets", lambda: {"tiktok": {"client_key": "K", "client_secret": "S"}})
    (tmp_path / "data" / "tokens").mkdir(parents=True)
    tok = tmp_path / "data" / "tokens" / "tiktok.json"
    tok.write_text(json.dumps({"scope": "user.info.basic,video.list"}))
    c = publish.Clients(config.load(), tmp_path)
    assert "connect tiktok" in c.ready("tiktok")          # stats-only connection: reconnect needed
    tok.write_text(json.dumps({"scope": "user.info.basic,video.list,video.upload"}))
    assert c.ready("tiktok") is None
    assert tiktok_api.scopes_for("inbox") == "user.info.basic,video.list,video.upload"
    assert tiktok_api.scopes_for("direct").endswith("video.publish")


# ---------------------------------------------------------------- publisher + claiming

class InboxClients:
    def __init__(self):
        self.sent = []

    def ready(self, p):
        return None

    def post(self, platform, video, texts, tags):
        self.sent.append(video.parent.name)
        return f"pub_{video.parent.name}", None   # inbox: a draft, not a post


@pytest.fixture()
def setup(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    cfg = config.load()
    cfg["publish"]["hours"] = [0, 24]
    cfg["publish"]["min_gap_minutes"] = 0
    for name in ("a", "b", "c"):
        d = tmp_path / "out" / name
        d.mkdir(parents=True)
        (d / "final.mp4").write_bytes(b"v")
        (d / "script.yaml").write_text(yaml.safe_dump({"kind": "moment", "title": f"Clip {name} jumps the dam",
                                                       "description": "d", "hashtags": ["#gta6"]}))
        tracker.register_clip(con, name, path=str(d), title=f"Clip {name}")
        con.execute("UPDATE clips SET status='approved', created=? WHERE id=?", (name, name))
    con.commit()
    return con, cfg, tmp_path


def test_inbox_sends_are_pending_not_posts(setup):
    con, cfg, root = setup
    cfg["publish"]["tiktok"]["daily_max"] = 2
    ic = InboxClients()
    run = lambda t: publish.run(cfg, con, root, clients=ic, platforms=["tiktok"], now=t)
    assert run(NOW)[0]["inbox"] and run(NOW + timedelta(minutes=5))[0]["inbox"]
    assert ic.sent == ["a", "b"]
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0      # not posted yet
    assert con.execute("SELECT COUNT(*) FROM pending_posts").fetchone()[0] == 2
    assert "daily max" in run(NOW + timedelta(minutes=10))[0]["skipped"]       # drafts count toward the cap
    assert ic.sent == ["a", "b"]                                              # nothing re-sent


def test_claim_matches_posted_drafts(setup):
    con, cfg, root = setup
    ic = InboxClients()
    for i in range(2):
        publish.run(cfg, con, root, clients=ic, platforms=["tiktok"], now=NOW + timedelta(minutes=i))
    ts = NOW.timestamp()
    vids = [
        {"id": "900", "video_description": "something else entirely", "create_time": ts + 100},
        {"id": "902", "video_description": "Clip b jumps the dam | GTA 6 #gta6 #fyp", "create_time": ts + 3000,
         "share_url": "https://www.tiktok.com/@me/video/902"},
        {"id": "901", "video_description": "clip a jumps the DAM!! #gta6", "create_time": ts + 2000},
    ]
    n = publish.claim_tiktok(cfg, con, root, now=NOW + timedelta(hours=2), list_fn=lambda since: vids)
    assert n == 2
    posts = {r["clip_id"]: (r["url"], r["external_id"]) for r in con.execute("SELECT * FROM posts")}
    assert posts["a"] == ("https://www.tiktok.com/video/901", "901")
    assert posts["b"] == ("https://www.tiktok.com/@me/video/902", "902")
    assert con.execute("SELECT COUNT(*) FROM pending_posts").fetchone()[0] == 0
    # already-logged videos aren't claimed twice
    assert publish.claim_tiktok(cfg, con, root, list_fn=lambda since: vids) == 0


def test_claim_lone_draft_with_edited_caption(setup):
    con, cfg, root = setup
    publish.run(cfg, con, root, clients=InboxClients(), platforms=["tiktok"], now=NOW)
    vids = [{"id": "777", "video_description": "totally rewritten caption", "create_time": NOW.timestamp() + 60}]
    assert publish.claim_tiktok(cfg, con, root, list_fn=lambda s: vids) == 1


def test_pasting_the_link_by_hand_clears_the_draft(setup):
    con, cfg, root = setup
    publish.run(cfg, con, root, clients=InboxClients(), platforms=["tiktok"], now=NOW)
    page = server.review_page(con, False, ["tiktok"]).decode()
    assert "In your TikTok inbox" in page
    tracker.record_post(con, "a", "tiktok", "https://www.tiktok.com/@me/video/1")
    assert con.execute("SELECT COUNT(*) FROM pending_posts").fetchone()[0] == 0
