"""Setup wizard: one guided pass through everything the setup checklist used to ask you to do by hand.

It opens each developer page for you, asks you to paste what it shows, TESTS each key straight
away, writes secrets.yaml / config.yaml / sources.yaml, runs the sign-ins, and can schedule the
daily jobs. Every step can be skipped and the wizard can be run again any time - it keeps what's
already set unless you choose to redo a step.
"""
from __future__ import annotations

import re
import smtplib
import textwrap
import webbrowser
from pathlib import Path

import yaml

from . import net
from .config import ROOT

SECRETS_HEADER = ("# Written by the setup wizard (python -m clipfarm wizard). Step-by-step notes for every\n"
                  "# value are in secrets.example.yaml. Never share or upload this file.\n")


class Wizard:
    def __init__(self, cfg: dict, root: Path = ROOT, ask=input, secret=None, open_url=webbrowser.open,
                 connect=None, say=print):
        self.cfg, self.root, self.ask, self.open_url, self.say = cfg, root, ask, open_url, say
        self.secret = secret or ask
        self.connect = connect or _default_connect   # (platform, extra) -> runs the sign-in
        self.sec_path = root / "secrets.yaml"
        self.sec = (yaml.safe_load(self.sec_path.read_text(encoding="utf-8")) or {}) if self.sec_path.exists() else {}

    # ---------- helpers
    def yes(self, q: str, default: bool = False) -> bool:
        a = self.ask(f"{q} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not a else a.startswith("y")

    def value(self, q: str, current: str | None = None, hidden: bool = False) -> str:
        shown = f" [{'keep current' if hidden else current}]" if current else ""
        a = (self.secret if hidden else self.ask)(f"{q}{shown}: ").strip()
        return a or (current or "")

    def save_secrets(self) -> None:
        self.sec_path.write_text(SECRETS_HEADER + yaml.safe_dump(self.sec, sort_keys=False, allow_unicode=True),
                                 encoding="utf-8")

    def set_secret(self, section: str, **values) -> None:
        self.sec.setdefault(section, {}).update({k: v for k, v in values.items() if v is not None})
        self.save_secrets()

    def done(self, section: str, *keys) -> bool:
        return all((self.sec.get(section) or {}).get(k) for k in keys)

    def step(self, title: str, optional: bool, already: bool) -> bool:
        self.say(f"\n=== {title} ===")
        if already:
            return self.yes("Already set up. Redo it?", False)
        return self.yes("Set it up now?", not optional) if optional else True

    # ---------- steps
    def you(self) -> None:
        o = self.cfg.get("outreach") or {}
        if not self.step("You", False, bool(o.get("your_name") and o.get("your_channel"))):
            return
        name = self.value("Your name (how you sign messages to streamers)", o.get("your_name"))
        chan = self.value("Your clip channel link (e.g. youtube.com/@yourclips)", o.get("your_channel"))
        cfg_file = self.root / "config.yaml"
        text = cfg_file.read_text(encoding="utf-8")
        for key, val in (("your_name", name), ("your_channel", chan)):
            text = re.sub(rf'^(\s*{key}:\s*)"[^"\n]*"', lambda m: f'{m.group(1)}"{val}"', text, count=1, flags=re.M)
        cfg_file.write_text(text, encoding="utf-8")
        self.cfg.setdefault("outreach", {}).update(your_name=name, your_channel=chan)

    def twitch(self) -> None:
        if not self.step("Twitch API keys (needed to find moments)", False, self.done("twitch", "client_id", "client_secret")):
            return
        self.say("Opening the Twitch developer console. Register an application with:\n"
                 "  OAuth Redirect URLs: http://localhost  AND  http://localhost:8897/callback\n"
                 "  Category: Application Integration, Client type: Confidential\n"
                 "Then copy the Client ID and click New Secret.")
        self.open_url("https://dev.twitch.tv/console/apps")
        while True:
            cid = self.value("Twitch Client ID", (self.sec.get("twitch") or {}).get("client_id"))
            secret = self.value("Twitch Client Secret", (self.sec.get("twitch") or {}).get("client_secret"), hidden=True)
            try:
                net.request("POST", "https://id.twitch.tv/oauth2/token", form={
                    "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
                self.set_secret("twitch", client_id=cid, client_secret=secret)
                self.say("  Twitch keys work.")
                return
            except net.HttpError as e:
                self.say(f"  Twitch refused those keys (HTTP {e.status}). Check them and try again.")
                if not self.yes("Try again?", True):
                    return

    def streamer(self) -> None:
        from . import sources
        approved = [s for s in sources.load_all(self.root / "sources.yaml") if not sources.problems(s)]
        self.say(f"\n=== Streamers you're allowed to clip ({len(approved)} approved so far) ===")
        while self.yes("Add a streamer you have permission for?", not approved):
            plat = self.value("Platform: twitch, kick or youtube", "twitch").lower()
            if plat not in ("twitch", "kick", "youtube"):
                self.say("  Use twitch, kick or youtube.")
                continue
            login = self.value("Their channel name (twitch.tv/NAME, kick.com/NAME or YouTube @handle)").lstrip("/")
            ptype = self.value("Permission: campaign, written or public", "written").lower()
            proof = self.value("Proof: campaign link, screenshot path, or link to their public 'clip me'")
            if not login or ptype not in ("campaign", "written", "public") or not proof:
                self.say("  Skipped: a name, a valid permission type and proof are all needed.")
                continue
            entry = {"name": re.sub(r"[^a-z0-9_]+", "_", login.lower().lstrip("@")).strip("_"), "enabled": True,
                     "platform": plat, "login": login, "permission": {"type": ptype, "proof": proof},
                     "rules": {"credit": "@" + login.lstrip("@")}, "facecam": None}
            bad = sources.problems(entry)
            if bad:
                self.say("  Not added: " + "; ".join(bad))
                continue
            with open(self.root / "sources.yaml", "a", encoding="utf-8") as f:
                f.write("\n" + textwrap.indent(yaml.safe_dump([entry], sort_keys=False, allow_unicode=True), "  "))
            self.say(f"  Added {login}. Edit sources.yaml later for campaign rules (hashtags, length, platforms).")

    def youtube(self) -> None:
        if self.step("YouTube (posting + stats)", True, self.done("youtube_oauth", "client_id") and
                     (self.root / "data" / "tokens" / "youtube.json").exists()):
            self.say("In Google Cloud: create a project, enable 'YouTube Data API v3', set up the OAuth consent\n"
                     "screen (External, add yourself as a test user), then Credentials -> OAuth client ID ->\n"
                     "Desktop app, AND Credentials -> API key.")
            self.open_url("https://console.cloud.google.com/apis/library/youtube.googleapis.com")
            key = self.value("API key", (self.sec.get("youtube") or {}).get("api_key"), hidden=True)
            try:
                net.request("GET", "https://www.googleapis.com/youtube/v3/videos",
                            params={"part": "id", "id": "jNQXAC9IVRw", "key": key})
                self.set_secret("youtube", api_key=key)
                self.say("  API key works.")
            except net.HttpError as e:
                self.say(f"  YouTube refused the API key (HTTP {e.status}) - skipped, run the wizard again later.")
            cid = self.value("OAuth Client ID", (self.sec.get("youtube_oauth") or {}).get("client_id"))
            csec = self.value("OAuth Client Secret", (self.sec.get("youtube_oauth") or {}).get("client_secret"), hidden=True)
            if cid and csec:
                self.set_secret("youtube_oauth", client_id=cid, client_secret=csec)
                self._connect("youtube")

    def meta(self) -> None:
        if not self.step("Instagram + Facebook (posting + stats)", True, (self.root / "data" / "tokens" / "meta.json").exists()):
            return
        self.say("Needs: an Instagram Creator/Business account linked to a Facebook Page.\n"
                 "Create a Meta app (type Business), then copy its App ID and App Secret.")
        self.open_url("https://developers.facebook.com/apps")
        aid = self.value("App ID", (self.sec.get("meta") or {}).get("app_id"))
        asec = self.value("App Secret", (self.sec.get("meta") or {}).get("app_secret"), hidden=True)
        if not (aid and asec):
            return
        self.set_secret("meta", app_id=aid, app_secret=asec)
        from .meta_api import PERMISSIONS
        self.say("Opening Graph API Explorer: pick your app, add these permissions, click Generate Access Token:\n  "
                 + ", ".join(PERMISSIONS))
        self.open_url("https://developers.facebook.com/tools/explorer/")
        token = self.value("Paste the access token", hidden=True)
        if token:
            self._connect("meta", {"token": token})

    def tiktok(self) -> None:
        if not self.step("TikTok (posting drafts + stats)", True, (self.root / "data" / "tokens" / "tiktok.json").exists()):
            return
        self.say("Create a TikTok app with Login Kit (Desktop, redirect http://localhost:8899/callback/) and the\n"
                 "Content Posting API; scopes user.info.basic, video.list, video.upload. While in Sandbox, add your\n"
                 "TikTok account as a target user.")
        self.open_url("https://developers.tiktok.com/apps")
        key = self.value("Client key", (self.sec.get("tiktok") or {}).get("client_key"))
        sec = self.value("Client secret", (self.sec.get("tiktok") or {}).get("client_secret"), hidden=True)
        if key and sec:
            self.set_secret("tiktok", client_key=key, client_secret=sec)
            self._connect("tiktok")

    def x(self) -> None:
        if not self.step("X (paid: about 1.5c per post, 0.5c per stats read)", True,
                         (self.root / "data" / "tokens" / "x.json").exists()):
            return
        self.say("At developer.x.com: add credits under Billing, create an app. Copy the Bearer Token; under\n"
                 "User authentication settings -> OAuth 2.0, choose Read and write, callback\n"
                 "http://127.0.0.1:8898/callback, and copy the Client ID (and Secret).")
        self.open_url("https://developer.x.com/en/portal/dashboard")
        cur = self.sec.get("x") or {}
        bearer = self.value("Bearer Token", cur.get("bearer_token"), hidden=True)
        cid = self.value("OAuth 2.0 Client ID", cur.get("client_id"))
        csec = self.value("OAuth 2.0 Client Secret (blank if none)", cur.get("client_secret"), hidden=True)
        self.set_secret("x", bearer_token=bearer, client_id=cid, client_secret=csec)
        if cid:
            self._connect("x")

    def email(self) -> None:
        if not self.step("Outreach by email (asking streamers for permission)", True,
                         self.done("outreach_email", "username", "password")):
            return
        self.say("For Gmail: turn on 2-Step Verification, then create an App Password (16 characters).")
        self.open_url("https://myaccount.google.com/apppasswords")
        cur = self.sec.get("outreach_email") or {}
        user = self.value("Email address", cur.get("username"))
        pw = self.value("App password", cur.get("password"), hidden=True)
        host = cur.get("smtp_host") or "smtp.gmail.com"
        port = int(cur.get("smtp_port") or 587)
        try:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(user, pw)
            self.set_secret("outreach_email", smtp_host=host, smtp_port=port, username=user, password=pw,
                            from_name=cur.get("from_name") or (self.cfg.get("outreach") or {}).get("your_name", ""))
            self.say("  Email login works.")
        except (smtplib.SMTPException, OSError) as e:
            self.say(f"  Couldn't log in to {host}: {str(e)[:120]} - skipped, run the wizard again later.")

    def whispers(self) -> None:
        if not self.step("Outreach by Twitch whisper", True, (self.root / "data" / "tokens" / "twitch_user.json").exists()):
            return
        self.say("Your Twitch account needs a verified phone number. Sign in as the account you'll message from.")
        self._connect("twitch")

    def automation(self) -> None:
        import os
        from . import ops
        self.say("\n=== Run everything automatically ===")
        if os.name != "nt":
            self.say("  (Scheduled tasks are created on Windows - skipped here.)")
            return
        if self.yes("Schedule daily clips, discovery 3x a day, the publisher at logon and nightly backups?", True):
            for line in ops.schedule(self.root):
                self.say("  " + line)

    def _connect(self, platform: str, extra: dict | None = None) -> None:
        try:
            self.connect(platform, extra or {})
        except SystemExit as e:
            self.say(f"  {platform} sign-in didn't finish: {e}. Run the wizard again to retry.")

    def run(self) -> None:
        self.say("Clip farm setup. Press Enter to accept a [default]; every optional step can be skipped.")
        for fn in (self.you, self.twitch, self.streamer, self.meta, self.youtube, self.tiktok, self.x,
                   self.email, self.whispers, self.automation):
            fn()
        self.say("\nDone. Here's what's set up and what's left:\n")


def _default_connect(platform: str, extra: dict) -> None:
    import types

    from . import config
    from .__main__ import _connect
    _connect(config.load(), types.SimpleNamespace(platform=platform, token=extra.get("token"), page=None))
