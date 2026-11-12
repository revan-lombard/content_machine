"""Performance by source (streamer / source platform). Run:  python -m pytest -q"""
from datetime import datetime, timedelta, timezone

import yaml

from clipfarm import config, server, tracker

NOW = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")


def add(con, cid, source, plat, views, fmt="loop"):
    tracker.register_clip(con, cid, source=source, source_platform=plat, fmt=fmt)
    posted = NOW - timedelta(hours=60)
    pid = tracker.record_post(con, cid, "youtube", f"https://youtube.com/shorts/{cid}", iso(posted))
    if views is not None:
        tracker.add_stats(con, pid, views, views // 20, 1, 1, fetched_at=iso(posted + timedelta(hours=48)))


def test_source_platform_of():
    assert tracker.source_platform_of({"source": {"platform": "kick", "login": "n"}}) == "kick"
    assert tracker.source_platform_of({"sources": [{"platform": "twitch"}, {"platform": "youtube"}]}) == "mixed"
    assert tracker.source_platform_of({"sources": [{"platform": "kick"}, {"platform": "kick"}]}) == "kick"
    assert tracker.source_platform_of({"kind": "moment"}) == "own"


def test_stats_by_platform_and_streamer(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    for i in range(12):   # neon on Kick does great, alpha on Twitch poorly
        add(con, f"n{i}", "n3on", "kick", 5000 + i)
        add(con, f"a{i}", "alpha", "twitch", 100 + i)
    add(con, "r1", "alpha, n3on", "mixed", 3000)          # a reaction clip counts for both streamers
    add(con, "new", "yt", "youtube", None)                # posted, no numbers yet
    ss = tracker.source_stats(con)
    p, st = ss["platform"], ss["streamer"]
    assert p["kick"]["clips"] == 12 and p["kick"]["win_rate"] > 0.8
    assert p["twitch"]["win_rate"] < 0.2
    assert p["youtube"]["clips"] == 1 and p["youtube"]["scored"] == 0 and p["youtube"]["win_rate"] is None
    assert st["n3on"]["clips"] == 13 and st["alpha"]["clips"] == 13      # + the shared reaction clip
    assert st["n3on"]["total_views"] == sum(5000 + i for i in range(12)) + 3000
    tips = tracker.source_advice(st)
    assert any(t.startswith("n3on") and "more often" in t for t in tips)
    assert any(t.startswith("alpha") and "swapping" in t for t in tips)
    assert not any(t.startswith("yt") for t in tips)                     # not enough data: no advice


def test_backfill_from_script_yaml(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    d = tmp_path / "out" / "c1"
    d.mkdir(parents=True)
    (d / "script.yaml").write_text(yaml.safe_dump({"source": {"platform": "youtube", "login": "@x"}}))
    con.execute("INSERT INTO clips (id, created, source, path) VALUES ('c1', ?, '@x', ?)", (iso(NOW), str(d)))
    con.commit()
    assert tracker.backfill_source_platforms(con) == 1
    assert con.execute("SELECT source_platform FROM clips").fetchone()[0] == "youtube"


def test_dashboard_shows_sources(tmp_path):
    con = tracker.connect(tmp_path / "t.db")
    for i in range(3):
        add(con, f"n{i}", "n3on", "kick", 5000)
    html = server.dashboard_page(con, config.load()).decode()
    assert "Sources" in html and "By platform" in html and "By streamer" in html
    assert "Kick" in html and "n3on" in html and "Posting platforms" in html
