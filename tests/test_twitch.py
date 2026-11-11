"""Twitch path, with a fake Twitch API and a fake VOD downloader. Run:  python -m pytest -q"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from clipfarm import captions, config, ff, pipeline, render, sources, twitch

SRC = {"name": "streamer", "platform": "twitch", "login": "some_streamer",
       "permission": {"type": "campaign", "proof": "https://example.com/campaign"},
       "rules": {"credit": "@some_streamer", "hashtags": ["#gta6", "#somestreamer"], "max_length": 30},
       "facecam": [0.0, 0.7, 0.25, 0.3]}


def write_sources(tmp_path, items):
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump({"sources": items}))
    return p


# ---------------------------------------------------------------- permission gate

def test_unlisted_source_is_refused(tmp_path):
    with pytest.raises(sources.NotApproved, match="not in sources.yaml"):
        sources.get("random_streamer", write_sources(tmp_path, [SRC]))


def test_source_without_proof_is_refused(tmp_path):
    bad = {**SRC, "permission": {"type": "campaign", "proof": ""}}
    with pytest.raises(sources.NotApproved, match="proof"):
        sources.get("streamer", write_sources(tmp_path, [bad]))


def test_disabled_and_bad_facecam_are_refused(tmp_path):
    assert "enabled is false" in sources.problems({**SRC, "enabled": False})
    assert any("facecam" in p for p in sources.problems({**SRC, "facecam": [0, 0, 2, 1]}))


def test_approved_source_by_login(tmp_path):
    assert sources.get("SOME_STREAMER", write_sources(tmp_path, [SRC]))["name"] == "streamer"


# ---------------------------------------------------------------- moment signals

def test_viewer_clips_grouped_and_ranked():
    clips = [
        {"video_id": "1", "vod_offset": 100, "duration": 30, "view_count": 50, "title": "a"},
        {"video_id": "1", "vod_offset": 105, "duration": 25, "view_count": 900, "title": "he jumped off the dam"},
        {"video_id": "1", "vod_offset": 2000, "duration": 30, "view_count": 10, "title": "c"},
        {"video_id": "2", "vod_offset": 50, "duration": 30, "view_count": 9999},   # other VOD
        {"video_id": "1", "vod_offset": None, "duration": 30, "view_count": 9999},  # not in VOD
    ]
    ms = twitch.moments_from_clips(clips, "1")
    assert len(ms) == 2
    top = max(ms, key=lambda m: m.score)
    assert top.clippers == 2 and top.clip_views == 950
    assert 120 < top.peak < 130  # near the end of the clips, where the payoff is


def test_chat_spike_found_and_shifted_for_delay():
    comments = [{"content_offset_seconds": t, "message": {"body": "hi"}} for t in range(0, 900, 10)]
    comments += [{"content_offset_seconds": 400 + i * 0.1, "message": {"body": "KEKW LUL"}} for i in range(40)]
    ms = twitch.moments_from_chat(comments)
    assert ms and abs(ms[0].peak - (402.5 - twitch.CHAT_DELAY)) < 3


def test_combine_merges_and_spaces():
    a = [twitch.Moment(100, 5, ["clip"]), twitch.Moment(110, 4, ["clip"]), twitch.Moment(500, 3, ["clip"])]
    b = [twitch.Moment(104, 3, ["chat"]), twitch.Moment(900, 2, ["chat"])]
    out = twitch.combine(a, b, min_gap=20, max_n=10)
    assert [round(m.peak) for m in out] == [100, 500, 900]
    assert "chat" in out[0].reasons


def test_parse_duration():
    assert twitch.parse_duration("3h2m1s") == 10921
    assert twitch.parse_duration("45s") == 45


def test_parse_whisper_cpp():
    data = {"transcription": [{"offsets": {"from": 0, "to": 400}, "text": " NO"},
                              {"offsets": {"from": 400, "to": 900}, "text": " way"},
                              {"offsets": {"from": 900, "to": 1500}, "text": " [MUSIC]"}]}
    assert captions.parse_whisper_cpp(data) == [{"w": "NO", "s": 0.0, "e": 0.4}, {"w": "way", "s": 0.4, "e": 0.9}]


# ---------------------------------------------------------------- end to end

class FakeHelix:
    def user_id(self, login):
        return "42"

    def vods(self, uid, n=1):
        return [self.vod("777")]

    def vod(self, vod_id):
        return {"id": "777", "title": "GTA 6 DAY 1", "duration": "0h5m0s", "created_at": "2026-11-19T10:00:00Z"}

    def clips(self, uid, a, b):
        return [{"video_id": "777", "vod_offset": 60, "duration": 30, "view_count": 500,
                 "title": "he really did that"},
                {"video_id": "777", "vod_offset": 200, "duration": 20, "view_count": 80, "title": "GTA 6 DAY 1"}]


@pytest.fixture()
def fake_vod(tmp_path, monkeypatch):
    vod = tmp_path / "vod.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=300",
                    "-f", "lavfi", "-i", "sine=f=330:d=300", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(vod)], check=True)
    calls = []

    def fake_download(vod_id, start, end, out, max_height=1080):
        calls.append((start, end))
        ff.run(["-ss", f"{start}", "-t", f"{end - start}", "-i", vod, "-c", "copy", out])
        return out

    monkeypatch.setattr(twitch, "download_section", fake_download)
    monkeypatch.setattr(twitch, "download_chat", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    return calls


def test_twitch_end_to_end(fake_vod, tmp_path):
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    cfg["video"]["encoder"] = "h264_amf"  # not available here: must fall back to the CPU encoder
    session_out, dirs = pipeline.build_twitch(cfg, dict(SRC), helix=FakeHelix())
    assert len(dirs) == 2
    # only the parts around each moment were downloaded, never the whole 5-minute VOD
    assert all(end - start <= 40 for start, end in fake_vod)
    meta = yaml.safe_load((dirs[0] / "script.yaml").read_text())
    assert meta["credit"] == "@some_streamer" and meta["layout"] == "split"
    assert meta["beats"][0]["text"] == "he really did that"   # viewer clip title became the hook
    assert meta["source"]["url"].startswith("https://www.twitch.tv/videos/777?t=")
    meta2 = yaml.safe_load((dirs[1] / "script.yaml").read_text())
    assert meta2["beats"][0]["text"] != "GTA 6 DAY 1"          # default stream title is not a hook
    # rules: max_length 30 -> clip no longer than that
    assert ff.duration(dirs[0] / "rough.mp4") <= 30.5
    for d in dirs:
        out = pipeline.finish(cfg, d, use_captions=False)
        assert ff.video_size(out) == (1080, 1920)
    assert "@some_streamer" in (dirs[0] / "overlay.ass").read_text()
    post = pipeline.kit(session_out)
    items = json.loads((post / "posts.json").read_text())
    assert all("@some_streamer" in i["tiktok"] and "#somestreamer" in i["instagram"] for i in items)
    assert render._amf_failed  # fell back
