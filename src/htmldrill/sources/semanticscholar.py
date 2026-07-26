"""Semantic Scholar — every paper of an author, via the public Graph API.

A Semantic Scholar author page (``semanticscholar.org/author/<name>/<id>``) is a
JS SPA that pages its paper list. The Graph API returns the papers directly, with
clean ``offset``/``limit`` pagination (``next`` is the offset of the following
page, absent on the last):

    GET https://api.semanticscholar.org/graph/v1/author/<id>/papers
        ?fields=title,year,externalIds,url&offset=<n>&limit=100

htmldrill extracts the author id from the URL, walks ``next`` until it is absent,
and synthesises one HTML paper list (``<li id title links>`` per paper) for the
pipeline. No key is needed (subject to the API's anonymous rate limit).

Modular (anydrill/known-host pattern): recogniser + id extractor + paginate/merge,
pure except for an injectable JSON fetch (tests run offline).
"""
from __future__ import annotations

import json
import re
from html import escape
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .known_hosts import host_of, is_url

JsonFetch = Callable[[str], dict]

DEFAULT_LIMIT = 100
MAX_PAGES = 50
_FIELDS = "title,year,externalIds,url"


def author_id(url: str) -> "str | None":
    """The numeric author id from …/author/<slug>/<id> or …/author/<id>."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "author" in parts:
        tail = parts[parts.index("author") + 1:]
        for seg in reversed(tail):
            if seg.isdigit():
                return seg
    return None


def is_s2_author(url: str) -> bool:
    if not is_url(url):
        return False
    host = host_of(url)
    return host.endswith("semanticscholar.org") and author_id(url) is not None


def papers_api(aid: str, offset: int, limit: int) -> str:
    return (f"https://api.semanticscholar.org/graph/v1/author/{aid}/papers"
            f"?fields={_FIELDS}&offset={offset}&limit={limit}")


def default_json_fetch(timeout: float = 25.0, ua: "str | None" = None) -> JsonFetch:
    def _f(url: str) -> dict:
        req = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": ua or "htmldrill/0.1"})
        with urlopen(req, timeout=timeout) as r:               # noqa: S310
            return json.loads(r.read())
    return _f


def _li(paper: dict) -> str:
    title = escape(paper.get("title") or "")
    if not title:
        return ""
    body = f'<span class="title">{title}</span>'
    if paper.get("year"):
        body += f' ({escape(str(paper["year"]))})'
    url = paper.get("url")
    if url:
        body += f' <a href="{escape(str(url), quote=True)}">[link]</a>'
    doi = (paper.get("externalIds") or {}).get("DOI")
    if doi:
        body += f' <a href="https://doi.org/{escape(str(doi), quote=True)}">[doi]</a>'
    return f'<li id="s2-{escape(str(paper.get("paperId") or ""))}">{body}</li>'


def fetch_all_works(url_or_id: str, fetch_json: JsonFetch, *,
                    limit: int = DEFAULT_LIMIT,
                    max_pages: int = MAX_PAGES) -> "tuple[str, int, int]":
    """(papers_html, count, pages) — walk offset/next to the last page, merge."""
    aid = author_id(url_or_id) or url_or_id
    papers: list[dict] = []
    offset = 0
    pages = 0
    for _ in range(max_pages):
        data = fetch_json(papers_api(aid, offset, limit))
        papers.extend(data.get("data") or [])
        pages += 1
        nxt = data.get("next")
        if nxt is None:
            break
        offset = int(nxt)
    lis = [x for x in (_li(p) for p in papers) if x]
    html = (f"<html><head><title>Semantic Scholar author {escape(aid)} — "
            f"{len(lis)} papers</title></head><body>"
            f"<h1>Semantic Scholar author {escape(aid)}</h1>"
            f"<ol>{''.join(lis)}</ol></body></html>")
    return html, len(lis), pages
