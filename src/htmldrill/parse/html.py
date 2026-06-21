"""L0 HTML extractors — stdlib only (``html.parser``), zero dependencies.

This is the static-markup tier of the tower: everything here reads the *raw*
fetched bytes, never a rendered DOM. ``html.parser`` is lenient (it won't choke
on real-world tag soup) but it is NOT a real DOM — there is no tree, just a SAX
event stream. A single ``Collector`` pass records the handful of structures the
L0 commands need; the ``extract_*`` helpers then read off that collector.

Design notes:
  * One parse, many extractors — ``collect(html)`` runs the parser once.
  * Defensive throughout: malformed JSON-LD, missing attrs, and unterminated
    tags degrade to empty results, never exceptions (R7).
  * URL classification (internal/external) is resolved against the page's own
    final URL so protocol-relative and relative hrefs land on the right side.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass
class Collected:
    title: Optional[str] = None
    # <meta> tags: list of the raw attribute dicts (name/property/http-equiv/charset/content)
    metas: list[dict] = field(default_factory=list)
    # <link> tags: raw attribute dicts (rel/href/type/hreflang)
    links_rel: list[dict] = field(default_factory=list)
    # <a href> anchors: (href, visible_text)
    anchors: list[tuple[str, str]] = field(default_factory=list)
    # raw text of each <script type="application/ld+json"> block
    jsonld_blocks: list[str] = field(default_factory=list)
    # microdata: list of (itemtype, [itemprop names]) — shallow, per itemscope element
    microdata: list[dict] = field(default_factory=list)
    # headings: (level:int, text)
    headings: list[tuple[int, str]] = field(default_factory=list)
    tag_count: int = 0


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.c = Collected()
        self._capture: Optional[str] = None   # tag whose text we're accumulating
        self._buf: list[str] = []
        self._scripttype: Optional[str] = None

    # -- text capture helpers --
    def _start_capture(self, tag: str) -> None:
        self._capture = tag
        self._buf = []

    def _end_capture(self) -> str:
        text = "".join(self._buf).strip()
        self._capture = None
        self._buf = []
        return text

    def handle_starttag(self, tag, attrs):
        self.c.tag_count += 1
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self.c.metas.append(a)
        elif tag == "link":
            self.c.links_rel.append(a)
        elif tag == "a" and "href" in a:
            self._pending_href = a["href"]
            self._start_capture("a")
        elif tag == "title":
            self._start_capture("title")
        elif tag in _HEADINGS:
            self._pending_level = int(tag[1])
            self._start_capture(tag)
        elif tag == "script":
            self._scripttype = a.get("type", "").lower()
            if self._scripttype == "application/ld+json":
                self._start_capture("script")
        elif "itemscope" in a:
            props = []  # filled by descendant itemprops we can't tree-track; keep itemtype
            self.c.microdata.append({"itemtype": a.get("itemtype", ""), "props": props})
        if "itemprop" in a and self.c.microdata:
            self.c.microdata[-1]["props"].append(a["itemprop"])

    def handle_startendtag(self, tag, attrs):
        # self-closing tags (<meta .../>, <link .../>) — route through handle_starttag
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._capture != tag:
            return
        text = self._end_capture()
        if tag == "a":
            self.c.anchors.append((getattr(self, "_pending_href", ""), text))
        elif tag == "title":
            self.c.title = text
        elif tag in _HEADINGS:
            self.c.headings.append((getattr(self, "_pending_level", 1), text))
        elif tag == "script":
            if self._scripttype == "application/ld+json" and text:
                self.c.jsonld_blocks.append(text)
            self._scripttype = None


def collect(html: str) -> Collected:
    """Single lenient parse pass over the raw HTML."""
    p = _Collector()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # html.parser is lenient, but never let a parse blow up an L0 command.
        pass
    return p.c


# --------------------------------------------------------------------------- #
# Extractors — each takes Collected (+ base_url where URLs are involved).
# --------------------------------------------------------------------------- #

def _meta_key(a: dict) -> Optional[str]:
    return a.get("name") or a.get("property") or a.get("http-equiv")


def extract_meta(c: Collected) -> dict:
    """name/property/http-equiv → content, plus charset if declared."""
    out: dict[str, str] = {}
    for a in c.metas:
        if "charset" in a:
            out["charset"] = a["charset"]
        k = _meta_key(a)
        if k and "content" in a:
            out[k.lower()] = a["content"]
    return out


def extract_opengraph(c: Collected) -> dict:
    """All og:* and twitter:* meta properties."""
    meta = extract_meta(c)
    return {k: v for k, v in meta.items() if k.startswith("og:") or k.startswith("twitter:")}


def extract_canonical(c: Collected) -> dict:
    out: dict[str, str] = {}
    for a in c.links_rel:
        if "canonical" in a.get("rel", "").lower() and a.get("href"):
            out["canonical"] = a["href"]
    og_url = extract_meta(c).get("og:url")
    if og_url:
        out["og:url"] = og_url
    return out


def extract_feeds(c: Collected) -> list[dict]:
    """rel=alternate links advertising an RSS/Atom feed."""
    feeds = []
    for a in c.links_rel:
        rel = a.get("rel", "").lower()
        typ = a.get("type", "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ or "xml" in typ):
            feeds.append({"href": a.get("href", ""), "type": typ,
                          "title": a.get("title", "")})
    return feeds


def extract_links(c: Collected, base_url: str = "") -> dict:
    """Anchor hrefs resolved against base_url, split internal vs external."""
    base_host = urlparse(base_url).netloc if base_url else ""
    internal, external, other = [], [], []
    seen = set()
    for href, text in c.anchors:
        if not href or href.startswith("#"):
            continue
        resolved = urljoin(base_url, href) if base_url else href
        if resolved in seen:
            continue
        seen.add(resolved)
        scheme = urlparse(resolved).scheme
        if scheme in ("mailto", "tel", "javascript"):
            other.append((resolved, text))
        elif base_host and urlparse(resolved).netloc == base_host:
            internal.append((resolved, text))
        elif scheme in ("http", "https"):
            external.append((resolved, text))
        else:
            other.append((resolved, text))
    return {"internal": internal, "external": external, "other": other}


def extract_jsonld(c: Collected) -> list[dict]:
    """Parse each ld+json block; record parse errors instead of raising."""
    out = []
    for raw in c.jsonld_blocks:
        try:
            data = json.loads(raw)
            out.append({"ok": True, "data": data})
        except Exception as e:  # noqa: BLE001 — surface the parse error as data
            out.append({"ok": False, "error": str(e), "raw_len": len(raw)})
    return out


def extract_microdata(c: Collected) -> list[dict]:
    return [m for m in c.microdata if m.get("itemtype")]


def extract_outline(c: Collected) -> list[tuple[int, str]]:
    return [(lvl, txt) for lvl, txt in c.headings if txt]


class _TextExtractor(HTMLParser):
    """Accumulate visible text, skipping <script>/<style>/<head>-noise."""
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            s = data.strip()
            if s:
                self._out.append(s)


def extract_text(html: str) -> str:
    """Visible text content (scripts/styles stripped), whitespace-collapsed."""
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return " ".join(p._out)


# --------------------------------------------------------------------------- #
# Structural walk (M2) — block-level content in document order.
#
# The L0 extractors above each pick ONE feature off a single pass. ``model``
# instead needs the page's *spine*: an ordered list of typed content blocks
# (heading / paragraph / list item / code / table / figure / link) that becomes
# one docmodel DocObject apiece. This second parser walks the SAX stream once and
# emits that ordered list. Still stdlib ``html.parser`` only — no real tree, so
# nesting is approximated by a small tag stack and text is accumulated inside the
# nearest "interesting" block.
# --------------------------------------------------------------------------- #

@dataclass
class Block:
    """One block-level unit of page content, in document order.

    ``type`` ∈ {Heading, Paragraph, ListItem, CodeBlock, Table, Figure, Link}.
    ``text`` is the collapsed visible text. ``props`` carries type-specifics
    (heading ``level``; figure ``src``/``alt``; link ``href``; table ``rows``).
    """
    type: str
    text: str = ""
    props: dict = field(default_factory=dict)


# tags whose text we accumulate into a block; maps tag -> block type
_BLOCK_TEXT_TAGS = {
    "h1": "Heading", "h2": "Heading", "h3": "Heading", "h4": "Heading",
    "h5": "Heading", "h6": "Heading",
    "p": "Paragraph",
    "li": "ListItem",
    "pre": "CodeBlock", "code": "CodeBlock",
    "blockquote": "Paragraph",
    "td": "_cell", "th": "_cell",
}
_SKIP_STRUCTURAL = {"script", "style", "noscript", "template", "svg", "head"}


class _StructuralWalker(HTMLParser):
    """Emit an ordered list of :class:`Block` from a single SAX pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._skip_depth = 0
        # text-block capture: the currently-open block type + buffer + meta
        self._cap_type: Optional[str] = None
        self._cap_tag: Optional[str] = None
        self._cap_buf: list[str] = []
        self._cap_props: dict = {}
        # table accumulation
        self._table_rows: list[list[str]] = []
        self._cur_row: Optional[list[str]] = None
        self._in_table = 0

    # -- text-block lifecycle --
    def _open(self, tag: str, btype: str, props: Optional[dict] = None) -> None:
        # flush any block already open (handles non-nesting tag soup gracefully)
        self._flush()
        self._cap_tag = tag
        self._cap_type = btype
        self._cap_buf = []
        self._cap_props = dict(props or {})

    def _flush(self) -> None:
        if self._cap_type is None:
            return
        text = " ".join("".join(self._cap_buf).split())
        btype, props = self._cap_type, self._cap_props
        self._cap_type = self._cap_tag = None
        self._cap_buf = []
        self._cap_props = {}
        if btype == "_cell":
            if self._cur_row is not None:
                self._cur_row.append(text)
            return
        if text or btype in ("Figure",):
            self.blocks.append(Block(type=btype, text=text, props=props))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_STRUCTURAL:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        a = {k.lower(): (v or "") for k, v in attrs}

        if tag == "table":
            self._flush()
            self._in_table += 1
            self._table_rows = []
            self._cur_row = None
            return
        if self._in_table and tag == "tr":
            self._cur_row = []
            return

        if tag in _BLOCK_TEXT_TAGS:
            btype = _BLOCK_TEXT_TAGS[tag]
            if tag in _HEADINGS:
                self._open(tag, "Heading", {"level": int(tag[1])})
            else:
                self._open(tag, btype)
            return

        if tag == "img":
            # figure/image: self-contained, alt+src
            self._flush()
            self.blocks.append(Block(
                type="Figure", text=a.get("alt", ""),
                props={"src": a.get("src", ""), "alt": a.get("alt", "")}))
            return

        if tag == "a" and "href" in a:
            # links become their own blocks only when NOT inside a text block we're
            # already capturing (then the anchor text folds into that block instead).
            if self._cap_type is None:
                self._open("a", "Link", {"href": a["href"]})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        # void/self-closing form of these never has an end tag; close immediately
        if tag in ("a",) and self._cap_tag == "a":
            self._flush()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._cap_type is not None:
            self._cap_buf.append(data)

    def handle_endtag(self, tag):
        if tag in _SKIP_STRUCTURAL:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "table" and self._in_table:
            self._flush()
            self._in_table -= 1
            rows = [r for r in self._table_rows if r]
            self.blocks.append(Block(
                type="Table",
                text=" | ".join(" / ".join(r) for r in rows),
                props={"rows": rows}))
            self._table_rows = []
            self._cur_row = None
            return
        if self._in_table and tag == "tr":
            if self._cur_row is not None:
                self._table_rows.append(self._cur_row)
            self._cur_row = None
            return
        if tag == self._cap_tag:
            self._flush()

    def close(self):  # noqa: D102
        super().close()
        self._flush()


def walk_blocks(html: str) -> list[Block]:
    """Single structural pass → ordered list of typed content :class:`Block`s.

    Defensive like the rest of this module: a parse error degrades to whatever
    blocks were collected before it, never an exception.
    """
    w = _StructuralWalker()
    try:
        w.feed(html)
        w.close()
    except Exception:
        pass
    return w.blocks
