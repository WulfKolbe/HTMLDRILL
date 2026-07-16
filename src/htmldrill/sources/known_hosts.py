"""Known-host URL handling — recognise specific sources (arXiv first) and reach
for the cheapest, richest route per host instead of blindly capturing the page.

Copied from pdfdrill's ``sources.py`` (the arXiv/URL-specific functions), adapted
to htmldrill's own stdlib ``fetch`` so it carries no pdfdrill dependency. The pure
parsers (``parse_arxiv_id`` / ``arxiv_urls`` / ``parse_arxiv_abs_html`` / …) are
verbatim — they are stdlib-only and unit-tested offline; only the three network
routes were rewired onto :mod:`htmldrill.sources.fetch`.

Why this matters for htmldrill: a user often pastes an ``arxiv.org/abs/<id>`` URL
without realising it is an HTML landing page, not the paper. Rather than capture
the abs page, htmldrill can recognise the host and:
  * read title/authors/abstract/category off the abs page for FREE (no render),
  * hand the **PDF** URL to pdfdrill, and
  * offer the **e-print .tgz** — the author's LaTeX source, the gold form of every
    equation — which the abs-page download button hides behind the stable endpoint
    ``https://arxiv.org/e-print/<id>``.

``KNOWN_HOSTS`` is the extension point: add a host substring → kind to teach
htmldrill a new special source (stackexchange, a blog host, …) later.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from . import fetch as F

# host substring (after stripping a leading www.) → source kind
KNOWN_HOSTS = {
    "arxiv.org": "arxiv",
    "export.arxiv.org": "arxiv",
}


def is_url(s: str) -> bool:
    return isinstance(s, str) and bool(re.match(r"https?://", s.strip(), re.I))


def host_of(s: str) -> str:
    return urlparse(s).netloc.lower()


def known_host(s: str) -> Optional[str]:
    """Return the source kind for a URL whose host is in KNOWN_HOSTS, else None."""
    if not is_url(s):
        return None
    host = host_of(s)
    host = host[4:] if host.startswith("www.") else host
    return KNOWN_HOSTS.get(host)


# ---------------------------------------------------------------------------
# arXiv (pure helpers — verbatim from pdfdrill.sources)
# ---------------------------------------------------------------------------

# new-style id (1501.00001 / 2510.11170v2) or old-style (math/0309136, hep-th/9901001)
_ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
_ARXIV_OLD = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ARXIV_ANY = re.compile(rf"(?:arxiv:)?({_ARXIV_NEW}|{_ARXIV_OLD})", re.I)


def parse_arxiv_id(s: str) -> Optional[str]:
    """Extract an arXiv id from any spelling: an abs/pdf/e-print URL, an
    `arXiv:..` token, or a bare id (new- or old-style). A trailing `.pdf` is
    dropped. Returns None when the string carries no arXiv id."""
    if not s:
        return None
    text = s.strip()
    # only treat a URL as arXiv when it is actually an arxiv host
    if is_url(text) and known_host(text) != "arxiv":
        return None
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    m = _ARXIV_ANY.search(text)
    if not m:
        return None
    return m.group(1)


_BARE_ARXIV = re.compile(rf"(?:arxiv:)?({_ARXIV_NEW}|{_ARXIV_OLD})$", re.I)


def bare_arxiv_id(s: str) -> Optional[str]:
    """The arXiv id IFF the WHOLE argument is a bare id (optionally `arXiv:`-
    prefixed, with a trailing `.pdf` allowed) — NOT a URL and NOT an id merely
    embedded in a path. So `2510.11170v2` resolves, but `data/2312.11532.pdf`
    does not."""
    if not s or is_url(s):
        return None
    text = re.sub(r"\.pdf$", "", s.strip(), flags=re.I)
    m = _BARE_ARXIV.fullmatch(text)
    return m.group(1) if m else None


def arxiv_urls(arxiv_id: str) -> dict[str, str]:
    """abs / pdf / e-print URLs for an arXiv id (version preserved if present)."""
    return {
        "abs": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
        "eprint": f"https://arxiv.org/e-print/{arxiv_id}",
    }


def _strip_tags(html: str) -> str:
    html = re.sub(r'<span class="descriptor">.*?</span>', " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def parse_arxiv_abs_html(html: str) -> dict:
    """Parse title / authors / abstract / primary category from an arXiv abs page.
    Pure string work (no bs4), so it is fully unit-tested offline."""
    out: dict = {"title": "", "authors": [], "abstract": "", "primary_category": "",
                 "subjects": ""}
    t = re.search(r'<h1 class="title[^"]*">(.*?)</h1>', html, re.S | re.I)
    if t:
        out["title"] = _strip_tags(t.group(1))
    a = re.search(r'<blockquote class="abstract[^"]*">(.*?)</blockquote>', html, re.S | re.I)
    if a:
        out["abstract"] = _strip_tags(a.group(1))
    au = re.search(r'<div class="authors">(.*?)</div>', html, re.S | re.I)
    if au:
        names = re.findall(r"<a[^>]*>(.*?)</a>", au.group(1), re.S | re.I)
        out["authors"] = [re.sub(r"\s+", " ", n).strip() for n in names]
    su = re.search(r'<td class="tablecell subjects">(.*?)</td>', html, re.S | re.I)
    if su:
        out["subjects"] = _strip_tags(su.group(1))
        ps = re.search(r'<span class="primary-subject">(.*?)</span>', su.group(1), re.S | re.I)
        primary = _strip_tags(ps.group(1)) if ps else out["subjects"]
        code = re.search(r"\(([a-zA-Z\-]+\.[A-Za-z]{2})\)", primary)
        out["primary_category"] = code.group(1) if code else ""
    return out


# ---------------------------------------------------------------------------
# Network routes — rewired onto htmldrill's fetch (stdlib urllib underneath)
# ---------------------------------------------------------------------------

def fetch_arxiv_metadata(arxiv_id: str, timeout: float = F.DEFAULT_TIMEOUT) -> dict:
    """Free metadata (title/authors/abstract/category) from the arXiv abs page."""
    res = F.fetch(arxiv_urls(arxiv_id)["abs"], timeout=timeout)
    meta = parse_arxiv_abs_html(res.text)
    meta["arxiv_id"] = arxiv_id
    return meta


def download(url: str, dest: Path, timeout: float = F.DEFAULT_TIMEOUT) -> Path:
    """Stream a URL to `dest` (bounded by fetch's MAX_BYTES). Returns dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = F.fetch(url, timeout=timeout)
    dest.write_bytes(res.body)
    return dest


def download_arxiv_source(arxiv_id: str, dest_dir: Path,
                          timeout: float = F.DEFAULT_TIMEOUT) -> Path:
    """Download the arXiv e-print source tarball to `<dest_dir>/<id>.tgz`
    (the endpoint the abs-page download button hides). Idempotent."""
    safe = arxiv_id.replace("/", "_")
    dest = Path(dest_dir) / f"{safe}.tgz"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    return download(arxiv_urls(arxiv_id)["eprint"], dest, timeout=timeout)


def download_arxiv_pdf(arxiv_id: str, dest_dir: Path,
                       timeout: float = F.DEFAULT_TIMEOUT) -> Path:
    """Download the arXiv PDF to `<dest_dir>/<id>.pdf` — the form to hand to
    pdfdrill. Idempotent."""
    safe = arxiv_id.replace("/", "_")
    dest = Path(dest_dir) / f"{safe}.pdf"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    return download(arxiv_urls(arxiv_id)["pdf"], dest, timeout=timeout)


def _safe_filename(url: str) -> str:
    """A filesystem-safe basename for a download (percent-decoded first)."""
    name = Path(urlparse(unquote(url)).path).name or "download"
    return re.sub(r"[^\w.\-]", "_", name)
