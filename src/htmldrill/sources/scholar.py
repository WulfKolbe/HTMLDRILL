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
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .known_hosts import host_of, is_url

#: the works table body and one work row on a Scholar citations page
_TBODY_RE = re.compile(r'(<tbody id="gsc_a_b">)(.*?)(</tbody>)', re.S | re.I)
_ROW_RE = re.compile(r'class="gsc_a_tr"')

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


def fetch_all_works(url: str, fetch: Callable[[str], str], *,
                    pagesize: int = DEFAULT_PAGESIZE,
                    max_pages: int = MAX_PAGES) -> "tuple[str, int, int]":
    """Paginate cstart/pagesize until a short page, then merge every page's work
    rows into the first page's document. ``fetch`` maps a URL → HTML text (so tests
    inject a fake). Returns (merged_html, total_works, pages_fetched)."""
    first_html = ""
    all_rows: list[str] = []
    total = 0
    pages = 0
    for page in range(max_pages):
        html = fetch(_page_url(url, page * pagesize, pagesize))
        if page == 0:
            first_html = html
        rows, n = _extract_rows(html)
        all_rows.append(rows)
        total += n
        pages += 1
        if n < pagesize:            # a short (or empty) page = the last page
            break

    merged = first_html
    if all_rows and _TBODY_RE.search(first_html or ""):
        merged = _TBODY_RE.sub(
            lambda m: m.group(1) + "".join(all_rows) + m.group(3),
            first_html, count=1)
    return merged, total, pages
