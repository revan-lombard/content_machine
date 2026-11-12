"""Instagram Reels + Facebook Page Reels (Meta Graph API), uploading the local file directly.

Needs: an Instagram Business/Creator account linked to a Facebook Page, and a Meta app.
One-time:  python -m clipfarm connect meta --token <user token from Graph API Explorer>
That exchanges it for a long-lived token and stores a Page token, which doesn't expire.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import net

PERMISSIONS = ["pages_show_list", "pages_read_engagement", "pages_manage_posts",
               "instagram_basic", "instagram_content_publish", "instagram_manage_insights",
               "business_management"]


def graph(ver: str) -> str:
    return f"https://graph.facebook.com/{ver}"


def connect(app_id: str, app_secret: str, user_token: str, ver: str, token_file: Path,
            page: str | None = None) -> dict:
    """Exchange a short-lived user token, pick the Page (+ its Instagram account), save the Page token."""
    long = net.request("GET", f"{graph(ver)}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
        "fb_exchange_token": user_token}).json()["access_token"]
    pages = net.request("GET", f"{graph(ver)}/me/accounts", params={
        "fields": "id,name,access_token,instagram_business_account", "access_token": long}).json().get("data", [])
    if not pages:
        raise SystemExit("No Facebook Pages found for this login. Did you grant pages_show_list?")
    if page:
        pages = [p for p in pages if page.lower() in (p["id"], p["name"].lower())]
        if not pages:
            raise SystemExit(f"No Page matching '{page}'")
    if len(pages) > 1:
        names = ", ".join(f"{p['name']} ({p['id']})" for p in pages)
        raise SystemExit(f"Several Pages found: {names}. Run again with --page NAME")
    p = pages[0]
    conf = {"page_id": p["id"], "page_name": p["name"], "page_token": p["access_token"],
            "ig_user_id": (p.get("instagram_business_account") or {}).get("id")}
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(conf), encoding="utf-8")
    print(f"Meta connected: Page '{p['name']}'"
          + (f", Instagram account {conf['ig_user_id']}" if conf["ig_user_id"] else
             " - no Instagram account is linked to this Page, so only Facebook will be used"))
    return conf


def load(token_file: Path) -> dict:
    if not token_file.exists():
        raise RuntimeError("Meta isn't connected. Run: python -m clipfarm connect meta --token ...")
    return json.loads(token_file.read_text(encoding="utf-8"))


def _upload_bytes(url: str, token: str, video: Path) -> None:
    data = Path(video).read_bytes()
    net.request("POST", url, data=data, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))}, timeout=900)


def post_instagram(conf: dict, video: Path, caption: str, ver: str, max_wait: float = 600) -> tuple[str, str]:
    """Reel to Instagram. Returns (media_id, permalink)."""
    if not conf.get("ig_user_id"):
        raise RuntimeError("No Instagram account linked to the connected Facebook Page")
    tok, ig = conf["page_token"], conf["ig_user_id"]
    c = net.request("POST", f"{graph(ver)}/{ig}/media", form={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
        "share_to_feed": "true", "access_token": tok}).json()
    cid = c["id"]
    _upload_bytes(c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{ver}/{cid}", tok, video)
    waited = 0.0
    while True:  # Instagram processes the video before it can be published
        st = net.request("GET", f"{graph(ver)}/{cid}", params={
            "fields": "status_code,status", "access_token": tok}).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram rejected the video: {st.get('status')}")
        if waited >= max_wait:
            raise RuntimeError("Instagram is still processing the video - will retry next run")
        net.sleep(10)
        waited += 10
    mid = net.request("POST", f"{graph(ver)}/{ig}/media_publish", form={
        "creation_id": cid, "access_token": tok}).json()["id"]
    link = net.request("GET", f"{graph(ver)}/{mid}", params={
        "fields": "permalink", "access_token": tok}).json().get("permalink") or f"https://www.instagram.com/reel/{mid}"
    return mid, link


def post_facebook(conf: dict, video: Path, caption: str, ver: str) -> tuple[str, str]:
    """Reel to the Facebook Page. Returns (video_id, url). Meta allows 30 API Reels per Page per 24h."""
    tok, page = conf["page_token"], conf["page_id"]
    s = net.request("POST", f"{graph(ver)}/{page}/video_reels", form={
        "upload_phase": "start", "access_token": tok}).json()
    vid = s["video_id"]
    _upload_bytes(s.get("upload_url") or f"https://rupload.facebook.com/video-upload/{ver}/{vid}", tok, video)
    net.request("POST", f"{graph(ver)}/{page}/video_reels", form={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
        "description": caption[:2200], "access_token": tok})
    return vid, f"https://www.facebook.com/reel/{vid}"


def instagram_stats(conf: dict, media_id: str, ver: str) -> dict:
    """views / likes / comments / shares / saved / reach for one Instagram media.

    Meta renames and retires metrics from time to time (e.g. `plays` became `views` in 2025),
    and one unknown metric fails the whole request. So: try the full set, then views alone,
    and always fall back to like_count / comments_count on the media itself."""
    tok = conf["page_token"]
    out: dict = {}
    for metrics in ("views,likes,comments,shares,saved,reach", "views"):
        try:
            r = net.request("GET", f"{graph(ver)}/{media_id}/insights",
                            params={"metric": metrics, "access_token": tok}).json()
        except net.HttpError as e:
            if e.status == 400:
                continue  # an unsupported metric: try a smaller set
            raise
        for m in r.get("data", []):
            vals = m.get("values") or []
            v = vals[0].get("value") if vals else (m.get("total_value") or {}).get("value")
            if isinstance(v, (int, float)):
                out[m["name"]] = int(v)
        if "views" in out:
            break
    if "likes" not in out or "comments" not in out:
        node = net.request("GET", f"{graph(ver)}/{media_id}", params={
            "fields": "like_count,comments_count", "access_token": tok}).json()
        out.setdefault("likes", int(node.get("like_count", 0) or 0))
        out.setdefault("comments", int(node.get("comments_count", 0) or 0))
    return out


SHORTCODE = re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)")


def shortcode(url: str) -> str | None:
    m = SHORTCODE.search(url or "")
    return m.group(1) if m else None


def recent_media(conf: dict, ver: str, limit: int = 500) -> dict[str, str]:
    """shortcode -> media id for the connected account's recent posts (newest first)."""
    out: dict[str, str] = {}
    url = f"{graph(ver)}/{conf['ig_user_id']}/media"
    params = {"fields": "id,permalink,shortcode", "limit": 50, "access_token": conf["page_token"]}
    while url and len(out) < limit:
        r = net.request("GET", url, params=params).json()
        for m in r.get("data", []):
            code = m.get("shortcode") or shortcode(m.get("permalink", ""))
            if code:
                out[code] = m["id"]
        url = (r.get("paging") or {}).get("next")
        params = None  # the `next` link already carries every parameter
    return out
