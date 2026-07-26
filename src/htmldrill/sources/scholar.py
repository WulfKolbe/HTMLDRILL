"""Google Scholar citations — the cheapest-richest route past the "Show more" wall.

A Scholar profile (``scholar.google.com/citations?user=…&view_op=list_works``)
shows only the first 20 works; the rest hide behind a "Show more" button. But that
button is pure sugar over URL pagination: ``&cstart=<offset>&pagesize=100`` returns
100 rows per plain GET, no JavaScript, no button clicks, no browser. So instead of
driving a headless browser to click "Show more" N times (fragile, slow), htmldrill
recognises the host and walks ``cstart`` 0,100,200,… until a short page, then
MERGES every page's publication rows into one complete document — all N works in a
single snapshot the rest of the pipeline (size/links/model/single) consumes.

Modular by design (the anydrill/known-host pattern): a recogniser + a paginate-and
-merge function, pure except for an injectable ``fetch`` (tests run offline).
"""
from __future__ import annotations

import re
from html import escape, unescape
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .known_hosts import host_of, is_url

#: the works table body and one work row on a Scholar citations page
_TBODY_RE = re.compile(r'(<tbody id="gsc_a_b">)(.*?)(</tbody>)', re.S | re.I)
_ROW_RE = re.compile(r'class="gsc_a_tr"')
_ROW_ITER = re.compile(r'<tr class="gsc_a_tr">.*?</tr>', re.S)
_TITLE_RE = re.compile(r'<a href="([^"]+)" class="gsc_a_at">(.*?)</a>', re.S)
_GRAY_RE = re.compile(r'<div class="gs_gray">(.*?)</div>', re.S)
_YEAR_RE = re.compile(r'class="gsc_a_h[^"]*">(\d{4})')
_CITES_RE = re.compile(r'class="gsc_a_ac[^"]*"[^>]*>(\d+)</a>')
_CFV_RE = re.compile(r'citation_for_view=([^&"]+)')

DEFAULT_PAGESIZE = 100
MAX_PAGES = 20                      # 20*100 = 2000 works — far above any real profile


def is_scholar_citations(url: str) -> bool:
    """True for a Scholar author profile works-list URL (any google TLD)."""
    if not is_url(url):
        return False
    host = host_of(url)
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query))
    return (host.startswith("scholar.google.")
            and parsed.path.rstrip("/").endswith("/citations")
            and "user" in q)


def _page_url(url: str, cstart: int, pagesize: int) -> str:
    """The same profile URL with cstart/pagesize set (replacing any existing)."""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query)
         if k not in ("cstart", "pagesize")]
    q += [("cstart", str(cstart)), ("pagesize", str(pagesize))]
    return urlunparse(p._replace(query=urlencode(q)))


def _extract_rows(html: str) -> "tuple[str, int]":
    """(inner-tbody-html, row-count) for a Scholar works page, or ('', 0)."""
    m = _TBODY_RE.search(html or "")
    if not m:
        return "", 0
    body = m.group(2)
    return body, len(_ROW_RE.findall(body))


def _text(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html or "")).strip()


def _parse_row(row: str) -> dict:
    """Extract one Scholar work row into title/url/authors/venue/year/citations."""
    t = _TITLE_RE.search(row)
    url = unescape(t.group(1)) if t else ""
    if url.startswith("/"):
        url = "https://scholar.google.com" + url
    grays = _GRAY_RE.findall(row)
    yr = _YEAR_RE.search(row)
    cites = _CITES_RE.search(row)
    cfv = _CFV_RE.search(url)
    return {
        "id": (cfv.group(1).replace(":", "-") if cfv else ""),
        "title": _text(t.group(2)) if t else "",
        "url": url,
        "authors": _text(grays[0]) if grays else "",
        "venue": _text(grays[1]) if len(grays) > 1 else "",
        "year": yr.group(1) if yr else "",
        "citations": cites.group(1) if cites else "0",
    }


def _build_html(works: list[dict]) -> str:
    """A CLEAN, static works list — no Scholar JavaScript, no 'Show more' button,
    so it opens standalone in a browser and ingests as structured ListItems."""
    lis = []
    for w in works:
        if not w["title"]:
            continue
        body = f'<span class="title">{escape(w["title"])}</span>'
        meta = " — ".join(x for x in (w["authors"], w["venue"]) if x)
        if meta:
            body += f" — {escape(meta)}"
        if w["year"]:
            body += f' ({escape(w["year"])})'
        if w["citations"] not in ("", "0"):
            body += f' [cited by {escape(w["citations"])}]'
        if w["url"]:
            body += f' <a href="{escape(w["url"], quote=True)}">[link]</a>'
        lis.append(f'<li id="scholar-{escape(w["id"])}">{body}</li>')
    return (f'<html><head><meta charset="utf-8">'
            f"<title>Google Scholar — {len(lis)} publications</title></head>"
            f"<body><h1>Google Scholar — {len(lis)} publications</h1>"
            f"<ol>{''.join(lis)}</ol></body></html>")


def fetch_all_works(url: str, fetch: Callable[[str], str], *,
                    pagesize: int = DEFAULT_PAGESIZE,
                    max_pages: int = MAX_PAGES) -> "tuple[str, int, int]":
    """Paginate cstart/pagesize until a short page, extract every work row across
    all pages, and build ONE clean static works list. ``fetch`` maps a URL → HTML
    text (tests inject a fake). Returns (works_html, total_works, pages_fetched)."""
    rows: list[str] = []
    pages = 0
    for page in range(max_pages):
        html = fetch(_page_url(url, page * pagesize, pagesize))
        body, n = _extract_rows(html)
        rows.extend(_ROW_ITER.findall(body))
        pages += 1
        if n < pagesize:            # a short (or empty) page = the last page
            break
    works = [_parse_row(r) for r in rows]
    return _build_html(works), len(works), pages
