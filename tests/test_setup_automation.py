"""Setup automation: wizard, scheduled tasks, backups, update, doctor. Run:  python -m pytest -q"""
import json
import shutil
import types
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from clipfarm import config, net, ops, sources, tracker, wizard
from clipfarm.net import Response

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def root(tmp_path):
    for f in ("config.yaml", "sources.yaml"):
        shutil.copy(REPO / f, tmp_path / f)
    return tmp_path


def proc(code=0, out="", err=""):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


# ---------------------------------------------------------------- scheduled tasks

def test_schedule_creates_every_task(root):
    calls = []
    lines = ops.schedule(root, run=lambda cmd, **k: calls.append(cmd) or proc())
    assert len(calls) == len(ops.TASKS) and all(l.startswith("scheduled") for l in lines)
    daily = next(c for c in calls if "ClipFarm - daily clips" in c)
    tr = daily[daily.index("/TR") + 1]
    assert ".venv" in tr and "-m clipfarm clip --all" in tr and "clip.log" in tr and f'cd /d "{root}"' in tr
    assert daily[daily.index("/SC") + 1] == "DAILY" and daily[-1] == "/F"
    pub = next(c for c in calls if "ClipFarm - publisher" in c)
    assert pub[pub.index("/SC") + 1] == "ONLOGON" and "publish --watch" in pub[pub.index("/TR") + 1]
    calls.clear()
    ops.schedule(root, remove=True, run=lambda cmd, **k: calls.append(cmd) or proc(1, err="not found"))
    assert all(c[:2] == ["schtasks", "/Delete"] for c in calls)


# ---------------------------------------------------------------- backups

def test_backup_contents_and_rotation(root):
    con = tracker.connect(root / "data" / "tracker.db")
    tracker.register_clip(con, "c1")
    (root / "secrets.yaml").write_text("twitch: {client_id: x}\n")
    (root / "data" / "tokens").mkdir()
    (root / "data" / "tokens" / "meta.json").write_text("{}")
    cfg = config.load(root / "config.yaml")
    cfg["backup"] = {"dir": str(root / "bk"), "keep": 2}
    for day in (1, 2, 3):
        z = ops.backup(cfg, root, now=datetime(2026, 11, day, 23, 40))
    assert sorted(p.name for p in (root / "bk").iterdir()) == ["clipfarm-2026-11-02_2340.zip",
                                                               "clipfarm-2026-11-03_2340.zip"]
    names = zipfile.ZipFile(z).namelist()
    assert {"data/tracker.db", "secrets.yaml", "sources.yaml", "config.yaml", "data/tokens/meta.json"} <= set(names)
    with zipfile.ZipFile(z) as zf:
        zf.extract("data/tracker.db", root / "restore")
    restored = tracker.connect(root / "restore" / "data" / "tracker.db")
    assert restored.execute("SELECT id FROM clips").fetchone()[0] == "c1"


def test_backup_dir_prefers_onedrive(root, monkeypatch):
    od = root / "OneDrive"
    od.mkdir()
    monkeypatch.setenv("OneDrive", str(od))
    assert ops.backup_dir({"backup": {"dir": ""}}, root) == od / "ClipFarm backups"
    monkeypatch.delenv("OneDrive")
    assert ops.backup_dir({}, root) == root / "backups"


# ---------------------------------------------------------------- update

def test_update_stops_when_pull_fails(root):
    calls = []
    out = ops.update(root, run=lambda cmd, **k: calls.append(cmd) or proc(1, err="local changes"))
    assert len(calls) == 1 and "stopped" in out[-1]
    calls.clear()
    out = ops.update(root, run=lambda cmd, **k: calls.append(cmd) or proc(0, out="Updated"))
    assert calls[0][:3] == ["git", "-C", str(root)] and "pip" in calls[1] and out[-1] == "packages up to date"


# ---------------------------------------------------------------- doctor

def test_doctor_reports_what_is_missing(root):
    cfg = config.load(root / "config.yaml")
    rows = {item: (status, fix) for _, item, status, fix in ops.checks(cfg, root, run=lambda *a, **k: proc(1))}
    assert rows["Twitch API keys"][0] == "missing" and "wizard" in rows["Twitch API keys"][1]
    assert rows["approved streamers (0)"][0] == "missing"
    assert rows["X connected"][0] == "optional"
    (root / "secrets.yaml").write_text("twitch: {client_id: a, client_secret: b}\n")
    rows = {item: status for _, item, status, _ in ops.checks(cfg, root, run=lambda *a, **k: proc(1))}
    assert rows["Twitch API keys"] == "ok"


# ---------------------------------------------------------------- wizard

class Script:
    """Answers the wizard's questions in order and records what it asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, prompt):
        self.asked.append(prompt)
        assert self.answers, f"wizard asked more than scripted: {prompt}"
        return self.answers.pop(0)


def test_wizard_core_path(root, monkeypatch):
    tokens = []

    def fake(method, url, headers, data, timeout):
        f = urllib.parse.parse_qs((data or b"").decode())
        if "id.twitch.tv/oauth2/token" in url:
            ok = f.get("client_secret") == ["good"]
            return Response(200 if ok else 400, {}, b'{"access_token":"t"}' if ok else b"{}")
        return Response(404, {}, b"{}")

    monkeypatch.setattr(net, "transport", fake)
    cfg = config.load(root / "config.yaml")
    answers = Script(
        "Revan", "youtube.com/@gtaclips",             # you
        "abc123", "bad", "y", "abc123", "good",       # twitch: wrong secret, retry, right one
        "y", "kick", "n3on", "campaign", "https://contentrewards.com/c/1",   # add a streamer
        "y", "twitch", "x", "written", "",            # second streamer missing proof -> refused
        "n",                                          # no more streamers
        "y", "APPID", "APPSECRET", "TOKEN123",        # meta
        "n", "n", "n", "n", "n",                      # youtube, tiktok, x, email, whispers
    )
    w = wizard.Wizard(cfg, root, ask=answers, open_url=lambda u: None,
                      connect=lambda platform, extra: tokens.append((platform, extra)), say=lambda *a: None)
    w.run()
    assert not answers.answers
    sec = yaml.safe_load((root / "secrets.yaml").read_text())
    assert sec["twitch"] == {"client_id": "abc123", "client_secret": "good"}
    assert sec["meta"] == {"app_id": "APPID", "app_secret": "APPSECRET"}
    assert tokens == [("meta", {"token": "TOKEN123"})]
    text = (root / "config.yaml").read_text()
    assert 'your_name: "Revan"' in text and 'your_channel: "youtube.com/@gtaclips"' in text
    assert "# how you sign messages" in text                       # comments kept
    approved = [s for s in sources.load_all(root / "sources.yaml") if not sources.problems(s)]
    assert [(s["platform"], s["login"]) for s in approved] == [("kick", "n3on")]


def test_wizard_rerun_keeps_existing(root):
    (root / "secrets.yaml").write_text("twitch: {client_id: a, client_secret: b}\n")
    cfg = config.load(root / "config.yaml")
    cfg["outreach"].update(your_name="R", your_channel="c")
    answers = Script("n", "n", "n", "n", "n", "n", "n", "n", "n", "n")  # "redo?" no, no streamers, skip the rest
    w = wizard.Wizard(cfg, root, ask=answers, open_url=lambda u: pytest.fail("opened a page"),
                      connect=lambda *a: pytest.fail("signed in"), say=lambda *a: None)
    w.run()
    assert yaml.safe_load((root / "secrets.yaml").read_text())["twitch"]["client_id"] == "a"


def test_wizard_email_step_validates_login(root, monkeypatch):
    class FakeSMTP:
        def __init__(self, host, port, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            pass

        def login(self, u, p):
            if p != "right":
                raise wizard.smtplib.SMTPAuthenticationError(535, b"bad")

    monkeypatch.setattr(wizard.smtplib, "SMTP", FakeSMTP)
    cfg = config.load(root / "config.yaml")
    w = wizard.Wizard(cfg, root, ask=Script("y", "me@gmail.com", "wrong"), open_url=lambda u: None, say=lambda *a: None)
    w.email()
    assert not (root / "secrets.yaml").exists()                    # failed login: nothing saved
    w = wizard.Wizard(cfg, root, ask=Script("y", "me@gmail.com", "right"), open_url=lambda u: None, say=lambda *a: None)
    w.email()
    assert yaml.safe_load((root / "secrets.yaml").read_text())["outreach_email"]["password"] == "right"
