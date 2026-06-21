"""L0 transport — fetch raw HTML, stdlib only (``urllib``), zero dependencies.

The ONLY network boundary in M0. Everything downstream operates on the snapshot
this writes (``raw.html`` + ``headers.json`` blobs), never the live network, so
re-runs are deterministic and replay against the captured bytes.

A URL is not a filesystem path, so the *local id* — the sidecar key — is derived
deterministically from the normalized URL: a readable slug + a short blake2b hash
(collision-resistant, stable across runs). Local files and ``file://`` URLs are
accepted too, which keeps the whole L0 tier testable with no network.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_UA = os.environ.get(
    "HTMLDRILL_UA", "htmldrill/0.1 (+https://github.com/WulfKolbe/htmldrill)")
DEFAULT_TIMEOUT = float(os.environ.get("HTMLDRILL_TIMEOUT", "20"))


def normalize_url(url: str) -> str:
    """Light normalization for stable id derivation (not full canonicalization)."""
    u = url.strip()
    if "://" not in u and not u.startswith("/") and "." in u.split("/")[0]:
        u = "https://" + u            # bare host like example.com → https://
    return u


def is_local(url: str) -> bool:
    if url.startswith("file://"):
        return True
    if "://" in url:
        return False
    return True                        # no scheme → treat as a local path


def local_id_for(url: str) -> str:
    """Deterministic sidecar key: <slug>-<hash8> from the normalized URL/path."""
    norm = normalize_url(url)
    p = urlparse(norm)
    if p.scheme in ("http", "https"):
        base = (p.netloc + p.path).rstrip("/")
    else:
        base = Path(norm.replace("file://", "")).name or norm
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()[:40] or "page"
    h = hashlib.blake2b(norm.encode("utf-8"), digest_size=4).hexdigest()
    return f"{slug}-{h}"


class FetchResult:
    def __init__(self, url: str, final_url: str, status: int,
                 headers: dict, body: bytes, content_type: str):
        self.url = url
        self.final_url = final_url
        self.status = status
        self.headers = headers
        self.body = body
        self.content_type = content_type

    @property
    def text(self) -> str:
        # Prefer charset from Content-Type; fall back to utf-8 (lenient).
        enc = "utf-8"
        m = re.search(r"charset=([\w\-]+)", self.content_type, re.I)
        if m:
            enc = m.group(1)
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
          ua: Optional[str] = None) -> FetchResult:
    """Fetch http(s) URL (following redirects) or read a local file / file://."""
    norm = normalize_url(url)
    if is_local(norm):
        path = Path(norm.replace("file://", "")).expanduser().resolve()
        body = path.read_bytes()
        ctype = "text/html; charset=utf-8"
        return FetchResult(url, path.as_uri(), 200,
                           {"Content-Type": ctype, "Content-Length": str(len(body))},
                           body, ctype)
    req = Request(norm, headers={"User-Agent": ua or DEFAULT_UA,
                                 "Accept": "text/html,application/xhtml+xml,*/*"})
    with urlopen(req, timeout=timeout) as resp:        # noqa: S310 — http(s) only above
        body = resp.read()
        headers = {k: v for k, v in resp.headers.items()}
        ctype = resp.headers.get("Content-Type", "text/html")
        return FetchResult(url, resp.geturl(), resp.status, headers, body, ctype)
