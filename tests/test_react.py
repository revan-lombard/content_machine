"""Multi-streamer reactions. Run:  python -m pytest -q"""
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import yaml

from clipfarm import config, ff, pipeline, publish, react, sources, tracker, twitch

RATE = 16000


def write_wav(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def game_audio(dur, seed=1):
    """Same 'game sound' for everyone: random bursts of different loudness."""
    rng = np.random.default_rng(seed)
    env = np.zeros(int(dur * RATE))
    t = 0.0
    while t < dur:
        n = int(rng.uniform(0.1, 0.6) * RATE)
        i = int(t * RATE)
        env[i:i + n] = rng.uniform(0.05, 0.9)
        t += rng.uniform(0.2, 1.2)
    return env * np.random.default_rng(seed + 100).standard_normal(len(env))


def stream(path, color, delay, dur=90, voice_seed=None):
    """A fake streamer VOD: video in `color`, game audio delayed by `delay` s, plus their own voice."""
    g = game_audio(dur + 10)
    a = np.zeros(int(dur * RATE))
    d = int(delay * RATE)
    a[d:] = g[: len(a) - d]
    if voice_seed is not None:
        a += 0.15 * np.random.default_rng(voice_seed).standard_normal(len(a))
    wav = path.with_suffix(".wav")
    write_wav(wav, a * 0.6)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=640x360:r=30:d={dur}", "-i", str(wav), "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def vods(tmp_path_factory):
    d = tmp_path_factory.mktemp("vods")
    return {"111": stream(d / "a.mp4", "red", 0.0, voice_seed=5),
            "222": stream(d / "b.mp4", "blue", 2.3, voice_seed=6),
            "333": stream(d / "c.mp4", "green", 0.0, voice_seed=7)}


SRCS = [
    {"name": "alpha", "login": "alpha", "permission": {"type": "campaign", "proof": "x"},
     "rules": {"hashtags": ["#gta6", "#alpha"], "platforms": ["youtube", "instagram"]},
     "facecam": [0.0, 0.0, 0.3, 0.3]},
    {"name": "bravo", "login": "bravo", "permission": {"type": "written", "proof": "dm.png"},
     "rules": {"hashtags": ["#GTA6", "#bravo"]}, "facecam": [0.7, 0.0, 0.3, 0.3]},
    {"name": "charlie", "login": "charlie", "permission": {"type": "public", "proof": "link"},
     "facecam": [0.0, 0.7, 0.3, 0.3]},
]


@pytest.fixture()
def env(tmp_path, vods, monkeypatch):
    sf = tmp_path / "sources.yaml"
    sf.write_text(yaml.safe_dump({"sources": SRCS}))
    real_get = sources.get
    monkeypatch.setattr(sources, "get", lambda n, path=None: real_get(n, path or sf))

    def fake_download(vod_id, start, end, out, max_height=1080):
        ff.run(["-ss", f"{start}", "-t", f"{end - start}", "-i", vods[vod_id], "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", out])
        return out

    monkeypatch.setattr(twitch, "download_section", fake_download)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    return cfg, sf, tmp_path


def plan(tmp_path, entries, **kw):
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump({"title": "3 streamers react to the dam jump", "moment": "the dam jump",
                                 **kw, "entries": entries}))
    return p


# ---------------------------------------------------------------- parsing + permission

def test_parse_times_and_links():
    assert react.parse_time("1h2m3s") == 3723 and react.parse_time("1:02:03") == 3723
    assert react.parse_time("62:03") == 3723 and react.parse_time(90) == 90 and react.parse_time("45s") == 45
    assert react.parse_vod("https://www.twitch.tv/videos/123456?t=0h10m5s") == ("123456", 605)
    assert react.parse_vod("123456", at="1:00") == ("123456", 60)
    with pytest.raises(ValueError):
        react.parse_vod("123456")  # no time
    with pytest.raises(ValueError):
        react.parse_time("soon")


def test_plan_refuses_streamer_without_permission(env):
    cfg, sf, tmp = env
    p = plan(tmp, [{"source": "alpha", "vod": 111, "at": 40}, {"source": "stranger", "vod": 222, "at": 40}])
    with pytest.raises(SystemExit, match="not in sources.yaml"):
        pipeline.build_react(cfg, p)


# ---------------------------------------------------------------- sync

def test_slide_finds_offset():
    rng = np.random.default_rng(0)
    other = rng.standard_normal(1000)
    ref = other[300:700].copy()
    k, c = react.slide(ref, other)
    assert k == 300 and c > 0.99


def test_align_recovers_shift_and_ignores_unrelated(vods, tmp_path):
    lone = stream(tmp_path / "lone.mp4", "black", 0.0, dur=60, voice_seed=9)
    # write different game audio into `lone` so nothing matches
    write_wav(tmp_path / "x.wav", game_audio(60, seed=42) * 0.6)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(lone), "-i",
                    str(tmp_path / "x.wav"), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                    "-shortest", str(tmp_path / "lone2.mp4")], check=True)
    out = react.align([(vods["111"], 40.0), (vods["222"], 41.8), (tmp_path / "lone2.mp4", 40.0)])
    (t_b, c_b), (t_lone, c_lone) = out[1], out[2]
    assert abs(t_b - 42.3) < 0.06 and c_b > 0.6       # B's moment is really 2.3s later
    assert t_lone == 40.0 and c_lone < 0.45           # no match -> planned time kept


# ---------------------------------------------------------------- building

def test_sequence_end_to_end(env):
    cfg, sf, tmp = env
    p = plan(tmp, [{"source": "charlie", "vod": "https://www.twitch.tv/videos/333?t=40s"},
                   {"source": "alpha", "vod": 111, "at": "0:40"},
                   {"source": "bravo", "vod": 222, "at": 42, "label": "BRAVO"}])
    d = pipeline.build_react(cfg, p)
    meta = yaml.safe_load((d / "script.yaml").read_text())
    keys = [b["key"] for b in meta["beats"]]
    assert keys == ["hook", "problem", "react1", "react2", "react3"]
    assert [b["text"] for b in meta["beats"][2:]] == ["@charlie", "@alpha", "BRAVO"]
    assert meta["format"] == "multi_react" and meta["credits_all"] == "@charlie · @alpha · BRAVO"
    assert [s["login"] for s in meta["sources"]] == ["charlie", "alpha", "bravo"]
    assert meta["sources"][2]["sync"] > 0.6                    # bravo synced from game audio
    assert meta["hashtags"][:3] == ["#gta6", "#alpha", "#bravo"]  # merged, de-duplicated
    assert 22 <= ff.duration(d / "rough.mp4") <= 24             # 2 + 6 + 3 x 5
    out = pipeline.finish(cfg, d, use_captions=False)
    assert ff.video_size(out) == (1080, 1920)
    con = pipeline.db()
    assert con.execute("SELECT source FROM clips").fetchone()[0] == "charlie, alpha, bravo"
    post = pipeline.kit(d.parent)
    assert "@charlie · @alpha · BRAVO" in (post / "posts.json").read_text(encoding="utf-8")


def test_campaign_max_length_shrinks_slots(env, monkeypatch):
    cfg, sf, tmp = env
    SRCS_SHORT = [dict(s) for s in SRCS]
    SRCS_SHORT[0] = {**SRCS[0], "rules": {**SRCS[0]["rules"], "max_length": 17}}
    sf.write_text(yaml.safe_dump({"sources": SRCS_SHORT}))
    p = plan(tmp, [{"source": "alpha", "vod": 111, "at": 40}, {"source": "bravo", "vod": 222, "at": 42},
                   {"source": "charlie", "vod": 333, "at": 40}], align=False)
    d = pipeline.build_react(cfg, p)
    assert ff.duration(d / "rough.mp4") <= 17.5


def test_grid_layout(env):
    cfg, sf, tmp = env
    p = plan(tmp, [{"source": "alpha", "vod": 111, "at": 40}, {"source": "bravo", "vod": 222, "at": 42},
                   {"source": "charlie", "vod": 333, "at": 40}], layout="grid")
    d = pipeline.build_react(cfg, p)
    meta = yaml.safe_load((d / "script.yaml").read_text())
    assert len(meta["labels"]) == 3 and meta["labels"][1][0] == "@bravo"
    assert 10.5 <= ff.duration(d / "rough.mp4") <= 11.5
    pipeline.finish(cfg, d, use_captions=False)
    assert "\\pos(" in (d / "overlay.ass").read_text()
    assert ff.video_size(d / "final.mp4") == (1080, 1920)


def test_grid_without_facecams_falls_back(env):
    cfg, sf, tmp = env
    sf.write_text(yaml.safe_dump({"sources": [{**SRCS[0], "facecam": None}, SRCS[1]]}))
    p = plan(tmp, [{"source": "alpha", "vod": 111, "at": 40}, {"source": "bravo", "vod": 222, "at": 42}],
             layout="grid", align=False)
    d = pipeline.build_react(cfg, p)
    assert yaml.safe_load((d / "script.yaml").read_text())["beats"][-1]["key"] == "react2"


# ---------------------------------------------------------------- finding + publishing

class FakeHelix:
    CLIPS = {"alpha": [("111", 30, 400, "ALPHA DAM JUMP omg"), ("111", 300, 20, "other stuff")],
             "bravo": [("222", 33, 90, "the dam jump reaction"), ("222", 500, 999, "dam jump again lol")],
             "charlie": [("333", 10, 50, "nothing related")]}

    def user_id(self, login):
        return login

    def vods(self, uid, n):
        return [{"id": self.CLIPS[uid][0][0], "created_at": "2026-11-19T10:00:00Z", "duration": "2h0m0s"}]

    def clips(self, uid, a, b):
        return [{"video_id": v, "vod_offset": o, "duration": 20, "view_count": c, "title": t}
                for v, o, c, t in self.CLIPS[uid]]


def test_find_drafts_a_plan(env):
    cfg, sf, tmp = env
    path = pipeline.find_react(cfg, ["dam", "jump"], ["alpha", "bravo", "charlie"], helix=FakeHelix(),
                               plan_dir=tmp / "plans")
    text = path.read_text()
    assert "videos/111?t=0h0m46s" in text                 # alpha: best match, peak near clip end
    assert "videos/222?t=0h8m36s" in text                 # bravo: most-viewed match first
    assert "# - source: charlie" in text                  # no match: left for you to fill in
    p = react.load_plan(path, sources.get)
    assert [e["source"]["name"] for e in p["entries"]] == ["alpha", "bravo"]


def test_publish_needs_every_streamers_ok(env):
    cfg, sf, tmp = env
    meta = {"sources": [{"platform": "twitch", "login": "alpha"}, {"platform": "twitch", "login": "bravo"}]}
    assert "allow only" in publish._allowed_by_source(meta, "facebook", sf)   # alpha's campaign
    assert publish._allowed_by_source(meta, "youtube", sf) is None
    meta["sources"].append({"platform": "twitch", "login": "gone"})
    assert "no longer approved" in publish._allowed_by_source(meta, "youtube", sf)
