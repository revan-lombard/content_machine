"""Kick support, against a fake Kick API. Run:  python -m pytest -q"""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from clipfarm import config, ff, kick, pipeline, publish, sources

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def clip(i, minutes_ago, views, category="Grand Theft Auto V", stream="ls1", title=None, **extra):
    return {"id": f"clip_{i}", "title": title if title is not None else f"clip {i}", "views": views,
            "duration": 40, "created_at": iso(NOW - timedelta(minutes=minutes_ago)),
            "livestream_id": stream, "category": {"name": category}, "clip_url": f"MEDIA{i}", **extra}


PAGES = {
    "0": {"clips": [clip(1, 10, 500, title="he jumped the dam on a bike"),
                    clip(2, 11, 90),                                         # same moment as 1 (60s apart)
                    clip(3, 30, 300, category="Slots & Casino", title="huge win"),
                    clip(4, 120, 50, title="the cops never saw it coming")],
          "nextCursor": "c2"},
    "c2": {"clips": [clip(5, 60 * 30, 900, title="old but gold"),            # 30h ago
                     clip(6, 60 * 60, 9999, title="too old")],               # 60h ago: outside 48h
           "nextCursor": "c3"},
    "c3": {"clips": [clip(7, 60 * 70, 1, title="never reached")], "nextCursor": None},
}


def fake_get(calls=None):
    def get(url, params):
        if calls is not None:
            calls.append(params["cursor"])
        return PAGES[params["cursor"]]
    return get


def test_normalize_reads_both_field_spellings():
    c = kick.normalize({"id": 9, "title": " Hi ", "view_count": "12", "likes_count": 3, "duration": "30.5",
                        "created_at": "2026-11-20T10:00:00Z", "livestream": {"id": 77},
                        "category_name": "Grand Theft Auto VI", "video_url": "https://x/v.mp4"}, "someone")
    assert (c["title"], c["views"], c["likes"], c["stream"], c["category"]) == ("Hi", 12, 3, "77", "Grand Theft Auto VI")
    assert c["page"] == "https://kick.com/someone/clips/9" and c["media"] == "https://x/v.mp4"


def test_clips_pages_until_window_ends():
    calls = []
    got = kick.clips("n", hours=48, get=fake_get(calls), now=NOW)
    assert calls == ["0", "c2"]                          # stopped once clips got older than 48h
    assert [c["id"] for c in got] == ["clip_1", "clip_2", "clip_3", "clip_4", "clip_5"]


def test_gambling_always_blocked_and_gta_only():
    cs = [kick.normalize(c, "n") for c in PAGES["0"]["clips"]]
    blocked = ["Slots", "Casino", "Gambling"]
    assert [c["id"] for c in cs if kick.allowed(c, ["Grand Theft Auto"], blocked)] == ["clip_1", "clip_2", "clip_4"]
    assert "clip_3" not in [c["id"] for c in cs if kick.allowed(c, [], blocked)]   # even with "any category"
    just_chatting = kick.normalize(clip(8, 5, 10, category="Just Chatting"), "n")
    assert not kick.allowed(just_chatting, ["Grand Theft Auto"], blocked)
    assert kick.allowed(just_chatting, [], blocked)


def test_moments_group_same_moment():
    cs = [kick.normalize(c, "n") for c in PAGES["0"]["clips"] + PAGES["c2"]["clips"][:1]]
    ms = kick.moments([c for c in cs if c["id"] != "clip_3"], 10)
    by_id = {m["id"]: m for m in ms}
    assert set(by_id) == {"clip_1", "clip_4", "clip_5"}            # clip_2 merged into clip_1
    assert by_id["clip_1"]["clippers"] == 2 and by_id["clip_1"]["total_views"] == 590
    # two people clipping the same moment outranks one bigger clip (same rule as Twitch)
    assert [m["id"] for m in ms] == ["clip_1", "clip_5", "clip_4"]


def test_download_from_media_url(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=30:duration=3", "-f", "lavfi", "-i", "sine=d=3", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    out = kick.download({"media": str(src), "page": "https://kick.com/n/clips/x"}, tmp_path / "out.mp4")
    assert abs(ff.duration(out) - 3) < 0.2


# ---------------------------------------------------------------- end to end

@pytest.fixture()
def env(tmp_path, monkeypatch):
    video = tmp_path / "stream.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=30:duration=40", "-f", "lavfi", "-i", "sine=d=40", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(video)], check=True)
    downloads = []

    def fake_download(c, out):
        downloads.append(c["id"] if isinstance(c, dict) else c)
        ff.run(["-i", video, "-c", "copy", out])
        return out

    monkeypatch.setattr(kick, "download", fake_download)
    monkeypatch.setattr(kick, "clips", lambda slug, hours=48, get=None, now=None, max_pages=6:
                        kick.__dict__["_real_clips"](slug, hours, max_pages, fake_get(), NOW))
    kick._real_clips = _REAL_CLIPS
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    sf = tmp_path / "sources.yaml"
    sf.write_text(yaml.safe_dump({"sources": [
        {"name": "neon", "platform": "kick", "login": "n3on", "permission": {"type": "campaign", "proof": "discord"},
         "rules": {"credit": "@n3on", "hashtags": ["#gta6", "#n3on"], "platforms": ["youtube", "tiktok"]}},
        {"name": "alpha", "login": "alpha", "permission": {"type": "written", "proof": "dm.png"}}]}))
    real_get = sources.get
    monkeypatch.setattr(sources, "get", lambda n, path=None: real_get(n, path or sf))
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    cfg["experiments"]["enabled"] = False
    return cfg, sf, tmp_path, downloads


_REAL_CLIPS = kick.clips


def test_build_kick(env):
    cfg, sf, root, downloads = env
    src = sources.get("neon")
    out, dirs = pipeline.build_kick(cfg, src, now=NOW)
    assert set(downloads) == {"clip_1", "clip_4", "clip_5"}          # gambling clip never downloaded
    metas = [yaml.safe_load((d / "script.yaml").read_text()) for d in dirs]
    assert all(m["source"]["platform"] == "kick" and m["credit"] == "@n3on" for m in metas)
    assert {m["source"]["url"] for m in metas} == {f"https://kick.com/n3on/clips/{i}" for i in ("clip_1", "clip_4", "clip_5")}
    assert all("Casino" not in m["source"]["category"] for m in metas)
    hooks = {m["beats"][0]["text"] for m in metas}
    assert "he jumped the dam on a bike" in hooks                    # a viewer's clip title as the hook
    pipeline.finish(cfg, dirs[0], use_captions=False)
    assert ff.video_size(dirs[0] / "final.mp4") == (1080, 1920)
    meta = metas[0]
    assert publish._allowed_by_source(meta, "youtube", sf) is None
    assert "allow only" in publish._allowed_by_source(meta, "facebook", sf)


def test_kick_sources_are_valid():
    assert sources.problems({"platform": "kick", "login": "n3on", "permission": {"type": "campaign", "proof": "x"}}) == []
    assert any("not supported" in p for p in sources.problems(
        {"platform": "facebook", "login": "x", "permission": {"type": "campaign", "proof": "x"}}))


def test_react_mixes_kick_clip_and_twitch_vod(env, monkeypatch):
    cfg, sf, root, downloads = env
    from clipfarm import twitch

    def fake_section(vod_id, start, end, out, max_height=1080):
        ff.run(["-ss", "0", "-t", f"{end - start}", "-i", root / "stream.mp4", "-c", "copy", out])
        return out

    monkeypatch.setattr(twitch, "download_section", fake_section)
    plan = root / "plan.yaml"
    plan.write_text(yaml.safe_dump({"title": "2 streamers react", "moment": "the dam", "align": False, "entries": [
        {"source": "alpha", "vod": 111, "at": 20},
        {"source": "neon", "clip": "https://kick.com/n3on/clips/clip_1"}]}))
    d = pipeline.build_react(cfg, plan)
    meta = yaml.safe_load((d / "script.yaml").read_text())
    assert [s["platform"] for s in meta["sources"]] == ["twitch", "kick"]
    assert meta["sources"][1]["url"] == "https://kick.com/n3on/clips/clip_1"
    assert [b["text"] for b in meta["beats"][2:]] == ["@alpha", "@n3on"]


def test_react_find_searches_kick_titles(env):
    cfg, sf, root, _ = env
    path = pipeline.find_react(cfg, ["dam"], ["neon"], plan_dir=root / "plans", kick_get=fake_get())
    text = path.read_text()
    assert "clip: https://kick.com/n3on/clips/clip_1" in text
