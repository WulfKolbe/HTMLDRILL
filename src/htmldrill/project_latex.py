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

Fidelity note: htmldrill's blocks carry *collapsed plain text* (inline markup was
flattened at L0 parse time), so the emitted HTML — and thus the LaTeX — is
structurally faithful but inline-plain. Preserving per-block inner HTML to unlock
html2latex's inline/math strengths is a documented future enhancement.
"""
from __future__ import annotations

from html import escape
from typing import Iterable


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


def _figure_html(props: dict) -> str:
    src = escape(str(props.get("src") or ""), quote=True)
    alt = escape(str(props.get("alt") or ""), quote=True)
    caption = escape(str(props.get("caption") or props.get("alt") or "").strip())
    img = f'<img src="{src}" alt="{alt}"/>' if src else ""
    cap = f"<figcaption>{caption}</figcaption>" if caption else ""
    inner = img + cap
    return f"<figure>{inner}</figure>" if inner else (f"<p>{caption}</p>" if caption else "")


def document_to_html(doc) -> str:
    """Reconstruct one HTML document from the docmodel objects, in flow order.

    Section → ``<h{level}>``; Paragraph (and any other text-bearing type) →
    ``<p>``; runs of ListItem → a single ``<ul>``; Table → ``<table>`` rebuilt
    from ``props['rows']``; Picture/Figure → ``<figure>``. Text and attributes are
    escaped so model content can never inject markup.
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
            parts.append(_figure_html(props))
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
