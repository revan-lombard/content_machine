"""YouTube Shorts upload (YouTube Data API v3, OAuth for a desktop app).

One-time:  python -m clipfarm connect youtube   (opens Google sign-in in your browser)

Note: until Google approves an audit of your API project, every upload is locked to
PRIVATE, whatever privacy you ask for. Apply for the audit early (see README).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import net

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def connect(client_id: str, client_secret: str, token_file: Path, open_browser=True) -> None:
    """Sign in once in the browser; stores a refresh token in token_file."""
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = pysecrets.token_urlsafe(16)
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

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Opening Google sign-in. If the browser doesn't open, visit:\n" + url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    while not got:
        srv.handle_request()
    if "code" not in got:
        raise SystemExit(f"YouTube sign-in failed: {got.get('error', 'unknown error')}")
    tok = net.request("POST", TOKEN, form={
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}).json()
    if "refresh_token" not in tok:
        raise SystemExit("Google didn't return a refresh token - remove the app's access at "
                         "https://myaccount.google.com/permissions and run connect again.")
    _save(token_file, {**tok, "expires_at": time.time() + tok.get("expires_in", 3600) - 60})
    print(f"YouTube connected. Token saved to {token_file}")


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def access_token(client_id: str, client_secret: str, token_file: Path) -> str:
    if not token_file.exists():
        raise RuntimeError("YouTube isn't connected. Run: python -m clipfarm connect youtube")
    t = json.loads(token_file.read_text(encoding="utf-8"))
    if t.get("expires_at", 0) > time.time() and t.get("access_token"):
        return t["access_token"]
    new = net.request("POST", TOKEN, form={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}).json()
    t.update(access_token=new["access_token"], expires_at=time.time() + new.get("expires_in", 3600) - 60)
    _save(token_file, t)
    return t["access_token"]


def upload(token: str, video: Path, title: str, description: str, tags: list[str],
           privacy: str = "public") -> tuple[str, str]:
    """Resumable upload. Returns (video_id, url)."""
    data = Path(video).read_bytes()
    meta = {"snippet": {"title": title[:100], "description": description[:5000],
                        "tags": [t.lstrip("#") for t in tags][:15], "categoryId": "20"},  # 20 = Gaming
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    start = net.request("POST", UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                        json_body=meta, headers={
                            "Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                            "X-Upload-Content-Length": str(len(data))})
    location = start.headers.get("location")
    if not location:
        raise RuntimeError("YouTube didn't return an upload URL")
    done = net.request("PUT", location, data=data, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, timeout=900).json()
    vid = done["id"]
    return vid, f"https://youtube.com/shorts/{vid}"
