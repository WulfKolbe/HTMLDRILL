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

    __slots__ = ("html", "headers", "url", "status", "rendered_html",
                 "robots_txt_disallow", "body_kind",
                 "_collected", "_text", "_splits", "_lower")

    def __init__(self, html: str | None, headers: dict | None, url: str | None,
                 status: int | None, rendered_html: str | None = None,
                 robots_txt_disallow: bool | None = None,
                 body_kind: str | None = None) -> None:
        self.html = html or ""
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.url = url or ""
        self.status = status
        self.rendered_html = rendered_html
        self.robots_txt_disallow = robots_txt_disallow
        self.body_kind = body_kind
        self._collected: Any = None
        self._text: Any = None
        self._splits: Any = None
        self._lower: Any = None

    # Each of these is a full pass over the markup. On a large input (the
    # codebase's own worked example is a 62MB export) running them per-detector
    # instead of per-observation is the difference between one pass and five, so
    # they are memoized here rather than called freely inside detectors.

    @property
    def collected(self) -> H.Collected:
        """One lenient parse of the static markup, shared by every detector."""
        if self._collected is None:
            self._collected = H.collect(self.html)
        return self._collected

    @property
    def text(self) -> str:
        """The extracted visible static text, parsed once."""
        if self._text is None:
            self._text = H.extract_text(self.html).strip()
        return self._text

    @property
    def lower(self) -> str:
        """The markup lowercased once, for cheap substring pre-filtering."""
        if self._lower is None:
            self._lower = self.html.lower()
        return self._lower

    @property
    def splits(self) -> list:
        """The split/hidden-content occurrences, detected once."""
        if self._splits is None:
            self._splits = H.detect_splits(self.html)
        return self._splits

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


def _hit(o: Observation, tokens: tuple[str, ...], pattern: "re.Pattern") -> bool:
    """`pattern.search(html)` behind a necessary-condition substring guard.

    Every token is a lowercased substring that the pattern CANNOT match without.
    A plain `in` scan is C-speed and short-circuits; the regex only runs when a
    token is actually present. On a 12MB page this is the difference between
    ~19 full-document regex scans and a handful.

    The guard is a correctness risk in exactly one direction — a token that is
    not truly necessary would turn a real match into a silent False, which is
    the failure class this module exists to prevent. So it is not taken on
    faith: `test_prefilter_never_changes_a_verdict` re-runs every pattern
    unguarded over the whole corpus and asserts the two agree.
    """
    lower = o.lower
    if not any(t in lower for t in tokens):
        return False
    return bool(pattern.search(o.html))


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


@detector("body_kind")
def _body_kind(o: Observation):
    """htmldrill's OWN verdict on the bytes — `fetch.content_kind()`, whose
    comment reads "magic bytes are authoritative".

    Observed live: a 32-page PDF served as `binary/octet-stream`. The declared
    Content-Type said nothing usable, so `payload` came out `unknown` and
    `delivery` came out `non_html`, which routed to po.nonhtml -> fetch_underlying
    — the very step that had just produced the file. A fixed point that never
    reached po.pdf ("hand to pdfdrill").

    The correct answer was already in the sidecar as `content_kind`; the lattice
    simply was not reading it. This is that wire, not a new inference.
    """
    if o.body_kind is None:
        return NOT_OBSERVED
    return str(o.body_kind).lower()


@detector("payload_not_html")
def _payload_not_html(o: Observation):
    sniffed = _body_kind(o)
    if sniffed is not NOT_OBSERVED:
        return sniffed != "html"     # magic bytes outrank a declared content-type
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


@detector("robots_txt_disallow")
def _robots_txt_disallow(o: Observation):
    """Did a FETCH-LEVEL refusal actually happen for this URL?

    Deliberately NOT the `noindex` meta tag. `noindex` is an INDEXING directive;
    it says nothing about whether the resource may be retrieved. Routing it to
    access=blocked made htmldrill silently refuse pages it could read perfectly
    well — a worse failure than the verdict bug this lattice replaces, because
    a refusal looks like correctness.

    It is also CRAWL-SCOPED. robots.txt states the owner's intent about automated
    crawlers — agents that traverse a link graph on their own initiative. It is
    not access control and does not govern fetching one URL because a person
    asked for it, which is why a browser and a screen reader do not consult it
    either. So this stays NOT_OBSERVED on the single-retrieval path, and
    `ac.robots` is unreachable there by design; `crawl`, which really does
    traverse, honours the policy directly (commands._robots_ok).

    `blocked` is therefore reachable only from evidence, in the one activity the
    evidence is about. See htmldrill/robots.py for the full reasoning.
    """
    if o.robots_txt_disallow is None:
        return NOT_OBSERVED
    return bool(o.robots_txt_disallow)


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


#: necessary lowercased substrings per framework pattern, same order
_FRAMEWORK_TOKENS = [
    ("__next_data__", "/_next/static/"),
    ("__nuxt__",),
    ("data-reactroot", "react-dom", "__react_devtools"),
    ("data-v-", "__vue__", "vue.runtime"),
    ("ng-version", "_nghost-", "ng-app"),
    ("svelte-",),
    ("tiddlywiki", "storearea"),
]


def guess_framework(html: str) -> str:
    """The human-readable framework list, or ``"none detected"``.

    Kept for the ``size`` report's prose. It is NOT a verdict: "none detected"
    means "no marker in THIS pattern list matched", which is precisely why it
    may not be used on its own to decide whether a page needs rendering.
    """
    lower = html.lower()
    hits = [name for (name, p), toks in zip(_FRAMEWORK_PATTERNS, _FRAMEWORK_TOKENS)
            if any(t in lower for t in toks) and re.search(p, html, re.I)]
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
    return len(o.text)


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

_VIEWER_TOKENS = ("pdf", "/viewer", "viewerng", "gview", "viewercontainer")


@detector("embedded_viewer")
def _embedded_viewer(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _VIEWER_TOKENS, _VIEWER)


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

_CONSENT_TOKENS = ("onetrust", "ot-sdk", "cookiebot", "trustarc", "truste", "didomi",
            "usercentrics", "quantcast", "cmpbox", "sp_message_container", "cookie",
            "gdpr", "consent")


@detector("consent_marker")
def _consent_marker(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _CONSENT_TOKENS, _CONSENT)


_LOGIN = re.compile(
    r"<input\b[^>]*type\s*=\s*[\"']?password|<input\b[^>]*name\s*=\s*[\"']?password|"
    r"<form\b[^>]*action\s*=\s*[\"'][^\"']*/(login|signin|sign-in|session|auth)\b", re.I)

_LOGIN_TOKENS = ("password", "login", "signin", "sign-in", "/session", "/auth")


@detector("login_form")
def _login_form(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _LOGIN_TOKENS, _LOGIN)


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

_SCROLL_SENTINEL_TOKENS = ("sentinel", "infinite", "loadmoreonscroll")


@detector("scroll_sentinel")
def _scroll_sentinel(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _SCROLL_SENTINEL_TOKENS, _SCROLL_SENTINEL)


_ROLE_FEED = re.compile(r"\brole\s*=\s*[\"']?feed\b", re.I)

_ROLE_FEED_TOKENS = ("feed",)


@detector("role_feed")
def _role_feed(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _ROLE_FEED_TOKENS, _ROLE_FEED)


_REL_NEXT_ANCHOR = re.compile(r"<a\b[^>]*\brel\s*=\s*[\"']?[^\"'>]*\bnext\b", re.I)


@detector("rel_next")
def _rel_next(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    for link in o.collected.links_rel:
        if "next" in (link.get("rel") or "").lower().split():
            return True
    return _hit(o, ("next",), _REL_NEXT_ANCHOR)


_PAGER = re.compile(
    r"(class|id)\s*=\s*[\"'][^\"']*\b(pagination|pager|page-numbers)\b|"
    r"aria-label\s*=\s*[\"'][^\"']*\bpagination\b|"
    r"\brel\s*=\s*[\"']?prev\b|[?&]page=\d+", re.I)

_PAGER_TOKENS = ("pagination", "pager", "page-numbers", "prev", "page=")


@detector("pager_controls")
def _pager_controls(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _PAGER_TOKENS, _PAGER)


_LAZY = re.compile(
    r"<(img|iframe)\b[^>]*loading\s*=\s*[\"']?lazy|"
    r"<img\b[^>]*\bdata-(src|srcset|original)\s*=", re.I)

_LAZY_TOKENS = ("lazy", "data-src", "data-original")


@detector("lazy_media")
def _lazy_media(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return _hit(o, _LAZY_TOKENS, _LAZY)


@detector("collapsed_bodies")
def _collapsed_bodies(o: Observation):
    if not o.has_html:
        return NOT_OBSERVED
    return sum(1 for s in o.splits if s.kind == "collapsed")


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
    total = len(o.text)
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
    static_chars = len(o.text)
    rendered_chars = len(H.extract_text(o.rendered_html).strip())
    return max(0, rendered_chars - static_chars)


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------

def collect(html: str | None, headers: dict | None = None, url: str | None = None,
            status: int | None = None, *, rendered_html: str | None = None,
            robots_txt_disallow: bool | None = None,
            body_kind: str | None = None) -> dict[str, Any]:
    """Run every registered detector once. Total: always returns one entry per
    registered feature id, never raises.

    A detector that blows up on hostile markup yields ``NOT_OBSERVED`` — an
    explicit demotion to not-measured, not a swallowed error that would let a
    missing observation read as a negative finding.
    """
    obs = Observation(html, headers, url, status, rendered_html,
                      robots_txt_disallow, body_kind)
    out: dict[str, Any] = {}
    for feature_id, fn in REGISTRY.items():
        try:
            out[feature_id] = fn(obs)
        except Exception:
            out[feature_id] = NOT_OBSERVED
    return out
