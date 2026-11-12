"""Outreach: drafts, queue, limits, follow-ups, senders. Run:  python -m pytest -q"""
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from clipfarm import config, discover, net, outreach, pipeline, server, sources, tracker, writer
from clipfarm.net import Response

NOW = datetime.now(timezone.utc).replace(microsecond=0, minute=0).replace(hour=15)
BIO = "GTA RP daily! Business: Steady.Biz@gmail.com | discord.gg/steadyrp | x.com/steady_rp"


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


@pytest.fixture()
def cfg():
    c = config.load()
    c["outreach"].update(your_name="Revan", your_channel="youtube.com/@gtaclips", hours=[0, 24], min_gap_minutes=0)
    return c


@pytest.fixture()
def con(tmp_path):
    c = tracker.connect(tmp_path / "t.db")
    discover.setup(c)
    outreach.setup(c)
    return c


def prospect(con, login="steady", bio=BIO, platform="twitch", days=6, status="new"):
    for i in range(days):
        discover.record(con, platform, [{"login": login, "name": login.title(), "viewers": 800, "bio": bio,
                                         "title": "GTA 6 RP - bank heist night", "url": f"https://x/{login}"}],
                        NOW - timedelta(days=i))
    con.execute("UPDATE prospects SET status=? WHERE login=?", (status, login))
    con.commit()
    return next(d for d in discover.rank(con, config.load(), set(), NOW) if d["login"] == login)


def test_contacts_from_bio():
    assert outreach.contacts(BIO) == {"email": "Steady.Biz@gmail.com", "discord": "discord.gg/steadyrp", "x": "steady_rp"}
    assert outreach.contacts("just vibes") == {"email": None, "discord": None, "x": None}


def test_drafts_are_personal_short_enough_and_offer_an_opt_out(cfg, con):
    d = prospect(con)
    e = outreach.draft(cfg, d, "first", "email")
    assert "Steady" in e["body"] and "6 times" in e["body"] and "bank heist" in e["body"]
    assert "Revan" in e["body"] and "youtube.com/@gtaclips" in e["body"] and "rather not hear" in e["body"]
    w = outreach.draft(cfg, d, "first", "whisper")
    assert len(w["body"]) <= 500 and "rather not hear" in w["body"]
    f = outreach.draft(cfg, d, "followup", "email")
    assert "won't follow up again" in f["body"]


def test_ai_rewrite_that_drops_the_opt_out_is_rejected(cfg, con, monkeypatch):
    cfg["llm"]["provider"] = "lmstudio"
    d = prospect(con)
    monkeypatch.setattr(writer, "_ask", lambda c, p: {"text": "Yo! Let me clip you, it'll be huge!!"})
    assert "rather not hear" in outreach.draft(cfg, d, "first", "email")["body"]   # fell back to the template
    monkeypatch.setattr(writer, "_ask", lambda c, p: {"text": "Hi Steady, mind if I clip you? "
                                                               "(If you'd rather not hear from me, just say so.)"})
    assert outreach.draft(cfg, d, "first", "email")["body"].startswith("Hi Steady, mind")


def test_queue_rules(cfg, con):
    d = prospect(con)
    outreach.queue(cfg, con, d, "email", NOW)
    with pytest.raises(ValueError, match="already queued"):
        outreach.queue(cfg, con, d, "whisper", NOW)
    with pytest.raises(ValueError, match="'no'"):
        outreach.queue(cfg, con, prospect(con, "nope", status="no"), "email", NOW)
    with pytest.raises(ValueError, match="no public email"):
        outreach.queue(cfg, con, prospect(con, "quiet", bio="no contact here"), "email", NOW)
    assert outreach.channels_for(prospect(con, "quiet", bio=""), {"email": True, "whisper": True}) == ["whisper"]
    assert outreach.channels_for(prospect(con, "kk", platform="kick"), {"email": True, "whisper": True}) == ["email"]


def test_send_limits_and_status(cfg, con):
    sent = []
    senders = {"email": lambda r: sent.append(r["to_addr"]), "whisper": lambda r: sent.append("w:" + r["to_addr"])}
    cfg["outreach"]["daily_max"] = 2
    cfg["outreach"]["min_gap_minutes"] = 10
    for i, login in enumerate(["a", "b", "c"]):
        outreach.queue(cfg, con, prospect(con, login, bio=f"mail {login}@x.com"), "email", NOW)
    assert outreach.send_due(cfg, con, senders, NOW, dry_run=True)[0]["would_send"] == "email" and not sent
    outreach.send_due(cfg, con, senders, NOW)
    assert sent == ["a@x.com"]
    assert "gap" in outreach.send_due(cfg, con, senders, NOW + timedelta(minutes=5))[0]["skipped"]
    outreach.send_due(cfg, con, senders, NOW + timedelta(minutes=11))
    assert "daily limit" in outreach.send_due(cfg, con, senders, NOW + timedelta(minutes=30))[0]["skipped"]
    assert sent == ["a@x.com", "b@x.com"]
    assert con.execute("SELECT status FROM prospects WHERE login='a'").fetchone()[0] == "contacted"
    cfg["outreach"]["hours"] = [(NOW.astimezone().hour + 2) % 24, (NOW.astimezone().hour + 3) % 24 or 24]
    assert "sending hours" in outreach.send_due(cfg, con, senders, NOW + timedelta(days=1))[0]["skipped"]


def test_answer_cancels_and_followup_only_once(cfg, con):
    sent = []
    senders = {"email": lambda r: sent.append((r["to_addr"], r["kind"]))}
    for login in ("quiet", "replied"):
        outreach.queue(cfg, con, prospect(con, login, bio=f"{login}@x.com"), "email", NOW)
        outreach.send_due(cfg, con, senders, NOW + timedelta(minutes=len(sent)))
    con.execute("UPDATE prospects SET status='yes' WHERE login='replied'")
    con.commit()
    assert outreach.send_due(cfg, con, senders, NOW + timedelta(days=3)) == []          # too early to follow up
    outreach.send_due(cfg, con, senders, NOW + timedelta(days=6))
    assert sent[-1] == ("quiet@x.com", "followup") and ("replied@x.com", "followup") not in sent
    outreach.send_due(cfg, con, senders, NOW + timedelta(days=20))
    assert sum(1 for s in sent if s[0] == "quiet@x.com") == 2                          # never a third message


def test_marking_no_cancels_a_queued_message(cfg, con):
    outreach.queue(cfg, con, prospect(con), "email", NOW)
    con.execute("UPDATE prospects SET status='no' WHERE login='steady'")
    con.commit()
    outreach.send_due(cfg, con, {"email": lambda r: pytest.fail("must not send")}, NOW)
    assert con.execute("SELECT status FROM outreach").fetchone()[0] == "cancelled"


def test_failure_is_recorded_not_retried(cfg, con):
    outreach.queue(cfg, con, prospect(con), "email", NOW)
    calls = []

    def bad(r):
        calls.append(1)
        raise RuntimeError("535 bad credentials")

    outreach.send_due(cfg, con, {"email": bad}, NOW)
    outreach.send_due(cfg, con, {"email": bad}, NOW + timedelta(hours=1))
    assert len(calls) == 1
    assert con.execute("SELECT status, error FROM outreach").fetchone()[:] == ("failed", "535 bad credentials")


def test_email_sender(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            seen["tls"] = True

        def login(self, u, p):
            seen["login"] = (u, p)

        def send_message(self, m):
            seen["msg"] = m

    monkeypatch.setattr(outreach.smtplib, "SMTP", FakeSMTP)
    send = outreach.email_sender({"username": "me@gmail.com", "password": "app-pass", "from_name": "Revan"})
    send({"to_addr": "them@x.com", "subject": "Hi", "body": "Hello there"})
    assert seen["host"] == ("smtp.gmail.com", 587) and seen["tls"] and seen["login"] == ("me@gmail.com", "app-pass")
    m = seen["msg"]
    assert m["To"] == "them@x.com" and "Revan" in m["From"] and m.get_content().strip() == "Hello there"


def test_whisper_sender(tmp_path, monkeypatch):
    tok = tmp_path / "tu.json"
    tok.write_text(json.dumps({"access_token": "UT", "refresh_token": "R", "user_id": "me1",
                               "expires_at": time.time() + 999}))
    calls = []

    def fake(method, url, headers, data, timeout):
        calls.append((method, url, data))
        if "/users" in url:
            return J({"data": [{"id": "them9"}]})
        if "/whispers" in url:
            return Response(204, {}, b"")
        return J({}, 404)

    monkeypatch.setattr(net, "transport", fake)
    outreach.whisper_sender("CID", "SEC", tok)({"to_addr": "steady", "body": "x" * 600})
    method, url, data = calls[-1]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q == {"from_user_id": "me1", "to_user_id": "them9"} and len(json.loads(data)["message"]) == 500


def test_discover_tab_queue_and_cancel(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(sources, "load_all", lambda path=None: [])
    monkeypatch.setattr(server, "_outreach_ready", lambda: {"email": True})
    con = pipeline.db()
    discover.setup(con)
    for i in range(3):
        discover.record(con, "twitch", [{"login": "steady", "name": "Steady", "viewers": 700, "bio": BIO,
                                         "title": "GTA RP", "url": "https://www.twitch.tv/steady"}],
                        datetime.now(timezone.utc) - timedelta(days=i))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda path, body: urllib.request.urlopen(urllib.request.Request(
        base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
    try:
        page = urllib.request.urlopen(base + "/discover").read().decode()
        assert "Queue email" in page and "Queue whisper" not in page and "Steady.Biz@gmail.com" in page
        post("/api/outreach/queue", {"platform": "twitch", "login": "steady", "channel": "email"})
        page = urllib.request.urlopen(base + "/discover").read().decode()
        assert "Queued (email, first)" in page and "1</b> queued" in page
        with pytest.raises(urllib.error.HTTPError):
            post("/api/outreach/queue", {"platform": "twitch", "login": "steady", "channel": "whisper"})
        mid = con.execute("SELECT id FROM outreach").fetchone()[0]
        post("/api/outreach/cancel", {"id": mid})
        assert con.execute("SELECT status FROM outreach").fetchone()[0] == "cancelled"
    finally:
        srv.shutdown()
