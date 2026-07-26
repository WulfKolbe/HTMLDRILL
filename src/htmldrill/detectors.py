"""detectors — pure feature observation for the pagekind lattice.

CONTRACT (stage 1 of four; see docs/CR-pagekind-lattice.md)

    collect(html, headers, url, status) -> dict[feature_id, object]

Detectors OBSERVE. They never decide, never name a page genre, and never
mention `render`. Every feature id referenced by any ``when:`` clause in
``pagekind.yaml`` has exactly one detector registered here, and nothing else.

The one invariant that matters:

    A detector that cannot determine its feature returns ``NOT_OBSERVED``,
    which is a DISTINCT sentinel from ``False`` / ``0`` / ``""`` / ``None``.

That distinction is the whole point. The defect this module replaces
(``commands.py`` ``needs_render``) collapsed "no framework was recognised"
into "no framework is present", and routed the collapsed value to a
confident wrong answer. Here, ``bool(NOT_OBSERVED)`` raises rather than
silently reading as False — the conflation is a crash, not a wrong verdict.

Regex literals are permitted in THIS module (they are observation detail,
registered by feature id) and are forbidden in ``pagekind.py``, where every
pattern must come from the YAML.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlparse

from .parse import html as H


class _NotObserved:
    """The not-yet-measured sentinel. Deliberately NOT falsy.

    ``bool(NOT_OBSERVED)`` raises: an unmeasured feature must never be able to
    masquerade as a measured negative in an ``if`` or an ``and``. Every consumer
    must test ``is NOT_OBSERVED`` explicitly.
    """

    _instance: "Optional[_NotObserved]" = None

    def __new__(cls) -> "_NotObserved":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        raise TypeError(
            "NOT_OBSERVED has no truth value — a not-yet-measured feature must "
            "not masquerade as a measured False. Test `is NOT_OBSERVED`.")

    def __repr__(self) -> str:
        return "NOT_OBSERVED"


NOT_OBSERVED = _NotObserved()


class Observation:
    """The raw material every detector reads. Nothing derived, nothing decided."""

    __slots__ = ("html", "headers", "url", "status", "rendered_html", "_collected")

    def __init__(self, html: str | None, headers: dict | None, url: str | None,
                 status: int | None, rendered_html: str | None = None) -> None:
        self.html = html or ""
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.url = url or ""
        self.status = status
        self.rendered_html = rendered_html
        self._collected: Any = None

    @property
    def collected(self) -> H.Collected:
        """One lenient parse of the static markup, shared by every detector."""
        if self._collected is None:
            self._collected = H.collect(self.html)
        return self._collected

    def header(self, name: str) -> str | None:
        v = self.headers.get(name.lower())
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v)
        return str(v)

    @property
    def has_html(self) -> bool:
        return bool(self.html.strip())


REGISTRY: dict[str, Callable[[Observation], Any]] = {}


def detector(feature_id: str):
    """Register a pure observer under a feature id used by ``pagekind.yaml``."""

    def wrap(fn: Callable[[Observation], Any]) -> Callable[[Observation], Any]:
        if feature_id in REGISTRY:
            raise RuntimeError(f"duplicate detector for feature {feature_id!r}")
        REGISTRY[feature_id] = fn
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Transport-level observations (headers / status / url)
# ---------------------------------------------------------------------------

@detector("status")
def _status(o: Observation):
    return o.status if isinstance(o.status, int) and not isinstance(o.status, bool) \
        else NOT_OBSERVED


@detector("content_type")
def _content_type(o: Observation):
    raw = o.header("content-type")
    if not raw:
        return NOT_OBSERVED
    return raw.split(";")[0].strip().lower()


_CT_CLASS = [
    ("html", re.compile(r"^(text/html|application/xhtml\+xml)$")),
    ("json", re.compile(r"^(application/(ld\+)?json|text/json|application/[\w.\-]+\+json)$")),
    ("feed", re.compile(r"^application/(rss|atom|rdf)\+xml$")),
    ("image", re.compile(r"^image/")),
    ("archive", re.compile(
        r"^application/(zip|gzip|x-tar|x-gzip|x-7z-compressed|x-rar-compressed|x-bzip2)$")),
]


@detector("content_type_class")
def _content_type_class(o: Observation):
    mime = _content_type(o)
    if mime is NOT_OBSERVED:
        return NOT_OBSERVED
    for name, pat in _CT_CLASS:
        if pat.match(mime):
            return name
    return "other"


@detector("payload_not_html")
def _payload_not_html(o: Observation):
    cls = _content_type_class(o)
    if cls is NOT_OBSERVED:
        return NOT_OBSERVED          # no content-type: not measured, do not guess
    return cls != "html"


@detector("payload_is_data")
def _payload_is_data(o: Observation):
    cls = _content_type_class(o)
    if cls is NOT_OBSERVED:
        return NOT_OBSERVED
    return cls in ("json", "feed")


_SESSION_COOKIE = re.compile(
    r"\b(sessionid|sessid|phpsessid|jsessionid|asp\.net_sessionid|_session|sid)\s*=", re.I)


@detector("set_cookie_session")
def _set_cookie_session(o: Observation):
    raw = o.header("set-cookie")
    if raw is None:
        return NOT_OBSERVED
    if _SESSION_COOKIE.search(raw):
        return True
    # a cookie with neither Expires nor Max-Age IS a session cookie by definition
    return not re.search(r"\b(expires|max-age)\s*=", raw, re.I)


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "referrer", "source",
    "_ga", "igshid", "yclid",
}


@detector("query_significant")
def _query_significant(o: Observation):
    if not o.url:
        return NOT_OBSERVED
    parsed = urlparse(o.url)
    if not parsed.scheme and not parsed.netloc and not parsed.query:
        return NOT_OBSERVED           # a bare local path carries no URL identity
    params = [k.lower() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    return any(p not in _TRACKING_PARAMS for p in params)


_ROBOTS_BLOCK = re.compile(r"\b(noindex|none)\b", re.I)


@detector("robots_disallow")
def _robots_disallow(o: Observation):
    hdr = o.header("x-robots-tag")
    if hdr and _ROBOTS_BLOCK.search(hdr):
        return True
    if o.has_html:
        meta = H.extract_meta(o.collected)
        directive = meta.get("robots") or ""
        if directive:
            return bool(_ROBOTS_BLOCK.search(directive))
        return False
    return NOT_OBSERVED if hdr is None else False


# ---------------------------------------------------------------------------
# Markup-shape observations
# ---------------------------------------------------------------------------

_HTML_START = re.compile(r"^\s*(<!doctype\s+html|<html\b)", re.I)


@detector("body_starts_html")
def _body_starts_html(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_HTML_START.match(o.html[:2048]))


# Framework markers must be SPECIFIC (attributes / globals / generator meta), not
# bare words like "vue"/"angular" — those substring-match chat and JSON payloads
# and produce false positives (a 62MB ChatGPT export "is Vue + Angular"; a
# TiddlyWiki "is Vue"). Moved here verbatim from commands.py::_guess_framework,
# which is now one FEATURE DETECTOR among many rather than the gate on `render`.
_FRAMEWORK_PATTERNS = [
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


def guess_framework(html: str) -> str:
    """The human-readable framework list, or ``"none detected"``.

    Kept for the ``size`` report's prose. It is NOT a verdict: "none detected"
    means "no marker in THIS pattern list matched", which is precisely why it
    may not be used on its own to decide whether a page needs rendering.
    """
    hits = [name for name, p in _FRAMEWORK_PATTERNS if re.search(p, html, re.I)]
    return ", ".join(hits) if hits else "none detected"


@detector("fw_marker")
def _fw_marker(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return guess_framework(o.html) != "none detected"


@detector("static_text_chars")
def _static_text_chars(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return len(H.extract_text(o.html).strip())


_SCRIPT_BODY = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.I | re.S)


@detector("script_ratio")
def _script_ratio(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    total = len(o.html)
    if total == 0:
        return NOT_OBSERVED
    scripted = sum(len(m.group(1)) for m in _SCRIPT_BODY.finditer(o.html))
    return scripted / total


_CLASS_ATTR = re.compile(r'\bclass\s*=\s*"([^"]*)"|\bclass\s*=\s*\'([^\']*)\'', re.I)
_TR_TAG = re.compile(r"<tr\b", re.I)
_ARTICLE_TAG = re.compile(r"<article\b", re.I)


@detector("repeated_blocks")
def _repeated_blocks(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    counts: dict[str, int] = {}
    for m in _CLASS_ATTR.finditer(o.html):
        for token in (m.group(1) or m.group(2) or "").split():
            counts[token] = counts.get(token, 0) + 1
    if counts and max(counts.values()) >= 5:
        return True
    if len(_TR_TAG.findall(o.html)) >= 5:
        return True
    return len(_ARTICLE_TAG.findall(o.html)) >= 3


_VIEWER = re.compile(
    r"<embed\b[^>]*application/pdf|<object\b[^>]*(application/pdf|\.pdf)|"
    r"<iframe\b[^>]*(\.pdf|/viewer|viewerng|docs\.google\.com/(viewer|gview))|"
    r"pdfjs[-.]?(dist|viewer)|id=[\"']viewerContainer[\"']", re.I)


@detector("embedded_viewer")
def _embedded_viewer(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_VIEWER.search(o.html))


@detector("jsonld_blocks")
def _jsonld_blocks(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return len(o.collected.jsonld_blocks)


@detector("canonical_present")
def _canonical_present(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(H.extract_canonical(o.collected).get("canonical"))


_CONSENT = re.compile(
    r"onetrust|ot-sdk|cookiebot|trustarc|truste\b|didomi|usercentrics|quantcast|"
    r"cmpbox|sp_message_container|cookie[-_ ]?(consent|banner|notice|wall)|"
    r"gdpr[-_ ]?(consent|banner)|consent[-_ ]?manager", re.I)


@detector("consent_marker")
def _consent_marker(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_CONSENT.search(o.html))


_LOGIN = re.compile(
    r"<input\b[^>]*type\s*=\s*[\"']?password|<input\b[^>]*name\s*=\s*[\"']?password|"
    r"<form\b[^>]*action\s*=\s*[\"'][^\"']*/(login|signin|sign-in|session|auth)\b", re.I)


@detector("login_form")
def _login_form(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_LOGIN.search(o.html))


_SOFT_ERROR = re.compile(
    r"\b(404|410|not found|page not found|no longer exists|doesn'?t exist|"
    r"does not exist|gone missing)\b", re.I)


@detector("soft_error_marker")
def _soft_error_marker(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    c = o.collected
    # Only the page's own IDENTITY strings count — a "404" inside body prose is
    # a mention, not a verdict.
    candidates = [c.title or ""] + [t for lvl, t in c.headings if lvl == 1]
    return any(_SOFT_ERROR.search(t) for t in candidates)


# ---------------------------------------------------------------------------
# Completeness observations
# ---------------------------------------------------------------------------

_SCROLL_SENTINEL = re.compile(
    r"infinite[-_]?scroll|scroll[-_]?sentinel|data-sentinel|"
    r"(id|class)\s*=\s*[\"'][^\"']*\bsentinel\b|loadMoreOnScroll|"
    r"data-infinite", re.I)


@detector("scroll_sentinel")
def _scroll_sentinel(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_SCROLL_SENTINEL.search(o.html))


_ROLE_FEED = re.compile(r"\brole\s*=\s*[\"']?feed\b", re.I)


@detector("role_feed")
def _role_feed(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_ROLE_FEED.search(o.html))


_REL_NEXT_ANCHOR = re.compile(r"<a\b[^>]*\brel\s*=\s*[\"']?[^\"'>]*\bnext\b", re.I)


@detector("rel_next")
def _rel_next(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    for link in o.collected.links_rel:
        if "next" in (link.get("rel") or "").lower().split():
            return True
    return bool(_REL_NEXT_ANCHOR.search(o.html))


_PAGER = re.compile(
    r"(class|id)\s*=\s*[\"'][^\"']*\b(pagination|pager|page-numbers)\b|"
    r"aria-label\s*=\s*[\"'][^\"']*\bpagination\b|"
    r"\brel\s*=\s*[\"']?prev\b|[?&]page=\d+", re.I)


@detector("pager_controls")
def _pager_controls(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_PAGER.search(o.html))


_LAZY = re.compile(
    r"<(img|iframe)\b[^>]*loading\s*=\s*[\"']?lazy|"
    r"<img\b[^>]*\bdata-(src|srcset|original)\s*=", re.I)


@detector("lazy_media")
def _lazy_media(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return bool(_LAZY.search(o.html))


@detector("collapsed_bodies")
def _collapsed_bodies(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return sum(1 for s in H.detect_splits(o.html) if s.kind == "collapsed")


# ---------------------------------------------------------------------------
# Richness observations
# ---------------------------------------------------------------------------

@detector("link_count")
def _link_count(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return len(o.collected.anchors)


@detector("link_text_ratio")
def _link_text_ratio(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    anchor_chars = sum(len(t.strip()) for _, t in o.collected.anchors)
    total = len(H.extract_text(o.html).strip())
    if total == 0:
        return 1.0 if anchor_chars else 0.0
    return min(1.0, anchor_chars / total)


_MEDIA_TAG = re.compile(r"<(img|video|audio|picture|figure)\b", re.I)
_TEXT_BLOCK_TAG = re.compile(r"<(p|li|h[1-6]|blockquote|pre)\b", re.I)


@detector("media_ratio")
def _media_ratio(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    media = len(_MEDIA_TAG.findall(o.html))
    text_blocks = len(_TEXT_BLOCK_TAG.findall(o.html))
    if media + text_blocks == 0:
        return 0.0
    return media / (media + text_blocks)


# ---------------------------------------------------------------------------
# Cross-snapshot observation (only measurable when a render exists)
# ---------------------------------------------------------------------------

@detector("render_delta_chars")
def _render_delta_chars(o: Observation):
    """How much text the rendered DOM has that the static markup does not.

    NOT_OBSERVED unless a rendered snapshot was supplied. Absent a render this
    is genuinely unmeasured — reporting 0 would be the original defect.
    """
    if o.rendered_html is None or not o.has_html:
        return NOT_OBSERVED
    static_chars = len(H.extract_text(o.html).strip())
    rendered_chars = len(H.extract_text(o.rendered_html).strip())
    return max(0, rendered_chars - static_chars)


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------

def collect(html: str | None, headers: dict | None = None, url: str | None = None,
            status: int | None = None, *, rendered_html: str | None = None
            ) -> dict[str, Any]:
    """Run every registered detector once. Total: always returns one entry per
    registered feature id, never raises.

    A detector that blows up on hostile markup yields ``NOT_OBSERVED`` — an
    explicit demotion to not-measured, not a swallowed error that would let a
    missing observation read as a negative finding.
    """
    obs = Observation(html, headers, url, status, rendered_html)
    out: dict[str, Any] = {}
    for feature_id, fn in REGISTRY.items():
        try:
            out[feature_id] = fn(obs)
        except Exception:
            out[feature_id] = NOT_OBSERVED
    return out
