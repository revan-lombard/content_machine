"""Format testing + performance tracking. Run:  python -m pytest -q"""
import json
import random
import subprocess
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest
import yaml

from clipfarm import config, ff, formats, pipeline, server, stats, tracker, twitch

T0 = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def con(tmp_path):
    return tracker.connect(tmp_path / "t.db")


def add_post(con, cid, fmt, plat, views, hook="question", hours_old=60, snap_at=48):
    tracker.register_clip(con, cid, fmt=fmt, hook_style=hook)
    posted = T0 - timedelta(hours=hours_old)
    pid = tracker.record_post(con, cid, plat, f"https://x.test/{cid}/{plat}", posted_at=iso(posted))
    if views is not None:
        tracker.add_stats(con, pid, views, 10, 2, 1, fetched_at=iso(posted + timedelta(hours=snap_at)))
    return pid


# ---------------------------------------------------------------- scoring

def test_status_pending_needs_stats_scored(con):
    add_post(con, "a", "loop", "tiktok", None, hours_old=10)       # too young
    add_post(con, "b", "loop", "tiktok", None, hours_old=60)       # old, no numbers
    add_post(con, "c", "loop", "tiktok", 500, hours_old=60)        # scored
    add_post(con, "d", "loop", "tiktok", 500, hours_old=60, snap_at=5)  # only a 5h snapshot: too early
    st = {d["clip_id"]: d["status"] for d in tracker.post_scores(con, 48, now=T0)}
    assert st == {"a": "pending", "b": "needs_stats", "c": "scored", "d": "needs_stats"}
    assert [d["clip_id"] for d in tracker.todo(con, 48, now=T0)] == ["b", "d"]


def test_uses_snapshot_closest_to_48h(con):
    pid = add_post(con, "a", "loop", "youtube", 100, snap_at=30)
    tracker.add_stats(con, pid, 900, fetched_at=iso(T0 - timedelta(hours=60) + timedelta(hours=47)))
    tracker.add_stats(con, pid, 5000, fetched_at=iso(T0 - timedelta(hours=60) + timedelta(hours=58)))
    d = tracker.post_scores(con, 48, now=T0)[0]
    assert d["views"] == 900


def test_wins_are_relative_to_each_platform(con):
    # YouTube reach is 10x TikTok here; format "big" wins on both relative to its platform
    for i in range(6):
        add_post(con, f"yt{i}", "big" if i % 2 else "small", "youtube", (10000 if i % 2 else 1000) + i)
        add_post(con, f"tt{i}", "big" if i % 2 else "small", "tiktok", (1000 if i % 2 else 100) + i)
    st = tracker.arm_stats(con, "format", ["big", "small"])
    assert st["big"]["win_rate"] > 0.6 > 0.4 > st["small"]["win_rate"]


# ---------------------------------------------------------------- choosing

def test_warmup_rotates_evenly(con):
    cfg = config.load()
    arms = ["a", "b", "c"]
    rng = random.Random(1)
    for i in range(30):
        tracker.register_clip(con, f"c{i}", fmt=tracker.choose(con, cfg, "format", arms, rng))
    counts = {a: con.execute("SELECT COUNT(*) FROM clips WHERE format=?", (a,)).fetchone()[0] for a in arms}
    assert counts == {"a": 10, "b": 10, "c": 10}


def test_after_warmup_winner_gets_most_but_not_all(con):
    cfg = config.load()
    cfg["experiments"]["min_scored"] = 5
    for i in range(10):
        add_post(con, f"w{i}", "winner", "youtube", 5000 + i)
        add_post(con, f"l{i}", "loser", "youtube", 100 + i)
    share, warming = tracker.mix(con, cfg, "format", ["winner", "loser"])
    assert not warming
    assert share["winner"] > 0.8 and share["loser"] >= 0.05  # exploration keeps the loser alive
    assert abs(sum(share.values()) - 1) < 1e-9


# ---------------------------------------------------------------- stats

def test_youtube_id():
    assert stats.youtube_id("https://youtube.com/shorts/abcDEF12345?feature=share") == "abcDEF12345"
    assert stats.youtube_id("https://www.youtube.com/watch?v=abcDEF12345") == "abcDEF12345"
    assert stats.youtube_id("https://youtu.be/abcDEF12345") == "abcDEF12345"
    assert stats.youtube_id("https://tiktok.com/@me/video/1") is None


def test_fetch_youtube(con):
    tracker.register_clip(con, "a")
    pid = tracker.record_post(con, "a", "youtube", "https://youtube.com/shorts/abcDEF12345")
    seen = []

    def fake(url):
        seen.append(url)
        return {"items": [{"id": "abcDEF12345", "statistics": {"viewCount": "1234", "likeCount": "56"}}]}

    assert stats.fetch_youtube(con, api_key="k", fetch=fake) == 1
    assert "abcDEF12345" in seen[0]
    row = con.execute("SELECT * FROM stats WHERE post_id=?", (pid,)).fetchone()
    assert (row["views"], row["likes"], row["origin"]) == (1234, 56, "youtube-api")


def test_record_post_validates(con):
    with pytest.raises(ValueError):
        tracker.record_post(con, "missing", "youtube", "https://y")
    tracker.register_clip(con, "a")
    with pytest.raises(ValueError):
        tracker.record_post(con, "a", "myspace", "https://y")


# ---------------------------------------------------------------- formats (rendering)

@pytest.fixture(scope="module")
def video(tmp_path_factory):
    v = tmp_path_factory.mktemp("v") / "v.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=90",
                    "-f", "lavfi", "-i", "sine=f=300:d=90", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(v)], check=True)
    return v


@pytest.mark.parametrize("fmt,lo,hi", [("payoff_first", 19, 20.5), ("straight", 17.5, 18.5),
                                       ("beat_story", 17.5, 18.5), ("loop", 8.5, 9.5)])
def test_single_formats_render(video, tmp_path, fmt, lo, hi):
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    beats = formats.render_single(cfg, fmt, video, 45.0, 90.0, tmp_path / fmt, "hook!")
    d = ff.duration(tmp_path / fmt / "rough.mp4")
    assert lo <= d <= hi, d
    assert beats[0]["text"] == "hook!"
    assert not list((tmp_path / fmt).glob("p*.mp4"))  # temp parts cleaned up
    if fmt == "beat_story":
        assert beats[1]["text"] == "Wait for it..." and beats[1]["end"] <= d


def test_countdown_renders(video, tmp_path):
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    beats = formats.render_countdown(cfg, [(video, 20, 90), (video, 50, 90), (video, 80, 90)], tmp_path, "Top 3")
    assert [b["text"] for b in beats] == ["Top 3", "#3", "#2", "#1"]
    assert 28 <= ff.duration(tmp_path / "rough.mp4") <= 31


# ---------------------------------------------------------------- end to end: Twitch with experiments on

class FakeHelix:
    def user_id(self, login):
        return "42"

    def vods(self, uid, n=1):
        return [self.vod("9")]

    def vod(self, vod_id):
        return {"id": "9", "title": "GTA 6", "duration": "0h1m30s", "created_at": "2026-11-19T10:00:00Z"}

    def clips(self, uid, a, b):
        return [{"video_id": "9", "vod_offset": o, "duration": 20, "view_count": v, "title": t}
                for o, v, t in ((5, 50, "the bridge jump was insane"), (35, 300, "GTA 6"), (60, 90, "no way"))]


def test_twitch_with_experiments(video, tmp_path, monkeypatch):
    def fake_download(vod_id, start, end, out, max_height=1080):
        ff.run(["-ss", f"{start}", "-t", f"{end - start}", "-i", video, "-c", "copy", out])
        return out

    monkeypatch.setattr(twitch, "download_section", fake_download)
    monkeypatch.setattr(twitch, "download_chat", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    cfg["moments"]["min_gap"] = 15
    src = {"name": "s", "login": "s", "permission": {"type": "written", "proof": "x"}, "rules": {}}
    _, dirs = pipeline.build_twitch(cfg, src, helix=FakeHelix(), rng=random.Random(3))
    metas = [yaml.safe_load((d / "script.yaml").read_text()) for d in dirs]
    single = [m for m in metas if m["format"] != "countdown"]
    # warm-up rotation: 3 moments -> 3 different formats
    assert len({m["format"] for m in single}) == 3
    assert all(m["format"] in formats.SINGLE for m in single)
    # the countdown is made in warm-up (it has the fewest clips), from the 3 moments
    assert any(m["format"] == "countdown" for m in metas)
    # viewer_title is only used where a usable title exists; "ai" never without an AI configured
    for m in single:
        assert m["hook_style"] in ("viewer_title", "question", "curiosity")
    con = pipeline.db()
    assert con.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == len(dirs)

    # finish one clip, then drive the web app: review page, log a post, enter stats, dashboard
    pipeline.finish(cfg, dirs[0], use_captions=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert metas[0]["clip_id"] in page and "Posted" in page
        req = urllib.request.Request(base + "/media?clip=" + metas[0]["clip_id"], headers={"Range": "bytes=0-99"})
        r = urllib.request.urlopen(req)
        assert r.status == 206 and len(r.read()) == 100

        def post(path, body):
            return urllib.request.urlopen(urllib.request.Request(
                base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))

        post("/api/post", {"clip_id": metas[0]["clip_id"], "platform": "tiktok", "url": "https://tiktok.com/@me/video/1"})
        pid = con.execute("SELECT id FROM posts").fetchone()[0]
        post("/api/stats", {"post_id": pid, "views": 4321, "likes": 100})
        assert con.execute("SELECT views FROM stats").fetchone()[0] == 4321
        with pytest.raises(urllib.error.HTTPError):
            post("/api/post", {"clip_id": metas[0]["clip_id"], "platform": "tiktok", "url": "not a link"})
        post("/api/status", {"clip_id": metas[0]["clip_id"], "status": "approved"})
        assert con.execute("SELECT status FROM clips WHERE id=?", (metas[0]["clip_id"],)).fetchone()[0] == "approved"
        assert "approved" in urllib.request.urlopen(base + "/").read().decode()
        dash = urllib.request.urlopen(base + "/dashboard").read().decode()
        assert "Formats" in dash and "Hook styles" in dash and "Warming up" in dash
    finally:
        srv.shutdown()
