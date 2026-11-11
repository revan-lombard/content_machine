"""Tiny HTTP helper (stdlib only). Tests replace `transport` to fake the platforms."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body or b"{}")


class HttpError(RuntimeError):
    def __init__(self, method, url, status, body):
        safe = url.split("?")[0]  # never print tokens from query strings
        super().__init__(f"{method} {safe} -> HTTP {status}: {body[:500]!r}")
        self.status, self.body = status, body


def _urllib(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


transport = _urllib
sleep = time.sleep


def request(method: str, url: str, *, params: dict | None = None, form: dict | None = None,
            json_body=None, data: bytes | None = None, headers: dict | None = None,
            timeout: float = 300, ok=(200, 201)) -> Response:
    headers = dict(headers or {})
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    for attempt in range(3):
        r = transport(method, url, headers, data, timeout)
        if r.status in (429, 500, 502, 503, 504) and attempt < 2:
            sleep(5 * (attempt + 1))
            continue
        break
    if r.status not in ok:
        raise HttpError(method, url, r.status, r.body)
    return r
