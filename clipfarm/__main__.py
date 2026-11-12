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

    sub.add_parser("sources", help="check which streamers in sources.yaml are approved")

    p = sub.add_parser("twitch", help="clip an approved streamer's VOD (default: their latest)")
    p.add_argument("source", nargs="?", help="name from sources.yaml (omit with --all)")
    p.add_argument("--all", action="store_true", help="latest VOD of every approved source")
    p.add_argument("--vod", help="a specific VOD id (from twitch.tv/videos/<id>)")
    p.add_argument("--chat", help="chat replay JSON, if you downloaded it yourself")
    p.add_argument("--max", type=int, help="most clips to make")
    p.add_argument("--no-finish", action="store_true", help="stop after rough cuts")
    p.add_argument("--no-captions", action="store_true")

    p = sub.add_parser("facecam", help="save a frame with a 10%% grid to find a streamer's face cam box")
    p.add_argument("source")
    p.add_argument("--vod", help="VOD id (default: latest)")
    p.add_argument("--at", type=float, default=600, help="seconds into the VOD (default 600)")
    p.add_argument("--file", help="use a local video file instead of downloading")

    p = sub.add_parser("review", help="open the review + performance dashboard in your browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")

    sub.add_parser("stats", help="fetch YouTube view counts and show the format leaderboard")

    p = sub.add_parser("post", help="log a post by hand (the review page does this too)")
    p.add_argument("clip_id", help="e.g. streamer-777/moments/03 (shown in script.yaml)")
    p.add_argument("platform", choices=["youtube", "tiktok", "instagram", "facebook", "x"])
    p.add_argument("url")

    p = sub.add_parser("connect", help="one-time sign-in for auto-posting: youtube or meta")
    p.add_argument("platform", choices=["youtube", "meta", "tiktok"])
    p.add_argument("--token", help="meta: a user access token from Graph API Explorer")
    p.add_argument("--page", help="meta: which Facebook Page, if you manage several")

    p = sub.add_parser("approve", help="approve clips for auto-posting (the review page does this too)")
    p.add_argument("clip_ids", nargs="+")
    p.add_argument("--reject", action="store_true")

    p = sub.add_parser("publish", help="post approved clips to YouTube / Instagram / Facebook")
    p.add_argument("--platform", action="append", choices=["youtube", "instagram", "facebook"])
    p.add_argument("--dry-run", action="store_true", help="show what would be posted")
    p.add_argument("--watch", action="store_true", help="keep running, checking every 10 minutes")

    p = sub.add_parser("react", help="multi-streamer reaction clips: find a moment, then build")
    rs = p.add_subparsers(dest="react_cmd", required=True)
    q = rs.add_parser("find", help="search approved streamers' viewer clips and draft a plan")
    q.add_argument("keywords", nargs="+", help='words from the moment, e.g. dam jump')
    q.add_argument("--sources", nargs="+", required=True, help="streamer names from sources.yaml")
    q.add_argument("--vods", type=int, default=5, help="how many recent VODs per streamer to search")
    q = rs.add_parser("build", help="build the clip from a plan file")
    q.add_argument("plan")
    q.add_argument("--no-finish", action="store_true")
    q.add_argument("--no-captions", action="store_true")

    a = ap.parse_args()
    cfg = config.load(a.config)

    if a.cmd == "connect":
        _connect(cfg, a)
        return
    if a.cmd == "approve":
        from . import tracker
        with pipeline.db() as con:
            for cid in a.clip_ids:
                tracker.set_status(con, cid, "rejected" if a.reject else "approved")
        print(f"{len(a.clip_ids)} clip(s) {'rejected' if a.reject else 'approved'}")
        return
    if a.cmd == "publish":
        _publish(cfg, a)
        return

    if a.cmd == "review":
        from . import server
        server.serve(cfg, a.port, not a.no_browser)
        return
    if a.cmd == "stats":
        _stats(cfg)
        return
    if a.cmd == "post":
        from . import tracker
        with pipeline.db() as con:
            tracker.record_post(con, a.clip_id, a.platform, a.url)
        print("Logged.")
        return

    if a.cmd == "sources":
        from . import sources
        sources.report()
        return

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
    if a.cmd == "twitch":
        _twitch(cfg, a)
        return
    if a.cmd == "react":
        if a.react_cmd == "find":
            pipeline.find_react(cfg, a.keywords, a.sources, vods_back=a.vods)
        else:
            d = pipeline.build_react(cfg, a.plan)
            if not a.no_finish:
                pipeline.finish(cfg, d, use_captions=False if a.no_captions else None)
                post = pipeline.kit(d.parent)
                print(f"Review: python -m clipfarm review   (or {post / 'review.html'})")
        return
    if a.cmd == "facecam":
        _facecam(a)
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



def _twitch(cfg: dict, a) -> None:
    from . import sources, twitch
    if a.all:
        names = [s.get("name") or s.get("login") for s in sources.load_all() if not sources.problems(s)]
        if not names:
            raise SystemExit("No approved sources. Run: python -m clipfarm sources")
    elif a.source:
        names = [a.source]
    else:
        raise SystemExit("Name a source from sources.yaml, or use --all")
    helix = twitch.Helix()
    caps = False if a.no_captions else None
    for name in names:
        src = sources.get(name)  # refuses anything without permission
        session_out, dirs = pipeline.build_twitch(cfg, src, vod_id=a.vod if not a.all else None,
                                                  chat_file=a.chat, max_n=a.max, helix=helix)
        if a.no_finish:
            continue
        print("Finishing clips...")
        for d in dirs:
            pipeline.finish(cfg, d, use_captions=caps)
        post = pipeline.kit(session_out)
        print(f"Review: {post / 'review.html'}\n")


def _connect(cfg: dict, a) -> None:
    from . import meta_api, publish, youtube_api
    sec = publish._secrets()
    tokens = publish.token_dir(config.ROOT)
    if a.platform == "tiktok":
        from . import tiktok_api
        t = sec.get("tiktok") or {}
        if not (t.get("client_key") and t.get("client_secret")):
            raise SystemExit("Add tiktok client_key and client_secret to secrets.yaml first "
                             "(steps in secrets.example.yaml)")
        tiktok_api.connect(t["client_key"], t["client_secret"], tokens / "tiktok.json",
                           port=int(cfg["tiktok"]["port"]))
        return
    if a.platform == "youtube":
        y = sec.get("youtube_oauth") or {}
        if not (y.get("client_id") and y.get("client_secret")):
            raise SystemExit("Add youtube_oauth client_id and client_secret to secrets.yaml first "
                             "(steps in secrets.example.yaml)")
        youtube_api.connect(y["client_id"], y["client_secret"], tokens / "youtube.json")
    else:
        m = sec.get("meta") or {}
        if not (m.get("app_id") and m.get("app_secret")):
            raise SystemExit("Add meta app_id and app_secret to secrets.yaml first (steps in secrets.example.yaml)")
        if not a.token:
            raise SystemExit("Pass --token: in Graph API Explorer pick your app, add these permissions: "
                             + ", ".join(meta_api.PERMISSIONS) + ", then Generate Access Token")
        meta_api.connect(m["app_id"], m["app_secret"], a.token,
                         cfg["publish"].get("meta_api_version", "v23.0"), tokens / "meta.json", a.page)


def _publish(cfg: dict, a) -> None:
    import time
    from . import publish
    last_stats = 0.0
    while True:
        with pipeline.db() as con:
            for r in publish.run(cfg, con, config.ROOT, platforms=a.platform, dry_run=a.dry_run):
                p = r["platform"].ljust(10)
                if "url" in r:
                    print(f"  {p} posted {r['clip']} -> {r['url']}")
                elif r.get("would_post"):
                    print(f"  {p} would post {r['clip']}")
                elif "error" in r:
                    print(f"  {p} FAILED {r['clip']}: {r['error']}")
                else:
                    print(f"  {p} {('[' + r['clip'] + '] ') if r.get('clip') else ''}{r['skipped']}")
        if not a.watch:
            return
        if time.time() - last_stats >= 3600:  # keep view counts fresh while it runs
            _refresh_stats(cfg)
            last_stats = time.time()
        time.sleep(600)


def _print_ig(r: dict, name: str = "Instagram") -> None:
    if not any(r.values()):
        return
    if r.get("capped") and not (r["updated"] or r["failed"]):
        return  # the cap message was already printed
    msg = f"{name}: updated {r['updated']} post(s)"
    if r["matched"]:
        msg += f", linked {r['matched']} hand-posted link(s) to your account"
    if r["unmatched"]:
        msg += (f", {r['unmatched']} link(s) not found on the connected account "
                "(enter those numbers by hand)" if name == "Instagram" else
                f", {r['unmatched']} short link(s) without a video id - paste the full "
                + ("tiktok.com/@you/video/... link, not vm.tiktok.com" if name == "TikTok" else
                   "x.com/you/status/... link" if name == "X" else
                   "facebook.com/reel/... link, not fb.watch") + " (or enter those numbers by hand)")
    if r["failed"]:
        msg += f", {r['failed']} failed" + (" (not on the connected account?)" if name == "TikTok" else "")
    print(msg)


def _refresh_stats(cfg: dict) -> None:
    """Quiet stats refresh used by publish --watch."""
    from . import publish, stats
    try:
        with pipeline.db() as con:
            stats.fetch_youtube(con)
            _print_ig(publish.fetch_instagram_stats(cfg, con, config.ROOT))
            _print_ig(publish.fetch_facebook_stats(cfg, con, config.ROOT), "Facebook")
            _print_ig(stats.fetch_tiktok_stats(cfg, con, config.ROOT), "TikTok")
            _print_ig(stats.fetch_x_stats(cfg, con, config.ROOT), "X")
    except Exception as e:  # never let stats stop the publisher
        print(f"  stats refresh failed: {str(e)[:200]}")


def _stats(cfg: dict) -> None:
    from . import formats, stats, tracker, writer
    exp = cfg["experiments"]
    with pipeline.db() as con:
        n = stats.fetch_youtube(con)
        print(f"YouTube: updated {n} post(s)")
        from . import publish
        _print_ig(publish.fetch_instagram_stats(cfg, con, config.ROOT))
        _print_ig(publish.fetch_facebook_stats(cfg, con, config.ROOT), "Facebook")
        _print_ig(stats.fetch_tiktok_stats(cfg, con, config.ROOT), "TikTok")
        _print_ig(stats.fetch_x_stats(cfg, con, config.ROOT), "X")
        todo = tracker.todo(con, exp.get("score_hours", 48))
        if todo:
            print(f"{len(todo)} post(s) need numbers typed in - run: python -m clipfarm review  (Performance tab)")
        for dim, arms in (("format", [f for f in exp["formats"] if f in formats.ALL]),
                          ("hook_style", pipeline._arms(cfg, "hook_style", has_viewer_title=True))):
            st = tracker.arm_stats(con, dim, arms, exp.get("score_hours", 48))
            share, warming = tracker.mix(con, cfg, dim, [x for x in arms if x not in ("countdown", "multi_react")])
            print(f"\n{dim.replace('_', ' ').title()}s" + ("  (warming up - rotating evenly)" if warming else ""))
            print(f"  {'option':<14}{'clips':>6}{'scored':>8}{'win rate':>10}{'next mix':>10}")
            for x in arms:
                wr = st[x]["win_rate"]
                print(f"  {x:<14}{st[x]['clips']:>6}{st[x]['scored']:>8}"
                      f"{'-' if wr is None else f'{wr * 100:.0f}%':>10}"
                      f"{'+1/stream' if x == 'countdown' else 'on request' if x == 'multi_react' else f'{share[x] * 100:.0f}%':>10}")


def _facecam(a) -> None:
    from . import sources, twitch
    src = sources.get(a.source)
    out = config.ROOT / "out" / f"facecam-{src['name']}.png"
    out.parent.mkdir(exist_ok=True)
    if a.file:
        video, at = a.file, a.at
    else:
        h = twitch.Helix()
        vod = h.vod(a.vod) if a.vod else h.vods(h.user_id(src["login"]), 1)[0]
        video = out.with_suffix(".mp4")
        twitch.download_section(vod["id"], a.at, a.at + 3, video)
        at = 1.0
    ff.run(["-ss", f"{at}", "-i", video, "-frames:v", "1", "-vf",
            "scale=1280:-2,drawgrid=w=iw/10:h=ih/10:t=1:c=yellow@0.8", out])
    print(f"Saved {out}")
    print("Each grid square is 10% of the frame. Count squares to the face cam's top-left corner (x, y)")
    print("and its size (w, h), then set e.g.  facecam: [0.0, 0.7, 0.2, 0.3]  in sources.yaml.")


if __name__ == "__main__":
    main()
