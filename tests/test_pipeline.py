"""End-to-end test on a synthetic 'recording'. Run:  python -m pytest -q   (needs ffmpeg)"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from clipfarm import config, ff, pipeline
from clipfarm.logger import Logger


@pytest.fixture(scope="module")
def rec(tmp_path_factory):
    d = tmp_path_factory.mktemp("rec")
    video = d / "rec.mp4"
    # 150s of test pattern; loud 2s bursts every 45s, quiet otherwise
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=150",
                    "-f", "lavfi", "-i",
                    "anoisesrc=a=0.3:d=150,volume='if(lt(mod(t\\,45)\\,2)*gt(t\\,10)\\,1\\,0.03)':eval=frame",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(video)],
                   check=True)
    return video


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    c = config.load()
    c["video"]["preset"] = "ultrafast"
    c["moments"]["max"] = 3
    c["experiments"]["enabled"] = False  # these tests check the original fixed formats
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    return c


def session(rec, tmp_path, tags):
    p = tmp_path / "s.beats.json"
    p.write_text(json.dumps({"challenge": "escape 5 stars", "recording": str(rec), "tags": tags}))
    s = pipeline.load_session(p, None, None)
    s["out"] = str(tmp_path / "out" / "s")
    return s


def test_logger_session(tmp_path):
    log = Logger(config.load(), "test", out_dir=tmp_path)
    log.tag("problem", None, "obs")          # not recording: ignored
    assert log.session is None
    log.tag("problem", 12.3, "obs")
    log.tag("result", 40.0, "obs")
    log.undo()
    path = log.path
    log.finish("C:/rec.mkv")
    data = json.loads(path.read_text())
    assert data["tags"] == [{"t": 12.3, "beat": "problem"}]
    assert data["recording"] == "C:/rec.mkv"


def test_challenge_full_structure(rec, tmp_path, cfg):
    tags = [{"t": 20, "beat": "problem"}, {"t": 40, "beat": "attempt"}, {"t": 55, "beat": "attempt"},
            {"t": 85, "beat": "obstacle"}, {"t": 100, "beat": "retry"}, {"t": 120, "beat": "result"}]
    d = pipeline.build_challenge(cfg, session(rec, tmp_path, tags))
    meta = yaml.safe_load((d / "script.yaml").read_text())
    assert [b["key"] for b in meta["beats"]] == config.BEATS
    assert abs(ff.duration(d / "rough.mp4") - 60) < 1.0
    # fake a transcript so captions are exercised without downloading a model
    (d / "words.json").write_text(json.dumps([{"w": "no", "s": 1.0, "e": 1.3},
                                              {"w": "way", "s": 1.3, "e": 1.8}]))
    out = pipeline.finish(cfg, d)
    assert ff.video_size(out) == (1080, 1920)
    assert "Dialogue: 0," in (d / "overlay.ass").read_text()


def test_challenge_missing_beats_still_builds(rec, tmp_path, cfg):
    d = pipeline.build_challenge(cfg, session(rec, tmp_path, [{"t": 90, "beat": "result"}]))
    keys = [b["key"] for b in yaml.safe_load((d / "script.yaml").read_text())["beats"]]
    assert keys == ["hook", "problem", "challenge", "build", "result", "end"]


def test_challenge_needs_result(rec, tmp_path, cfg):
    with pytest.raises(SystemExit):
        pipeline.build_challenge(cfg, session(rec, tmp_path, [{"t": 20, "beat": "problem"}]))


def test_moments_auto_only(rec, tmp_path, cfg):
    dirs = pipeline.build_moments(cfg, session(rec, tmp_path, []))
    assert 1 <= len(dirs) <= 3
    for d in dirs:
        meta = yaml.safe_load((d / "script.yaml").read_text())
        assert meta["found_by"] == "auto:loud"
        # loud bursts are at 45-47, 90-92, 135-137
        assert any(abs(meta["source_at"] - b) < 3 for b in (46, 91, 136))
    for d in dirs:
        pipeline.finish(cfg, d, use_captions=False)
    post = pipeline.kit(Path(session(rec, tmp_path, [])["out"]))
    items = json.loads((post / "posts.json").read_text())
    assert len(items) == len(dirs)
    assert all(len(i["x"]) <= 280 and "#shorts" in i["youtube_description"] for i in items)
