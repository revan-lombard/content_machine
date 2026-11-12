"""Local web app: review + log posts (/) and the performance dashboard (/dashboard).

    python -m clipfarm review      ->  http://localhost:8765

Runs only on your own machine (127.0.0.1). Nothing is uploaded anywhere.
"""
from __future__ import annotations

import html
import json
import math
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import pipeline, tracker

E = html.escape
LABELS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "facebook": "Facebook", "x": "X"}
CAPTION_KEY = {"youtube": "youtube_title", "tiktok": "tiktok", "instagram": "instagram",
               "facebook": "facebook", "x": "x"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e2e5ea;--ink:#16181d;--ink2:#4a505b;--mute:#7b818c;
--accent:#1f7a68;--accent-soft:#d5ece6;--track:#eceef2;--warn:#8a5a00;--warn-bg:#fff4dc}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111318;--card:#1a1d24;--line:#2b2f38;
--ink:#e8eaee;--ink2:#b7bcc6;--mute:#8a909b;--accent:#4cc3a8;--accent-soft:#1d3a34;--track:#262a33;
--warn:#f2c46b;--warn-bg:#3a2f16}}
:root[data-theme="dark"]{--bg:#111318;--card:#1a1d24;--line:#2b2f38;--ink:#e8eaee;--ink2:#b7bcc6;--mute:#8a909b;
--accent:#4cc3a8;--accent-soft:#1d3a34;--track:#262a33;--warn:#f2c46b;--warn-bg:#3a2f16}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
main{max-width:1150px;margin:auto;padding:20px 16px 60px}a{color:var(--accent)}
nav{display:flex;gap:14px;align-items:center;margin-bottom:18px}nav a{text-decoration:none;font-weight:600}
nav a.on{color:var(--ink);border-bottom:2px solid var(--accent)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--mute);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.clip{display:grid;grid-template-columns:220px 1fr;gap:16px}video{width:220px;aspect-ratio:9/16;background:#000;border-radius:6px}
.tag{display:inline-block;background:var(--accent-soft);color:var(--ink);border-radius:4px;padding:1px 7px;font-size:12px;margin-right:4px}
.row{display:grid;grid-template-columns:90px 1fr auto;gap:8px;align-items:center;border-top:1px solid var(--line);padding:7px 0}
.row .cap{color:var(--ink2);font-size:12.5px;white-space:pre-wrap;max-height:3.2em;overflow:hidden}
select{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:4px 6px;font:inherit}
input{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:5px 7px;font:inherit;min-width:0}
button{background:var(--accent);color:#04201a;border:0;border-radius:5px;padding:5px 11px;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.done{color:var(--mute);font-size:12.5px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.kpi b{display:block;font-size:24px}.kpi span{color:var(--mute);font-size:12px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--mute);font-weight:600;font-size:12px}th.n{text-align:right}td.n{text-align:right;font-variant-numeric:tabular-nums}
.bar{position:relative;height:10px;background:var(--track);border-radius:4px;min-width:120px}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);border-radius:0 4px 4px 0}
.bar u{position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--mute)}
.note{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:9px 12px;margin:8px 0}
.stats{display:flex;gap:6px;flex-wrap:wrap}
details pre{white-space:pre-wrap;max-width:420px;font:12.5px/1.45 system-ui,sans-serif;background:var(--bg);padding:8px;border-radius:6px}
a.tag{text-decoration:none;color:var(--ink)}.stats input{width:96px}
@media(max-width:720px){.clip{grid-template-columns:1fr}video{width:100%}.kpis{grid-template-columns:repeat(2,1fr)}
.row{grid-template-columns:1fr}.hide-sm{display:none}}
"""

JS = """
async function api(path, body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 if(!r.ok){alert(await r.text());return false}return true}
async function posted(btn,clip,platform){const url=btn.parentElement.querySelector('input').value.trim();
 if(!url){alert('Paste the link to the post first');return}
 if(await api('/api/post',{clip_id:clip,platform,url}))location.reload()}
async function stats(btn,post){const f=btn.closest('.stats');const v=n=>f.querySelector('[name='+n+']').value||0;
 if(!f.querySelector('[name=views]').value){alert('Enter at least the views');return}
 if(await api('/api/stats',{post_id:post,views:v('views'),likes:v('likes'),comments:v('comments'),shares:v('shares')}))location.reload()}
async function prospect(sel,platform,login){if(await api('/api/prospect',{platform,login,status:sel.value}))sel.closest('tr').dataset.status=sel.value}
async function outreach(path,body){if(await api(path,body))location.reload()}
async function setStatus(clip,status){if(await api('/api/status',{clip_id:clip,status}))location.reload()}
function cp(btn,text){navigator.clipboard.writeText(text);btn.textContent='Copied';setTimeout(()=>btn.textContent='Copy',1200)}
"""


def page(title: str, body: str, active: str) -> bytes:
    nav = "".join(f'<a href="{h}" class="{"on" if k == active else ""}">{t}</a>'
                  for k, h, t in (("review", "/", "Review & post"), ("dash", "/dashboard", "Performance"),
                                  ("discover", "/discover", "Discover")))
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{E(title)}</title><style>{CSS}</style>"
            f"</head><body><main><nav>{nav}</nav>{body}</main><script>{JS}</script></body></html>").encode()


# ---------------------------------------------------------------- review

def review_page(con, show_all: bool, auto: list[str] | None = None) -> bytes:
    """auto = platforms the publisher posts to; the rest get copy + paste-link controls."""
    auto = auto or []
    clips = con.execute("SELECT * FROM clips ORDER BY created DESC").fetchall()
    posts: dict[str, dict] = {}
    for p in con.execute("SELECT * FROM posts"):
        posts.setdefault(p["clip_id"], {})[p["platform"]] = p
    pending = {(r["clip_id"], r["platform"]) for r in con.execute("SELECT clip_id, platform FROM pending_posts")}
    errors: dict[tuple, str] = {}
    for e in con.execute("SELECT clip_id, platform, error FROM publish_attempts ORDER BY at"):
        errors[(e["clip_id"], e["platform"])] = e["error"]
    cards, hidden = [], 0
    for c in clips:
        d = Path(c["path"])
        final = d / "final.mp4"
        if not final.exists():
            continue
        done = posts.get(c["id"], {})
        status = c["status"] or "new"
        if not show_all and (len(done) == len(tracker.PLATFORMS) or status == "rejected"):
            hidden += 1
            continue
        try:
            meta = yaml.safe_load((d / "script.yaml").read_text(encoding="utf-8"))
            texts = pipeline._platform_text(meta)
        except Exception:
            texts = {}
        cid = E(json.dumps(c["id"]))
        rows = []
        for plat in tracker.PLATFORMS:
            cap = texts.get(CAPTION_KEY[plat], "")
            cap_js = E(json.dumps(cap))
            copy = f'<button class="ghost" onclick="cp(this,{cap_js})">Copy</button>' if cap else ""
            err = errors.get((c["id"], plat))
            if plat in done:
                act = f'<span class="done">Posted · <a href="{E(done[plat]["url"])}" target="_blank" rel="noopener">link</a></span>'
                copy = ""
            elif (c["id"], plat) in pending:
                # TikTok inbox draft: you post it from the app; it's logged automatically after
                act = (f'<span style="display:flex;gap:6px;align-items:center"><span class="done">In your TikTok '
                       f'inbox: copy the caption, post it in the app</span><input placeholder="or paste link">'
                       f'<button class="ghost" onclick="posted(this,{cid},\'{plat}\')">Posted</button></span>')
            elif plat in auto and status == "approved":
                act = (f'<span class="done" title="{E(err or "")}">'
                       f'{"Auto-post failed, retrying: " + E(err[:80]) if err else "Queued for auto-post"}</span>')
                copy = ""
            elif plat in auto:
                act = '<span class="done">Approve to auto-post</span>'
                copy = ""
            else:
                act = (f'<span style="display:flex;gap:6px"><input placeholder="paste post link">'
                       f'<button onclick="posted(this,{cid},\'{plat}\')">Posted</button></span>')
            rows.append(f'<div class="row"><b>{LABELS[plat]}</b><div class="cap">{E(cap)}</div>'
                        f'<div style="display:flex;gap:6px;align-items:center">{copy}{act}</div></div>')
        media = "/media?clip=" + urllib.parse.quote(c["id"])
        if status == "approved":
            decide = (f'<span class="tag">approved</span>'
                      f'<button class="ghost" onclick="setStatus({cid},\'new\')">Undo</button>')
        elif status == "rejected":
            decide = (f'<span class="tag">rejected</span>'
                      f'<button class="ghost" onclick="setStatus({cid},\'new\')">Undo</button>')
        else:
            decide = (f'<button onclick="setStatus({cid},\'approved\')">Approve</button>'
                      f'<button class="ghost" onclick="setStatus({cid},\'rejected\')">Reject</button>')
        cards.append(
            f'<section class="card clip"><video src="{media}" controls preload="metadata" playsinline></video>'
            f'<div><div style="display:flex;justify-content:space-between;gap:10px;align-items:start;margin-bottom:6px">'
            f'<b>{E(c["title"] or c["id"])}</b><span style="display:flex;gap:6px;align-items:center">{decide}</span></div>'
            f'<div style="margin-bottom:8px"><span class="tag">{E(c["format"] or "-")}</span>'
            f'<span class="tag">hook: {E(c["hook_style"] or "-")}</span>'
            f'<span class="tag">{E(c["source"] or "")}</span></div>{"".join(rows)}</div></section>')
    toggle = ('<a href="/?all=1">show posted and rejected clips</a>' if not show_all else '<a href="/">hide them</a>')
    names = [LABELS[p] for p in auto]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""
    auto_txt = (f"<b>Approve</b> a clip and it's posted to {joined} automatically. "
                if auto else "")
    head = (f"<h1>Review &amp; post</h1><p class='sub'>{auto_txt}For the other platforms, copy the caption, "
            f"post it, then paste the post's link and press Posted, so its views get tracked. "
            f"{hidden} clip(s) hidden · {toggle}</p>")
    if not cards:
        cards = ["<div class='card'>No finished clips yet. Run twitch.bat or daily.bat first.</div>"]
    return page("Review & post", head + "".join(cards), "review")


# ---------------------------------------------------------------- dashboard

def _fmt_n(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.1f}K" if v >= 1e3 else f"{v:.0f}"


def _arm_table(con, cfg, dim: str, arms: list[str], title: str, blurb: str) -> str:
    exp = cfg["experiments"]
    st = tracker.arm_stats(con, dim, arms, exp.get("score_hours", 48))
    regular = [a for a in arms if a not in ("countdown", "multi_react")]  # made in addition / on request
    share, warming = tracker.mix(con, cfg, dim, regular)
    need = int(exp.get("min_scored", 15))
    rows = []
    for a in sorted(arms, key=lambda a: -(st[a]["win_rate"] if st[a]["win_rate"] is not None else -1)):
        r = st[a]
        wr = r["win_rate"]
        eng = "–" if r["engagement_rate"] is None else f"{r['engagement_rate'] * 100:.1f}%"
        bar = (f'<div class="bar" title="{a}: beat your median on {wr * 100:.0f}% of scored posts">'
               f'<i style="width:{wr * 100:.1f}%"></i><u></u></div>') if wr is not None else '<span class="done">no scores yet</span>'
        rows.append(f"<tr><td><b>{E(a)}</b></td><td class='n'>{r['clips']}</td>"
                    f"<td class='n'>{r['scored']}{'' if r['scored'] >= need else f' / {need}'}</td>"
                    f"<td>{bar}</td><td class='n'>{'' if wr is None else f'{wr * 100:.0f}%'}</td>"
                    f"<td class='n hide-sm'>{_fmt_n(r['median_views'])}</td>"
                    f"<td class='n hide-sm'>{eng}</td>"
                    f"<td class='n'>{'+1 per stream' if a == 'countdown' else 'on request' if a == 'multi_react' else f'{share[a] * 100:.0f}%'}</td></tr>")
    note = (f"<div class='note'>Warming up: options rotate evenly until each has {need} scored clips. "
            f"Win rates below are early and noisy.</div>") if warming else ""
    return (f"<h2>{title}</h2><p class='sub'>{blurb}</p>{note}<div class='card' style='overflow-x:auto'><table>"
            f"<tr><th>Option</th><th class='n'>Clips</th><th class='n'>Scored</th>"
            f"<th>Win rate <span class='done'>(line = 50%, average)</span></th><th class='n'></th>"
            f"<th class='n hide-sm'>Median views</th><th class='n hide-sm'>Engagement</th>"
            f"<th class='n'>Next mix</th></tr>{''.join(rows)}</table></div>")


PLATFORM_NAMES = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube", "own": "Your own gameplay",
                  "mixed": "Mixed (multi-streamer)", "unknown": "Unknown"}


def _source_table(title: str, rows: dict[str, dict], blurb: str, names: dict | None = None) -> str:
    if not rows:
        return ""
    trs = []
    for name, r in sorted(rows.items(), key=lambda kv: (-(kv[1]["win_rate"] if kv[1]["win_rate"] is not None else -1),
                                                        -kv[1]["clips"])):
        wr = r["win_rate"]
        bar = (f'<div class="bar" title="{E(name)}: beat your median on {wr * 100:.0f}% of scored posts">'
               f'<i style="width:{wr * 100:.1f}%"></i><u></u></div>') if wr is not None else '<span class="done">no scores yet</span>'
        eng = "–" if r["engagement_rate"] is None else f"{r['engagement_rate'] * 100:.1f}%"
        trs.append(f"<tr><td><b>{E((names or {}).get(name, name))}</b></td><td class='n'>{r['clips']}</td>"
                   f"<td class='n'>{r['scored']}</td><td>{bar}</td>"
                   f"<td class='n'>{'' if wr is None else f'{wr * 100:.0f}%'}</td>"
                   f"<td class='n hide-sm'>{_fmt_n(r['median_views'])}</td>"
                   f"<td class='n hide-sm'>{_fmt_n(r['total_views'])}</td><td class='n hide-sm'>{eng}</td></tr>")
    return (f"<h3 style='font-size:14px;margin:14px 0 6px'>{title}</h3><p class='sub' style='margin-bottom:8px'>{blurb}</p>"
            f"<div class='card' style='overflow-x:auto'><table><tr><th>Source</th><th class='n'>Clips</th>"
            f"<th class='n'>Scored</th><th>Win rate <span class='done'>(line = 50%)</span></th><th class='n'></th>"
            f"<th class='n hide-sm'>Median views</th><th class='n hide-sm'>Total views</th>"
            f"<th class='n hide-sm'>Engagement</th></tr>{''.join(trs)}</table></div>")


def dashboard_page(con, cfg) -> bytes:
    exp = cfg["experiments"]
    hours = exp.get("score_hours", 48)
    scores = tracker.post_scores(con, hours)
    n_clips = con.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    scored = [d for d in scores if d["status"] == "scored"]
    need = [d for d in scores if d["status"] == "needs_stats"]
    kpis = "".join(f"<div class='card kpi'><b>{v}</b><span>{k}</span></div>" for k, v in (
        ("clips made", n_clips), ("posts logged", len(scores)), (f"posts scored ({hours}h)", len(scored)),
        ("waiting for your numbers", len(need))))
    body = [f"<h1>Performance</h1><p class='sub'>A post <b>wins</b> when its views {hours}h after posting "
            f"beat the median of your last 30 posts on the same platform. Formats that win more get "
            f"more of the next clips.</p><div class='kpis'>{kpis}</div>"]

    if need:
        rows = "".join(
            f"<tr><td>{LABELS[d['platform']]}</td><td>{E(d['title'] or d['clip_id'])}<br>"
            f"<a class='done' href='{E(d['url'])}' target='_blank' rel='noopener'>open post</a></td>"
            f"<td><div class='stats'><input name='views' type='number' min='0' placeholder='views'>"
            f"<input name='likes' type='number' min='0' placeholder='likes'>"
            f"<input name='comments' type='number' min='0' placeholder='comments'>"
            f"<input name='shares' type='number' min='0' placeholder='shares'>"
            f"<button onclick='stats(this,{d['id']})'>Save</button></div></td></tr>" for d in need)
        body.append(f"<h2>Enter numbers ({len(need)})</h2><p class='sub'>These posts are {hours}h+ old. "
                    f"Open each one, type in its numbers. Every platform fills in automatically once connected "
                    f"(<code>python -m clipfarm stats</code>, or hourly while publish.bat runs).</p><div class='card' style='overflow-x:auto'>"
                    f"<table>{rows}</table></div>")

    fails = con.execute("""SELECT a.*, c.title FROM publish_attempts a LEFT JOIN clips c ON c.id = a.clip_id
                           WHERE NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id=a.clip_id AND p.platform=a.platform)
                           ORDER BY a.at DESC LIMIT 10""").fetchall()
    if fails:
        rows = "".join(f"<tr><td>{LABELS.get(f['platform'], f['platform'])}</td><td>{E(f['title'] or f['clip_id'])}</td>"
                       f"<td class='done'>{E(f['at'])}</td><td>{E((f['error'] or '')[:200])}</td></tr>" for f in fails)
        body.append("<h2>Auto-post problems</h2><p class='sub'>A clip is retried up to 3 times per platform.</p>"
                    f"<div class='card' style='overflow-x:auto'><table>{rows}</table></div>")

    from . import formats, writer
    fmts = [f for f in exp["formats"] if f in formats.ALL]
    styles = pipeline._arms(cfg, "hook_style", has_viewer_title=True)
    body.append(_arm_table(con, cfg, "format", fmts, "Formats",
                           "How each clip format performs. \"Next mix\" is the share of upcoming clips each will get."))
    body.append(_arm_table(con, cfg, "hook_style", styles, "Hook styles",
                           "The opening line on screen, tested separately from the format."))

    tracker.backfill_source_platforms(con)
    ss = tracker.source_stats(con, hours)
    tips = tracker.source_advice(ss["streamer"])  # once a streamer has 10+ scored clips
    tip_html = "".join(f"<div class='note' style='background:var(--accent-soft);color:var(--ink)'>{E(t)}</div>"
                       for t in tips)
    body.append("<h2>Sources</h2><p class='sub'>Which streamers and platforms your best clips come from, "
                "scored the same way as formats. A multi-streamer clip counts for each streamer in it.</p>"
                + tip_html
                + _source_table("By platform", ss["platform"], "Where the footage came from.", PLATFORM_NAMES)
                + _source_table("By streamer", ss["streamer"],
                                "Streamers need about 10 scored clips before their win rate means much."))

    plat_rows = []
    for plat in tracker.PLATFORMS:
        vs = [d["views"] for d in scored if d["platform"] == plat][-30:]
        if vs:
            vs_sorted = sorted(vs)
            med = vs_sorted[len(vs) // 2]
            plat_rows.append(f"<tr><td>{LABELS[plat]}</td><td class='n'>{len(vs)}</td>"
                             f"<td class='n'>{_fmt_n(med)}</td><td class='n'>{_fmt_n(max(vs))}</td></tr>")
    if plat_rows:
        body.append("<h2>Posting platforms</h2><div class='card'><table><tr><th>Platform</th><th class='n' style='text-align:right'>Recent scored posts</th>"
                    f"<th style='text-align:right'>Median views</th><th style='text-align:right'>Best</th></tr>{''.join(plat_rows)}</table></div>")

    top = sorted(scored, key=lambda d: -(math.log1p(d["views"]) - math.log1p(d.get("baseline_views", 0))))[:10]
    if top:
        rows = "".join(f"<tr><td>{E(d['title'] or d['clip_id'])}</td><td>{LABELS[d['platform']]}</td>"
                       f"<td><span class='tag'>{E(d['format'])}</span><span class='tag'>{E(d['hook_style'])}</span></td>"
                       f"<td class='n'>{_fmt_n(d['views'])}</td><td class='n'>"
                       f"{(d['views'] + 1) / (d.get('baseline_views', 0) + 1):.1f}×</td></tr>" for d in top)
        body.append("<h2>Best posts</h2><div class='card' style='overflow-x:auto'><table><tr><th>Clip</th>"
                    "<th>Platform</th><th>Variant</th><th class='n'>Views</th><th class='n'>vs your median</th>"
                    f"</tr>{rows}</table></div>")
    return page("Performance", "".join(body), "dash")


# ---------------------------------------------------------------- server

def _outreach_ready() -> dict:
    from . import outreach, publish
    return {k: True for k in outreach.build_senders(publish._secrets(), publish.token_dir(pipeline.ROOT))}


def discover_page(con, cfg, show_all: bool, ready: dict | None = None) -> bytes:
    from datetime import datetime, timedelta, timezone
    from . import discover, outreach, sources
    discover.setup(con)
    outreach.setup(con)
    ready = _outreach_ready() if ready is None else ready
    now = datetime.now(timezone.utc)
    approved = {(x.get("platform", "twitch"), str(x.get("login", "")).lower()) for x in sources.load_all()}
    ranked = discover.rank(con, cfg, approved, now)
    hidden = [d for d in ranked if d["in_sources"] or d["status"] in ("no", "ignore")]
    rows_src = ranked if show_all else [d for d in ranked if d not in hidden]
    msgs: dict[tuple, dict] = {}
    for m in con.execute("SELECT * FROM outreach WHERE status IN ('queued','sent','failed') ORDER BY id"):
        msgs[(m["platform"], m["login"])] = dict(m)  # latest message per streamer
    lo, hi = (cfg.get("discovery") or {}).get("viewers", [100, 8000])
    rows = []
    for d in rows_src[:60]:
        opts = "".join(f'<option value="{st}"{" selected" if d["status"] == st else ""}>{label}</option>'
                       for st, label in (("new", "New"), ("contacted", "Contacted"), ("yes", "Said yes"),
                                         ("no", "Said no"), ("ignore", "Ignore")))
        plat, login = E(json.dumps(d["platform"])), E(json.dumps(d["login"]))
        c = outreach.contacts(d.get("bio") or "")
        chips = []
        if c["email"]:
            chips.append(f'<span class="tag">email: {E(c["email"])}</span>')
        if c["discord"]:
            dl = c["discord"] if c["discord"].startswith("http") else "https://" + c["discord"]
            chips.append(f'<a class="tag" href="{E(dl)}" target="_blank" rel="noopener">Discord</a>')
        if c["x"]:
            chips.append(f'<a class="tag" href="https://x.com/{E(c["x"])}" target="_blank" rel="noopener">X @{E(c["x"])}</a>')
        badges = "".join(f'<span class="tag">{t}</span>' for t in
                         (["mentions clipping"] if d["hint"] else []) + (["in sources"] if d["in_sources"] else [])
                         + ([d["size_fit"]] if d["size_fit"] != "in range" else []))
        clips = "–" if d["clips_7d"] is None else f'{d["clips_7d"]} ({_fmt_n(d["clip_views_7d"])} views)'
        m = msgs.get((d["platform"], d["login"]))
        if m and m["status"] == "queued":
            state = (f'<details><summary class="done">Queued ({m["channel"]}, {m["kind"]}) · preview</summary>'
                     f'<pre>{E(m["body"])}</pre></details>'
                     f'<button class="ghost" onclick="outreach(\'/api/outreach/cancel\',{{id:{m["id"]}}})">Cancel</button>')
        elif m and m["status"] == "sent":
            state = (f'<span class="done">Sent {m["kind"]} by {m["channel"]} · {E(m["sent_at"][:10])}</span>')
        else:
            fail = (f'<span class="done" title="{E(m["error"] or "")}">Last send failed: {E((m["error"] or "")[:60])}</span>'
                    if m else "")
            btns = "".join(f'<button onclick="outreach(\'/api/outreach/queue\',{{platform:{plat},login:{login},'
                           f'channel:\'{ch}\'}})">Queue {"email" if ch == "email" else "whisper"}</button>'
                           for ch in outreach.channels_for(d, ready))
            state = fail + btns
        rows.append(
            f'<tr data-status="{d["status"]}"><td><a href="{E(d["url"])}" target="_blank" rel="noopener"><b>{E(d["name"])}</b></a>'
            f'<div class="done">{E(PLATFORM_NAMES.get(d["platform"], d["platform"]))} · {E((d["last_title"] or "")[:70])}</div>'
            f'<div>{badges}{"".join(chips)}</div></td>'
            f'<td class="n">{_fmt_n(d["avg_viewers"])}</td><td class="n">{d["days_seen"]}</td>'
            f'<td class="n hide-sm">{clips}</td>'
            f'<td><div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">{state}'
            f'<button class="ghost" onclick="cp(this,{E(json.dumps(outreach.dm(cfg, d)))})">Copy message</button>'
            f'<button class="ghost" onclick="cp(this,{E(json.dumps(discover.sources_entry(d)))})">Copy sources.yaml entry</button>'
            f'<select onchange="prospect(this,{plat},{login})">{opts}</select></div></td></tr>')
    o = cfg.get("outreach") or {}
    queued = con.execute("SELECT COUNT(*) FROM outreach WHERE status='queued'").fetchone()[0]
    sent24 = con.execute("SELECT COUNT(*) FROM outreach WHERE status='sent' AND sent_at >= ?",
                         ((now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()[0]
    setup_bits = [f"email {'ready' if ready.get('email') else 'not set up'}",
                  f"Twitch whispers {'ready' if ready.get('whisper') else 'not set up'}"]
    summary = (f"<div class='card' style='display:flex;gap:18px;flex-wrap:wrap'><span><b>{queued}</b> queued</span>"
               f"<span><b>{sent24}</b>/{o.get('daily_max', 10)} sent in the last 24h</span>"
               f"<span class='done'>{' · '.join(setup_bits)} · sends {o.get('hours', [10, 20])[0]}:00–"
               f"{o.get('hours', [10, 20])[1]}:00 while publish.bat runs</span></div>")
    toggle = '<a href="/discover?all=1">show hidden</a>' if not show_all else '<a href="/discover">hide them</a>'
    head = (f"<h1>Discover</h1><p class='sub'>GTA streamers worth asking for permission, best first. Ranked by how often "
            f"they stream GTA (last 14 days), whether their audience is {lo:,}–{hi:,} viewers (the size most likely to "
            f"say yes), how much their viewers clip them, and whether their bio or title mentions clipping. "
            f"<b>Queue</b> sends a personalised request by email (if their bio lists one) or Twitch whisper, within your "
            f"limits, with one follow-up after {o.get('followup_days', 5)} days. Elsewhere, use Copy message. "
            f"{len(hidden)} hidden (already in sources.yaml, said no, or ignored) · {toggle}</p>"
            f"<div class='note'>Mark replies with the status menu: Said yes / Said no stops any follow-up. Add someone "
            f"to sources.yaml only after they say yes, with their yes as the proof.</div>{summary}")
    if not rows:
        table = "<div class='card'>No streamers recorded yet. Run <code>discover.bat</code> while GTA streams are live.</div>"
    else:
        table = ("<div class='card' style='overflow-x:auto'><table><tr><th>Streamer</th><th class='n'>Avg viewers</th>"
                 "<th class='n'>Days seen</th><th class='n hide-sm'>Clips last 7 days</th><th>Ask</th></tr>"
                 + "".join(rows) + "</table></div>")
    return page("Discover", head + table, "discover")


def _auto_platforms(cfg) -> list[str]:
    """Platforms the publisher is set up for (config + credentials present)."""
    from . import publish
    try:
        clients = publish.Clients(cfg, pipeline.ROOT)
        return [p for p in cfg["publish"]["platforms"] if p in publish.AUTO and not clients.ready(p)]
    except Exception:
        return []


def make_handler(cfg):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            with pipeline.db() as con:
                if u.path == "/":
                    return self._send(200, review_page(con, bool(q.get("all")), _auto_platforms(cfg)))
                if u.path == "/dashboard":
                    return self._send(200, dashboard_page(con, cfg))
                if u.path == "/discover":
                    return self._send(200, discover_page(con, cfg, bool(q.get("all"))))
                if u.path == "/media":
                    row = con.execute("SELECT path FROM clips WHERE id=?", (q.get("clip", [""])[0],)).fetchone()
                    if row:
                        return self._media(Path(row["path"]) / "final.mp4")
            self._send(404, b"not found", "text/plain")

        def _media(self, f: Path):
            if not f.exists():
                return self._send(404, b"not found", "text/plain")
            size = f.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else max(0, size - int(b))
                end = min(size - 1, int(b)) if a and b else end
            length = end - start + 1
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(1 << 16, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with pipeline.db() as con:
                    if self.path == "/api/post":
                        if not str(data.get("url", "")).startswith("http"):
                            raise ValueError("That doesn't look like a link")
                        tracker.record_post(con, data["clip_id"], data["platform"], data["url"])
                    elif self.path in ("/api/outreach/queue", "/api/outreach/cancel"):
                        from datetime import datetime, timezone
                        from . import discover, outreach, sources
                        discover.setup(con)
                        outreach.setup(con)
                        if self.path.endswith("cancel"):
                            outreach.cancel(con, int(data["id"]))
                        else:
                            approved = {(x.get("platform", "twitch"), str(x.get("login", "")).lower())
                                        for x in sources.load_all()}
                            d = next((x for x in discover.rank(con, cfg, approved, datetime.now(timezone.utc))
                                      if x["platform"] == data["platform"] and x["login"] == data["login"]), None)
                            if d is None:
                                raise ValueError("unknown streamer")
                            if data.get("channel") not in outreach.channels_for(d, _outreach_ready()):
                                raise ValueError(f"{data.get('channel')} isn't available for {d['name']}")
                            outreach.queue(cfg, con, d, data["channel"])
                    elif self.path == "/api/prospect":
                        from . import discover
                        if data.get("status") not in discover.STATUSES:
                            raise ValueError("unknown status")
                        discover.setup(con)
                        if not con.execute("UPDATE prospects SET status=? WHERE platform=? AND login=?",
                                           (data["status"], data["platform"], data["login"])).rowcount:
                            raise ValueError("unknown streamer")
                    elif self.path == "/api/status":
                        tracker.set_status(con, data["clip_id"], data["status"])
                    elif self.path == "/api/stats":
                        tracker.add_stats(con, int(data["post_id"]), int(data["views"]), int(data.get("likes") or 0),
                                          int(data.get("comments") or 0), int(data.get("shares") or 0))
                    else:
                        return self._send(404, b"not found", "text/plain")
                self._send(200, b"ok", "text/plain")
            except (ValueError, KeyError) as e:
                self._send(400, str(e).encode(), "text/plain")

    return H


def serve(cfg, port: int = 8765, open_browser: bool = True) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    url = f"http://localhost:{port}"
    print(f"Review & dashboard: {url}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
