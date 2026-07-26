"""Command handlers — return prose strings (PDFDRILL/CHATDRILL convention).

A small ``Ctx`` carries the resolved args; each handler takes it, does ONE cheap
thing, appends to the sidecar, and returns text. Idempotency is structural: a
command records a fact and, on re-run, detects it and returns the cached view
unless ``--force``.

L0 tier (this module): every command except ``fetch`` operates on the snapshot
``fetch`` captured — no network, no headless browser. Snapshot commands hard-gate
on the ``FETCHED`` fact and tell the user to run ``fetch`` first if it's absent
(they are deliberately NOT wired as planner ``requires:`` of ``fetch``, so
``--ensure`` can never trigger a network round-trip).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import planner
from .parse import html as H
from .sidecar import Sidecar, resolve_local_id, work_root
from .sources import capture as CAP
from .sources import fetch as F
from .sources import inline as INLINE
from .sources import known_hosts as K
from .sources import monolith as MONO
from .sources import scholar as SCH
from .sources import print_pdf as P
from .sources import render as R

# -- facts (module scope) ----------------------------------------------------
FETCHED = "FETCHED"
SIZE_KNOWN = "SIZE_KNOWN"
META_KNOWN = "META_KNOWN"
CANONICAL_KNOWN = "CANONICAL_KNOWN"
LINKS_KNOWN = "LINKS_KNOWN"
JSONLD_KNOWN = "JSONLD_KNOWN"
MICRODATA_KNOWN = "MICRODATA_KNOWN"
OPENGRAPH_KNOWN = "OPENGRAPH_KNOWN"
FEEDS_KNOWN = "FEEDS_KNOWN"
OUTLINE_KNOWN = "OUTLINE_KNOWN"
# L1 (render)
RENDERED = "RENDERED"
TEXT_KNOWN = "TEXT_KNOWN"
COMPARED = "COMPARED"
# L1b (print — the htmldrill -> pdfdrill bridge)
PRINTED = "PRINTED"
# L1c (capture — scroll-materialize a real page, then grab DOM + PDF + screenshot)
CAPTURED = "CAPTURED"
# L1d (single — monolith: freeze the page + all assets into one self-contained HTML)
SINGLE = "SINGLE"
# L0/known-host (arxiv — recognise the source, take the cheapest richest route)
ARXIV_KNOWN = "ARXIV_KNOWN"
# L0/known-host (scholar — paginate past the "Show more" wall, merge all works)
SCHOLAR = "SCHOLAR"
# L5 (model)
MODEL_BUILT = "MODEL_BUILT"
# L6 (projectors — offline)
TIDDLERS_BUILT = "TIDDLERS_BUILT"
MD_BUILT = "MD_BUILT"
LLMTEXT_BUILT = "LLMTEXT_BUILT"
# L6 (latex projector — offline; html2latex, optional dep)
LATEX_BUILT = "LATEX_BUILT"
# L4 (split recovery — lazy-load / virtualization)
SPLITS_KNOWN = "SPLITS_KNOWN"
MATERIALIZED = "MATERIALIZED"
# M5 (crawl / retrieve / chatlog)
CRAWLED = "CRAWLED"
RETRIEVED = "RETRIEVED"
CHATLOGGED = "CHATLOGGED"


@dataclass
class Ctx:
    url: Optional[str] = None         # URL / local path, or (for status/steps) an id prefix
    work: Optional[str] = None
    force: bool = False
    as_json: bool = False
    target: Optional[str] = None      # for `steps`
    out: Optional[str] = None
    ua: Optional[str] = None
    timeout: float = F.DEFAULT_TIMEOUT
    window: str = "1280,900"          # render viewport
    render_delta: bool = False        # materialize: headless virtual-time diff mode
    # M5 (crawl / retrieve / chatlog)
    query: Optional[str] = None       # retrieve: the question (space-joined words)
    k: int = 8                        # retrieve: top-k units
    ask: Optional[str] = None         # chatlog: a Q to append (with --answer)
    answer: Optional[str] = None      # chatlog: the answer text for --ask
    units: Optional[str] = None       # chatlog: comma-separated grounding unit ids
    model_name: str = ""              # chatlog: the LLM name recorded with the turn
    depth: int = 1                    # crawl: max link depth from the start url
    engine: str = "firefox"           # print: firefox (selenium) | chrome
    no_js: bool = False               # single: strip JavaScript from the archive
    isolate: bool = False             # single: cut the archive off from the network
    download_pdf: bool = False        # arxiv: also fetch the PDF
    download_source: bool = False     # arxiv: also fetch the e-print LaTeX .tgz
    max_pages: int = 20               # crawl: hard cap on pages visited
    same_origin: bool = True          # crawl: restrict to same-origin internal links


# -- id resolution -----------------------------------------------------------

def _resolve_id(ctx: Ctx) -> str:
    """Map the positional (a URL/path or an existing id prefix) to a sidecar id."""
    if not ctx.url:
        raise ValueError("a URL (or an existing sidecar id) is required")
    cand = F.local_id_for(ctx.url)
    root = work_root(ctx.work)
    if (root / f"{cand}.htmldrill.json").exists():
        return cand
    # `ctx.url` may be an absolute path (a non-relative glob pattern, which
    # Path.glob rejects); only try prefix-resolution when it's a bare id token.
    if "/" not in ctx.url and "\\" not in ctx.url:
        return resolve_local_id(ctx.url, ctx.work)
    return cand


def _load_snapshot(ctx: Ctx) -> tuple[Sidecar, str]:
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not sc.has(FETCHED):
        raise FileNotFoundError(
            f"no fetched snapshot for {ctx.url!r} — run `htmldrill fetch {ctx.url}` first")
    kind = sc.get_evidence("content_kind", "html")
    if kind != "html":
        blob = sc.get_evidence("raw_blob", f"raw.{kind}")
        raise ValueError(
            f"the fetched resource is a {kind.upper()}, not HTML "
            f"({sc.blob_path(blob)}). Snapshot commands need HTML — for a PDF, hand "
            f"it to pdfdrill (`pdfdrill md {sc.blob_path(blob)}`); for an arXiv URL "
            f"try `htmldrill arxiv {ctx.url}`.")
    html = sc.read_blob("raw.html") or ""
    return sc, html


def _prev(sc: Sidecar, fact: str) -> str:
    return ",".join(sorted(sc.facts - {fact})) or "INIT"


def _base_url(sc: Sidecar) -> str:
    return sc.get_evidence("final_url") or sc.get_evidence("url") or ""


# -- fetch (the only network command) ----------------------------------------

def cmd_fetch(ctx: Ctx) -> str:
    if not ctx.url:
        raise ValueError("usage: htmldrill fetch <url>")
    lid = F.local_id_for(ctx.url)
    sc = Sidecar(lid, work=ctx.work)
    if sc.has(FETCHED) and not ctx.force:
        return _fetch_report(sc, cached=True)
    t0 = time.perf_counter()
    res = F.fetch(ctx.url, timeout=ctx.timeout, ua=ctx.ua)
    kind = res.kind
    # Store LOSSLESSLY by kind. Text-decoding a binary body (PDF/zip/image) to
    # write raw.html replaces every non-utf8 byte with U+FFFD — it destroyed a
    # 7-page arXiv PDF into 811k replacement chars. HTML → text raw.html; anything
    # else → raw bytes in raw.<ext>, and the snapshot commands refuse to parse it.
    if kind == "html":
        blob = "raw.html"
        sc.write_blob(blob, res.text)
        sc.set_layer("raw_html", {"path": blob, "format": "text/html"})
    else:
        ext = {"gzip": "tgz"}.get(kind, kind)          # gzip body → .tgz
        blob = f"raw.{ext}"
        sc.write_blob_bytes(blob, res.body)            # lossless — no text decode
        sc.set_layer("raw_blob", {"path": blob, "format": res.content_type or kind})
    sc.write_blob("headers.json", json.dumps(res.headers, indent=2, ensure_ascii=False))
    cost_ms = (time.perf_counter() - t0) * 1000
    sc.set_evidence("url", ctx.url)
    sc.set_evidence("final_url", res.final_url)
    sc.set_evidence("status", res.status)
    sc.set_evidence("content_type", res.content_type)
    sc.set_evidence("content_kind", kind)
    sc.set_evidence("raw_blob", blob)
    sc.set_evidence("bytes", len(res.body))
    sc.add_fact(FETCHED)
    sc.log_transition("fetch", "INIT", FETCHED, cost_ms,
                      f"{res.status} {len(res.body)}B {res.content_type} kind={kind}")
    sc.save()
    return _fetch_report(sc, cached=False)


def _fetch_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached" if cached else "fetched"
    kind = ev.get("content_kind", "html")
    blob = ev.get("raw_blob", "raw.html")
    lines = [f"{tag} {ev.get('url')}",
             f"  id:          {sc.local_id}",
             f"  status:      {ev.get('status')}",
             f"  final url:   {ev.get('final_url')}",
             f"  content-type:{ev.get('content_type')}  (kind: {kind})",
             f"  size:        {ev.get('bytes')} bytes",
             f"  snapshot:    {sc.blob_path(blob)}"]
    if kind == "html":
        lines.append("  next: meta · links · jsonld · outline · size")
    elif kind == "pdf":
        lines.append(f"  ⚠ this is a PDF, not HTML — hand it to pdfdrill: "
                     f"`pdfdrill md {sc.blob_path(blob)}`")
        aid = K.parse_arxiv_id(ev.get("url") or "")
        if aid:
            lines.append(f"    (arXiv {aid} — `htmldrill arxiv {ev.get('url')}` "
                         f"gives the abstract + the LaTeX-source route)")
    else:
        lines.append(f"  ⚠ this is a {kind.upper()}, not HTML — stored raw; "
                     f"snapshot commands won't parse it")
    # routing hint: a 200-OK HTML page can still belong to another *drill tool —
    # e.g. an auth-gated chat SPA shell (CHATDRILL) or a video page (YTDRILL).
    from .sources import router as RT
    v = RT.route(ev.get("url") or "")
    if v.drill in ("chatdrill", "ytdrill"):
        lines.append(f"  → {v.handler} is the right tool for this URL: {v.reason}")
    return "\n".join(lines)


def cmd_scholar(ctx: Ctx) -> str:
    """Recognise a Google Scholar profile and fetch EVERY page of works past the
    "Show more" wall via cstart/pagesize URL pagination (no browser, no clicks),
    merging them into one complete raw.html snapshot. Records FETCHED (so the whole
    pipeline sees all works) + SCHOLAR. NETWORK. Idempotent via SCHOLAR / --force."""
    if not ctx.url:
        raise ValueError("usage: htmldrill scholar <profile-url>")
    if not SCH.is_scholar_citations(ctx.url):
        return (f"{ctx.url} is not a Google Scholar citations profile "
                f"(expected scholar.google.*/citations?user=…).")
    sc = Sidecar(F.local_id_for(ctx.url), work=ctx.work)
    if sc.has(SCHOLAR) and sc.has_blob("raw.html") and not ctx.force:
        return _scholar_report(sc, cached=True)

    t0 = time.perf_counter()
    def _fetch(u: str) -> str:
        return F.fetch(u, timeout=ctx.timeout, ua=ctx.ua).text or ""
    merged, total, pages = SCH.fetch_all_works(ctx.url, _fetch)
    cost_ms = (time.perf_counter() - t0) * 1000
    if total == 0:
        return (f"Fetched {pages} page(s) but found no work rows — Scholar may have "
                f"served a robot check, or the profile is empty/private.")

    sc.write_blob("raw.html", merged)
    sc.write_blob("headers.json", json.dumps({"x-htmldrill": "scholar-merged"}, indent=2))
    sc.set_evidence("url", ctx.url)
    sc.set_evidence("final_url", ctx.url)
    sc.set_evidence("status", 200)
    sc.set_evidence("content_type", "text/html")
    sc.set_evidence("content_kind", "html")
    sc.set_evidence("raw_blob", "raw.html")
    sc.set_evidence("bytes", len(merged.encode("utf-8", "replace")))
    sc.set_evidence("scholar_entries", total)
    sc.set_evidence("scholar_pages", pages)
    sc.set_layer("raw_html", {"path": "raw.html", "format": "text/html"})
    sc.add_fact(FETCHED)
    sc.add_fact(SCHOLAR)
    sc.log_transition("scholar", _prev(sc, SCHOLAR), SCHOLAR, cost_ms,
                      f"{total} works merged from {pages} page(s)")
    sc.save()
    return _scholar_report(sc, cached=False)


def _scholar_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached scholar" if cached else "scholar"
    return "\n".join([
        f"{tag} {ev.get('url')}",
        f"  id:       {sc.local_id}",
        f"  works:    {ev.get('scholar_entries')} entries merged from "
        f"{ev.get('scholar_pages')} page(s) (past the 'Show more' wall via cstart/pagesize)",
        f"  snapshot: {sc.blob_path('raw.html')}  ({ev.get('bytes')} bytes)",
        f"  next: links · outline · model · single — all now see every work",
    ])


def cmd_route(ctx: Ctx) -> str:
    """Classify a URL and name which *drill tool should handle it (offline, no
    fetch). A clear verdict for arXiv/PDF (pdfdrill), videos (YTDRILL), auth-gated
    chat (CHATDRILL), and ordinary pages (htmldrill)."""
    if not ctx.url:
        raise ValueError("usage: htmldrill route <url>")
    from .sources import router as RT
    v = RT.route(ctx.url)
    verdict = ("htmldrill can handle this." if v.drill == "htmldrill"
               else f"{v.handler} is the right tool — NOT htmldrill.")
    return "\n".join([
        f"route {ctx.url}",
        f"  handler: {v.handler}" + ("" if v.known else "  (default)"),
        f"  reason:  {v.reason}",
        f"  next:    {v.action}",
        f"  → {verdict}",
    ])


# -- known-host: arXiv (recognise the source, take the cheapest richest route) --

def cmd_arxiv(ctx: Ctx) -> str:
    """Recognise an arXiv URL/id and route it well (NETWORK; never auto-ensured).

    A user often pastes ``arxiv.org/abs/<id>`` not realising it is an HTML landing
    page, not the paper. This reads title/authors/abstract/category off the abs
    page for FREE (no render, no capture), and surfaces the two richer routes the
    page hides: the **PDF** (hand to pdfdrill) and the **e-print .tgz** — the
    author's LaTeX source, the gold form of every equation.

    ``--pdf`` also downloads the PDF, ``--source`` the e-print tarball, into the
    sidecar blob dir. Keyed by the canonical arXiv id, so the abs URL, the pdf
    URL, and the bare id all resolve to one sidecar."""
    aid = K.parse_arxiv_id(ctx.url or "") or K.bare_arxiv_id(ctx.url or "")
    if not aid:
        raise ValueError(f"{ctx.url!r} is not an arXiv URL or id "
                         f"(expected e.g. https://arxiv.org/abs/2510.11170 or 1706.03762).")
    lid = "arxiv-" + aid.replace("/", "_")
    sc = Sidecar(lid, work=ctx.work)
    urls = K.arxiv_urls(aid)
    if not (sc.has(ARXIV_KNOWN) and not ctx.force):
        t0 = time.perf_counter()
        meta = K.fetch_arxiv_metadata(aid, timeout=ctx.timeout)
        cost_ms = (time.perf_counter() - t0) * 1000
        sc.write_blob("arxiv_meta.json", json.dumps(meta, indent=2, ensure_ascii=False))
        sc.set_evidence("arxiv_id", aid)
        sc.set_evidence("url", ctx.url)
        sc.set_evidence("arxiv_urls", urls)
        sc.set_evidence("title", meta.get("title"))
        sc.set_evidence("authors", meta.get("authors"))
        sc.set_evidence("primary_category", meta.get("primary_category"))
        sc.add_fact(ARXIV_KNOWN)
        sc.log_transition("arxiv", _prev(sc, ARXIV_KNOWN), ARXIV_KNOWN, cost_ms,
                          f"{aid} {meta.get('primary_category')}")
        sc.save()
    # optional heavier downloads
    got = []
    if ctx.download_pdf:
        p = K.download_arxiv_pdf(aid, sc.blob_dir, timeout=ctx.timeout)
        sc.set_evidence("pdf_path", str(p)); got.append(f"PDF → {p}")
    if ctx.download_source:
        s = K.download_arxiv_source(aid, sc.blob_dir, timeout=ctx.timeout)
        sc.set_evidence("source_path", str(s)); got.append(f"source → {s}")
    if got:
        sc.save()

    meta = json.loads(sc.read_blob("arxiv_meta.json") or "{}")
    authors = meta.get("authors", [])
    auth = ", ".join(authors[:4]) + (f" … (+{len(authors) - 4})" if len(authors) > 4 else "")
    lines = [
        f"arXiv:{aid}  [{meta.get('primary_category') or '?'}]",
        f"  title:    {meta.get('title', '')}",
        f"  authors:  {auth or '(none parsed)'}",
        f"  abstract: {(meta.get('abstract') or '')[:240]}"
        + ("…" if len(meta.get('abstract') or '') > 240 else ""),
        f"  pdf:      {urls['pdf']}",
        f"  source:   {urls['eprint']}   (author's LaTeX .tgz — gold equations)",
    ]
    for g in got:
        lines.append(f"  downloaded: {g}")
    lines.append(f"  next: `pdfdrill md {urls['pdf']}` (or `--pdf`/`--source` to cache locally)")
    return "\n".join(lines)


# -- snapshot introspection (no network) -------------------------------------

def _guess_framework(html: str) -> str:
    # Patterns must be SPECIFIC markers (attributes/globals/generator meta), not
    # bare words like "vue"/"angular" — those substring-match chat/JSON payloads
    # and produce false positives (a 62MB ChatGPT export "is Vue + Angular";
    # a TiddlyWiki "is Vue"). We also detect TiddlyWiki, the most common real input.
    pats = [
        ("Next.js", r"__NEXT_DATA__|/_next/static/"),
        ("Nuxt", r"window\.__NUXT__"),
        ("React", r"data-reactroot|react-dom\.production|__REACT_DEVTOOLS"),
        ("Vue", r"data-v-[0-9a-f]{8}\b|__VUE__|vue\.runtime"),
        ("Angular", r"\bng-version=|_nghost-|\bng-app="),
        ("Svelte", r"\bsvelte-[0-9a-z]{6}\b"),
        ("TiddlyWiki",
         r'application-name"\s+content="TiddlyWiki"|tiddlywiki-tiddler-store|'
         r'id="storeArea"|generator"\s+content="TiddlyWiki"'),
    ]
    hits = [name for name, p in pats if re.search(p, html, re.I)]
    return ", ".join(hits) if hits else "none detected"


def cmd_size(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    nbytes = sc.get_evidence("bytes", len(html.encode("utf-8", "replace")))
    nlines = html.count("\n") + 1
    fw = _guess_framework(html)
    visible_text = sum(len(t) for _, t in c.anchors) + sum(len(t) for _, t in c.headings)
    # Shallow render-required heuristic: a framework shell with almost no static
    # text/headings is probably JS-rendered (the OCR-analog escalation signal).
    needs_render = bool(fw != "none detected" and len(c.headings) <= 1 and visible_text < 200)
    sc.set_evidence("tag_count", c.tag_count)
    sc.set_evidence("framework", fw)
    sc.set_evidence("needs_render", needs_render)
    sc.add_fact(SIZE_KNOWN)
    sc.log_transition("size", _prev(sc, SIZE_KNOWN), SIZE_KNOWN, 0,
                      f"{nbytes}B {c.tag_count} tags fw={fw}")
    sc.save()
    verdict = ("LIKELY JS-RENDERED — static markup is thin; `render` recommended (M1)"
               if needs_render else "static markup looks sufficient — no render needed")
    return (f"{sc.local_id}: {nbytes} bytes, {nlines} lines, ~{c.tag_count} tags\n"
            f"  framework:  {fw}\n"
            f"  headings:   {len(c.headings)}   anchors: {len(c.anchors)}\n"
            f"  verdict:    {verdict}")


def cmd_headers(ctx: Ctx) -> str:
    sc, _ = _load_snapshot(ctx)
    raw = sc.read_blob("headers.json")
    if not raw:
        return "no captured headers (snapshot has no headers.json)."
    headers = json.loads(raw)
    lines = [f"response headers for {sc.local_id} ({len(headers)}):"]
    for k, v in headers.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def cmd_meta(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    meta = H.extract_meta(c)
    sc.set_evidence("meta", meta)
    sc.add_fact(META_KNOWN)
    sc.log_transition("meta", _prev(sc, META_KNOWN), META_KNOWN, 0, f"{len(meta)} keys")
    sc.save()
    if not meta and not c.title:
        return f"{sc.local_id}: no <meta> tags or <title> found."
    lines = [f"{sc.local_id}: <title> {c.title!r}"]
    if meta:
        lines.append(f"  {len(meta)} meta key(s):")
        for k, v in meta.items():
            lines.append(f"    {k}: {v[:100]}")
    else:
        lines.append("  (no <meta> tags, but a <title> is present)")
    return "\n".join(lines)


def cmd_canonical(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    canon = H.extract_canonical(c)
    sc.set_evidence("canonical", canon)
    sc.add_fact(CANONICAL_KNOWN)
    sc.log_transition("canonical", _prev(sc, CANONICAL_KNOWN), CANONICAL_KNOWN, 0, "")
    sc.save()
    if not canon:
        return f"{sc.local_id}: no <link rel=canonical> and no og:url."
    return f"{sc.local_id}:\n" + "\n".join(f"  {k}: {v}" for k, v in canon.items())


# A URL stops at whitespace, quotes, brackets, backslashes (JS escapes), and
# common code punctuation — so we don't harvest `http://\\\\n…` or `http://$(x`
# escape-mangled fragments out of JS string literals as if they were real links.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\(|]+")


def _clean_url(u: str) -> str:
    """Decode the handful of HTML entities that leak into harvested URLs and trim
    trailing punctuation, so the invisible-URL list is clean."""
    import html as _html
    return _html.unescape(u).rstrip(".,;")


def cmd_links(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    base = _base_url(sc)
    links = H.extract_links(c, base_url=base)
    # KILLER CASE: URLs present in the raw markup (data-*, inline JSON, <link>,
    # JS string literals) that never surface as a visible <a href> anchor — the
    # HTML analog of pdfdrill reading invisible links out of the annotation layer.
    anchor_set = {u for u, _ in links["internal"] + links["external"] + links["other"]}
    raw_urls = {_clean_url(u) for u in _URL_RE.findall(html)}
    # keep only plausible hosts (a dot in the authority) — drops `http://<wikiname`,
    # `http://$(userName`, `http://127.0.0.1:8080` template noise stays but garbage
    # with no real host is dropped.
    hidden = sorted(
        u for u in raw_urls
        if u not in anchor_set and "." in u.split("//", 1)[-1].split("/", 1)[0])
    sc.set_evidence("link_counts", {k: len(v) for k, v in links.items()})
    sc.set_evidence("hidden_link_count", len(hidden))
    sc.add_fact(LINKS_KNOWN)
    sc.log_transition("links", _prev(sc, LINKS_KNOWN), LINKS_KNOWN, 0,
                      f"{len(anchor_set)} anchors, {len(hidden)} hidden")
    sc.save()
    lines = [f"{sc.local_id}: {len(links['internal'])} internal, "
             f"{len(links['external'])} external, {len(links['other'])} other anchors"]
    for u, t in links["external"][:8]:
        lines.append(f"  → {u[:78]}  {('· ' + t[:30]) if t else ''}")
    if hidden:
        lines.append(f"  ⚠ {len(hidden)} URL(s) in the markup but NOT visible anchors "
                     f"(data-*/inline-JSON/JS literals):")
        for u in hidden[:8]:
            lines.append(f"      {u[:80]}")
    return "\n".join(lines)


def cmd_jsonld(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    blocks = H.extract_jsonld(c)
    types: list[str] = []
    for b in blocks:
        if b["ok"]:
            data = b["data"]
            for node in (data if isinstance(data, list) else [data]):
                if isinstance(node, dict) and node.get("@type"):
                    t = node["@type"]
                    types.extend(t if isinstance(t, list) else [t])
    sc.set_evidence("jsonld_types", types)
    sc.set_evidence("jsonld_blocks", len(blocks))
    sc.add_fact(JSONLD_KNOWN)
    sc.log_transition("jsonld", _prev(sc, JSONLD_KNOWN), JSONLD_KNOWN, 0,
                      f"{len(blocks)} blocks {types}")
    sc.save()
    if not blocks:
        return f"{sc.local_id}: no <script type=application/ld+json> blocks."
    lines = [f"{sc.local_id}: {len(blocks)} JSON-LD block(s), @types: "
             f"{', '.join(types) or '(none)'}"]
    for i, b in enumerate(blocks):
        if b["ok"]:
            data = b["data"]
            top = data[0] if isinstance(data, list) and data else data
            keys = list(top.keys()) if isinstance(top, dict) else type(data).__name__
            lines.append(f"  [{i}] ok — top-level keys: {keys}")
        else:
            lines.append(f"  [{i}] PARSE ERROR: {b['error']} ({b['raw_len']} bytes)")
    return "\n".join(lines)


def cmd_microdata(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    items = H.extract_microdata(c)
    sc.set_evidence("microdata_types", [m["itemtype"] for m in items])
    sc.add_fact(MICRODATA_KNOWN)
    sc.log_transition("microdata", _prev(sc, MICRODATA_KNOWN), MICRODATA_KNOWN, 0,
                      f"{len(items)} items")
    sc.save()
    if not items:
        return f"{sc.local_id}: no microdata (itemscope/itemtype) found."
    lines = [f"{sc.local_id}: {len(items)} microdata item(s):"]
    for m in items[:10]:
        lines.append(f"  {m['itemtype']}  props: {', '.join(m['props'][:8]) or '(none)'}")
    return "\n".join(lines)


def cmd_opengraph(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    og = H.extract_opengraph(c)
    sc.set_evidence("opengraph", og)
    sc.add_fact(OPENGRAPH_KNOWN)
    sc.log_transition("opengraph", _prev(sc, OPENGRAPH_KNOWN), OPENGRAPH_KNOWN, 0,
                      f"{len(og)} props")
    sc.save()
    if not og:
        return f"{sc.local_id}: no OpenGraph/Twitter-card meta."
    return f"{sc.local_id}: {len(og)} og:/twitter: prop(s)\n" + \
        "\n".join(f"  {k}: {v[:100]}" for k, v in og.items())


def cmd_feeds(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    feeds = H.extract_feeds(c)
    sc.set_evidence("feeds", feeds)
    sc.add_fact(FEEDS_KNOWN)
    sc.log_transition("feeds", _prev(sc, FEEDS_KNOWN), FEEDS_KNOWN, 0, f"{len(feeds)}")
    sc.save()
    if not feeds:
        return f"{sc.local_id}: no RSS/Atom feed links."
    lines = [f"{sc.local_id}: {len(feeds)} feed(s):"]
    for f in feeds:
        lines.append(f"  {f['type']:<24} {f['href']}  {f['title']}")
    return "\n".join(lines)


def cmd_outline(ctx: Ctx) -> str:
    sc, html = _load_snapshot(ctx)
    c = H.collect(html)
    heads = H.extract_outline(c)
    sc.set_evidence("heading_count", len(heads))
    sc.add_fact(OUTLINE_KNOWN)
    sc.log_transition("outline", _prev(sc, OUTLINE_KNOWN), OUTLINE_KNOWN, 0,
                      f"{len(heads)} headings")
    sc.save()
    if not heads:
        return f"{sc.local_id}: no headings (h1–h6) in the static markup."
    lines = [f"{sc.local_id}: heading outline ({len(heads)}):"]
    for lvl, txt in heads:
        lines.append(f"  {'  ' * (lvl - 1)}h{lvl} {txt[:80]}")
    return "\n".join(lines)


# -- L1 render (headless escalation) -----------------------------------------

def cmd_render(ctx: Ctx) -> str:
    """The expensive, escalation-gated layer: materialize the page with headless
    Chrome and snapshot the rendered DOM + a screenshot. Like `fetch`, it is the
    only other command allowed to touch the network — never auto-ensured."""
    if not ctx.url:
        raise ValueError("usage: htmldrill render <url>")
    lid = F.local_id_for(ctx.url)
    sc = Sidecar(lid, work=ctx.work)
    if sc.has(RENDERED) and not ctx.force:
        return _render_report(sc, cached=True)
    t0 = time.perf_counter()
    res = R.render(ctx.url, timeout=ctx.timeout, window=ctx.window)
    cost_ms = (time.perf_counter() - t0) * 1000
    sc.write_blob("rendered.html", res.dom)
    has_shot = False
    if res.screenshot:
        sc.write_blob_bytes("screenshot.png", res.screenshot)
        has_shot = True
    c = H.collect(res.dom)
    sc.set_evidence("url", sc.get_evidence("url") or ctx.url)
    sc.set_evidence("rendered_bytes", len(res.dom.encode("utf-8", "replace")))
    sc.set_evidence("rendered_tags", c.tag_count)
    sc.set_evidence("chrome", res.chrome)
    sc.set_evidence("has_screenshot", has_shot)
    sc.set_layer("rendered_html", {"path": "rendered.html", "format": "text/html"})
    sc.add_fact(RENDERED)
    sc.log_transition("render", _prev(sc, RENDERED), RENDERED, cost_ms,
                      f"{c.tag_count} tags shot={has_shot} via {Path(res.chrome).name}")
    sc.save()
    return _render_report(sc, cached=False)


def _render_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached render" if cached else "rendered"
    static = ev.get("tag_count")
    delta = (f"  static tags: {static} → rendered: {ev.get('rendered_tags')} "
             f"(Δ{ev.get('rendered_tags', 0) - static:+d})\n" if static is not None else "")
    shot = sc.blob_path("screenshot.png")
    return (f"{tag} {ev.get('url')}\n"
            f"  id:          {sc.local_id}\n"
            f"  rendered:    {ev.get('rendered_bytes')} bytes, {ev.get('rendered_tags')} tags\n"
            f"{delta}"
            f"  chrome:      {ev.get('chrome')}\n"
            f"  screenshot:  {shot if ev.get('has_screenshot') else '(none)'}\n"
            f"  next: dom · text · compare · screenshot")


def _rendered_or_static(sc: Sidecar) -> tuple[str, str]:
    """(html, source-label) — prefer the fully-materialized `capture` DOM (lazy
    content included), then a one-shot render, then the static snapshot."""
    if sc.has(CAPTURED) and sc.has_blob("captured.html"):
        return sc.read_blob("captured.html") or "", "captured"
    if sc.has(RENDERED):
        return sc.read_blob("rendered.html") or "", "rendered"
    if sc.has(FETCHED):
        return sc.read_blob("raw.html") or "", "static"
    raise FileNotFoundError("nothing captured yet — run `capture`, `fetch`, or `render` first")


def _model_source_html(sc: Sidecar) -> tuple[str, str]:
    """(html, source-label) for the `model` ingestion. Prefers the monolith
    ``single.html`` — its assets are inlined as `data:` URIs, so offline
    `ingest_dom` sees real image src's and complete markup instead of dangling
    network references. Falls through to the capture/render/static DOM otherwise,
    keeping every prior model path unchanged when no `single.html` exists."""
    if sc.has(SINGLE) and sc.has_blob("single.html"):
        return sc.read_blob("single.html") or "", "single"
    return _rendered_or_static(sc)


def cmd_dom(ctx: Ctx) -> str:
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not sc.has(RENDERED):
        raise FileNotFoundError(
            f"no render for {ctx.url!r} — run `htmldrill render {ctx.url}` first")
    c = H.collect(sc.read_blob("rendered.html") or "")
    lines = [f"{sc.local_id}: rendered DOM — ~{c.tag_count} tags, "
             f"{len(c.headings)} headings, {len(c.anchors)} anchors"]
    if sc.has(FETCHED):
        sc_static = H.collect(sc.read_blob("raw.html") or "")
        lines.append(f"  vs static: {sc_static.tag_count} tags "
                     f"(Δ{c.tag_count - sc_static.tag_count:+d}), "
                     f"{len(sc_static.anchors)} anchors "
                     f"(Δ{len(c.anchors) - len(sc_static.anchors):+d})")
        if c.tag_count > sc_static.tag_count * 1.3:
            lines.append("  → JS injected substantial DOM after load (SPA-like).")
    return "\n".join(lines)


def cmd_text(ctx: Ctx) -> str:
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    html, source = _rendered_or_static(sc)
    text = H.extract_text(html)
    sc.write_blob("text.txt", text)
    sc.set_evidence("text_chars", len(text))
    sc.set_evidence("text_source", source)
    sc.add_fact(TEXT_KNOWN)
    sc.log_transition("text", _prev(sc, TEXT_KNOWN), TEXT_KNOWN, 0,
                      f"{len(text)} chars from {source}")
    sc.save()
    preview = text[:400] + ("…" if len(text) > 400 else "")
    return (f"{sc.local_id}: {len(text)} chars of visible text (from the {source} DOM)\n"
            f"  → {sc.blob_path('text.txt')}\n\n{preview}")


def cmd_screenshot(ctx: Ctx) -> str:
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not (sc.has(RENDERED) and sc.has_blob("screenshot.png")):
        raise FileNotFoundError(
            f"no screenshot for {ctx.url!r} — run `htmldrill render {ctx.url}` first")
    p = sc.blob_path("screenshot.png")
    return f"{sc.local_id}: screenshot {p.stat().st_size} bytes\n  → {p}"


def cmd_compare(ctx: Ctx) -> str:
    """Three-way fidelity: static-DOM text | rendered-DOM text | screenshot — the
    HTML analog of pdfdrill's LaTeX|KaTeX|image QC. Surfaces content that exists
    only after render (SPA bodies) or only in the markup (SEO data never painted)."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    static_html = sc.read_blob("raw.html") if sc.has(FETCHED) else None
    rendered_html = sc.read_blob("rendered.html") if sc.has(RENDERED) else None
    if not static_html and not rendered_html:
        raise FileNotFoundError(
            f"nothing to compare for {ctx.url!r} — run `fetch` and/or `render` first")
    static_txt = H.extract_text(static_html) if static_html else ""
    rendered_txt = H.extract_text(rendered_html) if rendered_html else ""
    sl, rl = len(static_txt), len(rendered_txt)
    has_shot = sc.has_blob("screenshot.png")
    delta = rl - sl
    if static_html and rendered_html:
        if delta > max(200, sl * 0.3):
            verdict = "content appears mostly AFTER render — static scrape would miss it (SPA)"
        elif sl > rl + 200:
            verdict = "static markup carries text the render dropped (e.g. noscript/SEO body)"
        else:
            verdict = "static and rendered text broadly agree — static scrape is sufficient"
    else:
        verdict = ("only the rendered DOM captured — run `fetch` for the static side"
                   if rendered_html else
                   "only the static snapshot captured — run `render` for the rendered side")
    rows = (f"  static-DOM text:   {sl:>7} chars  {'✓' if static_html else '—'}\n"
            f"  rendered-DOM text: {rl:>7} chars  {'✓' if rendered_html else '—'}\n"
            f"  screenshot:        {'present' if has_shot else 'absent':>7}  "
            f"{'✓' if has_shot else '—'}")
    html_report = (f"<!DOCTYPE html><meta charset=utf-8><title>compare {sc.local_id}</title>"
                   f"<h1>compare: {sc.local_id}</h1><table border=1 cellpadding=6>"
                   f"<tr><th>source</th><th>chars</th></tr>"
                   f"<tr><td>static-DOM</td><td>{sl}</td></tr>"
                   f"<tr><td>rendered-DOM</td><td>{rl}</td></tr>"
                   f"<tr><td>screenshot</td><td>{'yes' if has_shot else 'no'}</td></tr>"
                   f"</table><p>{verdict}</p>")
    sc.write_blob("compare.html", html_report)
    sc.set_evidence("compare_delta_chars", delta)
    sc.add_fact(COMPARED)
    sc.log_transition("compare", _prev(sc, COMPARED), COMPARED, 0, f"Δ{delta} chars")
    sc.save()
    return (f"{sc.local_id}: static | rendered | screenshot fidelity\n{rows}\n"
            f"  Δ rendered−static: {delta:+d} chars\n  verdict: {verdict}\n"
            f"  → {sc.blob_path('compare.html')}")


# -- L5 model (offline docmodel ingestion) -----------------------------------

def cmd_model(ctx: Ctx) -> str:
    """Lift the captured DOM into a shared docmodel Document and persist it as
    ``model.docmodel.json``. OFFLINE: requires a prior ``fetch`` or ``render``
    (it NEVER touches the network / auto-ensures a fetch — like the snapshot
    commands, it hard-gates on the captured-snapshot fact). Idempotent: skips when
    MODEL_BUILT unless ``--force``."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not (sc.has(SINGLE) or sc.has(CAPTURED) or sc.has(RENDERED) or sc.has(FETCHED)):
        raise FileNotFoundError(
            f"no snapshot for {ctx.url!r} — run `htmldrill fetch {ctx.url}` "
            f"(or `single`/`render`) first; `model` is offline and won't fetch.")
    if sc.has(MODEL_BUILT) and sc.has_blob("model.docmodel.json") and not ctx.force:
        return _model_report(sc, cached=True)

    html, source = _model_source_html(sc)         # prefer the self-contained single.html
    bibkey = sc.local_id.upper()

    # docmodel is the one external bridge — import lazily, only inside `model`.
    from .ingest_dom import build_document
    # If `materialize` already recovered hidden content (role=continuation
    # fragments), fold them into the model so a fresh build includes them too.
    continuation = None
    if sc.has_blob("continuation.json"):
        try:
            continuation = json.loads(sc.read_blob("continuation.json") or "[]")
        except Exception:
            continuation = None
    t0 = time.perf_counter()
    doc = build_document(html, bibkey=bibkey, local_id=sc.local_id,
                         continuation=continuation)
    cost_ms = (time.perf_counter() - t0) * 1000

    sc.write_blob("model.docmodel.json",
                  json.dumps(doc.to_dict(), indent=2, ensure_ascii=False))

    by_type: dict[str, int] = {}
    for o in doc.objects.values():
        by_type[o.type] = by_type.get(o.type, 0) + 1
    sc.set_evidence("model_source", source)
    sc.set_evidence("model_object_count", len(doc.objects))
    sc.set_evidence("model_objects_by_type", by_type)
    sc.set_evidence("model_bibkey", bibkey)
    sc.set_layer("docmodel", {"path": "model.docmodel.json", "format": "application/json"})
    sc.add_fact(MODEL_BUILT)
    sc.log_transition("model", _prev(sc, MODEL_BUILT), MODEL_BUILT, cost_ms,
                      f"{len(doc.objects)} objs from {source} {by_type}")
    sc.save()
    return _model_report(sc, cached=False)


def _model_report(sc: Ctx | Sidecar, cached: bool) -> str:  # type: ignore[name-defined]
    ev = sc.evidence
    by_type = ev.get("model_objects_by_type", {})
    tag = "cached model" if cached else "built model"
    lines = [
        f"{tag} {sc.local_id}: docmodel Document with "
        f"{ev.get('model_object_count', 0)} object(s) "
        f"(from the {ev.get('model_source', '?')} DOM, bibkey {ev.get('model_bibkey')})",
        f"  → {sc.blob_path('model.docmodel.json')}",
    ]
    if by_type:
        lines.append("  objects by type:")
        for t, n in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {t:<12} {n}")
    lines.append("  next: load with docmodel.Document.from_dict and project")
    return "\n".join(lines)


# -- L6 projectors (offline) — run pdfdrill's REAL docops projectors ----------
#
# THE PAYOFF: lift the persisted docmodel Document and feed it to pdfdrill's
# canonical projectors (PlainText / TiddlyWiki / LLMCompact / LLMText), the SAME
# operators it runs over PDFs. These are OFFLINE (no network), so `model` is a
# safe `requires:` prerequisite (--ensure auto-runs it; `model` itself errors if
# there's no captured snapshot — never a fetch). Each is idempotent via a fact.

#: projector classname → dotted docops module (a subset of docops' own registry;
#: we reuse the loader/registry so the wiring matches pdfdrill exactly).
_PROJECTORS = {
    "tiddlers": "TiddlyWikiProjector",
    "md": "LLMCompactProjector",
    "llmtext": "PlainTextProjector",
}


def _load_document(sc: Sidecar):
    """Load the persisted docmodel Document (== docops.main.load_document)."""
    if not sc.has_blob("model.docmodel.json"):
        raise FileNotFoundError(
            f"no docmodel for {sc.local_id!r} — run `htmldrill model {sc.local_id}` "
            f"first (offline; needs a prior fetch/render).")
    from .ingest_dom import ensure_pdfdrill  # re-export of _core.ensure_pdfdrill
    ensure_pdfdrill()
    from docmodel import Document  # noqa: WPS433
    raw = sc.read_blob("model.docmodel.json") or ""
    return Document.from_dict(json.loads(raw))


def _make_projector(classname: str):
    """Instantiate a docops projector via its loader registry (no class import)."""
    from .ingest_dom import ensure_pdfdrill
    ensure_pdfdrill()
    import importlib

    from docops.base import OperatorConfig
    from docops.loader import DEFAULT_REGISTRY

    modname = DEFAULT_REGISTRY[classname]
    mod = importlib.import_module(modname)
    cls = getattr(mod, classname)
    return cls(OperatorConfig(op="projector", classname=classname))


def _run_projector(ctx: Ctx, *, name: str, classname: str, out_blob: str,
                   fact: str) -> str:
    """Shared projector body: gate on the fact (idempotent / --force), load the
    persisted Document, run the projector, write the output blob, return prose."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if sc.has(fact) and sc.has_blob(out_blob) and not ctx.force:
        return _projector_report(sc, name=name, classname=classname,
                                 out_blob=out_blob, cached=True)
    doc = _load_document(sc)
    proj = _make_projector(classname)
    t0 = time.perf_counter()
    result = proj.project(doc)
    cost_ms = (time.perf_counter() - t0) * 1000
    if not isinstance(result, str):
        result = result.decode("utf-8") if isinstance(result, bytes) else str(result)
    sc.write_blob(out_blob, result)
    sc.set_evidence(f"{name}_classname", classname)
    sc.set_evidence(f"{name}_bytes", len(result.encode("utf-8", "replace")))
    sc.set_layer(name, {"path": out_blob, "format": "text/plain"})
    sc.add_fact(fact)
    sc.log_transition(name, _prev(sc, fact), fact, cost_ms,
                      f"{classname} → {out_blob} ({len(result)} chars)")
    sc.save()
    return _projector_report(sc, name=name, classname=classname,
                             out_blob=out_blob, cached=False)


def _projector_report(sc: Sidecar, *, name: str, classname: str,
                      out_blob: str, cached: bool) -> str:
    tag = f"cached {name}" if cached else name
    nbytes = sc.get_evidence(f"{name}_bytes", "?")
    return (f"{tag} {sc.local_id}: {classname} → {out_blob} ({nbytes} bytes)\n"
            f"  → {sc.blob_path(out_blob)}")


def _tiddlers_dir(ctx: Ctx, sc: Sidecar) -> Path:
    """Per-tiddler output dir. Defaults INSIDE this target's blob dir so runs are
    isolated per target — never the shared repo-root ``tiddlers/`` (which pooled
    every target's files into the working tree). Overridable via --out /
    $HTMLDRILL_TIDDLERS."""
    explicit = ctx.out or os.environ.get("HTMLDRILL_TIDDLERS")
    return Path(explicit) if explicit else (sc.blob_dir / "tiddlers")


def cmd_tiddlers(ctx: Ctx) -> str:
    """Project the docmodel Document through pdfdrill's TiddlyWikiProjector,
    writing the importable ``tiddlers.json`` blob (a JSON array of tiddler dicts)
    plus individual ``.tid`` files into ``./tiddlers/`` (like chatdrill). OFFLINE;
    requires ``model`` (auto-ensured). Idempotent via TIDDLERS_BUILT / --force."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    out_dir = _tiddlers_dir(ctx, sc)
    if sc.has(TIDDLERS_BUILT) and sc.has_blob("tiddlers.json") and not ctx.force:
        n = sc.get_evidence("tiddler_count", "?")
        return (f"cached tiddlers {sc.local_id}: {n} tiddler(s) — skipped. "
                f"--force to redo.\n  → {sc.blob_path('tiddlers.json')}")
    doc = _load_document(sc)
    proj = _make_projector("TiddlyWikiProjector")
    t0 = time.perf_counter()
    blob = proj.project(doc)                       # JSON array string
    cost_ms = (time.perf_counter() - t0) * 1000
    tids = json.loads(blob)

    sc.write_blob("tiddlers.json", blob)
    # individual .tid-ish files into the live wiki folder (trivial: one JSON each)
    out_dir.mkdir(parents=True, exist_ok=True)
    for t in tids:
        fn = re.sub(r"[^\w.\-]+", "_", str(t.get("title", "untitled"))) + ".tid.json"
        (out_dir / fn).write_text(
            json.dumps(t, indent=2, ensure_ascii=False), encoding="utf-8")

    sc.set_evidence("tiddler_count", len(tids))
    sc.set_layer("tiddlers", {"path": "tiddlers.json", "format": "tiddlywiki/json"})
    sc.add_fact(TIDDLERS_BUILT)
    sc.log_transition("tiddlers", _prev(sc, TIDDLERS_BUILT), TIDDLERS_BUILT,
                      cost_ms, f"{len(tids)} tiddlers via TiddlyWikiProjector")
    sc.save()
    return (f"tiddlers {sc.local_id}: {len(tids)} tiddler(s) via TiddlyWikiProjector "
            f"in {cost_ms:.0f} ms\n"
            f"  → import blob: {sc.blob_path('tiddlers.json')}\n"
            f"  → per-tiddler files in {out_dir}/")


def cmd_md(ctx: Ctx) -> str:
    """Project the docmodel Document through pdfdrill's LLMCompactProjector —
    token-optimized markdown for LLM ingestion — writing ``md.md``. OFFLINE;
    requires ``model`` (auto-ensured). Idempotent via MD_BUILT / --force."""
    return _run_projector(ctx, name="md", classname="LLMCompactProjector",
                          out_blob="md.md", fact=MD_BUILT)


def cmd_llmtext(ctx: Ctx) -> str:
    """Project the docmodel Document through pdfdrill's PlainTextProjector — a
    flat, flow-ordered text dump — writing ``llm.txt``. OFFLINE; requires
    ``model`` (auto-ensured). Idempotent via LLMTEXT_BUILT / --force."""
    return _run_projector(ctx, name="llmtext", classname="PlainTextProjector",
                          out_blob="llm.txt", fact=LLMTEXT_BUILT)


# -- L6 latex projector: docmodel -> HTML -> LaTeX (html2latex, optional dep) --
#
# The other projectors are pdfdrill's own docops operators; this one bridges to
# html2latex instead. It is the FIRST HALF of "fill the docmodel with LaTeX": the
# same docmodel→HTML→LaTeX conversion is exactly what pdfdrill's LaTeX ingestion
# (merge_latex.merge_latex_prose / the provenance="tex" latex_candidate
# realizations) consumes — see the design doc's bridge note.

def cmd_latex(ctx: Ctx) -> str:
    """Project the docmodel Document to a standalone LaTeX ``out.tex`` via
    html2latex: reconstruct a clean HTML document from the model (flow order,
    section hierarchy), then ``html2latex.render`` it to a full ``\\documentclass``
    document. OFFLINE; requires ``model`` (auto-ensured). Idempotent via
    LATEX_BUILT / --force.

    html2latex is an OPTIONAL dependency (Python 3.10+, not stdlib). Absent, this
    errors with an install hint — exactly how `print --engine firefox` gates
    selenium. The emitted LaTeX is structurally faithful but inline-plain (the
    model carries collapsed text; see docs/superpowers/specs)."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if sc.has(LATEX_BUILT) and sc.has_blob("out.tex") and not ctx.force:
        return _latex_report(sc, cached=True)
    doc = _load_document(sc)                 # ensures pdfdrill bridge + loads model
    try:
        import html2latex as _h2l
    except Exception as e:  # noqa: BLE001 — actionable, not a cryptic ImportError
        raise RuntimeError(
            f"`latex` needs html2latex, which isn't importable ({e}). Install it: "
            f"`pip install 'git+https://github.com/pankaj28843/html2latex'` "
            f"(it also needs justhtml<2 for the current adapter)."
        ) from e
    from .project_latex import document_to_html, make_extractor

    # data: images in single.html must become real files on disk, or
    # \includegraphics won't compile — extract them beside out.tex. Clear any
    # stale assets from a prior build so counts and files stay in sync.
    assets_dir = sc.blob_dir / "tex-assets"
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)
    extractor = make_extractor(assets_dir)

    html = document_to_html(doc, extractor=extractor)
    t0 = time.perf_counter()
    raw_tex = _h2l.render(html)
    cost_ms = (time.perf_counter() - t0) * 1000
    if not isinstance(raw_tex, str):
        raw_tex = raw_tex.decode("utf-8") if isinstance(raw_tex, bytes) else str(raw_tex)

    # Renovate html2latex's dated output (\par terminators, bare preamble, glued
    # environments) into idiomatic modern LaTeX. Purely textual + idempotent.
    from .latex_renovate import renovate
    tex = renovate(raw_tex)

    sc.write_blob("out.tex", tex)
    by_type: dict[str, int] = {}
    for o in doc.objects.values():
        by_type[o.type] = by_type.get(o.type, 0) + 1
    pars_removed = len(re.findall(r"\\par(?![a-zA-Z])", raw_tex))
    sc.set_evidence("latex_bytes", len(tex.encode("utf-8", "replace")))
    sc.set_evidence("latex_objects_by_type", by_type)
    sc.set_evidence("latex_images_embedded", extractor.embedded)
    sc.set_evidence("latex_images_dropped", len(extractor.dropped))
    sc.set_evidence("latex_renovated", True)
    sc.set_evidence("latex_pars_removed", pars_removed)
    sc.set_layer("latex", {"path": "out.tex", "format": "application/x-tex"})
    sc.add_fact(LATEX_BUILT)
    sc.log_transition("latex", _prev(sc, LATEX_BUILT), LATEX_BUILT, cost_ms,
                      f"html2latex → out.tex ({len(tex)} chars) from {by_type}; "
                      f"{extractor.embedded} images embedded, {len(extractor.dropped)} dropped")
    sc.save()
    return _latex_report(sc, cached=False)


def _latex_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached latex" if cached else "latex"
    by_type = ev.get("latex_objects_by_type", {})
    lines = [
        f"{tag} {sc.local_id}: docmodel → standalone LaTeX "
        f"({ev.get('latex_bytes', '?')} bytes)",
        f"  → {sc.blob_path('out.tex')}",
    ]
    if by_type:
        lines.append("  projected objects: " +
                     ", ".join(f"{t}×{n}" for t, n in sorted(by_type.items())))
    emb = ev.get("latex_images_embedded")
    if emb is not None:
        drp = ev.get("latex_images_dropped", 0)
        lines.append(f"  images:      {emb} embedded → {sc.blob_path('tex-assets')}/"
                     + (f"  ({drp} unresolved offline, dropped to captions)" if drp else ""))
    if ev.get("latex_renovated"):
        lines.append(f"  renovated:   modern preamble + {ev.get('latex_pars_removed', 0)} "
                     f"archaic \\par terminators → blank-line paragraphs")
    lines.append("  next: compile with `xelatex out.tex` (Unicode-safe — web text "
                 "carries glyphs pdflatex needs a preamble for; data: images are files)")
    return "\n".join(lines)


# -- L4 split recovery (lazy-load & virtualization) --------------------------
#
# A virtualized list, an infinite-scroll feed, a lazy node, or a collapsed
# subtree is CUT content. `splits` (OFFLINE detector) reports it, classified by
# KIND + REPAIR ENERGY. `materialize` (ESCALATION, never a `requires:`) is the
# repair: it recovers the hidden body that is ALREADY in the markup (details /
# template / noscript) and re-injects each as a docmodel role=continuation
# support fragment, or — with --render-delta — runs headless Chrome with the
# virtual-time-budget flag and records the NEW post-render blocks as continuation
# fragments. Each materialization is a confidence-bearing claim in the log.

#: per-kind one-line gloss for the prose report
_SPLIT_KIND_GLOSS = {
    "collapsed": "collapsed <details> (toggle to open)",
    "hidden": "content-visibility render hint (text still in markup)",
    "deferred": "<template>/<noscript> deferred body",
    "lazy-media": "loading=\"lazy\" image (src in markup, bytes deferred)",
    "virtualized": "role=feed / aria-setsize windowed list",
}


def cmd_splits(ctx: Ctx) -> str:
    """L4 split/hidden-content DETECTOR (OFFLINE — reads the snapshot, no render).

    Parses the captured static (or rendered) snapshot and reports content the
    rendered VIEW hides but the static MARKUP keeps, each classified by KIND
    (collapsed / hidden / deferred / lazy-media / virtualized) and REPAIR ENERGY
    (toggle / scroll / intersection / none). Records the fact SPLITS_KNOWN plus
    evidence so `materialize` can recover the bodies. Idempotent via the fact /
    --force."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not (sc.has(CAPTURED) or sc.has(RENDERED) or sc.has(FETCHED)):
        raise FileNotFoundError(
            f"no snapshot for {ctx.url!r} — run `htmldrill fetch {ctx.url}` first; "
            f"`splits` is offline and won't fetch.")
    html, source = _rendered_or_static(sc)
    splits = H.detect_splits(html)
    summary = H.summarize_splits(splits)
    # record a compact evidence digest (kind/energy/evidence/recovered-length) —
    # the full recovered bodies are re-derived by `materialize` from the snapshot.
    digest = [{"kind": s.kind, "energy": s.energy, "tag": s.tag,
               "evidence": s.evidence, "recovered_chars": len(s.recovered)}
              for s in splits]
    sc.set_evidence("splits_source", source)
    sc.set_evidence("splits_total", summary["total"])
    sc.set_evidence("splits_by_kind", summary["by_kind"])
    sc.set_evidence("splits_by_energy", summary["by_energy"])
    sc.set_evidence("splits_recoverable", summary["recoverable"])
    sc.set_evidence("splits_digest", digest)
    sc.add_fact(SPLITS_KNOWN)
    sc.log_transition("splits", _prev(sc, SPLITS_KNOWN), SPLITS_KNOWN, 0,
                      f"{summary['total']} splits {summary['by_kind']} from {source}")
    sc.save()
    if not splits:
        return (f"{sc.local_id}: no split/hidden content detected in the {source} "
                f"markup (no collapsed <details>, <template>/<noscript>, "
                f"loading=lazy img, content-visibility, or role=feed).")
    lines = [f"{sc.local_id}: {summary['total']} split(s) in the {source} markup "
             f"— {summary['recoverable']} carry body text recoverable offline"]
    lines.append("  by kind:   " + ", ".join(
        f"{k}×{n}" for k, n in sorted(summary["by_kind"].items())))
    lines.append("  by energy: " + ", ".join(
        f"{e}×{n}" for e, n in sorted(summary["by_energy"].items())) +
        "   (none = already in markup; toggle/scroll/intersection = browser action)")
    # a few examples, preferring those with recovered body
    examples = sorted(splits, key=lambda s: -len(s.recovered))[:4]
    lines.append("  examples:")
    for s in examples:
        gloss = _SPLIT_KIND_GLOSS.get(s.kind, s.kind)
        body = (f" — {len(s.recovered)} chars recovered: "
                f"{s.recovered[:60].strip()!r}" if s.recovered else "")
        lines.append(f"    [{s.kind}/{s.energy}] {gloss}  ⟨{s.evidence}⟩{body}")
    lines.append("  → repair with `htmldrill materialize " + sc.local_id +
                 "` (offline: expand the in-markup bodies as role=continuation "
                 "fragments; --render-delta: headless diff for post-render blocks)")
    return "\n".join(lines)


def _continuation_blocks(splits: list) -> list[tuple[str, str, str]]:
    """The offline-recoverable splits → (kind, energy, body) continuation tuples.

    Only splits whose body is ALREADY in the markup (non-empty ``recovered``)
    become continuation fragments offline — collapsed details, template,
    noscript. lazy-media keeps only a src reference (no inline body) and
    virtualized has no inline children, so those are reported by `splits` but not
    materialized here (they need network/scroll — honestly documented as a limit).
    """
    out = []
    for s in splits:
        if s.recovered and s.kind in ("collapsed", "deferred"):
            out.append((s.kind, s.energy, s.recovered))
    return out


def cmd_materialize(ctx: Ctx) -> str:
    """REPAIR the splits (ESCALATION — never listed in any `requires:`).

    Two modes:

    mode a (DEFAULT, OFFLINE): expand content already in the markup but hidden —
    collapsed <details> bodies, <template>, <noscript> — and inject each as a
    docmodel **role=continuation** support fragment (a continuation DocObject
    carrying a continuation Realization, appended to the model). Records a
    ``continuation`` layer, the fact MATERIALIZED, and one transition per
    materialization (each is a confidence-bearing claim in the log). If the model
    is already built (MODEL_BUILT) the fragments are appended into it; otherwise
    they are persisted to ``continuation.json`` and folded in when `model` runs.

    mode b (--render-delta, NETWORK + headless): run Chrome with the
    virtual-time-budget flag, diff the materialized DOM against the plain render
    snapshot, and record the NEW blocks as continuation fragments with
    trigger=virtual-time. Clear error if Chrome is absent.

    LIMIT (documented honestly): virtual-time materializes timer/microtask/
    animation-frame content but NOT scroll-intersection content — infinite-scroll
    feeds gated on an IntersectionObserver need CDP input events (an explicit
    scroll step) which this command does not yet drive. Those show up in `splits`
    as virtualized/lazy-media but are not auto-materialized."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not (sc.has(CAPTURED) or sc.has(RENDERED) or sc.has(FETCHED)):
        raise FileNotFoundError(
            f"no snapshot for {ctx.url!r} — run `htmldrill fetch {ctx.url}` first.")

    if getattr(ctx, "render_delta", False):
        return _materialize_render_delta(ctx, sc)

    # -- mode a: offline expansion of in-markup hidden bodies --
    html, source = _rendered_or_static(sc)
    splits = H.detect_splits(html)
    frags = _continuation_blocks(splits)
    # DEDUP: the base model already captures content the stdlib parser reads
    # statically — a CLOSED <details> body is collapsed in the browser but its
    # text is right there in the markup, so walk_blocks already has it. Injecting
    # it again would duplicate paragraphs in the projection. Only genuinely
    # DEFERRED content (<noscript>/<template>, which walk_blocks skips) is absent
    # from the base model and worth recovering. Keep a fragment only when its
    # text is not already in what `model` extracted.
    base_text = " ".join(" ".join(b.text.split()) for b in H.walk_blocks(html))
    def _absent_from_base(t: str) -> bool:
        probe = " ".join(t.split())[:120]
        return bool(probe) and probe not in base_text
    frags = [(k, e, t) for (k, e, t) in frags if _absent_from_base(t)]
    if not frags:
        # still record the (empty) outcome so the fact/idempotency is honest
        return (f"{sc.local_id}: nothing to materialize offline — collapsed "
                f"<details> bodies are already in the {source} model; only "
                f"<noscript>/<template> deferred content (absent from the base) "
                f"or --render-delta JS content is recovered here. "
                f"(run `htmldrill splits {sc.local_id}` to see all splits.)")

    t0 = time.perf_counter()
    fragments = [{"kind": k, "energy": e, "trigger": "in-markup",
                  "role": "continuation", "text": t} for k, e, t in frags]

    injected_into_model = False
    if sc.has(MODEL_BUILT) and sc.has_blob("model.docmodel.json"):
        injected_into_model = _inject_continuation(sc, fragments)

    # persist the continuation layer regardless (so a later `model` folds it in)
    sc.write_blob("continuation.json",
                  json.dumps(fragments, indent=2, ensure_ascii=False))
    total_chars = sum(len(f["text"]) for f in fragments)
    cost_ms = (time.perf_counter() - t0) * 1000
    sc.set_layer("continuation", {"path": "continuation.json",
                                  "format": "application/json"})
    sc.set_evidence("materialized_count", len(fragments))
    sc.set_evidence("materialized_chars", total_chars)
    sc.set_evidence("materialized_mode", "offline")
    sc.set_evidence("materialized_into_model", injected_into_model)
    sc.add_fact(MATERIALIZED)
    # one confidence-bearing transition PER materialization
    for f in fragments:
        sc.log_transition("materialize", _prev(sc, MATERIALIZED), MATERIALIZED,
                          0, f"continuation[{f['kind']}/{f['energy']}] "
                             f"{len(f['text'])} chars trigger={f['trigger']}")
    sc.save()
    where = ("appended into model.docmodel.json (MODEL_BUILT)"
             if injected_into_model else
             "saved to continuation.json — folded in when you run `model`")
    lines = [f"{sc.local_id}: materialized {len(fragments)} hidden block(s) "
             f"({total_chars} chars) as role=continuation fragments in {cost_ms:.0f} ms"]
    for f in fragments:
        lines.append(f"  + [{f['kind']}/{f['energy']}] {len(f['text'])} chars: "
                     f"{f['text'][:60].strip()!r}")
    lines.append(f"  → {where}")
    lines.append(f"  → {sc.blob_path('continuation.json')}")
    lines.append("  next: `model` (folds continuation in) · `tiddlers`/`md`/`llmtext`")
    return "\n".join(lines)


def _inject_continuation(sc: Sidecar, fragments: list[dict]) -> bool:
    """Append each continuation fragment into the persisted docmodel Document as a
    role=continuation Realization on a continuation DocObject. Returns True on a
    successful in-place model update. Reuses the same docmodel machinery as
    `model` (pdfdrill's REAL Document/DocObject/Realization)."""
    try:
        from .ingest_dom import ensure_pdfdrill, HTML_STREAM
        ensure_pdfdrill()
        from docmodel import Document, DocObject, Realization  # noqa: WPS433
    except Exception:
        return False
    raw = sc.read_blob("model.docmodel.json")
    if not raw:
        return False
    doc = Document.from_dict(json.loads(raw))
    stream = doc.ensure_stream(HTML_STREAM)
    base = doc.meta.get("block_count", len(doc.objects))
    for j, f in enumerate(fragments):
        # extra anchor for the recovered body + a continuation DocObject carrying a
        # role=continuation Realization (the pdfdrill continuation-role contract).
        anchor = stream.append(text=f["text"], type="Continuation",
                               _line_index=base + j)
        obj = DocObject(type="Paragraph", props={
            "flow_index": base + j, "text": f["text"],
            "continuation": True, "split_kind": f["kind"]})
        obj.add_realization(Realization(
            stream=HTML_STREAM, start=anchor, end=anchor, role="continuation"))
        doc.add(obj)
    doc.meta["continuation_count"] = len(fragments)
    sc.write_blob("model.docmodel.json",
                  json.dumps(doc.to_dict(), indent=2, ensure_ascii=False))
    return True


def _materialize_render_delta(ctx: Ctx, sc: Sidecar) -> str:
    """mode b: headless render with the virtual-time-budget flag, diff against the
    plain render snapshot, record NEW post-render blocks as continuation
    fragments (trigger=virtual-time). Clear error if Chrome is absent."""
    base_url = _base_url(sc) or sc.get_evidence("url") or ctx.url
    if not base_url:
        raise ValueError("no URL recorded for this target — cannot render-delta.")
    t0 = time.perf_counter()
    res = R.render_materialize(base_url, timeout=ctx.timeout, window=ctx.window)
    cost_ms = (time.perf_counter() - t0) * 1000
    # plain render snapshot for the diff: prefer the captured rendered.html, else
    # the plain DOM the render_materialize call also produced.
    plain = sc.read_blob("rendered.html") or res.plain_dom or ""
    new_blocks = _dom_block_delta(plain, res.dom)
    sc.write_blob("materialized.html", res.dom)
    fragments = [{"kind": "deferred", "energy": "virtual-time",
                  "trigger": "virtual-time", "role": "continuation", "text": t}
                 for t in new_blocks if t.strip()]
    injected = False
    if fragments and sc.has(MODEL_BUILT) and sc.has_blob("model.docmodel.json"):
        injected = _inject_continuation(sc, fragments)
    sc.write_blob("continuation.json",
                  json.dumps(fragments, indent=2, ensure_ascii=False))
    sc.set_layer("continuation", {"path": "continuation.json",
                                  "format": "application/json"})
    sc.set_evidence("materialized_count", len(fragments))
    sc.set_evidence("materialized_mode", "render-delta")
    sc.set_evidence("materialized_into_model", injected)
    sc.set_evidence("render_delta_bytes",
                    len(res.dom.encode("utf-8", "replace")) -
                    len(plain.encode("utf-8", "replace")))
    sc.add_fact(MATERIALIZED)
    for f in fragments:
        sc.log_transition("materialize", _prev(sc, MATERIALIZED), MATERIALIZED,
                          cost_ms, f"continuation[render-delta] {len(f['text'])} "
                                   f"chars trigger=virtual-time")
    if not fragments:
        sc.log_transition("materialize", _prev(sc, MATERIALIZED), MATERIALIZED,
                          cost_ms, "render-delta: no new post-render blocks")
    sc.save()
    delta_bytes = sc.get_evidence("render_delta_bytes", 0)
    lines = [f"{sc.local_id}: render-delta materialize via "
             f"{Path(res.chrome).name} in {cost_ms:.0f} ms "
             f"(Δ{delta_bytes:+d} bytes vs plain render)"]
    if fragments:
        lines.append(f"  {len(fragments)} NEW post-render block(s) recorded as "
                     f"role=continuation (trigger=virtual-time):")
        for f in fragments[:6]:
            lines.append(f"    + {len(f['text'])} chars: {f['text'][:60].strip()!r}")
    else:
        lines.append("  no NEW blocks appeared after virtual-time render "
                     "(static markup already complete, or content is scroll-gated).")
    lines.append("  NOTE: virtual-time covers timer/microtask/raf content; "
                 "scroll-driven infinite-scroll needs CDP input events (a known limit).")
    return "\n".join(lines)


def _dom_block_delta(plain: str, materialized: str) -> list[str]:
    """Block-text present in the materialized DOM but absent from the plain render.

    Compares the structural walk of both DOMs; returns the text of blocks the
    virtual-time render added (timer/raf-injected nodes). Deterministic, offline
    once both DOMs are in hand."""
    plain_texts = {b.text.strip() for b in H.walk_blocks(plain) if b.text.strip()}
    out: list[str] = []
    for b in H.walk_blocks(materialized):
        t = b.text.strip()
        if t and t not in plain_texts and t not in out:
            out.append(t)
    return out


# -- M5 crawl (bounded same-origin frontier) ---------------------------------
#
# A web document is rarely one page — `crawl` walks the bounded same-origin
# frontier from a start URL, fetching + modelling each page (it reuses cmd_fetch
# and cmd_model verbatim, so each page lands as a normal target with its own
# sidecar/blobs/model). It follows only INTERNAL links (the `extract_links`
# internal bucket: same host for http(s), same directory subtree for file://).
# NETWORK for http(s) — like `fetch`/`render`, it is NEVER listed in any
# `requires:` so --ensure can't trigger it; offline for file://. Bounded by
# --max, cycle-safe via a visited set, robots.txt-respecting for http.

def _same_origin(start_url: str, candidate: str) -> bool:
    """Same-origin test used to bound the frontier.

    For http(s): identical (scheme, host, port). For file://: the candidate's
    path lives under the START url's *directory subtree* (so the crawl never
    escapes the site folder). `extract_links` already buckets these as internal,
    but we re-check here so --same-origin is a real, independent guard."""
    from urllib.parse import urlparse
    a, b = urlparse(start_url), urlparse(candidate)
    if a.scheme in ("http", "https"):
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)
    if a.scheme == "file":
        base_dir = a.path.rsplit("/", 1)[0] + "/"
        return b.scheme == "file" and b.path.startswith(base_dir)
    return False


def _robots_ok(url: str, ua: str) -> bool:
    """robots.txt politeness for http(s): True if the UA may fetch `url`. If the
    robots fetch fails (network error / no robots), proceed politely (True) — we
    never block the crawl on an unreachable robots file. Offline file:// always
    allowed."""
    from urllib.parse import urlparse, urlunparse
    from urllib.robotparser import RobotFileParser
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return True
    robots_url = urlunparse((p.scheme, p.netloc, "/robots.txt", "", "", ""))
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception:  # noqa: BLE001 — unreachable/garbled robots ⇒ proceed politely
        return True
    try:
        return rp.can_fetch(ua, url)
    except Exception:  # noqa: BLE001
        return True


def cmd_crawl(ctx: Ctx) -> str:
    """Bounded same-origin crawl from a start URL (M5).

    Breadth-first to --depth (default 1), capped at --max pages (default 20),
    restricted to same-origin internal links when --same-origin (default on).
    Each page is fetched + modelled (reusing `fetch`/`model`), so every visited
    page becomes a normal htmldrill target. NETWORK for http(s) (robots.txt
    respected; an unreachable robots file ⇒ proceed politely); OFFLINE for
    file:// (same-origin == same directory subtree). Idempotent + cycle-safe:
    a visited set dedups, and the per-target fetch/model are themselves cached.

    Records a crawl summary on the START target's sidecar (fact CRAWLED + a
    `crawl.json` evidence blob: pages visited in order, links followed, per-page
    object ids)."""
    if not ctx.url:
        raise ValueError("usage: htmldrill crawl <url> [--depth N] [--max P]")
    from urllib.parse import urlparse

    ua = ctx.ua or F.DEFAULT_UA
    start_norm = F.normalize_url(ctx.url)
    start_is_http = urlparse(start_norm).scheme in ("http", "https")

    # The start target's sidecar carries the crawl summary.
    start_sc = Sidecar(F.local_id_for(ctx.url), work=ctx.work)
    if start_sc.has(CRAWLED) and start_sc.has_blob("crawl.json") and not ctx.force:
        summary = json.loads(start_sc.read_blob("crawl.json") or "{}")
        return _crawl_report(start_sc, summary, cached=True)

    t0 = time.perf_counter()
    # frontier holds (url, depth); we resolve everything to absolute URLs so the
    # visited set and same-origin checks are unambiguous. The start origin is the
    # first fetched page's final_url (its own file:// uri or canonical http url).
    visited: set[str] = set()
    pages: list[dict] = []          # per-page record: {url, id, depth, objects, links}
    followed: list[str] = []        # the edges actually traversed (for the summary)
    skipped_robots: list[str] = []
    origin_anchor: Optional[str] = None

    # BFS queue keyed by the ORIGINAL url string; we map each to a target id.
    queue: list[tuple[str, int]] = [(ctx.url, 0)]
    while queue and len(pages) < ctx.max_pages:
        url, depth = queue.pop(0)
        # robots gate (http only); file:// always allowed.
        if start_is_http and not _robots_ok(url, ua):
            skipped_robots.append(url)
            continue
        page_ctx = Ctx(url=url, work=ctx.work, force=ctx.force, ua=ctx.ua,
                       timeout=ctx.timeout)
        # fetch + model (both idempotent/cached). Reuse the real handlers.
        cmd_fetch(page_ctx)
        page_sc = Sidecar(F.local_id_for(url), work=ctx.work)
        final_url = page_sc.get_evidence("final_url") or start_norm
        if origin_anchor is None:
            origin_anchor = final_url
        if final_url in visited:
            continue
        visited.add(final_url)
        try:
            cmd_model(page_ctx)
        except Exception:  # noqa: BLE001 — a model build failure must not abort the crawl
            pass
        obj_ids = _crawl_object_ids(page_sc)

        html = page_sc.read_blob("raw.html") or ""
        c = H.collect(html)
        links = H.extract_links(c, base_url=final_url)
        internal = [u for u, _ in links["internal"]]
        ext_count = len(links["external"])

        pages.append({
            "url": url, "final_url": final_url, "id": page_sc.local_id,
            "depth": depth, "objects": len(obj_ids), "object_ids": obj_ids,
            "internal_links": len(internal), "external_links": ext_count,
        })

        # enqueue same-origin internal children one level deeper, until depth cap.
        if depth < ctx.depth:
            for target in internal:
                if ctx.same_origin and not _same_origin(origin_anchor, target):
                    continue
                if target in visited:
                    continue
                if any(target == q[0] for q in queue):
                    continue
                if len(pages) + len(queue) >= ctx.max_pages:
                    break
                followed.append(f"{page_sc.local_id} → {target}")
                queue.append((target, depth + 1))

    cost_ms = (time.perf_counter() - t0) * 1000
    summary = {
        "start": ctx.url,
        "origin": origin_anchor,
        "depth": ctx.depth,
        "max_pages": ctx.max_pages,
        "same_origin": ctx.same_origin,
        "pages_visited": len(pages),
        "links_followed": len(followed),
        "robots_skipped": len(skipped_robots),
        "pages": pages,
        "followed": followed,
    }
    start_sc.write_blob("crawl.json", json.dumps(summary, indent=2, ensure_ascii=False))
    start_sc.set_layer("crawl", {"path": "crawl.json", "format": "application/json"})
    start_sc.set_evidence("crawl_pages", len(pages))
    start_sc.set_evidence("crawl_links_followed", len(followed))
    start_sc.add_fact(CRAWLED)
    start_sc.log_transition("crawl", _prev(start_sc, CRAWLED), CRAWLED, cost_ms,
                            f"{len(pages)} pages depth={ctx.depth} max={ctx.max_pages} "
                            f"followed={len(followed)}")
    start_sc.save()
    return _crawl_report(start_sc, summary, cached=False)


def _crawl_object_ids(sc: Sidecar) -> list[str]:
    """The docmodel object ids for a crawled page (empty if no model / no pdfdrill)."""
    if not sc.has_blob("model.docmodel.json"):
        return []
    try:
        doc = json.loads(sc.read_blob("model.docmodel.json") or "{}")
        objs = doc.get("objects", {})
        if isinstance(objs, dict):
            return list(objs.keys())
        if isinstance(objs, list):
            return [o.get("id", "") for o in objs if isinstance(o, dict) and o.get("id")]
        return []
    except Exception:  # noqa: BLE001
        return []


def _crawl_report(sc: Sidecar, summary: dict, cached: bool) -> str:
    tag = "cached crawl" if cached else "crawled"
    pages = summary.get("pages", [])
    lines = [
        f"{tag} {summary.get('start')}: {summary.get('pages_visited')} page(s) "
        f"(depth {summary.get('depth')}, max {summary.get('max_pages')}, "
        f"{summary.get('links_followed')} link(s) followed)",
    ]
    if summary.get("robots_skipped"):
        lines.append(f"  robots.txt disallowed: {summary['robots_skipped']} page(s) skipped")
    for p in pages:
        lines.append(f"  [{p['depth']}] {p['id']:<28} {p['objects']:>3} objs · "
                     f"{p['internal_links']} internal / {p['external_links']} external links")
    lines.append(f"  → crawl summary: {sc.blob_path('crawl.json')}")
    lines.append("  next: retrieve <url> <query> · chatlog <url> --ask …")
    return "\n".join(lines)


# -- M5 retrieve (offline lexical IDF/overlap over the docmodel Document) ------
#
# Reuse pdfdrill's REAL retrieve.py (pure + stdlib) over the htmldrill-built
# Document — NOT a reimplementation. OFFLINE; requires a `model` (auto-ensured).
# The retriever takes any iterable of objects exposing .type/.id/.props, which is
# exactly doc.objects.values().

def cmd_retrieve(ctx: Ctx) -> str:
    """Rank the document's units against a query (M5, OFFLINE).

    Reuses pdfdrill's lexical IDF/overlap retriever (`pdfdrill.retrieve`) over the
    htmldrill-built docmodel Document — same operator pdfdrill runs over PDFs.
    `requires: [model]` (auto-ensured by --ensure; offline, never a fetch).
    Returns the ranked matching units (section/paragraph text + score), each
    tagged by object id so an answer can cite them. `--json` emits
    {question, units, prompt, title, subjects} for a wrapper (drillui)."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    question = (ctx.query or "").strip()
    if not question:
        raise ValueError("usage: htmldrill retrieve <url> <query...>")
    if not sc.has_blob("model.docmodel.json"):
        raise FileNotFoundError(
            f"no docmodel for {ctx.url!r} — run `htmldrill model {ctx.url}` first "
            f"(offline; needs a prior fetch/render). `retrieve` is offline and "
            f"won't fetch.")
    # pdfdrill's retrieve.py is pure stdlib but lives in the pdfdrill src tree we
    # bridge to; import it the same way `model` imports docmodel.
    from .ingest_dom import ensure_pdfdrill
    ensure_pdfdrill()
    try:
        from docmodel import Document  # noqa: WPS433
        from pdfdrill import retrieve as R  # noqa: WPS433
    except Exception as e:  # noqa: BLE001 — clear error if the import fails
        raise ModuleNotFoundError(
            f"htmldrill `retrieve` needs pdfdrill's retrieve.py + docmodel, but the "
            f"import failed ({e}). Point $HTMLDRILL_PDFDRILL at a pdfdrill checkout's "
            f"src dir.") from e

    doc = Document.from_dict(json.loads(sc.read_blob("model.docmodel.json") or "{}"))
    nodes = list(doc.objects.values())
    t0 = time.perf_counter()
    hits = R.retrieve(question, nodes, k=ctx.k)
    cost_ms = (time.perf_counter() - t0) * 1000
    title = doc.meta.get("title", "") or sc.local_id
    prompt = R.build_prompt(question, hits, title=str(title), subjects="")

    sc.set_evidence("retrieve_last_query", question)
    sc.set_evidence("retrieve_last_hits", len(hits))
    sc.add_fact(RETRIEVED)
    sc.log_transition("retrieve", _prev(sc, RETRIEVED), RETRIEVED, cost_ms,
                      f"{len(hits)} hits for {question[:40]!r}")
    sc.save()

    if ctx.as_json:
        return json.dumps({"question": question, "units": hits, "prompt": prompt,
                           "title": str(title), "subjects": ""}, ensure_ascii=False)
    if not hits:
        return (f"{sc.local_id}: no unit shares a term with {question!r} "
                f"({len(nodes)} object(s) in the model).")
    lines = [f"{sc.local_id}: top {len(hits)} unit(s) for {question!r} (cite these ids):"]
    for h in hits:
        lines.append(f"  [{h['id']}] ({h['type']}, {h['score']:.1f}) "
                     f"{h['text'][:140].strip()}")
    return "\n".join(lines)


# -- M5 chatlog (offline transcript blob) ------------------------------------
#
# Append a Q&A turn to a per-target chat log blob (chat.jsonl) and show it. Same
# line shape as pdfdrill's cmd_chatlog (question, answer, units, model, ts) so a
# transcript reader is identical; the semantic-kitem half is dropped (htmldrill
# has no semantic graph). No network.

def cmd_chatlog(ctx: Ctx) -> str:
    """Append a Q&A turn to the per-target chat transcript, or show it (M5).

    `htmldrill chatlog <url> --ask "Q" --answer "A" [--units id,id] [--model name]`
    appends one JSON line to ``chat.jsonl`` in the target's blob dir;
    `htmldrill chatlog <url>` (no --ask) prints the existing transcript. Line
    shape mirrors pdfdrill: {question, answer, units, model, ts}. OFFLINE."""
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    log_path = sc.blob_path("chat.jsonl")

    if ctx.ask:
        unit_ids = [u for u in (ctx.units or "").split(",") if u.strip()]
        turn = {
            "question": ctx.ask,
            "answer": ctx.answer or "",
            "units": unit_ids,
            "model": ctx.model_name or "",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }
        sc.blob_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")
        sc.set_layer("chatlog", {"path": "chat.jsonl", "format": "application/jsonl"})
        n_turns = _chatlog_count(log_path)
        sc.set_evidence("chatlog_turns", n_turns)
        sc.add_fact(CHATLOGGED)
        sc.log_transition("chatlog", _prev(sc, CHATLOGGED), CHATLOGGED, 0,
                          f"appended turn {n_turns} ({len(unit_ids)} units)")
        sc.save()
        return (f"{sc.local_id}: appended turn {n_turns} to the chat log\n"
                f"  Q: {ctx.ask[:80]}\n"
                f"  A: {(ctx.answer or '')[:80]}\n"
                f"  → {log_path}")

    # show mode
    if not log_path.exists():
        return (f"{sc.local_id}: no chat log yet — append one with "
                f"`htmldrill chatlog {ctx.url} --ask \"…\" --answer \"…\"`.")
    lines = [f"{sc.local_id}: chat transcript ({_chatlog_count(log_path)} turn(s))"]
    with open(log_path, encoding="utf-8") as f:
        for i, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                t = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            units = ", ".join(t.get("units", []))
            lines.append(f"  [{i}] {t.get('ts', '')}  ({t.get('model', '') or '—'})")
            lines.append(f"      Q: {t.get('question', '')[:100]}")
            lines.append(f"      A: {t.get('answer', '')[:100]}")
            if units:
                lines.append(f"      grounded in: {units}")
    lines.append(f"  → {log_path}")
    return "\n".join(lines)


def _chatlog_count(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    with open(log_path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


# -- L1c capture: scroll-materialize a real page, then grab everything --------

def cmd_capture(ctx: Ctx) -> str:
    """Drive a REAL browser to fully materialize the page (scroll until it stops
    growing), then capture the loaded DOM + a print-to-PDF + a screenshot
    (ESCALATION — network + headless; never in any `requires:`).

    This is the "get as much as possible from a URL, today" fallback. Unlike a
    one-shot render, it recovers lazy content — infinite-scroll feeds and
    IntersectionObserver-gated tables that a plain capture drops (a 600-row lazy
    table comes back complete, not truncated to the first 50). The scroll-until-
    stable rule is general, not tuned to any page.

    Profile isolation is mandatory: the automation browser uses a throwaway
    profile, never the user's real one, so their running browser is untouched.
    --engine firefox (default, Selenium ephemeral profile) or chrome (explicit
    throwaway --user-data-dir). A fresh profile is not logged in, so gated pages
    yield only their logged-out content for now."""
    if not ctx.url:
        raise ValueError("usage: htmldrill capture <url> [--engine firefox|chrome]")
    sc = Sidecar(F.local_id_for(ctx.url), work=ctx.work)
    if sc.has(CAPTURED) and not ctx.force:
        return _capture_report(sc, cached=True)
    t0 = time.perf_counter()
    res = CAP.capture(ctx.url, engine=ctx.engine, page_timeout=ctx.timeout)
    cost_ms = (time.perf_counter() - t0) * 1000

    sc.write_blob("captured.html", res.dom)
    pdf_verdict: dict = {}
    if res.pdf:
        pdf_path = sc.blob_path("capture.pdf")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(res.pdf)
        pdf_verdict = P.validate_text_layer(pdf_path)
    if res.screenshot:
        sc.write_blob_bytes("capture.png", res.screenshot)

    c = H.collect(res.dom)
    tables = res.dom.count("<table") + res.dom.count("<TABLE")
    sc.set_evidence("url", sc.get_evidence("url") or ctx.url)
    sc.set_evidence("capture_engine", res.engine)
    sc.set_evidence("capture_final_url", res.final_url)
    sc.set_evidence("capture_scroll_rounds", res.rounds)
    sc.set_evidence("capture_dom_bytes", len(res.dom.encode("utf-8", "replace")))
    sc.set_evidence("capture_dom_tags", c.tag_count)
    sc.set_evidence("capture_tables", tables)
    sc.set_evidence("capture_pdf_bytes", len(res.pdf) if res.pdf else 0)
    sc.set_evidence("capture_pdf_text_usable", pdf_verdict.get("usable"))
    sc.set_evidence("capture_profile", res.profile)
    sc.set_layer("captured_html", {"path": "captured.html", "format": "text/html"})
    if res.pdf:
        sc.set_layer("capture_pdf", {"path": "capture.pdf", "format": "application/pdf"})
    sc.add_fact(CAPTURED)
    sc.log_transition("capture", _prev(sc, CAPTURED), CAPTURED, cost_ms,
                      f"{res.engine} {res.rounds} scrolls, {c.tag_count} tags, "
                      f"{tables} tables, pdf_text={pdf_verdict.get('usable')}")
    sc.save()
    return _capture_report(sc, cached=False)


def _capture_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached capture" if cached else "captured"
    usable = ev.get("capture_pdf_text_usable")
    mark = {True: "✓", False: "✗", None: "?"}.get(usable, "?")
    lines = [
        f"{tag} {ev.get('url')} via {ev.get('capture_engine')} "
        f"({ev.get('capture_scroll_rounds')} scroll rounds — lazy content materialized)",
        f"  id:          {sc.local_id}",
        f"  DOM:         {ev.get('capture_dom_bytes')} bytes, ~{ev.get('capture_dom_tags')} tags, "
        f"{ev.get('capture_tables')} tables  → {sc.blob_path('captured.html')}",
        f"  PDF:         {ev.get('capture_pdf_bytes')} bytes, text layer {mark}  "
        f"→ {sc.blob_path('capture.pdf')}",
        f"  screenshot:  {sc.blob_path('capture.png') if sc.has_blob('capture.png') else '(none)'}",
        f"  profile:     {ev.get('capture_profile')}  (isolated — your browser untouched)",
    ]
    if usable:
        lines.append(f"  next: hand the PDF to pdfdrill — `pdfdrill size {sc.blob_path('capture.pdf')}` "
                     f"— or `model`/`tiddlers` over the captured DOM")
    else:
        lines.append(f"  next: `model` over the captured DOM, or OCR the PDF via pdfdrill")
    return "\n".join(lines)


# -- L1d single: freeze the page + all assets into one self-contained HTML ----

def cmd_single(ctx: Ctx) -> str:
    """Download the URL with monolith and inline EVERY asset (CSS/images/fonts/JS)
    into one self-contained ``single.html`` (ESCALATION — network; never in any
    `requires:`). Two payoffs: a portable archive that renders offline, and the
    preferred SOURCE for the offline `model` pipeline (inlined `data:` assets let
    `ingest_dom` see real image src's and complete markup, not dangling network
    references). Idempotent: skips when SINGLE unless ``--force``.

    Optional levers: ``--no-js`` drops scripts (a cleaner, more deterministic
    model source); ``--isolate`` cuts the result off from the network entirely."""
    if not ctx.url:
        raise ValueError("usage: htmldrill single <url> [--engine auto|monolith|python] "
                         "[--no-js] [--isolate]")
    sc = Sidecar(F.local_id_for(ctx.url), work=ctx.work)
    if sc.has(SINGLE) and sc.has_blob("single.html") and not ctx.force:
        return _single_report(sc, cached=True)

    # engine: monolith (Rust binary) or python (stdlib inliner, JS-aware via Chrome).
    # auto → monolith when installed, else the pure-Python path (no Rust needed).
    engine = ctx.engine if ctx.engine in ("monolith", "python") else (
        "monolith" if MONO.find_monolith() else "python")
    t0 = time.perf_counter()
    if engine == "monolith":
        res = MONO.save_single(ctx.url, sc.blob_path("single.html"), ua=ctx.ua,
                               timeout=ctx.timeout, no_js=ctx.no_js, isolate=ctx.isolate)
        nbytes, source, stats = res.bytes, "monolith", {}
    else:
        nbytes, source, stats = _single_python(ctx, sc)
    cost_ms = (time.perf_counter() - t0) * 1000

    sc.set_evidence("url", sc.get_evidence("url") or ctx.url)
    sc.set_evidence("single_bytes", nbytes)
    sc.set_evidence("single_engine", engine)
    sc.set_evidence("single_source", source)
    sc.set_evidence("single_no_js", ctx.no_js)
    sc.set_evidence("single_stats", stats)
    sc.set_layer("single_html", {"path": "single.html", "format": "text/html"})
    sc.add_fact(SINGLE)
    sc.log_transition("single", _prev(sc, SINGLE), SINGLE, cost_ms,
                      f"{nbytes}B via {engine}/{source} (no_js={ctx.no_js}) {stats}")
    sc.save()
    return _single_report(sc, cached=False)


def _single_python(ctx: Ctx, sc: Sidecar) -> tuple[int, str, dict]:
    """Pure-Python archive: render with Chrome for a JS-aware DOM when available
    (captures JS-built content monolith can't), else the static fetch; then inline
    every asset as a data: URI. Returns (bytes_written, source_label, stats)."""
    chrome = R.find_chrome()
    if chrome and not ctx.isolate:
        rr = R.render(ctx.url, timeout=max(ctx.timeout, 45.0), screenshot=False)
        html, base, source = rr.dom, rr.final_url, "rendered"
    else:
        fr = F.fetch(ctx.url, timeout=ctx.timeout, ua=ctx.ua)
        html, base, source = fr.text, fr.final_url, "static"
    out, stats = INLINE.inline_assets(html, base, timeout=ctx.timeout, ua=ctx.ua,
                                      strip_scripts=ctx.no_js)
    n = len(out.encode("utf-8", "replace"))
    sc.write_blob("single.html", out)
    return n, source, stats


def _single_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached single" if cached else "archived"
    engine = ev.get("single_engine", "monolith")
    source = ev.get("single_source", "")
    stats = ev.get("single_stats") or {}
    lines = [
        f"{tag} {ev.get('url')} → one self-contained HTML "
        f"({ev.get('single_bytes')} bytes, all assets inlined)",
        f"  id:          {sc.local_id}",
        f"  engine:      {engine}"
        + (f" (from the {source} DOM)" if source and source != "monolith" else ""),
        f"  archive:     {sc.blob_path('single.html')}",
    ]
    if stats:
        lines.append(f"  inlined:     {stats.get('assets', 0)} assets, "
                     f"{stats.get('stylesheets', 0)} stylesheets, "
                     f"{stats.get('scripts_dropped', 0)} scripts dropped"
                     + (f", {stats['failed']} failed" if stats.get('failed') else ""))
    lines.append("  next: `model` (prefers this self-contained HTML) → `latex`/`tiddlers`/`md`")
    return "\n".join(lines)


# -- L1b print: the htmldrill -> pdfdrill bridge -----------------------------

def cmd_print(ctx: Ctx) -> str:
    """Print the page to PDF, then JUDGE the text layer (ESCALATION — network +
    headless; never listed in any `requires:`).

    Makes a web page into a base medium pdfdrill already understands: hand the
    resulting print.pdf to pdfdrill and its whole tower (text-layer gate, OCR,
    docmodel, projectors) applies unchanged.

    --engine firefox uses Selenium + geckodriver WebDriver print_page (the
    standards-based path); --engine chrome uses the headless Chrome `render`
    already depends on (no extra deps). Measured on real pages the two extract
    equivalently, so the engine is a fallback lever, not a quality choice — the
    load-bearing step is the validator, which says whether OCR is actually needed
    instead of assuming it."""
    if not ctx.url:
        raise ValueError("usage: htmldrill print <url> [--engine firefox|chrome]")
    sc = Sidecar(F.local_id_for(ctx.url), work=ctx.work)
    if sc.has(PRINTED) and not ctx.force:
        return _print_report(sc, cached=True)
    t0 = time.perf_counter()
    dest = sc.blob_path("print.pdf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    engine = P.to_pdf(ctx.url, dest, engine=ctx.engine, timeout=ctx.timeout)
    cost_ms = (time.perf_counter() - t0) * 1000

    verdict = P.validate_text_layer(dest)
    sc.set_evidence("url", sc.get_evidence("url") or ctx.url)
    sc.set_evidence("print_engine", engine)
    sc.set_evidence("print_bytes", dest.stat().st_size if dest.exists() else 0)
    sc.set_evidence("print_text_usable", verdict.get("usable"))
    sc.set_evidence("print_text_verdict", verdict.get("verdict"))
    sc.set_evidence("print_text_stats", {k: verdict[k] for k in
                                         ("chars", "letters", "words", "letter_ratio")
                                         if k in verdict})
    sc.set_layer("print_pdf", {"path": "print.pdf", "format": "application/pdf",
                               "engine": engine})
    sc.add_fact(PRINTED)
    sc.log_transition("print", _prev(sc, PRINTED), PRINTED, cost_ms,
                      f"{engine} {sc.get_evidence('print_bytes')}B "
                      f"text_usable={verdict.get('usable')}")
    sc.save()
    return _print_report(sc, cached=False)


def _print_report(sc: Sidecar, cached: bool) -> str:
    ev = sc.evidence
    tag = "cached print" if cached else "printed"
    stats = ev.get("print_text_stats") or {}
    usable = ev.get("print_text_usable")
    mark = {True: "✓", False: "✗", None: "?"}.get(usable, "?")
    lines = [f"{tag} {ev.get('url')} via {ev.get('print_engine')}",
             f"  id:        {sc.local_id}",
             f"  pdf:       {ev.get('print_bytes')} bytes → {sc.blob_path('print.pdf')}",
             f"  text layer:{mark} {ev.get('print_text_verdict')}"]
    if stats:
        lines.append(f"             {stats.get('words')} words, "
                     f"{stats.get('chars')} chars, "
                     f"letter-ratio {stats.get('letter_ratio')}")
    # The point of the bridge: say what to do with the PDF next.
    if usable:
        lines.append(f"  next: hand to pdfdrill — `pdfdrill size {sc.blob_path('print.pdf')}` "
                     f"(its text-layer gate will agree), then md/tiddlers")
    else:
        lines.append(f"  next: pdfdrill will OCR it — `pdfdrill size {sc.blob_path('print.pdf')}` "
                     f"then `pdfdrill ocr …` (don't OCR what doesn't need it)")
    return "\n".join(lines)


# -- state / planning / diagnostics ------------------------------------------

def cmd_artifacts(ctx: Ctx) -> str:
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not sc.blob_dir.exists():
        return f"{sc.local_id}: no blobs yet — run `htmldrill fetch {ctx.url}`."
    lines = [f"{sc.local_id}: blobs in {sc.blob_dir}"]
    for p in sorted(sc.blob_dir.iterdir()):
        if p.is_file():
            lines.append(f"  {p.name:<22} {p.stat().st_size:>9} bytes")
    return "\n".join(lines)


def cmd_status(ctx: Ctx) -> str:
    sc = Sidecar(_resolve_id(ctx), work=ctx.work)
    if not sc.json_path.exists():
        return (f"no sidecar for {ctx.url!r} yet — nothing built. "
                f"Run `htmldrill fetch {ctx.url}`.")
    facts = ", ".join(sorted(sc.facts)) or "(none)"
    ev = sc.evidence
    lines = [
        f"sidecar {sc.json_path}",
        f"  url:         {ev.get('url', '?')}",
        f"  facts:       {facts}",
        f"  status:      {ev.get('status', '?')}   bytes: {ev.get('bytes', '?')}   "
        f"framework: {ev.get('framework', '?')}",
        f"  transitions: {len(sc.transitions)}",
    ]
    for t in sc.transitions[-6:]:
        lines.append(f"    {t['ts']}  {t['node']}: {t['from']} → {t['to']}  "
                     f"({t['cost_ms']} ms) {t['detail']}")
    return "\n".join(lines)


def cmd_steps(ctx: Ctx) -> str:
    # `url` is optional for steps: with no target, plan against a fresh (empty)
    # sidecar so we still describe the generic prerequisite chain.
    lid = _resolve_id(ctx) if ctx.url else "(no-target)"
    sc = Sidecar(lid, work=ctx.work)
    return planner.describe(ctx.target, sc)


def cmd_doctor(ctx: Ctx) -> str:
    checks = []
    checks.append(("python >= 3.10", sys.version_info >= (3, 10), platform.python_version()))
    try:
        import html.parser  # noqa: F401
        checks.append(("stdlib html.parser", True, "ok"))
    except Exception as e:  # pragma: no cover
        checks.append(("stdlib html.parser", False, str(e)))
    try:
        import yaml  # noqa: F401
        checks.append(("pyyaml (planner manifest)", True, "ok"))
    except Exception:
        checks.append(("pyyaml (planner manifest)", False, "pip install pyyaml"))
    root = work_root(ctx.work)
    try:
        root.mkdir(parents=True, exist_ok=True)
        writable = os.access(root, os.W_OK)
    except Exception:
        writable = False
    checks.append((f"work dir writable ({root})", writable, "ok" if writable else "NOT writable"))
    chrome = R.find_chrome()
    checks.append(("headless chrome (M1 render)", bool(chrome),
                   chrome or "none — set $HTMLDRILL_CHROME (L0 still works without it)"))
    gecko = P.find_geckodriver()
    checks.append(("geckodriver (print --engine firefox)", bool(gecko),
                   gecko or "none — optional; `print --engine chrome` needs no driver"))
    try:
        import selenium  # noqa: F401
        _sel = True
    except Exception:
        _sel = False
    checks.append(("selenium (print --engine firefox)", _sel,
                   "ok" if _sel else "pip install selenium — optional"))
    checks.append(("pdftotext (print text-layer validator)", bool(shutil.which("pdftotext")),
                   shutil.which("pdftotext") or "none — install poppler-utils"))
    mono = MONO.find_monolith()
    checks.append(("monolith (single --engine monolith)", bool(mono),
                   MONO.monolith_version(mono) or mono
                   or "none — `single --engine python` works without it (stdlib inliner)"))
    try:
        import html2latex  # noqa: F401
        _h2l = True
    except Exception:
        _h2l = False
    checks.append(("html2latex (latex projector)", _h2l,
                   "ok" if _h2l else
                   "pip install 'git+https://github.com/pankaj28843/html2latex' — optional"))
    lines = ["htmldrill doctor:"]
    for name, ok, detail in checks:
        lines.append(f"  [{'✓' if ok else '✗'}] {name:<32} {detail}")
    # Chrome is optional (L0 needs none); don't let its absence fail the verdict.
    _optional = ("chrome", "geckodriver", "selenium", "pdftotext", "monolith", "html2latex")
    ok_all = all(ok for name, ok, _ in checks
                 if not any(o in name for o in _optional))
    lines.append("  → all systems go." if ok_all else "  → fix the ✗ items above.")
    return "\n".join(lines)


def cmd_config(ctx: Ctx) -> str:
    return (
        "htmldrill effective config:\n"
        f"  work dir:    {work_root(ctx.work)}  (--work / $HTMLDRILL_WORK)\n"
        f"  user-agent:  {ctx.ua or F.DEFAULT_UA}  ($HTMLDRILL_UA)\n"
        f"  timeout:     {ctx.timeout}s  ($HTMLDRILL_TIMEOUT)\n"
        f"  monolith:    {MONO.find_monolith() or '(not found)'}  ($HTMLDRILL_MONOLITH)\n"
        f"  python:      {platform.python_version()} @ {sys.executable}"
    )


# handler registry (name → fn), used by the CLI and the planner's --ensure
HANDLERS = {
    "fetch": cmd_fetch,
    "arxiv": cmd_arxiv,
    "scholar": cmd_scholar,
    "route": cmd_route,
    "size": cmd_size,
    "headers": cmd_headers,
    "meta": cmd_meta,
    "canonical": cmd_canonical,
    "links": cmd_links,
    "jsonld": cmd_jsonld,
    "microdata": cmd_microdata,
    "opengraph": cmd_opengraph,
    "feeds": cmd_feeds,
    "outline": cmd_outline,
    "render": cmd_render,
    "dom": cmd_dom,
    "text": cmd_text,
    "screenshot": cmd_screenshot,
    "compare": cmd_compare,
    "print": cmd_print,
    "capture": cmd_capture,
    "single": cmd_single,
    "model": cmd_model,
    "tiddlers": cmd_tiddlers,
    "md": cmd_md,
    "llmtext": cmd_llmtext,
    "latex": cmd_latex,
    "splits": cmd_splits,
    "materialize": cmd_materialize,
    "crawl": cmd_crawl,
    "retrieve": cmd_retrieve,
    "chatlog": cmd_chatlog,
    "artifacts": cmd_artifacts,
    "status": cmd_status,
    "steps": cmd_steps,
    "doctor": cmd_doctor,
    "config": cmd_config,
}
