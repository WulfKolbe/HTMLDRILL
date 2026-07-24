"""project_latex — serialize a shared docmodel Document back into a clean HTML
document, in flow order, so html2latex can project it to LaTeX.

This is the input half of the `latex` projector. html2latex converts HTML → LaTeX;
the docmodel is our normalized intermediate. So the projector's job is a faithful
docmodel → HTML reconstruction honoring the object vocabulary `ingest_dom`
produces (Section / Paragraph / ListItem / Table / Picture), which
``html2latex.render`` then turns into a standalone ``.tex``.

Kept as its own module (not inlined in commands.py) so the serializer is small,
single-purpose, and unit-testable WITHOUT html2latex or a live model — a plain
Document in, an HTML string out.

Image assets (the compile-critical bit)
---------------------------------------
A self-contained ``single.html`` inlines images as ``data:...;base64`` URIs. But
``\\includegraphics`` from standard graphicx wants a FILE on disk, not a data URI —
so passing those through produces a ``.tex`` that will not compile. Only LuaLaTeX
+ the ``luaimageembed`` package can read base64 directly (png/jpg/pdf, NOT gif).

So when an ``assets_dir`` is provided, an :class:`_AssetExtractor` decodes each
``data:`` image to a real file beside the ``.tex`` and the emitted ``<img src>``
points at that file — an engine-agnostic ``.tex`` that compiles under
pdflatex/xelatex/lualatex. Formats pdflatex can't load directly (gif/webp/svg/…)
are transcoded to PNG via ImageMagick ``magick`` when available; anything that
can't be resolved offline (remote/relative src, or a format we can't transcode)
is DROPPED to a caption-only figure rather than emitting a broken include.

Fidelity note: htmldrill's blocks carry *collapsed plain text* (inline markup was
flattened at L0 parse time), so the emitted HTML — and thus the LaTeX — is
structurally faithful but inline-plain.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
import tempfile
from html import escape
from pathlib import Path
from typing import Iterable, Optional

#: data:<mime>[;base64],<payload> — we only embed the base64 form (offline-safe).
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[^;,]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.S)


class _AssetExtractor:
    """Decode ``data:`` image URIs to real files under ``assets_dir`` and hand back
    a relative path ``<dir>/imgNNN.<ext>``. Non-data srcs and un-transcodable
    formats are recorded in :attr:`dropped` and resolve to ``None`` (caller emits a
    caption-only figure)."""

    #: mime → file extension
    _EXT = {
        "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "application/pdf": "pdf", "image/gif": "gif", "image/webp": "webp",
        "image/svg+xml": "svg", "image/bmp": "bmp", "image/tiff": "tiff",
    }
    #: formats pdflatex/xelatex load directly — kept as-is, others transcode → png
    _DIRECT = {"png", "jpg", "jpeg", "pdf"}

    def __init__(self, assets_dir) -> None:
        self.dir = Path(assets_dir)
        self.n = 0
        self.embedded = 0
        self.dropped: list[str] = []

    def resolve(self, src: str) -> Optional[str]:
        m = _DATA_URI_RE.match((src or "").strip())
        if not m or not m.group("b64"):
            self.dropped.append(src)             # remote/relative or non-base64
            return None
        try:
            raw = base64.b64decode(m.group("data"), validate=False)
        except (binascii.Error, ValueError):
            self.dropped.append(src)
            return None
        if not raw:
            self.dropped.append(src)
            return None
        ext = self._EXT.get((m.group("mime") or "image/png").lower(), "png")

        self.dir.mkdir(parents=True, exist_ok=True)
        candidate = self.n + 1
        if ext in self._DIRECT:
            fname = f"img{candidate:03d}.{ext}"
            (self.dir / fname).write_bytes(raw)
        else:
            png = self._transcode_png(raw, ext)
            if png is None:                       # can't make it pdflatex-loadable
                self.dropped.append(src)
                return None
            fname = f"img{candidate:03d}.png"
            (self.dir / fname).write_bytes(png)
        self.n = candidate
        self.embedded += 1
        return f"{self.dir.name}/{fname}"

    @staticmethod
    def _transcode_png(raw: bytes, ext: str) -> Optional[bytes]:
        """Convert odd formats (gif/webp/svg/…) to PNG via ImageMagick v7 `magick`
        (fallback to legacy `convert`). Returns None when no converter is present."""
        tool = shutil.which("magick") or shutil.which("convert")
        if not tool:
            return None
        src_path = out_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
                tf.write(raw)
                src_path = tf.name
            out_path = src_path + ".png"
            r = subprocess.run([tool, src_path, out_path],
                               capture_output=True, timeout=30)
            if r.returncode == 0 and Path(out_path).exists():
                return Path(out_path).read_bytes()
            return None
        except Exception:  # noqa: BLE001 — a broken asset must never sink the run
            return None
        finally:
            for p in (src_path, out_path):
                if p:
                    try:
                        os.remove(p)
                    except OSError:
                        pass


def make_extractor(assets_dir) -> _AssetExtractor:
    """Factory so callers (cmd_latex) own the extractor and can read its stats."""
    return _AssetExtractor(assets_dir)


def _objects(doc) -> list:
    o = getattr(doc, "objects", None)
    if o is None:
        return []
    return list(o.values()) if isinstance(o, dict) else list(o)


def _flow_key(o) -> tuple:
    props = getattr(o, "props", {}) or {}
    return (props.get("flow_index", 0), str(getattr(o, "id", "")))


def _text_of(o) -> str:
    props = getattr(o, "props", {}) or {}
    return str(props.get("text") or getattr(o, "text", "") or "").strip()


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _table_html(rows: Iterable) -> str:
    trs = []
    for row in rows or []:
        cells = row if isinstance(row, (list, tuple)) else [row]
        tds = "".join(f"<td>{escape(str(c))}</td>" for c in cells)
        trs.append(f"<tr>{tds}</tr>")
    return f"<table>{''.join(trs)}</table>" if trs else ""


def _figure_html(props: dict, extractor: Optional[_AssetExtractor]) -> str:
    raw_src = str(props.get("src") or "")
    alt = escape(str(props.get("alt") or ""), quote=True)
    caption = escape(str(props.get("caption") or props.get("alt") or "").strip())

    local: Optional[str] = None
    if raw_src:
        # With an extractor, data: URIs become on-disk files and everything else is
        # dropped (offline). Without one (unit tests), pass the src through verbatim.
        local = extractor.resolve(raw_src) if extractor is not None else raw_src
    img = (f'<img src="{escape(str(local), quote=True)}" alt="{alt}"/>'
           if local else "")
    cap = f"<figcaption>{caption}</figcaption>" if caption else ""
    inner = img + cap
    return f"<figure>{inner}</figure>" if inner else ""


def document_to_html(doc, extractor: Optional[_AssetExtractor] = None) -> str:
    """Reconstruct one HTML document from the docmodel objects, in flow order.

    Section → ``<h{level}>``; Paragraph (and any other text-bearing type) →
    ``<p>``; runs of ListItem → a single ``<ul>``; Table → ``<table>`` rebuilt
    from ``props['rows']``; Picture/Figure → ``<figure>``. Text and attributes are
    escaped so model content can never inject markup.

    ``extractor`` (an :class:`_AssetExtractor`, usually via :func:`make_extractor`)
    turns ``data:`` image URIs into on-disk files so the resulting LaTeX compiles;
    when omitted, image srcs are passed through unchanged (the dependency-free
    serializer path).
    """
    objs = sorted(_objects(doc), key=_flow_key)
    parts: list[str] = []
    list_buffer: list[str] = []

    def flush_list() -> None:
        if list_buffer:
            items = "".join(f"<li>{escape(t)}</li>" for t in list_buffer)
            parts.append(f"<ul>{items}</ul>")
            list_buffer.clear()

    for o in objs:
        otype = getattr(o, "type", "") or ""
        props = getattr(o, "props", {}) or {}
        text = _text_of(o)

        if otype == "ListItem":
            if text:
                list_buffer.append(text)
            continue
        flush_list()

        if otype == "Section":
            level = _clamp(int(props.get("level", 1) or 1), 1, 6)
            title = str(props.get("caption") or text).strip()
            if title:
                parts.append(f"<h{level}>{escape(title)}</h{level}>")
        elif otype == "Table":
            html = _table_html(props.get("rows") or [])
            parts.append(html or (f"<p>{escape(text)}</p>" if text else ""))
        elif otype in ("Picture", "Figure"):
            parts.append(_figure_html(props, extractor))
        elif text:                       # Paragraph and any other prose object
            parts.append(f"<p>{escape(text)}</p>")
    flush_list()

    # Emit a plain body FRAGMENT — no <!DOCTYPE>/<html>/<head>. html2latex.render
    # supplies the standalone \documentclass wrapper itself, and its strict parser
    # rejects a DOCTYPE declaration. A leading <h1> from the model's title (when it
    # isn't already the first heading) gives the document a real title line.
    meta = getattr(doc, "meta", {}) or {}
    title = str(meta.get("title") or meta.get("bibkey") or "").strip()
    if title and not (parts and parts[0].startswith("<h1>")):
        parts.insert(0, f"<h1>{escape(title)}</h1>")
    return "\n".join(p for p in parts if p)
