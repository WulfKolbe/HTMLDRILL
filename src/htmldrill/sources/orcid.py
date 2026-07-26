"""ORCID — every work from a researcher's record, via the public JSON API.

An ORCID record page (``orcid.org/0000-…``) is a JS SPA that loads works from the
public API and hides the tail behind a "Show more works" button. But the API
returns the WHOLE grouped work list in one call — no pagination, no browser:

    GET https://pub.orcid.org/v3.0/<orcid-id>/works   (Accept: application/json)

htmldrill recognises the host, hits that endpoint, and synthesises a single HTML
works list (one ``<li id title links>`` per work) that the rest of the pipeline
ingests as structured ListItems. No auth is needed for public works; a record
with none simply yields zero (reported honestly, not an empty snapshot).

Modular (anydrill/known-host pattern): a recogniser + an id extractor + a build
function, pure except for an injectable JSON fetch (tests run offline).
"""
from __future__ import annotations

import json
import re
from html import escape
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .known_hosts import host_of, is_url

_ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")

#: url -> parsed JSON. Injectable so tests never touch the network.
JsonFetch = Callable[[str], dict]


def orcid_id(s: str) -> "str | None":
    m = _ORCID_RE.search(s or "")
    return m.group(0) if m else None


def is_orcid(url: str) -> bool:
    """True for an orcid.org / pub.orcid.org record URL carrying an ORCID id."""
    if not is_url(url):
        return False
    host = host_of(url)
    return host.endswith("orcid.org") and orcid_id(urlparse(url).path) is not None


def works_api(oid: str) -> str:
    return f"https://pub.orcid.org/v3.0/{oid}/works"


def default_json_fetch(timeout: float = 25.0, ua: "str | None" = None) -> JsonFetch:
    def _f(url: str) -> dict:
        req = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": ua or "htmldrill/0.1"})
        with urlopen(req, timeout=timeout) as r:               # noqa: S310
            return json.loads(r.read())
    return _f


def _work_from_group(group: dict) -> dict:
    ws = (group.get("work-summary") or [{}])[0]
    title = (((ws.get("title") or {}).get("title") or {}) or {}).get("value") or ""
    pd = ws.get("publication-date") or {}
    year = ((pd.get("year") or {}) or {}).get("value") if pd else None
    links: list[str] = []
    for eid in ((ws.get("external-ids") or {}).get("external-id") or []):
        url = ((eid.get("external-id-url") or {}) or {}).get("value")
        if url:
            links.append(url)
        elif eid.get("external-id-type") == "doi" and eid.get("external-id-value"):
            links.append(f"https://doi.org/{eid['external-id-value']}")
    return {"id": str(ws.get("put-code") or ""), "title": title,
            "year": year, "links": links}


def _build_html(oid: str, works: list[dict]) -> str:
    lis = []
    for w in works:
        body = f'<span class="title">{escape(w["title"])}</span>'
        if w["year"]:
            body += f' ({escape(str(w["year"]))})'
        for url in w["links"]:
            body += f' <a href="{escape(url, quote=True)}">[link]</a>'
        lis.append(f'<li id="orcid-{escape(w["id"])}">{body}</li>')
    return (f"<html><head><title>ORCID {escape(oid)} — {len(works)} works</title></head>"
            f"<body><h1>ORCID {escape(oid)}</h1><ol>{''.join(lis)}</ol></body></html>")


def fetch_all_works(url_or_id: str, fetch_json: JsonFetch) -> "tuple[str, int]":
    """(works_html, count) for an ORCID record. The API returns every work group
    in one call, so there is nothing to paginate."""
    oid = orcid_id(url_or_id) or url_or_id
    data = fetch_json(works_api(oid))
    works = [_work_from_group(g) for g in (data.get("group") or [])]
    works = [w for w in works if w["title"]]
    return _build_html(oid, works), len(works)
