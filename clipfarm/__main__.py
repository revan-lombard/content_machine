"""python -m clipfarm <command> ...   (run with -h for help)"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import config, ff, pipeline


def main() -> None:
    ap = argparse.ArgumentParser(prog="clipfarm", description="GTA 6 clip farm")
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("log", help="run the hotkey beat logger while you play")
    p.add_argument("--challenge", default="", help='e.g. "escape 5 stars with no car"')

    def session_args(p):
        p.add_argument("session", help="a .beats.json from the logger, or a video file")
        p.add_argument("--video", help="recording path, if it isn't in the session file")
        p.add_argument("--challenge", help="override the challenge text")

    p = sub.add_parser("challenge", help="build the 60s 8-beat challenge Short (rough cut)")
    session_args(p)
    p = sub.add_parser("moments", help="build short moment clips from tags + loud spikes")
    session_args(p)
    p.add_argument("--no-auto", action="store_true", help="only use tagged moments")

    p = sub.add_parser("finish", help="add text, captions, sound effects, voiceover -> final.mp4")
    p.add_argument("clip_dirs", nargs="+", help="clip folder(s) containing rough.mp4 + script.yaml")
    p.add_argument("--vo", help="voiceover file (default: vo.wav in the clip folder, if present)")
    p.add_argument("--no-captions", action="store_true")

    p = sub.add_parser("kit", help="collect finished clips + captions per platform + review page")
    p.add_argument("session_out", help="e.g. out/20261119-201500")

    p = sub.add_parser("daily", help="everything: challenge + moments + finish + kit")
    session_args(p)
    p.add_argument("--no-captions", action="store_true")

    a = ap.parse_args()
    cfg = config.load(a.config)

    if a.cmd == "log":
        from . import logger
        logger.run(cfg, a.challenge)
        return

    ff.require()
    if a.cmd == "finish":
        for d in a.clip_dirs:
            pipeline.finish(cfg, Path(d), vo=a.vo, use_captions=False if a.no_captions else None)
        return
    if a.cmd == "kit":
        pipeline.kit(Path(a.session_out))
        return

    s = pipeline.load_session(a.session, a.video, a.challenge)
    if a.cmd == "challenge":
        pipeline.build_challenge(cfg, s)
    elif a.cmd == "moments":
        if a.no_auto:
            cfg["moments"]["auto"] = False
        pipeline.build_moments(cfg, s)
    elif a.cmd == "daily":
        caps = False if a.no_captions else None
        dirs = []
        if any(t["beat"] == "result" for t in s["tags"]):
            dirs.append(pipeline.build_challenge(cfg, s))
        else:
            print("No RESULT tag - skipping the challenge Short, making moment clips only.")
        dirs += pipeline.build_moments(cfg, s)
        print("Finishing clips...")
        for d in dirs:
            pipeline.finish(cfg, d, use_captions=caps)
        post = pipeline.kit(Path(s["out"]))
        print("\nYOUR TURN:")
        print(f"  1. Open {post / 'review.html'} and watch the clips.")
        if dirs and dirs[0].name == "challenge":
            print(f"  2. Optional, recommended: record a voiceover from {dirs[0] / 'vo_script.txt'},")
            print(f"     save it as {dirs[0] / 'vo.wav'}, then: python -m clipfarm finish \"{dirs[0]}\""
                  f" && python -m clipfarm kit \"{s['out']}\"")
        print("  3. Post each clip with the copy buttons, then tick 'posted'.")


if __name__ == "__main__":
    main()
