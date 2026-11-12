"""Automatic X stats, against a fake X API. Run:  python -m pytest -q"""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from clipfarm import config, net, publish, stats, tracker, x_api
from clipfarm.net import Response

NOW = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


class FakeX:
    def __init__(self, posts, status=200):
        self.posts, self.status, self.calls = posts, status, []

    def __call__(self, method, url, headers, data, timeout):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        ids = q["ids"].split(",")
        self.calls.append({"ids": ids, "headers": headers, "q": q})
        if self.status != 200:
            return J({"title": "CreditsDepleted"}, status=self.status)
        data, media, errors = [], [], []
        for i in ids:
            p = self.posts.get(i)
            if p is None:
                errors.append({"resource_id": i, "title": "Not Found Error"})
                continue
            t = {"id": i, "public_metrics": {"impression_count": p["imp"], "like_count": 7, "reply_count": 2,
                                             "retweet_count": 3, "quote_count": 1}}
            if "video" in p:
                t["attachments"] = {"media_keys": [f"m{i}"]}
                media.append({"media_key": f"m{i}", "public_metrics": {"view_count": p["video"]}})
            data.append(t)
        body = {"data": data, "includes": {"media": media}}
        if errors:
            body["errors"] = errors
        return J(body)


def test_post_id_from_links():
    assert x_api.post_id("https://x.com/myclips/status/1855555555555555555?s=20") == "1855555555555555555"
    assert x_api.post_id("https://twitter.com/myclips/status/1855555555555555555") == "1855555555555555555"
    assert x_api.post_id("https://x.com/myclips") is None


def test_metrics(monkeypatch):
    fake = FakeX({"1": {"imp": 900}, "2": {"imp": 500, "video": 800}, "3": {"imp": 900, "video": 100}})
    monkeypatch.setattr(net, "transport", fake)
    m = x_api.metrics("BEARER", ["1", "2", "3", "404"])
    assert m["1"] == {"views": 900, "likes": 7, "comments": 2, "shares": 4}
    assert m["2"]["views"] == 800 and m["3"]["views"] == 900   # larger of impressions / video views
    assert "404" not in m
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer BEARER"
    assert fake.calls[0]["q"]["tweet.fields"] == "public_metrics"


def test_metrics_batches_of_100(monkeypatch):
    posts = {str(i): {"imp": i} for i in range(1, 251)}
    fake = FakeX(posts)
    monkeypatch.setattr(net, "transport", fake)
    assert len(x_api.metrics("B", list(posts))) == 250 and len(fake.calls) == 3


@pytest.fixture()
def db(tmp_path, monkeypatch):
    con = tracker.connect(tmp_path / "t.db")
    monkeypatch.setattr(publish, "_secrets", lambda: {"x": {"bearer_token": "B"}})
    for c in ("a", "b", "c", "d"):
        tracker.register_clip(con, c, fmt="loop")
    return con, tmp_path


def test_reads_only_at_checkpoints(db, monkeypatch):
    con, root = db
    tracker.record_post(con, "a", "x", "https://x.com/me/status/111111", iso(NOW))
    fake = FakeX({"111111": {"imp": 4000}})
    monkeypatch.setattr(net, "transport", fake)
    run = lambda h: stats.fetch_x_stats(config.load(), con, root, now=NOW + timedelta(hours=h))["updated"]
    assert [run(h) for h in (1, 24, 47.9)] == [0, 0, 0]   # nothing before 48h: no money spent
    assert run(48.5) == 1                                  # the 48h read (used for the score)
    assert [run(h) for h in (49, 72, 100)] == [0, 0, 0]
    assert run(170) == 1                                   # the 7-day read
    assert run(400) == 0
    assert len(fake.calls) == 2                            # two paid reads in total
    # the post is scored from the 48h read, nothing to type in
    assert tracker.todo(con, 48, now=NOW + timedelta(hours=60)) == []


def test_missed_checkpoint_reads_once_late(db, monkeypatch):
    con, root = db
    tracker.record_post(con, "a", "x", "https://x.com/me/status/111111", iso(NOW - timedelta(hours=200)))
    monkeypatch.setattr(net, "transport", FakeX({"111111": {"imp": 1}}))
    # PC was off for both checkpoints: one read catches up, not two
    assert stats.fetch_x_stats(config.load(), con, root, now=NOW)["updated"] == 1
    assert stats.fetch_x_stats(config.load(), con, root, now=NOW + timedelta(hours=1))["updated"] == 0


def test_monthly_cap_and_unmatched(db, monkeypatch):
    con, root = db
    cfg = config.load()
    cfg["x"]["monthly_read_limit"] = 2
    posted = iso(NOW - timedelta(hours=50))
    for c, url in (("a", "https://x.com/me/status/100000"), ("b", "https://x.com/me/status/200000"),
                   ("c", "https://x.com/me/status/300000"), ("d", "https://x.com/me")):
        tracker.record_post(con, c, "x", url, posted)
    fake = FakeX({"100000": {"imp": 10}, "200000": {"imp": 20}, "300000": {"imp": 30}})
    monkeypatch.setattr(net, "transport", fake)
    r = stats.fetch_x_stats(cfg, con, root, now=NOW)
    assert r["updated"] == 2 and r["capped"] == 1 and r["unmatched"] == 1
    assert len(fake.calls[0]["ids"]) == 2                  # only paid for what the cap allows


def test_no_credits_is_reported_not_crashing(db, monkeypatch, capsys):
    con, root = db
    tracker.record_post(con, "a", "x", "https://x.com/me/status/100000", iso(NOW - timedelta(hours=50)))
    monkeypatch.setattr(net, "transport", FakeX({}, status=402))
    r = stats.fetch_x_stats(config.load(), con, root, now=NOW)
    assert r["failed"] == 1 and "no credits" in capsys.readouterr().out


def test_not_configured_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_secrets", lambda: {})
    con = tracker.connect(tmp_path / "t.db")
    assert stats.fetch_x_stats(config.load(), con, tmp_path)["updated"] == 0
