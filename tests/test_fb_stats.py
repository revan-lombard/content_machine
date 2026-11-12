"""Automatic Facebook Reel stats, against a fake Graph API. Run:  python -m pytest -q"""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from clipfarm import config, meta_api, net, publish, tracker
from clipfarm.net import Response

NOW = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
CONF = {"page_token": "PT", "page_id": "P1", "ig_user_id": None}
KNOWN = {"fb_reels_total_plays", "blue_reels_play_count", "post_video_likes_by_reaction_type",
         "post_video_social_actions"}


def J(obj, status=200):
    return Response(status, {}, json.dumps(obj).encode())


class FakeGraph:
    def __init__(self, videos, retired=()):
        self.videos, self.known, self.calls = videos, KNOWN - set(retired), []

    def __call__(self, method, url, headers, data, timeout):
        u = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        self.calls.append((parts[-1], q.get("metric") or q.get("fields")))
        if parts[-1] == "video_insights":
            v = self.videos.get(parts[-2])
            if v is None:
                return J({"error": {"message": "Unsupported get request", "code": 100}}, status=400)
            asked = q["metric"].split(",")
            if not set(asked) <= self.known:
                return J({"error": {"message": "invalid metric", "code": 100}}, status=400)
            vals = {"fb_reels_total_plays": v["plays"] + v["replays"], "blue_reels_play_count": v["plays"],
                    "post_video_likes_by_reaction_type": {"REACTION_LIKE": v["likes"], "REACTION_LOVE": 2},
                    "post_video_social_actions": {"COMMENT": v["comments"], "SHARE": v["shares"]}}
            return J({"data": [{"name": m, "period": "lifetime", "values": [{"value": vals[m]}]} for m in asked]})
        v = self.videos.get(parts[-1])
        if v is not None:  # the video node (likes / comments totals)
            return J({"likes": {"summary": {"total_count": v["likes"]}},
                      "comments": {"summary": {"total_count": v["comments"]}}})
        return J({"error": "no route"}, status=404)


VID = {"111111111": {"plays": 201, "replays": 17, "likes": 30, "comments": 4, "shares": 6},
       "222222222": {"plays": 1000, "replays": 50, "likes": 80, "comments": 9, "shares": 1}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net, "sleep", lambda s: None)


def test_video_id_from_links():
    cases = {"https://www.facebook.com/reel/1234567890123": "1234567890123",
             "https://facebook.com/MyPage/videos/1234567890123/": "1234567890123",
             "https://www.facebook.com/MyPage/videos/a-title/1234567890123": "1234567890123",
             "https://www.facebook.com/watch/?v=1234567890123": "1234567890123",
             "https://fb.watch/abcd/": None, "https://www.instagram.com/reel/X/": None}
    for url, want in cases.items():
        assert meta_api.facebook_video_id(url) == want, url


def test_full_stats_count_replays(monkeypatch):
    monkeypatch.setattr(net, "transport", FakeGraph(VID))
    s = meta_api.facebook_stats(CONF, "111111111", "v23.0")
    # views include replays (matches Facebook's own insights page); reactions summed
    assert s == {"views": 218, "likes": 32, "comments": 4, "shares": 6}


def test_retired_metrics_fall_back(monkeypatch):
    fake = FakeGraph(VID, retired={"fb_reels_total_plays", "post_video_social_actions"})
    monkeypatch.setattr(net, "transport", fake)
    s = meta_api.facebook_stats(CONF, "111111111", "v23.0")
    assert s == {"views": 201, "likes": 30, "comments": 4}   # plays without replays + node totals
    assert ("111111111", "likes.summary(true).limit(0),comments.summary(true).limit(0)") in fake.calls


@pytest.fixture()
def db(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    (tmp_path / "data" / "tokens").mkdir(parents=True)
    (tmp_path / "data" / "tokens" / "meta.json").write_text(json.dumps(CONF))
    for c in ("auto", "hand", "short", "notours"):
        tracker.register_clip(con, c, fmt="loop")
    return con, tmp_path


def test_fetch_auto_and_hand_posted(db, monkeypatch):
    con, root = db
    posted = iso(NOW - timedelta(hours=49))
    tracker.record_post(con, "auto", "facebook", "https://www.facebook.com/reel/111111111", posted,
                        external_id="111111111")
    tracker.record_post(con, "hand", "facebook", "https://www.facebook.com/MyPage/videos/222222222/", posted)
    tracker.record_post(con, "short", "facebook", "https://fb.watch/xyz/", posted)
    tracker.record_post(con, "notours", "facebook", "https://www.facebook.com/reel/999999999", posted)
    monkeypatch.setattr(net, "transport", FakeGraph(VID))
    r = publish.fetch_facebook_stats(config.load(), con, root, now=NOW)
    assert r == {"updated": 2, "matched": 2, "unmatched": 1, "failed": 1}
    views = dict(con.execute("SELECT p.clip_id, s.views FROM stats s JOIN posts p ON p.id=s.post_id").fetchall())
    assert views == {"auto": 218, "hand": 1050}
    assert con.execute("SELECT DISTINCT origin FROM stats").fetchone()[0] == "facebook-api"
    left = {d["clip_id"] for d in tracker.todo(con, 48, now=NOW + timedelta(hours=1))}
    assert left == {"short", "notours"}            # those two still need numbers typed in
    # same schedule as Instagram: nothing new 30 minutes later
    assert publish.fetch_facebook_stats(config.load(), con, root, now=NOW + timedelta(minutes=30))["updated"] == 0


def test_instagram_and_facebook_dont_mix(db, monkeypatch):
    con, root = db
    posted = iso(NOW - timedelta(hours=5))
    tracker.record_post(con, "auto", "facebook", "https://www.facebook.com/reel/111111111", posted,
                        external_id="111111111")
    tracker.record_post(con, "hand", "instagram", "https://www.instagram.com/reel/ABC/", posted, external_id="M1")
    fake = FakeGraph(VID)
    monkeypatch.setattr(net, "transport", fake)
    assert publish.fetch_facebook_stats(config.load(), con, root, now=NOW)["updated"] == 1
    assert all(name != "insights" for name, _ in fake.calls)   # no Instagram calls
    # no Instagram account linked in this connection -> Instagram fetch is a quiet no-op
    assert publish.fetch_instagram_stats(config.load(), con, root, now=NOW)["updated"] == 0
