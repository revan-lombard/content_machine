"""TikTok stats via the Display API (Login Kit for Desktop + video.list).

One-time:  python -m clipfarm connect tiktok   (opens TikTok sign-in in your browser)

The Display API only returns videos from the account that signed in, so connect the TikTok
account you post clips with. Posting itself stays manual (see README).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets as pysecrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import net

AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
QUERY = "https://open.tiktokapis.com/v2/video/query/"
SCOPES = "user.info.basic,video.list"
FIELDS = "id,view_count,like_count,comment_count,share_count,create_time"
VIDEO_ID = re.compile(r"tiktok\.com/(?:@[^/]+/video|v|embed(?:/v2)?)/(\d{8,})")


def video_id(url: str) -> str | None:
    """Video id from a full TikTok link. vm.tiktok.com short links don't contain it."""
    m = VIDEO_ID.search(url or "")
    return m.group(1) if m else None


def pkce() -> tuple[str, str]:
    """TikTok's desktop flow wants the challenge as HEX-encoded SHA-256 (not base64url like Google)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(pysecrets.choice(alphabet) for _ in range(64))
    return verifier, hashlib.sha256(verifier.encode()).hexdigest()


def connect(client_key: str, client_secret: str, token_file: Path, port: int = 8899,
            open_browser: bool = True) -> None:
    verifier, challenge = pkce()
    state = pysecrets.token_urlsafe(16)
    redirect = f"http://localhost:{port}/callback/"
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items()})
            msg = b"Connected - you can close this tab." if "code" in got else b"Sign-in failed or cancelled."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg)

    try:
        srv = HTTPServer(("127.0.0.1", port), H)
    except OSError:
        raise SystemExit(f"Port {port} is busy. Set tiktok.port in config.yaml to another port "
                         f"and register http://localhost:<port>/callback/ in your TikTok app.")
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_key": client_key, "scope": SCOPES, "response_type": "code", "redirect_uri": redirect,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Opening TikTok sign-in (redirect {redirect} must be registered in your app). "
          f"If the browser doesn't open, visit:\n{url}")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    while not got:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise SystemExit(f"TikTok sign-in failed: {got.get('error_description') or got.get('error', 'unknown')}")
    tok = net.request("POST", TOKEN, form={
        "client_key": client_key, "client_secret": client_secret, "code": got["code"],
        "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier}).json()
    if "access_token" not in tok:
        raise SystemExit(f"TikTok didn't return a token: {tok.get('error_description') or tok}")
    _save(token_file, tok)
    print(f"TikTok connected (scopes: {tok.get('scope', SCOPES)}). Token saved to {token_file}")


def _save(path: Path, tok: dict) -> None:
    now = time.time()
    tok = {**tok, "expires_at": now + float(tok.get("expires_in", 86400)) - 120,
           "refresh_expires_at": now + float(tok.get("refresh_expires_in", 365 * 86400)) - 3600}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tok), encoding="utf-8")


def access_token(client_key: str, client_secret: str, token_file: Path) -> str:
    if not token_file.exists():
        raise RuntimeError("TikTok isn't connected. Run: python -m clipfarm connect tiktok")
    t = json.loads(token_file.read_text(encoding="utf-8"))
    if t.get("expires_at", 0) > time.time():
        return t["access_token"]
    if t.get("refresh_expires_at", 0) <= time.time():
        raise RuntimeError("TikTok sign-in has expired. Run: python -m clipfarm connect tiktok")
    new = net.request("POST", TOKEN, form={
        "client_key": client_key, "client_secret": client_secret,
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}).json()
    if "access_token" not in new:
        raise RuntimeError(f"TikTok token refresh failed: {new.get('error_description') or new}")
    _save(token_file, {**t, **new})  # the refresh token can rotate: keep the new one
    return new["access_token"]


def query(token: str, ids: list[str]) -> dict[str, dict]:
    """Stats for your own videos, 20 ids per request. Returns id -> {views, likes, comments, shares}.
    Ids that aren't the connected account's videos are simply absent from the result."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 20):
        r = net.request("POST", QUERY, params={"fields": FIELDS}, json_body={"filters": {"video_ids": ids[i:i + 20]}},
                        headers={"Authorization": f"Bearer {token}"}).json()
        err = (r.get("error") or {})
        if err.get("code") not in (None, "ok"):
            raise RuntimeError(f"TikTok: {err.get('code')}: {err.get('message', '')}")
        for v in (r.get("data") or {}).get("videos", []):
            out[str(v["id"])] = {"views": int(v.get("view_count", 0) or 0), "likes": int(v.get("like_count", 0) or 0),
                                 "comments": int(v.get("comment_count", 0) or 0),
                                 "shares": int(v.get("share_count", 0) or 0)}
    return out
