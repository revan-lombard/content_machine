"""X auto-posting, against a fake X API. Run:  python -m pytest -q"""
import base64
import contextlib
import io
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from clipfarm import config, net, publish, stats, tracker, x_api
from clipfarm.net import Response

MB = 1024 * 1024
NOW = datetime.now(timezone.utc).replace(microsecond=0, minute=0).replace(hour=15)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


class FakeX:
    def __init__(self, states=("pending", "in_progress", "succeeded")):
        self.calls, self.segments, self.states = [], [], iter(states)

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        path = urllib.parse.urlparse(url).path
        if path.endswith("/oauth2/token"):
            f = urllib.parse.parse_qs(data.decode())
            if f["grant_type"] == ["refresh_token"]:
                return J({"access_token": "A2", "refresh_token": "R2", "expires_in": 7200,
                          "scope": "tweet.read tweet.write users.read media.write offline.access"})
            return J({"access_token": "A1", "refresh_token": "R1", "expires_in": 7200,
                      "scope": "tweet.read tweet.write users.read media.write offline.access"})
        if path.endswith("/media/upload/initialize"):
            assert json.loads(data)["media_category"] == "tweet_video"
            return J({"data": {"id": "M77"}})
        if path.endswith("/append"):
            body = data.decode("latin-1")
            idx = int(body.split('name="segment_index"\r\n\r\n')[1].split("\r\n")[0])
            payload = data.split(b"application/octet-stream\r\n\r\n")[1].rsplit(b"\r\n--", 1)[0]
            self.segments.append((idx, len(payload)))
            return Response(204, {}, b"")
        if path.endswith("/finalize"):
            return J({"data": {"id": "M77", "processing_info": {"state": next(self.states), "check_after_secs": 1}}})
        if path.endswith("/media/upload") and method == "GET":
            st = next(self.states)
            info = {"state": st, "check_after_secs": 1}
            if st == "failed":
                info["error"] = {"message": "InvalidMedia"}
            return J({"data": {"processing_info": info}})
        if path.endswith("/2/tweets") and method == "POST":
            return J({"data": {"id": "1866666666666666666", "text": json.loads(data)["text"]}}, status=201)
        return J({"error": "no route"}, status=404)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


def test_connect_confidential_and_public_clients(tmp_path, monkeypatch):
    for secret in ("SEC", None):
        fake = FakeX()
        monkeypatch.setattr(net, "transport", fake)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        buf, tok = io.StringIO(), tmp_path / f"x{bool(secret)}.json"

        def go():
            with contextlib.redirect_stdout(buf):
                x_api.connect("CID", secret, tok, port=port, open_browser=False)

        t = threading.Thread(target=go)
        t.start()
        for _ in range(100):
            if "https://" in buf.getvalue():
                break
            time.sleep(0.05)
        auth = next(line for line in buf.getvalue().splitlines() if line.startswith("https://"))
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(auth).query).items()}
        assert "media.write" in q["scope"] and "offline.access" in q["scope"]
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?state={q['state']}&code=C").read()
        t.join(5)
        _, _, headers, data = fake.calls[0]
        form = urllib.parse.parse_qs(data.decode())
        assert form["code_verifier"] and form["redirect_uri"] == [f"http://127.0.0.1:{port}/callback"]
        if secret:
            assert headers["Authorization"] == "Basic " + base64.b64encode(b"CID:SEC").decode()
        else:
            assert form["client_id"] == ["CID"] and "Authorization" not in headers
        assert x_api.has_scopes(tok)


def test_refresh_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "transport", FakeX())
    tok = tmp_path / "x.json"
    tok.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R1", "expires_at": 0}))
    assert x_api.access_token("CID", None, tok) == "A2"
    assert json.loads(tok.read_text())["refresh_token"] == "R2"


def test_upload_chunks_and_waits_for_processing(tmp_path, monkeypatch):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"\x02" * (9 * MB))
    fake = FakeX()
    monkeypatch.setattr(net, "transport", fake)
    assert x_api.upload_video("TOK", v) == "M77"
    assert fake.segments == [(0, 4 * MB), (1, 4 * MB), (2, 1 * MB)]
    assert sum(1 for c in fake.calls if c[0] == "GET") == 2          # polled until "succeeded"
    monkeypatch.setattr(net, "transport", FakeX(states=("pending", "failed")))
    with pytest.raises(RuntimeError, match="InvalidMedia"):
        x_api.upload_video("TOK", v)


def test_post_strips_links_and_attaches_video(tmp_path, monkeypatch):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 100)
    fake = FakeX(states=("succeeded",))
    monkeypatch.setattr(net, "transport", fake)
    tid, url = x_api.post("TOK", v, "He jumped the dam https://twitch.tv/x | @streamer #gta6 www.example.com")
    assert (tid, url) == ("1866666666666666666", "https://x.com/i/status/1866666666666666666")
    body = json.loads(next(c[3] for c in fake.calls if c[1].endswith("/2/tweets")))
    assert body == {"text": "He jumped the dam | @streamer #gta6", "media": {"media_ids": ["M77"]}}
    assert len(x_api.clean_text("a" * 400)) == 280


def test_ready_needs_posting_scopes(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_secrets", lambda: {"x": {"client_id": "CID"}})
    (tmp_path / "data" / "tokens").mkdir(parents=True)
    c = publish.Clients(config.load(), tmp_path)
    assert "connect x" in c.ready("x")
    (tmp_path / "data" / "tokens" / "x.json").write_text(json.dumps({"scope": "tweet.read users.read"}))
    assert "connect x" in c.ready("x")
    (tmp_path / "data" / "tokens" / "x.json").write_text(json.dumps({"scope": "tweet.write media.write"}))
    assert c.ready("x") is None


class XClients:
    def __init__(self):
        self.posted = []

    def ready(self, p):
        return None

    def post(self, platform, video, texts, tags):
        self.posted.append(video.parent.name)
        n = len(self.posted)
        return str(n), f"https://x.com/i/status/{n}"


def test_monthly_post_limit(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    cfg = config.load()
    cfg["publish"]["hours"] = [0, 24]
    cfg["publish"]["min_gap_minutes"] = 0
    cfg["x"]["monthly_post_limit"] = 2
    for name in "abc":
        d = tmp_path / "out" / name
        d.mkdir(parents=True)
        (d / "final.mp4").write_bytes(b"v")
        (d / "script.yaml").write_text(yaml.safe_dump({"kind": "moment", "title": f"T {name}", "hashtags": []}))
        tracker.register_clip(con, name, path=str(d))
        con.execute("UPDATE clips SET status='approved', created=? WHERE id=?", (name, name))
    con.commit()
    xc = XClients()
    for i in range(3):
        res = publish.run(cfg, con, tmp_path, clients=xc, platforms=["x"], now=NOW + timedelta(minutes=i))
    assert xc.posted == ["a", "b"] and "monthly X post limit" in res[0]["skipped"]
    assert con.execute("SELECT COUNT(*) FROM api_spend WHERE platform='x'").fetchone()[0] == 2


def test_stats_read_as_signed_in_user(tmp_path, monkeypatch):
    con = tracker.connect(tmp_path / "t.db")
    tracker.register_clip(con, "a")
    tracker.record_post(con, "a", "x", "https://x.com/me/status/1855555555555555555", iso(NOW - timedelta(hours=50)))
    (tmp_path / "data" / "tokens").mkdir(parents=True)
    (tmp_path / "data" / "tokens" / "x.json").write_text("{}")
    monkeypatch.setattr(publish, "_secrets", lambda: {"x": {"bearer_token": "APP", "client_id": "CID"}})
    monkeypatch.setattr(x_api, "access_token", lambda cid, sec, f: "USERTOK")
    used = []
    monkeypatch.setattr(x_api, "metrics", lambda tok, ids: used.append(tok) or
                        {i: {"views": 5, "likes": 0, "comments": 0, "shares": 0} for i in ids})
    assert stats.fetch_x_stats(config.load(), con, tmp_path, now=NOW)["updated"] == 1
    assert used == ["USERTOK"]   # your own posts read as you, not with the app token
