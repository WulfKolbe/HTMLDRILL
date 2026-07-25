"""Pure-Python self-contained HTML archiver — inline every asset as a data: URI.

The stdlib alternative to the `monolith` Rust binary for `single` (no cargo, no
Rust). Combined with a headless-Chrome render it archives the JS-RENDERED DOM into
one offline file — something monolith can't do, because monolith never runs JS
(which is exactly why a Distill page's JS-built bibliography was missing from its
output).

Approach: regex-targeted rewriting of the three ways HTML references an asset —
``<link rel=stylesheet href>``, ``<img src>``, and CSS ``url(...)`` / ``@import``
(in <style> blocks, style="" attrs, and fetched stylesheets, recursively). Each
referenced byte-stream is fetched once and embedded as ``data:<mime>;base64,…``.
Stdlib only: urllib + base64 + mimetypes. Failures are skipped (the asset's
original URL is left in place), never fatal — the monolith ``-e`` posture.

Not a general HTML rewriter: it edits asset URLs, not document structure. Edge
cases it does NOT chase (documented, not silently dropped): ``srcset`` variants,
``<link rel=icon/preload>``, and CSS in cross-origin @font-face beyond one import
hop. Good enough for a faithful offline archive; monolith remains the choice when
maximal static-archive robustness matters.
"""
from __future__ import annotations

import base64
import mimetypes
import re
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from . import fetch as F

#: url -> (bytes, content_type). Injectable so tests run offline.
Fetcher = Callable[[str], "tuple[bytes, str]"]


def _default_fetcher(timeout: float, ua: Optional[str]) -> Fetcher:
    def _f(url: str) -> "tuple[bytes, str]":
        req = Request(url, headers={"User-Agent": ua or F.DEFAULT_UA})
        with urlopen(req, timeout=timeout) as r:                # noqa: S310
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            return r.read(), ctype
    return _f


def _mime_for(url: str, ctype: str) -> str:
    if ctype:
        return ctype
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    return guessed or "application/octet-stream"


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)
_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*(['"]?)([^'")]+)\1\s*\)|(['"])([^'"]+)\3)[^;]*;""", re.I)


def _inline_css(css: str, base: str, fetch: Fetcher, stats: dict, depth: int = 0) -> str:
    if depth > 5:
        return css

    def _imp(m: "re.Match") -> str:
        u = m.group(2) or m.group(4)
        try:
            data, _ = fetch(urljoin(base, u))
            return _inline_css(data.decode("utf-8", "replace"),
                               urljoin(base, u), fetch, stats, depth + 1)
        except Exception:                          # noqa: BLE001 — drop a broken import
            stats["failed"] += 1
            return ""

    css = _IMPORT_RE.sub(_imp, css)

    def _url(m: "re.Match") -> str:
        u = m.group(2).strip()
        if not u or u.startswith(("data:", "#")):
            return m.group(0)
        try:
            data, ctype = fetch(urljoin(base, u))
            stats["assets"] += 1
            return f"url({_data_uri(data, _mime_for(u, ctype))})"
        except Exception:                          # noqa: BLE001 — leave the ref
            stats["failed"] += 1
            return m.group(0)

    return _URL_RE.sub(_url, css)


_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)
_IMG_RE = re.compile(r"(<img\b[^>]*?\bsrc\s*=\s*)(['\"])(.*?)\2", re.I)
_STYLE_ATTR_RE = re.compile(r"(\bstyle\s*=\s*)(['\"])(.*?)\2", re.I | re.S)
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc\s*=\s*['\"][^'\"]*['\"][^>]*>\s*</script>", re.I)
_SCRIPT_ANY_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)


def inline_assets(html: str, base_url: str, *, fetch: Optional[Fetcher] = None,
                  timeout: float = 20.0, ua: Optional[str] = None,
                  strip_scripts: bool = False) -> "tuple[str, dict]":
    """Return (self_contained_html, stats). Inlines stylesheets, images, and CSS
    url()/@import as data: URIs. External ``<script src>`` is always dropped (it
    can't run offline); ``strip_scripts`` also removes inline scripts."""
    fetch = fetch or _default_fetcher(timeout, ua)
    stats = {"assets": 0, "stylesheets": 0, "failed": 0, "scripts_dropped": 0}

    # 1. <link rel=stylesheet href> → <style>…</style> (with its url()/@import inlined)
    def _link(m: "re.Match") -> str:
        tag = m.group(0)
        if not re.search(r"""rel\s*=\s*['"]?[^'">]*stylesheet""", tag, re.I):
            return tag
        href = re.search(r"""href\s*=\s*['"]([^'"]+)['"]""", tag, re.I)
        if not href or href.group(1).startswith("data:"):
            return tag
        u = href.group(1)
        try:
            data, _ = fetch(urljoin(base_url, u))
            css = _inline_css(data.decode("utf-8", "replace"),
                              urljoin(base_url, u), fetch, stats)
            stats["stylesheets"] += 1
            return f"<style>{css}</style>"
        except Exception:                          # noqa: BLE001
            stats["failed"] += 1
            return tag

    html = _LINK_RE.sub(_link, html)

    # 2. existing <style> blocks: inline their url()/@import
    html = _STYLE_BLOCK_RE.sub(
        lambda m: m.group(1) + _inline_css(m.group(2), base_url, fetch, stats) + m.group(3),
        html)

    # 3. <img src> → data: URI
    def _img(m: "re.Match") -> str:
        u = m.group(3).strip()
        if not u or u.startswith("data:"):
            return m.group(0)
        try:
            data, ctype = fetch(urljoin(base_url, u))
            stats["assets"] += 1
            return f"{m.group(1)}{m.group(2)}{_data_uri(data, _mime_for(u, ctype))}{m.group(2)}"
        except Exception:                          # noqa: BLE001
            stats["failed"] += 1
            return m.group(0)

    html = _IMG_RE.sub(_img, html)

    # 4. style="" attributes carrying url()
    html = _STYLE_ATTR_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}"
                  f"{_inline_css(m.group(3), base_url, fetch, stats)}{m.group(2)}",
        html)

    # 5. scripts: external refs can't run offline → always dropped; inline too on request
    if strip_scripts:
        html, n = _SCRIPT_ANY_RE.subn("", html)
    else:
        html, n = _SCRIPT_SRC_RE.subn("", html)
    stats["scripts_dropped"] = n

    return html, stats
