"""YouTube streamer support, with yt-dlp faked. Run:  python -m pytest -q"""
import json
import subprocess
import types

import numpy as np
import pytest
import yaml

from clipfarm import config, ff, pipeline, publish, react, sources, ytlive


def test_video_ids_and_channel_urls():
    for s in ("https://www.youtube.com/watch?v=abcDEF12345&t=90s", "https://youtu.be/abcDEF12345",
              "https://www.youtube.com/live/abcDEF12345?si=x", "abcDEF12345"):
        assert ytlive.video_id(s) == "abcDEF12345", s
    assert ytlive.channel_url("@someone") == "https://www.youtube.com/@someone"
    assert ytlive.channel_url("someone") == "https://www.youtube.com/@someone"
    assert ytlive.channel_url("UCabcdefghijklmnopqrstuv") == "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"


def chat_line(offset_ms, *runs):
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(offset_ms), "actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": list(runs)}}}}}]}})


def test_parse_chat_text_and_emoji():
    text = "\n".join([chat_line(1500, {"text": "no way "}, {"emoji": {"emojiId": "😂"}}),
                      json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "2000", "actions": [
                          {"addLiveChatTickerItemAction": {}}]}}), "not json"])
    assert ytlive.parse_chat(text) == [{"content_offset_seconds": 1.5, "message": {"body": "no way 😂"}}]


def test_heatmap_peaks():
    hm = [{"start_time": i * 60.0, "end_time": (i + 1) * 60.0, "value": 0.1} for i in range(100)]
    hm[30]["value"], hm[31]["value"] = 1.0, 0.7     # one replayed stretch spanning 2 segments
    hm[70]["value"] = 0.6
    ms = ytlive.moments_from_heatmap(hm)
    assert [round(m.peak) for m in ms] == [1830, 4230]   # local maxima only, strongest first
    assert ms[0].span == (1800.0, 1860.0)
    assert ytlive.moments_from_heatmap(hm[:3]) == []


# ---------------------------------------------------------------- end to end, yt-dlp faked

DUR = 300


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # a 5-minute "stream": quiet, with one loud burst at 200-202s (inside the most-replayed stretch)
    rate = 16000
    a = 0.02 * np.random.default_rng(0).standard_normal(DUR * rate)
    a[200 * rate:202 * rate] = 0.8 * np.random.default_rng(1).standard_normal(2 * rate)
    import wave
    w = tmp_path / "a.wav"
    with wave.open(str(w), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(rate)
        f.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())
    stream = tmp_path / "stream.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=30:duration={DUR}", "-i", str(w), "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(stream)], check=True)
    chat = [chat_line(t * 1000, {"text": "hi"}) for t in range(0, DUR, 10)]
    chat += [chat_line(int((100 + i * 0.1) * 1000), {"text": "LMAO KEKW"}) for i in range(40)]  # burst at ~100s
    heatmap = [{"start_time": i * 3.0, "end_time": (i + 1) * 3.0, "value": 0.1} for i in range(100)]
    for i in (64, 65, 66, 67):                         # 192-204s replayed
        heatmap[i]["value"] = 0.9 if i == 66 else 0.6
    calls = []

    def fake_run(cfg, args):
        calls.append(args)
        url = args[-1]
        if url.endswith("/streams"):
            return types.SimpleNamespace(stdout=json.dumps({"entries": [
                {"id": "abcDEF12345", "title": "GTA 6 day 1", "live_status": "was_live"}]}))
        if "--write-subs" in args:
            out_tmpl = args[args.index("-o") + 1]
            (tmp_path / "x").parent.mkdir(exist_ok=True)
            open(out_tmpl.replace("%(id)s.%(ext)s", "abcDEF12345.live_chat.json"), "w").write("\n".join(chat))
            return types.SimpleNamespace(stdout="")
        if "--download-sections" in args:
            sec = args[args.index("--download-sections") + 1].lstrip("*").split("-")
            out = args[args.index("-o") + 1]
            ff.run(["-ss", sec[0], "-t", f"{float(sec[1]) - float(sec[0]):.1f}", "-i", stream,
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", out])
            return types.SimpleNamespace(stdout="")
        if "-J" in args:
            return types.SimpleNamespace(stdout=json.dumps({"title": "GTA 6 day 1", "duration": DUR,
                                                            "heatmap": heatmap, "live_status": "was_live"}))
        raise AssertionError(args)

    monkeypatch.setattr(ytlive, "run", fake_run)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    sf = tmp_path / "sources.yaml"
    sf.write_text(yaml.safe_dump({"sources": [
        {"name": "yt", "platform": "youtube", "login": "@someone", "permission": {"type": "written", "proof": "dm"},
         "rules": {"platforms": ["tiktok"]}},
        {"name": "alpha", "login": "alpha", "permission": {"type": "written", "proof": "dm"}}]}))
    real_get = sources.get
    monkeypatch.setattr(sources, "get", lambda n, path=None: real_get(n, path or sf))
    cfg = config.load()
    cfg["video"]["preset"] = "ultrafast"
    cfg["experiments"]["enabled"] = False
    cfg["moments"]["min_gap"] = 20
    return cfg, sf, tmp_path, calls


def test_build_youtube(env):
    cfg, sf, root, calls = env
    out, dirs = pipeline.build_youtube(cfg, sources.get("yt"))
    metas = [yaml.safe_load((d / "script.yaml").read_text()) for d in dirs]
    found = sorted((m["source_at"], m["found_by"]) for m in metas)
    assert len(found) == 2
    (t_chat, why_chat), (t_heat, why_heat) = found
    assert abs(t_chat - (102.5 - 6)) < 5 and "chat spike" in why_chat           # chat burst, shifted for delay
    assert 199 <= t_heat <= 203 and "loudest point" in why_heat                   # replayed stretch -> loud burst
    assert all(m["source"]["platform"] == "youtube" for m in metas)
    assert metas[0]["source"]["url"].startswith("https://www.youtube.com/watch?v=abcDEF12345&t=")
    assert metas[0]["credit"] == "@someone"
    # only the needed parts were downloaded, never the whole stream
    secs = [a[a.index("--download-sections") + 1] for a in calls if "--download-sections" in a]
    assert all(float(s.lstrip("*").split("-")[1]) - float(s.lstrip("*").split("-")[0]) < 40 for s in secs)
    pipeline.finish(cfg, dirs[0], use_captions=False)
    assert ff.video_size(dirs[0] / "final.mp4") == (1080, 1920)
    assert "allow only" in publish._allowed_by_source(metas[0], "youtube", sf)   # campaign: TikTok only
    assert publish._allowed_by_source(metas[0], "tiktok", sf) is None


def test_react_with_youtube_entry(env, monkeypatch):
    cfg, sf, root, calls = env
    from clipfarm import twitch

    def fake_section(vod_id, start, end, out, max_height=1080):
        ff.run(["-ss", "0", "-t", f"{end - start}", "-i", root / "stream.mp4", "-c", "copy", out])
        return out

    monkeypatch.setattr(twitch, "download_section", fake_section)
    plan = root / "plan.yaml"
    plan.write_text(yaml.safe_dump({"title": "2 react", "moment": "it", "align": False, "entries": [
        {"source": "alpha", "vod": 111, "at": 30},
        {"source": "yt", "vod": "https://www.youtube.com/watch?v=abcDEF12345&t=3m20s"}]}))
    p = react.load_plan(plan, sources.get)
    assert p["entries"][1]["vod"] == "abcDEF12345" and p["entries"][1]["at"] == 200
    d = pipeline.build_react(cfg, plan)
    meta = yaml.safe_load((d / "script.yaml").read_text())
    assert meta["sources"][1]["url"] == "https://www.youtube.com/watch?v=abcDEF12345&t=200s"
    bad = root / "bad.yaml"
    bad.write_text(yaml.safe_dump({"entries": [{"source": "alpha", "vod": 1, "at": 1},
                                               {"source": "yt", "vod": "https://youtu.be/abcDEF12345"}]}))
    with pytest.raises(SystemExit, match="no timestamp"):
        react.load_plan(bad, sources.get)


def test_youtube_sources_valid():
    assert sources.problems({"platform": "youtube", "login": "@x", "permission": {"type": "public", "proof": "l"}}) == []
