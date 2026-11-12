"""X (Twitter) stats via the official API v2 (pay-per-use since 2026: ~$0.005 per post read).

Needs only a Bearer token (developer.x.com -> your app -> Keys and tokens) in secrets.yaml.
Because every read costs money, posts are read at a few checkpoints (48h, 7 days) instead of
hourly, and a monthly read cap protects against surprise bills.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets as pysecrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import net

LOOKUP = "https://api.x.com/2/tweets"
POST_ID = re.compile(r"(?:x|twitter)\.com/[^/]+/status(?:es)?/(\d{5,})")


def post_id(url: str) -> str | None:
    m = POST_ID.search(url or "")
    return m.group(1) if m else None


def metrics(bearer: str, ids: list[str]) -> dict[str, dict]:
    """id -> {views, likes, comments, shares}. Deleted / protected posts are absent.
    views = the post's impression count (what X shows as "Views"); if a video's own view count is
    present and larger, that's used. shares = reposts + quotes. Up to 100 ids per request."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        r = net.request("GET", LOOKUP, params={
            "ids": ",".join(ids[i:i + 100]), "tweet.fields": "public_metrics",
            "expansions": "attachments.media_keys", "media.fields": "public_metrics"},
            headers={"Authorization": f"Bearer {bearer}"}).json()
        media = {m["media_key"]: (m.get("public_metrics") or {}).get("view_count", 0)
                 for m in (r.get("includes") or {}).get("media", [])}
        for t in r.get("data", []):
            pm = t.get("public_metrics") or {}
            vid_views = max([media.get(k, 0) or 0 for k in (t.get("attachments") or {}).get("media_keys", [])] or [0])
            out[str(t["id"])] = {
                "views": int(max(pm.get("impression_count", 0) or 0, vid_views)),
                "likes": int(pm.get("like_count", 0) or 0),
                "comments": int(pm.get("reply_count", 0) or 0),
                "shares": int((pm.get("retweet_count", 0) or 0) + (pm.get("quote_count", 0) or 0)),
            }
    return out


# ---------------------------------------------------------------- posting (OAuth 2.0 user context)

AUTHORIZE = "https://x.com/i/oauth2/authorize"
TOKEN = "https://api.x.com/2/oauth2/token"
MEDIA = "https://api.x.com/2/media/upload"
TWEETS = "https://api.x.com/2/tweets"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
CHUNK = 4 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)


def _client_auth(client_id: str, client_secret: str | None) -> tuple[dict, dict]:
    """Confidential apps (with a secret) use Basic auth; public 'native' apps send client_id."""
    if client_secret:
        b = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return {"Authorization": f"Basic {b}"}, {}
    return {}, {"client_id": client_id}


def _save(path: Path, tok: dict) -> None:
    tok = {**tok, "expires_at": time.time() + float(tok.get("expires_in", 7200)) - 120}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tok), encoding="utf-8")


def connect(client_id: str, client_secret: str | None, token_file: Path, port: int = 8898,
            open_browser: bool = True) -> None:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = pysecrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{port}/callback"
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

    try:
        srv = HTTPServer(("127.0.0.1", port), H)
    except OSError:
        raise SystemExit(f"Port {port} is busy. Set x.port in config.yaml to another port and register "
                         f"http://127.0.0.1:<port>/callback as the callback URL in your X app.")
    url = AUTHORIZE + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Opening X sign-in (callback {redirect} must be registered in your app). "
          f"If the browser doesn't open, visit:\n{url}")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    while not got:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise SystemExit(f"X sign-in failed: {got.get('error_description') or got.get('error', 'unknown')}")
    headers, extra = _client_auth(client_id, client_secret)
    tok = net.request("POST", TOKEN, headers=headers, form={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "code_verifier": verifier, **extra}).json()
    _save(token_file, tok)
    print(f"X connected. Token saved to {token_file}")


def has_scopes(token_file: Path, needed=("tweet.write", "media.write")) -> bool:
    if not token_file.exists():
        return False
    granted = json.loads(token_file.read_text(encoding="utf-8")).get("scope", "").split()
    return all(n in granted for n in needed)


def access_token(client_id: str, client_secret: str | None, token_file: Path) -> str:
    if not token_file.exists():
        raise RuntimeError("X isn't connected. Run: python -m clipfarm connect x")
    t = json.loads(token_file.read_text(encoding="utf-8"))
    if t.get("expires_at", 0) > time.time():
        return t["access_token"]
    headers, extra = _client_auth(client_id, client_secret)
    new = net.request("POST", TOKEN, headers=headers, form={
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"], **extra}).json()
    _save(token_file, {**t, **new})  # X rotates refresh tokens: keep the new one
    return new["access_token"]


def _multipart(fields: dict, file_field: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----clipfarm" + pysecrets.token_hex(12)
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="blob"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_video(token: str, video: Path, max_wait: float = 600) -> str:
    """Chunked upload (initialize -> append 4 MB segments -> finalize -> wait for processing)."""
    data = Path(video).read_bytes()
    auth = {"Authorization": f"Bearer {token}"}
    init = net.request("POST", f"{MEDIA}/initialize", headers=auth, json_body={
        "media_type": "video/mp4", "total_bytes": len(data), "media_category": "tweet_video"}).json()
    mid = str((init.get("data") or init)["id"])
    for i in range(0, max(1, len(data)), CHUNK):
        body, ctype = _multipart({"segment_index": i // CHUNK}, "media", data[i:i + CHUNK])
        net.request("POST", f"{MEDIA}/{mid}/append", data=body, ok=(200, 201, 202, 204), timeout=900,
                    headers={**auth, "Content-Type": ctype})
    fin = net.request("POST", f"{MEDIA}/{mid}/finalize", headers=auth, ok=(200, 201, 202)).json()
    info = (fin.get("data") or fin).get("processing_info")
    waited = 0.0
    while info and info.get("state") not in ("succeeded", None):
        if info.get("state") == "failed":
            raise RuntimeError(f"X couldn't process the video: {(info.get('error') or {}).get('message', info)}")
        if waited >= max_wait:
            raise RuntimeError("X is still processing the video - will retry next run")
        wait = float(info.get("check_after_secs", 5))
        net.sleep(wait)
        waited += wait
        st = net.request("GET", MEDIA, headers=auth, params={"command": "STATUS", "media_id": mid}).json()
        info = (st.get("data") or st).get("processing_info")
    return mid


def clean_text(text: str) -> str:
    """Links make an X post cost ~13x more (2026 pricing), so they're stripped."""
    return " ".join(URL_RE.sub("", text).split())[:280]


def post(token: str, video: Path, text: str) -> tuple[str, str]:
    mid = upload_video(token, video)
    r = net.request("POST", TWEETS, headers={"Authorization": f"Bearer {token}"},
                    json_body={"text": clean_text(text), "media": {"media_ids": [mid]}}).json()
    tid = str(r["data"]["id"])
    return tid, f"https://x.com/i/status/{tid}"
