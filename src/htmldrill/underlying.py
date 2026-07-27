"""underlying — locate the resource a viewer SHELL is displaying.

This is the execution half of the lattice's `fetch_underlying` capability. The
classifier decides `locus: external_resource`; this module answers "external to
what, exactly?".

The shape is everywhere: a page whose markup contains almost no text mounts a
JS viewer in an iframe, and the real document rides in a query parameter.

    <iframe src="/assets/hypothesis/web/viewer.html?file=/files/chapter-01.pdf">
                 └─ the viewer ─────────────────┘      └─ the CONTENT ──────┘

Scraping the shell yields the toolbar. The parameter yields the chapter. Every
strategy below reports HOW it found a candidate, so a wrong answer is traceable
to a rule rather than to a guess — same discipline as the classifier's rule ids.

Pure and offline: `(html, base_url) -> list[Candidate]`. No network here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urljoin, urlparse

#: query parameters that conventionally carry the real document
_DOC_PARAMS = ("file", "url", "src", "document", "doc", "pdf", "data")

#: viewer bundles we recognise by path — used only to explain the finding
_VIEWER_HINTS = ("pdfjs", "pdf.js", "/viewer", "viewerng", "hypothesis",
                 "docs.google.com", "gview", "flowpaper", "dflip")

_EMBED = re.compile(
    r"<(iframe|embed|object)\b([^>]*)>", re.I)
_ATTR = re.compile(
    r"""\b(src|data)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_ANCHOR = re.compile(
    r"""<a\b([^>]*)>""", re.I)
_HREF = re.compile(
    r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_HAS_DOWNLOAD = re.compile(r"\bdownload\b", re.I)
_DOCLIKE = re.compile(r"\.(pdf|epub|djvu)(?:$|[?#])", re.I)


@dataclass(frozen=True)
class Candidate:
    """One located resource, plus the rule that located it."""
    url: str
    rule: str
    confidence: float
    via: str = ""

    def __str__(self) -> str:
        return f"{self.url}  [{self.rule} conf {self.confidence:.2f}]"


def _attr(attrs: str) -> str | None:
    m = _ATTR.search(attrs)
    if not m:
        return None
    return m.group(2) or m.group(3) or m.group(4)


#: characters legal in a URL — `%` is safe so an already-encoded target is not
#: double-encoded, while a raw space decoded by parse_qs is put back correctly
_URL_SAFE = ":/?#[]@!$&'()*+,;=~%"


def _param_target(src: str) -> tuple[str, str] | None:
    """(target, param_name) if the URL carries a document in a query parameter.

    `parse_qs` already percent-DECODES, so the value must be re-encoded rather
    than unquoted a second time — decoding twice corrupts any target whose own
    filename legitimately contains a `%`.
    """
    q = parse_qs(urlparse(src).query)
    for name in _DOC_PARAMS:
        for key in (name, name.upper(), name.capitalize()):
            if key in q and q[key] and q[key][0].strip():
                return quote(q[key][0].strip(), safe=_URL_SAFE), key
    return None


def _looks_like_viewer(src: str) -> bool:
    low = src.lower()
    return any(h in low for h in _VIEWER_HINTS)


def candidates(html: str, base_url: str = "") -> list[Candidate]:
    """Every resource this page appears to be a shell around, best first.

    Ordered by how directly the markup states it:
      1. a document handed to an embedded viewer via a query parameter
      2. an embed whose own src IS the document
      3. an explicit download link
      4. any anchor pointing at a document
    """
    found: list[Candidate] = []
    seen: set[str] = set()

    def add(raw: str, rule: str, conf: float, via: str = "") -> None:
        if not raw:
            return
        url = urljoin(base_url, raw.strip()) if base_url else raw.strip()
        if url in seen:
            return
        seen.add(url)
        found.append(Candidate(url, rule, conf, via))

    for m in _EMBED.finditer(html):
        tag, attrs = m.group(1).lower(), m.group(2)
        src = _attr(attrs)
        if not src:
            continue
        param = _param_target(src)
        if param:
            target, key = param
            conf = 0.95 if _looks_like_viewer(src) else 0.8
            # the parameter is relative to the VIEWER document, not to the page
            # that embeds it — `viewer.html?file=doc.pdf` means doc.pdf sits
            # beside viewer.html
            viewer_url = urljoin(base_url, src) if base_url else src
            add(urljoin(viewer_url, target) if base_url else target,
                f"embed.param:{tag}?{key}", conf, via=src)
            continue
        if _DOCLIKE.search(src):
            add(src, f"embed.direct:{tag}", 0.9, via=src)

    for m in _ANCHOR.finditer(html):
        attrs = m.group(1)
        h = _HREF.search(attrs)
        if not h:
            continue
        href = h.group(1) or h.group(2) or h.group(3) or ""
        if _HAS_DOWNLOAD.search(attrs):
            add(href, "anchor.download", 0.7)
        elif _DOCLIKE.search(href):
            add(href, "anchor.doclike", 0.5)

    return found


def best(html: str, base_url: str = "") -> Candidate | None:
    """The single most direct candidate, or None when the page is not a shell."""
    found = candidates(html, base_url)
    return max(found, key=lambda c: c.confidence) if found else None
