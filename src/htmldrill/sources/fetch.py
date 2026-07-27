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

import gzip
import hashlib
import os
import re
import zlib
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_UA = os.environ.get(
    "HTMLDRILL_UA", "htmldrill/0.1 (+https://github.com/WulfKolbe/htmldrill)")
DEFAULT_TIMEOUT = float(os.environ.get("HTMLDRILL_TIMEOUT", "20"))

#: Hard cap on bytes we will read/keep (live fetch and local file). A multi-hundred
#: -MB page degrades gracefully (clear error) instead of hanging/OOM-ing the parser.
MAX_BYTES = int(os.environ.get("HTMLDRILL_MAX_BYTES", str(256 * 1024 * 1024)))


def _decompress(body: bytes, encoding: str) -> bytes:
    """Decode a Content-Encoding'd body (gzip / deflate / br). Best-effort: if a
    decoder is unavailable or the stream is not actually compressed, return the
    bytes unchanged rather than corrupting the snapshot."""
    enc = (encoding or "").lower().strip()
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate
        if enc == "br":
            import brotli  # type: ignore  # optional dependency
            return brotli.decompress(body)
    except Exception:
        return body
    return body


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


# magic-byte prefixes for common NON-html binary bodies a URL may return
_MAGIC = [
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),          # zip / docx / xlsx / epub
    (b"\x89PNG\r\n", "image"),
    (b"\xff\xd8\xff", "image"),       # jpeg
    (b"GIF8", "image"),
    (b"\x1f\x8b", "gzip"),            # arxiv e-print .tgz etc.
]


def content_kind(content_type: str, body: bytes) -> str:
    """Classify a fetched body as 'html' | 'pdf' | 'image' | 'zip' | 'gzip' |
    'text' | 'binary' — from the Content-Type first, then magic bytes. This is
    what stops htmldrill from text-decoding (and DESTROYING) a binary response:
    an arXiv /pdf URL returns application/pdf, and utf-8-decoding it replaces
    every non-utf8 byte with U+FFFD, corrupting the PDF beyond use."""
    ct = (content_type or "").lower()
    head = body[:8]
    for magic, kind in _MAGIC:                      # magic bytes are authoritative
        if head.startswith(magic):
            return kind
    if "html" in ct or "xhtml" in ct:
        return "html"
    if "pdf" in ct:
        return "pdf"
    if ct.startswith("image/"):
        return "image"
    if "zip" in ct or "epub" in ct or "officedocument" in ct:
        return "zip"
    if ct.startswith("text/") or "xml" in ct or "json" in ct or "javascript" in ct:
        return "text"
    # default: assume html only when nothing says otherwise (bare .html files,
    # servers that omit Content-Type) — the historical behaviour, now the fallback.
    return "html" if (not ct or "text" in ct) else "binary"


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
    def kind(self) -> str:
        """'html' | 'pdf' | 'image' | 'zip' | 'gzip' | 'text' | 'binary'."""
        return content_kind(self.content_type, self.body)

    @property
    def text(self) -> str:
        # Charset resolution order: Content-Type header → <meta charset> in the
        # first chunk of the body → utf-8 (lenient). This recovers pages that
        # declare their encoding only in markup (very common).
        enc = None
        m = re.search(r"charset=([\w\-]+)", self.content_type, re.I)
        if m:
            enc = m.group(1)
        if not enc:
            head = self.body[:4096]
            m2 = re.search(rb"charset=[\"']?([\w\-]+)", head, re.I)
            if m2:
                enc = m2.group(1).decode("ascii", "ignore")
        enc = enc or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except (LookupError, TypeError):
            return self.body.decode("utf-8", errors="replace")


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
          ua: Optional[str] = None) -> FetchResult:
    """Fetch http(s) URL (following redirects) or read a local file / file://."""
    norm = normalize_url(url)
    if is_local(norm):
        path = Path(norm.replace("file://", "")).expanduser().resolve()
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(
                f"{path} is {size} bytes — exceeds HTMLDRILL_MAX_BYTES ({MAX_BYTES}); "
                f"raise the limit to process it.")
        body = path.read_bytes()
        ctype = "text/html; charset=utf-8"
        return FetchResult(url, path.as_uri(), 200,
                           {"Content-Type": ctype, "Content-Length": str(len(body))},
                           body, ctype)
    req = Request(norm, headers={
        "User-Agent": ua or DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        # Observed on a real WAF (ubiquitypress, 2026-07): UA alone -> 403,
        # UA+Accept -> 403, UA+Accept-Language -> 200. Its absence was the
        # whole difference between a fetch and a refusal.
        "Accept-Language": "en-US,en;q=0.9",
        # Advertise the encodings we can actually undo — otherwise some origins
        # send gzip anyway and urllib hands back the raw compressed bytes.
        "Accept-Encoding": "gzip, deflate",
    })
    # An HTTP-level refusal (401/403/429/404) is an ANSWER about the resource, and
    # the pagekind lattice has terminals for exactly those answers. Letting
    # HTTPError propagate meant `access=auth_required` and `access=rate_limited`
    # could never be produced through the real fetch path — every terminal that
    # says "stop" was unreachable in practice. An HTTPError IS a response object,
    # so it is read like one. A TRANSPORT failure (DNS, refused, timeout) still
    # raises: there is no response, and nothing to classify.
    try:
        resp = urlopen(req, timeout=timeout)          # noqa: S310 — http(s) only above
    except HTTPError as err:
        resp = err
    try:
        # Bounded read: never pull more than MAX_BYTES + 1 (the +1 detects overflow).
        body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError(
                f"{norm} response exceeds HTMLDRILL_MAX_BYTES ({MAX_BYTES}); "
                f"raise the limit to process it.")
        headers = {k: v for k, v in resp.headers.items()}
        body = _decompress(body, resp.headers.get("Content-Encoding", ""))
        ctype = resp.headers.get("Content-Type", "text/html")
        status = getattr(resp, "status", None) or resp.getcode()
        return FetchResult(url, resp.geturl(), status, headers, body, ctype)
    finally:
        resp.close()
