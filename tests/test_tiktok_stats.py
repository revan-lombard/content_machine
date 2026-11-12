"""Automatic TikTok stats, against a fake TikTok API. Run:  python -m pytest -q"""
import contextlib
import hashlib
import io
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from clipfarm import config, net, publish, stats, tiktok_api, tracker
from clipfarm.net import Response

NOW = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


class FakeTikTok:
    def __init__(self, videos, error=None):
        self.videos, self.error, self.calls = videos, error, []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append({"url": url, "headers": headers, "data": data})
        if "/oauth/token/" in url:
            f = urllib.parse.parse_qs(data.decode())
            if f["grant_type"] == ["refresh_token"]:
                return J({"access_token": "A2", "refresh_token": "R2", "expires_in": 86400,
                          "refresh_expires_in": 31536000})
            return J({"access_token": "A1", "refresh_token": "R1", "open_id": "me", "scope": "user.info.basic,video.list",
                      "expires_in": 86400, "refresh_expires_in": 31536000})
        if "/video/query/" in url:
            if self.error:
                return J({"data": {}, "error": {"code": self.error, "message": "nope"}})
            ids = json.loads(data)["filters"]["video_ids"]
            assert len(ids) <= 20
            vids = [{"id": i, "view_count": v, "like_count": v // 10, "comment_count": v // 100,
                     "share_count": v // 50} for i, v in self.videos.items() if i in ids]
            return J({"data": {"videos": vids}, "error": {"code": "ok", "message": ""}})
        return J({"error": "no route"}, status=404)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


def test_video_id_from_links():
    assert tiktok_api.video_id("https://www.tiktok.com/@my.clips/video/7312345678901234567?is_from_webapp=1") \
        == "7312345678901234567"
    assert tiktok_api.video_id("https://www.tiktok.com/embed/v2/7312345678901234567") == "7312345678901234567"
    assert tiktok_api.video_id("https://vm.tiktok.com/ZMabc123/") is None


def test_pkce_is_hex_sha256():
    v, c = tiktok_api.pkce()
    assert 43 <= len(v) <= 128 and c == hashlib.sha256(v.encode()).hexdigest()
    assert tiktok_api.pkce()[0] != v  # fresh every time


def test_connect_flow(tmp_path, monkeypatch):
    fake = FakeTikTok({})
    monkeypatch.setattr(net, "transport", fake)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    buf, tok = io.StringIO(), tmp_path / "tiktok.json"

    def go():
        with contextlib.redirect_stdout(buf):
            tiktok_api.connect("KEY", "SECRET", tok, port=port, open_browser=False)

    t = threading.Thread(target=go)
    t.start()
    for _ in range(100):
        if "https://" in buf.getvalue():
            break
        time.sleep(0.05)
    auth = next(line for line in buf.getvalue().splitlines() if line.startswith("https://"))
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(auth).query).items()}
    assert q["redirect_uri"] == f"http://localhost:{port}/callback/" and q["code_challenge_method"] == "S256"
    assert set(q["scope"].split(",")) == {"user.info.basic", "video.list"}
    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback/?state={q['state']}&code=CODE").read()
    t.join(5)
    form = urllib.parse.parse_qs(fake.calls[0]["data"].decode())
    assert form["code"] == ["CODE"] and form["redirect_uri"] == [q["redirect_uri"]]
    assert hashlib.sha256(form["code_verifier"][0].encode()).hexdigest() == q["code_challenge"]
    saved = json.loads(tok.read_text())
    assert saved["access_token"] == "A1" and saved["refresh_token"] == "R1"


def test_refresh_rotates_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "transport", FakeTikTok({}))
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R1", "expires_at": 0,
                               "refresh_expires_at": time.time() + 999}))
    assert tiktok_api.access_token("K", "S", tok) == "A2"
    assert json.loads(tok.read_text())["refresh_token"] == "R2"
    tok.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R1", "expires_at": 0, "refresh_expires_at": 0}))
    with pytest.raises(RuntimeError, match="expired"):
        tiktok_api.access_token("K", "S", tok)


def test_query_batches_of_20_and_errors(monkeypatch):
    videos = {str(7000000000000000000 + i): 100 * i for i in range(45)}
    fake = FakeTikTok(videos)
    monkeypatch.setattr(net, "transport", fake)
    out = tiktok_api.query("TOK", list(videos))
    assert len(out) == 45 and len(fake.calls) == 3
    assert out[str(7000000000000000010)] == {"views": 1000, "likes": 100, "comments": 10, "shares": 20}
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer TOK"
    monkeypatch.setattr(net, "transport", FakeTikTok({}, error="scope_not_authorized"))
    with pytest.raises(RuntimeError, match="scope_not_authorized"):
        tiktok_api.query("TOK", ["1"])


@pytest.fixture()
def db(tmp_path, monkeypatch):
    con = tracker.connect(tmp_path / "t.db")
    (tmp_path / "data" / "tokens").mkdir(parents=True)
    (tmp_path / "data" / "tokens" / "tiktok.json").write_text(json.dumps({
        "access_token": "A1", "refresh_token": "R1", "expires_at": time.time() + 9999,
        "refresh_expires_at": time.time() + 99999}))
    monkeypatch.setattr(publish, "_secrets", lambda: {"tiktok": {"client_key": "K", "client_secret": "S"}})
    for c in ("mine", "short", "theirs", "old"):
        tracker.register_clip(con, c, fmt="loop")
    return con, tmp_path


def test_fetch_hand_logged_tiktoks(db, monkeypatch):
    con, root = db
    posted = iso(NOW - timedelta(hours=49))
    tracker.record_post(con, "mine", "tiktok", "https://www.tiktok.com/@me/video/7311111111111111111", posted)
    tracker.record_post(con, "short", "tiktok", "https://vm.tiktok.com/ZMabc/", posted)
    tracker.record_post(con, "theirs", "tiktok", "https://www.tiktok.com/@someone/video/7399999999999999999", posted)
    monkeypatch.setattr(net, "transport", FakeTikTok({"7311111111111111111": 12345}))
    r = stats.fetch_tiktok_stats(config.load(), con, root, now=NOW)
    assert r == {"updated": 1, "matched": 2, "unmatched": 1, "failed": 1}
    row = con.execute("SELECT views, likes, shares, origin FROM stats").fetchone()
    assert tuple(row) == (12345, 1234, 246, "tiktok-api")
    # scored with no typing; the short link and the other account's video still need numbers
    assert {d["clip_id"] for d in tracker.todo(con, 48, now=NOW + timedelta(hours=1))} == {"short", "theirs"}


def test_fetch_schedule_and_one_request_for_many(db, monkeypatch):
    con, root = db
    tracker.record_post(con, "mine", "tiktok", "https://www.tiktok.com/@me/video/7311111111111111111",
                        iso(NOW - timedelta(hours=5)))
    tracker.record_post(con, "old", "tiktok", "https://www.tiktok.com/@me/video/7322222222222222222",
                        iso(NOW - timedelta(days=4)))
    fake = FakeTikTok({"7311111111111111111": 10, "7322222222222222222": 20})
    monkeypatch.setattr(net, "transport", fake)
    run = lambda t: stats.fetch_tiktok_stats(config.load(), con, root, now=t)["updated"]
    assert run(NOW) == 2 and len(fake.calls) == 1        # both in one request
    assert run(NOW + timedelta(minutes=30)) == 0          # nothing due
    assert run(NOW + timedelta(minutes=61)) == 1          # young post: hourly
    assert run(NOW + timedelta(hours=24)) == 2            # old post: daily


def test_not_connected_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_secrets", lambda: {})
    con = tracker.connect(tmp_path / "t.db")
    assert stats.fetch_tiktok_stats(config.load(), con, tmp_path)["updated"] == 0
