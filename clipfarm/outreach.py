"""Outreach: ask streamers for clipping permission - drafted, queued by you, sent within limits.

Automated: finding contact details in bios, personalised drafts, sending what YOU queued (email,
Twitch whispers), one polite follow-up, marking people as contacted.
Never automated: deciding who to contact. Nothing is sent to anyone you didn't queue.

Guardrails: daily cap, spacing between sends, posting hours, at most one follow-up (two messages
per streamer, ever), never anyone marked "no"/"ignore", an opt-out line in every message.
Kick, YouTube, X and Discord have no official way to message people, so those stay copy-and-send.
"""
from __future__ import annotations

import json
import re
import secrets as pysecrets
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import net

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, login TEXT, channel TEXT, kind TEXT,
  to_addr TEXT, subject TEXT, body TEXT, status TEXT, created_at TEXT, sent_at TEXT, error TEXT);
"""
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DISCORD = re.compile(r"(?:https?://)?(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9-]+", re.I)
XHANDLE = re.compile(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})\b", re.I)
WHISPER_LIMIT = 500  # Twitch: max characters to someone who hasn't whispered you before
CHANNELS = ("email", "whisper")


def setup(con) -> None:
    con.executescript(SCHEMA)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def contacts(bio: str) -> dict:
    """Public contact details a streamer put in their bio."""
    bio = bio or ""
    emails = [e.rstrip(".") for e in EMAIL.findall(bio) if not e.lower().endswith((".png", ".jpg", ".gif"))]
    discord = DISCORD.findall(bio)
    xs = XHANDLE.findall(bio)
    x = xs[0] if xs else None
    return {"email": emails[0] if emails else None, "discord": discord[0] if discord else None, "x": x}


def channels_for(d: dict, ready: dict) -> list[str]:
    """Automatic channels usable for this streamer, given what's configured (ready[channel] = True)."""
    c = contacts(d.get("bio") or "")
    out = []
    if c["email"] and ready.get("email"):
        out.append("email")
    if d["platform"] == "twitch" and ready.get("whisper"):
        out.append("whisper")
    return out


# ---------------------------------------------------------------- drafts

def _you(cfg: dict) -> dict:
    o = cfg.get("outreach") or {}
    return {"name": o.get("your_name") or "a GTA clip channel", "channel": o.get("your_channel") or ""}


def draft(cfg: dict, d: dict, kind: str = "first", channel: str = "email") -> dict:
    """Personalised message. kind: first | followup. Whisper drafts fit Twitch's 500-char limit."""
    you = _you(cfg)
    where = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}.get(d["platform"], "")
    title = (d.get("last_title") or "").strip()
    days = d.get("days_seen") or 0
    seen = (f"I've caught your GTA streams {days} times in the last two weeks" if days >= 3
            else "I caught your GTA stream recently")
    about = f' ("{title[:60]}")' if title and channel == "email" else ""
    chan = f" ({you['channel']})" if you["channel"] else ""
    if kind == "first":
        subject = "Clipping your GTA streams? (credit to you on every clip)"
        body = (f"Hi {d['name']},\n\n"
                f"{seen}{about} and your moments are exactly what does well as Shorts. I'm {you['name']}{chan}, "
                f"and I'd love your permission to cut your best GTA moments into Shorts, TikToks and Reels.\n\n"
                f"Every clip credits you on screen and in the caption, and I'll follow any rules you like "
                f"(hashtags, platforms, length, or topics to avoid).\n\n"
                f"Would that be OK? A quick yes or no is perfect.\n\n"
                f"Thanks,\n{you['name']}\n\n"
                f"(If you'd rather not hear from me, just say so and I won't message again.)")
        short = (f"Hi {d['name']}! {seen} on {where}. I'm {you['name']}{chan}: could I have your permission to "
                 f"clip your best GTA moments into Shorts/TikToks/Reels? Every clip credits you on screen and in "
                 f"the caption, and I'll follow any rules you set. A quick yes/no is perfect. "
                 f"(If you'd rather not hear from me, just say so.)")
    else:
        subject = "Re: Clipping your GTA streams?"
        body = (f"Hi {d['name']},\n\nJust bumping this in case it got buried: would you be OK with me clipping "
                f"your GTA streams, credited to you on every clip? A yes or no is all I need, and I won't "
                f"follow up again.\n\nThanks,\n{you['name']}")
        short = (f"Hi {d['name']}, quick bump in case it got buried: OK if I clip your GTA streams, credited to "
                 f"you? A yes/no is all I need, and I won't message again.")
    text = short if channel == "whisper" else body
    if (cfg.get("llm") or {}).get("provider", "none") != "none" and kind == "first":
        text = _ai_polish(cfg, text, channel) or text
    if channel == "whisper":
        text = text[:WHISPER_LIMIT]
    return {"subject": subject, "body": text}


def _ai_polish(cfg: dict, text: str, channel: str) -> str | None:
    from . import writer
    out = writer._ask(cfg, "Rewrite this message so it sounds natural and personal, keeping every fact, the offer, "
                           "the credit promise and the opt-out sentence. No emojis, no exaggeration, "
                           f"{'max 480 characters' if channel == 'whisper' else 'max 120 words'}. "
                           f'Return JSON {{"text": "..."}}.\n\n{text}')
    t = (out or {}).get("text", "").strip()
    if not t or "rather not hear" not in t:  # never drop the opt-out
        return None
    return t


def dm(cfg: dict, d: dict) -> str:
    """The copy-and-send version for platforms without an automatic channel."""
    return draft(cfg, d, "first", "whisper")["body"]


# ---------------------------------------------------------------- queue

def queue(cfg: dict, con, d: dict, channel: str, now: datetime | None = None) -> int:
    """You approved contacting this streamer on this channel: store the exact message to send."""
    setup(con)
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    if d.get("status") in ("no", "ignore"):
        raise ValueError(f"{d['name']} is marked '{d['status']}' - not contacting them")
    if con.execute("SELECT 1 FROM outreach WHERE platform=? AND login=? AND kind='first' AND status IN ('queued','sent')",
                   (d["platform"], d["login"])).fetchone():
        raise ValueError(f"{d['name']} is already queued or contacted")
    to = contacts(d.get("bio") or "")["email"] if channel == "email" else d["login"]
    if not to:
        raise ValueError(f"no public email found for {d['name']}")
    m = draft(cfg, d, "first", channel)
    cur = con.execute("INSERT INTO outreach (platform, login, channel, kind, to_addr, subject, body, status, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (d["platform"], d["login"], channel, "first", to, m["subject"],
                                                      m["body"], "queued", _iso(now or datetime.now(timezone.utc))))
    con.commit()
    return cur.lastrowid


def cancel(con, msg_id: int) -> None:
    setup(con)
    if not con.execute("UPDATE outreach SET status='cancelled' WHERE id=? AND status='queued'", (msg_id,)).rowcount:
        raise ValueError("only queued messages can be cancelled")
    con.commit()


def _sent_today(con, now: datetime) -> list[str]:
    return [r[0] for r in con.execute("SELECT sent_at FROM outreach WHERE status='sent' AND sent_at >= ?",
                                      (_iso(now - timedelta(hours=24)),))]


def plan_followups(cfg: dict, con, now: datetime) -> int:
    """Queue one follow-up for first messages that got no answer after followup_days."""
    o = cfg.get("outreach") or {}
    if not o.get("followup", True):
        return 0
    cutoff = _iso(now - timedelta(days=float(o.get("followup_days", 5))))
    rows = con.execute("""SELECT f.*, p.status AS pstatus, p.name, p.bio FROM outreach f
                          JOIN prospects p ON p.platform=f.platform AND p.login=f.login
                          WHERE f.kind='first' AND f.status='sent' AND f.sent_at <= ?
                            AND NOT EXISTS (SELECT 1 FROM outreach g WHERE g.platform=f.platform AND g.login=f.login
                                            AND g.kind='followup')""", (cutoff,)).fetchall()
    n = 0
    for r in rows:
        if r["pstatus"] != "contacted":  # they answered (yes/no) or you marked them otherwise
            continue
        m = draft(cfg, {"platform": r["platform"], "login": r["login"], "name": r["name"]}, "followup", r["channel"])
        con.execute("INSERT INTO outreach (platform, login, channel, kind, to_addr, subject, body, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (r["platform"], r["login"], r["channel"], "followup", r["to_addr"],
                                                    m["subject"], m["body"], "queued", _iso(now)))
        n += 1
    con.commit()
    return n


def send_due(cfg: dict, con, senders: dict, now: datetime | None = None, dry_run: bool = False) -> list[dict]:
    """Send queued messages within the limits. senders[channel](row) -> raises on failure."""
    setup(con)
    o = cfg.get("outreach") or {}
    now = now or datetime.now(timezone.utc)
    lo, hi = o.get("hours", (cfg.get("publish") or {}).get("hours", [9, 21]))
    if not (lo <= now.astimezone().hour < hi):
        return [{"skipped": f"outside sending hours {lo}:00-{hi}:00"}]
    plan_followups(cfg, con, now)
    sent = _sent_today(con, now)
    cap, gap = int(o.get("daily_max", 10)), timedelta(minutes=float(o.get("min_gap_minutes", 10)))
    results = []
    for r in con.execute("""SELECT o.*, p.status AS pstatus FROM outreach o
                            LEFT JOIN prospects p ON p.platform=o.platform AND p.login=o.login
                            WHERE o.status='queued' ORDER BY o.kind='followup', o.created_at""").fetchall():
        if r["pstatus"] in ("no", "ignore", "yes"):
            con.execute("UPDATE outreach SET status='cancelled', error=? WHERE id=?",
                        (f"streamer marked '{r['pstatus']}'", r["id"]))
            continue
        if len(sent) >= cap:
            results.append({"skipped": f"daily limit {cap} reached"})
            break
        if sent and now - max(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                              for s in sent) < gap:
            results.append({"skipped": "waiting for the gap between messages"})
            break
        send = senders.get(r["channel"])
        if send is None:
            results.append({"id": r["id"], "skipped": f"{r['channel']} isn't set up"})
            continue
        if dry_run:
            results.append({"id": r["id"], "would_send": r["channel"], "to": r["to_addr"], "body": r["body"]})
            continue
        try:
            send(r)
        except Exception as e:  # noqa: BLE001 - any sender failure is recorded, never retried blindly
            con.execute("UPDATE outreach SET status='failed', error=? WHERE id=?", (str(e)[:500], r["id"]))
            results.append({"id": r["id"], "error": str(e)[:200]})
            continue
        con.execute("UPDATE outreach SET status='sent', sent_at=? WHERE id=?", (_iso(now), r["id"]))
        con.execute("UPDATE prospects SET status='contacted' WHERE platform=? AND login=? AND status='new'",
                    (r["platform"], r["login"]))
        sent.append(_iso(now))
        results.append({"id": r["id"], "sent": r["channel"], "to": r["to_addr"], "kind": r["kind"]})
    con.commit()
    return results


# ---------------------------------------------------------------- senders

def email_sender(conf: dict):
    """SMTP (e.g. Gmail with an app password). conf: smtp_host, smtp_port, username, password, from_name."""
    def send(row):
        msg = EmailMessage()
        msg["From"] = formataddr((conf.get("from_name") or conf["username"], conf["username"]))
        msg["To"] = row["to_addr"]
        msg["Subject"] = row["subject"]
        msg["Message-ID"] = make_msgid()
        msg.set_content(row["body"])
        with smtplib.SMTP(conf.get("smtp_host", "smtp.gmail.com"), int(conf.get("smtp_port", 587)), timeout=30) as s:
            s.starttls()
            s.login(conf["username"], conf["password"])
            s.send_message(msg)
    return send


TWITCH_AUTH = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"


def connect_twitch(client_id: str, client_secret: str, token_file: Path, port: int = 8897, open_browser=True) -> None:
    """Sign in as the Twitch account you'll whisper from (needs a verified phone number on Twitch)."""
    state = pysecrets.token_urlsafe(16)
    redirect = f"http://localhost:{port}/callback"
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Connected - you can close this tab." if "code" in got else b"Sign-in failed.")

    srv = HTTPServer(("127.0.0.1", port), H)
    url = TWITCH_AUTH + "?" + urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect,
                                                      "response_type": "code", "scope": "user:manage:whispers",
                                                      "state": state})
    print(f"Opening Twitch sign-in (add {redirect} as an OAuth Redirect URL in your Twitch app). "
          f"If the browser doesn't open, visit:\n{url}")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    while not got:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise SystemExit(f"Twitch sign-in failed: {got.get('error_description') or got.get('error', 'unknown')}")
    tok = net.request("POST", TWITCH_TOKEN, form={"client_id": client_id, "client_secret": client_secret,
                                                  "code": got["code"], "grant_type": "authorization_code",
                                                  "redirect_uri": redirect}).json()
    me = net.request("GET", "https://api.twitch.tv/helix/users", headers={
        "Client-Id": client_id, "Authorization": f"Bearer {tok['access_token']}"}).json()["data"][0]
    _save(token_file, {**tok, "user_id": me["id"], "login": me["login"]})
    print(f"Twitch connected as {me['login']} - whispers will come from this account.")


def _save(path: Path, tok: dict) -> None:
    tok = {**tok, "expires_at": time.time() + float(tok.get("expires_in", 14000)) - 120}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tok), encoding="utf-8")


def twitch_user_token(client_id: str, client_secret: str, token_file: Path) -> dict:
    t = json.loads(token_file.read_text(encoding="utf-8"))
    if t.get("expires_at", 0) <= time.time():
        new = net.request("POST", TWITCH_TOKEN, form={"client_id": client_id, "client_secret": client_secret,
                                                      "grant_type": "refresh_token",
                                                      "refresh_token": t["refresh_token"]}).json()
        t = {**t, **new}
        _save(token_file, t)
    return t


def whisper_sender(client_id: str, client_secret: str, token_file: Path):
    """Twitch whispers (Helix POST /whispers). Twitch's own limits apply on top of ours."""
    def send(row):
        t = twitch_user_token(client_id, client_secret, token_file)
        auth = {"Client-Id": client_id, "Authorization": f"Bearer {t['access_token']}"}
        users = net.request("GET", "https://api.twitch.tv/helix/users", params={"login": row["to_addr"]},
                            headers=auth).json().get("data", [])
        if not users:
            raise RuntimeError(f"Twitch user {row['to_addr']} not found")
        net.request("POST", "https://api.twitch.tv/helix/whispers", ok=(200, 204),
                    params={"from_user_id": t["user_id"], "to_user_id": users[0]["id"]},
                    json_body={"message": row["body"][:WHISPER_LIMIT]}, headers=auth)
    return send


def build_senders(sec: dict, token_dir: Path) -> dict:
    """The automatic channels that are set up: {"email": fn, "whisper": fn}."""
    out = {}
    e = sec.get("outreach_email") or {}
    if e.get("username") and e.get("password"):
        out["email"] = email_sender(e)
    t = sec.get("twitch") or {}
    if t.get("client_id") and t.get("client_secret") and (token_dir / "twitch_user.json").exists():
        out["whisper"] = whisper_sender(t["client_id"], t["client_secret"], token_dir / "twitch_user.json")
    return out
