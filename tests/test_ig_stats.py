"""Automatic Instagram stats, against a fake Meta API. Run:  python -m pytest -q"""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from clipfarm import config, meta_api, net, publish, tracker
from clipfarm.net import Response

NOW = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
CONF = {"page_token": "PT", "ig_user_id": "IG1", "page_id": "P1"}


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


class FakeMeta:
    """Minimal Graph API: an account with media, insights per media id."""

    def __init__(self, media, views, bad_metrics=()):
        self.media, self.views, self.bad, self.calls = media, views, set(bad_metrics), []

    def __call__(self, method, url, headers, data, timeout):
        u = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.calls.append(u.path + ("?" + q.get("metric", q.get("fields", "")) if q else ""))
        parts = u.path.strip("/").split("/")
        if parts[-1] == "media" and parts[-2] == "IG1":
            page = int(q.get("page", 0))
            chunk = self.media[page * 2:(page + 1) * 2]
            nxt = f"https://graph.facebook.com/v23.0/IG1/media?page={page + 1}&access_token=PT" \
                if (page + 1) * 2 < len(self.media) else None
            return J({"data": chunk, "paging": {"next": nxt} if nxt else {}})
        if parts[-1] == "insights":
            mid = parts[-2]
            asked = q["metric"].split(",")
            if self.bad & set(asked):
                return J({"error": {"message": "invalid metric", "code": 100}}, status=400)
            v = self.views[mid]
            data = []
            for m in asked:
                if m == "views":
                    data.append({"name": "views", "values": [{"value": v}]})
                elif m in ("likes", "comments", "shares"):
                    data.append({"name": m, "total_value": {"value": v // {"likes": 10, "comments": 100, "shares": 50}[m]}})
            return J({"data": data})
        if len(parts) == 2:  # a media node
            v = self.views[parts[-1]]
            return J({"like_count": v // 10, "comments_count": v // 100})
        return J({"error": "no route"}, status=404)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


def test_shortcode_from_links():
    for url in ("https://www.instagram.com/reel/C9xYz-1_aB/", "https://instagram.com/reels/C9xYz-1_aB?igsh=abc",
                "https://www.instagram.com/myclips/reel/C9xYz-1_aB/", "https://www.instagram.com/p/C9xYz-1_aB"):
        assert meta_api.shortcode(url) == "C9xYz-1_aB", url
    assert meta_api.shortcode("https://tiktok.com/@x/video/1") is None


def test_stats_full_set(monkeypatch):
    monkeypatch.setattr(net, "transport", FakeMeta([], {"M1": 5000}))
    assert meta_api.instagram_stats(CONF, "M1", "v23.0") == {"views": 5000, "likes": 500, "comments": 50, "shares": 100}


def test_stats_falls_back_when_a_metric_is_retired(monkeypatch):
    fake = FakeMeta([], {"M1": 5000}, bad_metrics={"saved"})
    monkeypatch.setattr(net, "transport", fake)
    s = meta_api.instagram_stats(CONF, "M1", "v23.0")
    assert s == {"views": 5000, "likes": 500, "comments": 50}   # likes/comments from the media node
    assert any("views,likes" in c for c in fake.calls) and any(c.endswith("?views") for c in fake.calls)


def test_recent_media_follows_pages(monkeypatch):
    media = [{"id": f"M{i}", "shortcode": f"code{i}"} for i in range(5)]
    monkeypatch.setattr(net, "transport", FakeMeta(media, {}))
    assert meta_api.recent_media(CONF, "v23.0") == {f"code{i}": f"M{i}" for i in range(5)}


@pytest.fixture()
def db(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    (tmp_path / "data" / "tokens").mkdir(parents=True)
    (tmp_path / "data" / "tokens" / "meta.json").write_text(json.dumps(CONF))
    for c in ("auto", "hand", "other", "old"):
        tracker.register_clip(con, c, fmt="loop")
    return con, tmp_path


def test_fetch_all_kinds_of_posts(db, monkeypatch):
    con, root = db
    cfg = config.load()
    posted = iso(NOW - timedelta(hours=50))
    tracker.record_post(con, "auto", "instagram", "https://www.instagram.com/reel/AUTO1/", posted, external_id="M1")
    tracker.record_post(con, "hand", "instagram", "https://www.instagram.com/reel/HAND2/?igsh=x", posted)
    tracker.record_post(con, "other", "instagram", "https://www.instagram.com/reel/NOTMINE/", posted)
    fake = FakeMeta([{"id": "M2", "shortcode": "HAND2"}, {"id": "M1", "permalink": "https://www.instagram.com/reel/AUTO1/"}],
                    {"M1": 1000, "M2": 9000})
    monkeypatch.setattr(net, "transport", fake)
    r = publish.fetch_instagram_stats(cfg, con, root, now=NOW)
    assert r == {"updated": 2, "matched": 1, "unmatched": 1, "failed": 0}
    assert con.execute("SELECT external_id FROM posts WHERE clip_id='hand'").fetchone()[0] == "M2"
    views = dict(con.execute("SELECT p.clip_id, s.views FROM stats s JOIN posts p ON p.id=s.post_id").fetchall())
    assert views == {"auto": 1000, "hand": 9000}
    # scored at 48h with no typing; only the link from another account still needs numbers
    assert [d["clip_id"] for d in tracker.todo(con, 48, now=NOW + timedelta(hours=1))] == ["other"]


def test_fetch_schedule_hourly_then_daily(db, monkeypatch):
    con, root = db
    cfg = config.load()
    tracker.record_post(con, "auto", "instagram", "https://instagram.com/reel/A/", iso(NOW - timedelta(hours=10)),
                        external_id="M1")
    tracker.record_post(con, "old", "instagram", "https://instagram.com/reel/B/", iso(NOW - timedelta(days=5)),
                        external_id="M2")
    monkeypatch.setattr(net, "transport", FakeMeta([], {"M1": 10, "M2": 20}))
    run = lambda t: publish.fetch_instagram_stats(cfg, con, root, now=t)["updated"]
    assert run(NOW) == 2                              # first time: both
    assert run(NOW + timedelta(minutes=30)) == 0      # too soon for either
    assert run(NOW + timedelta(minutes=61)) == 1      # young post: hourly
    assert run(NOW + timedelta(hours=24)) == 2        # old post: daily


def test_not_connected_is_quiet(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    assert publish.fetch_instagram_stats(config.load(), con, tmp_path) == \
        {"updated": 0, "matched": 0, "unmatched": 0, "failed": 0}
