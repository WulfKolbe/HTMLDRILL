"""latex — the html2latex projector: docmodel → HTML → standalone .tex.

Two layers:
  1. project_latex.document_to_html is DEPENDENCY-FREE — a duck-typed doc in, an
     HTML string out. Tested with a hand-built fake doc (no pdfdrill, no
     html2latex), so the serializer's flow-order + section/list/table/figure
     mapping is pinned everywhere.
  2. cmd_latex end-to-end (fetch fixture → model → latex → out.tex) is gated on
     html2latex + pdfdrill and skips cleanly when either is absent — html2latex is
     an OPTIONAL dependency, doctor-gated.

    python3 -m pytest tests/test_latex.py
    PYTHONPATH=src python3 tests/test_latex.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C                        # noqa: E402
from htmldrill.project_latex import document_to_html       # noqa: E402
from htmldrill.sidecar import Sidecar                      # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample.html"


# -- dependency-free serializer fakes ----------------------------------------
class _Obj:
    def __init__(self, oid, otype, props):
        self.id, self.type, self.props = oid, otype, props


class _Doc:
    def __init__(self, objs, meta):
        self.objects = {o.id: o for o in objs}
        self.meta = meta


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


def _pdfdrill_available() -> bool:
    try:
        from htmldrill._core import ensure_pdfdrill
        ensure_pdfdrill()
        import docmodel  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def test_serializer_is_dependency_free():
    """document_to_html maps the object vocabulary to HTML in flow order, with
    NO html2latex/pdfdrill import — the projector's input half, pinned."""
    doc = _Doc([
        _Obj("s1", "Section", {"flow_index": 0, "level": 2, "caption": "Intro"}),
        _Obj("p1", "Paragraph", {"flow_index": 1, "text": "Hello world."}),
        _Obj("l1", "ListItem", {"flow_index": 2, "text": "one"}),
        _Obj("l2", "ListItem", {"flow_index": 3, "text": "two"}),
        _Obj("t1", "Table", {"flow_index": 4, "rows": [["a", "b"], ["c", "d"]]}),
        _Obj("f1", "Picture", {"flow_index": 5, "src": "img.png", "caption": "A fig"}),
    ], {"title": "Doc"})
    html = document_to_html(doc)
    assert "<h2>Intro</h2>" in html
    assert "<p>Hello world.</p>" in html
    # consecutive list items collapse into ONE <ul>
    assert "<ul><li>one</li><li>two</li></ul>" in html
    assert html.count("<ul>") == 1
    assert "<table><tr><td>a</td><td>b</td></tr>" in html
    assert "<figure>" in html and "A fig" in html
    # flow order preserved: section before paragraph before list before table
    assert html.index("Intro") < html.index("Hello") < html.index("one") < html.index("<table>")


#: a real 1×1 transparent PNG — decodes to valid bytes, no ImageMagick needed
_PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_asset_extractor_writes_files_and_drops_remote():
    """data: images become on-disk files (no data: in the HTML, path emitted);
    remote/relative srcs can't be resolved offline → dropped to caption-only.
    Dependency-free: PNG is a direct format, so no ImageMagick is required."""
    from htmldrill.project_latex import document_to_html, make_extractor
    with tempfile.TemporaryDirectory() as d:
        assets = Path(d) / "tex-assets"
        doc = _Doc([_Obj("f", "Picture", {
            "flow_index": 0, "src": f"data:image/png;base64,{_PNG_1x1}",
            "caption": "a pic"})], {"title": "T"})
        ex = make_extractor(assets)
        html = document_to_html(doc, extractor=ex)
        assert "data:" not in html, "a raw data: URI leaked into the HTML/LaTeX"
        assert "tex-assets/img001.png" in html
        assert ex.embedded == 1 and ex.dropped == []
        assert (assets / "img001.png").exists() and (assets / "img001.png").stat().st_size > 0

        # remote src: dropped to a caption-only figure (offline can't fetch it)
        doc2 = _Doc([_Obj("f", "Picture", {
            "flow_index": 0, "src": "https://example.com/y.png",
            "caption": "remote"})], {"title": "T"})
        ex2 = make_extractor(Path(d) / "a2")
        html2 = document_to_html(doc2, extractor=ex2)
        assert "<img" not in html2 and "remote" in html2
        assert ex2.embedded == 0 and len(ex2.dropped) == 1


def test_renovate_modernizes_output():
    """The renovation pass kills \\par terminators, ungludes environments,
    modernizes the preamble, and is idempotent — without dropping content."""
    from htmldrill.latex_renovate import renovate
    raw = ("\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
           "\\section{T}\n\\begin{figure}\n\\includegraphics{a.png}\n"
           "\\caption{c}\n\\end{figure}Body text here\\par\nSecond para\\par\n"
           "\\end{document}\n")
    out = renovate(raw)
    # no bare \par terminators survive (but \paragraph-like macros would be safe)
    import re as _re
    assert not _re.search(r"\\par(?![a-zA-Z])", out), "a \\par terminator survived"
    # environment end no longer glued to following prose
    assert "\\end{figure}Body" not in out and "\\end{figure}\n" in out
    # content preserved
    assert "Body text here" in out and "Second para" in out
    # preamble modernized + engine-adaptive Unicode handling
    assert "\\documentclass[11pt]{article}" in out
    assert "\\ifpdftex" in out and "inputenc" in out
    assert "\\usepackage{graphicx}" in out            # html2latex's own pkg kept
    # idempotent: renovating again changes nothing
    assert renovate(out) == out


def test_dmath_block_parsed_as_equation():
    """<d-math block> is captured as a standalone Equation block carrying the gold
    TeX; inline <d-math> is (for now) folded into the surrounding prose."""
    from htmldrill.parse import html as H
    snippet = ("<p>rows of <d-math>W_U J_\\ell</d-math> here</p>"
               "<d-math block> J_\\ell = \\mathbb{E}[x] </d-math>")
    blocks = H.walk_blocks(snippet)
    eqs = [b for b in blocks if b.type == "Equation"]
    assert len(eqs) == 1, [(b.type, b.text) for b in blocks]
    assert eqs[0].text == "J_\\ell = \\mathbb{E}[x]"
    # inline d-math TeX still rides along in the paragraph (deferred to pass 2)
    assert any(b.type == "Paragraph" and "W_U J_\\ell" in b.text for b in blocks)


def test_equation_projects_as_display_math():
    """An Equation object → a math-display span (escaped TeX) so html2latex emits
    \\[...\\] — NOT an escaped <p>. Verified through html2latex when present."""
    from htmldrill.project_latex import document_to_html
    tex = "J_\\ell = \\mathbb{E}[x] \\leq y"
    doc = _Doc([_Obj("e", "Equation",
                     {"flow_index": 0, "latex": tex, "text": tex, "provenance": "dom"})],
               {"title": "D"})
    html = document_to_html(doc)
    assert 'class="math-display"' in html
    assert "<p>J_" not in html                      # NOT escaped prose
    if _have("html2latex"):
        import html2latex as h2l
        out = h2l.html2latex(html)
        assert "\\[" in out and "\\]" in out
        assert "\\mathbb{E}" in out                 # TeX survived unescaped
        assert "textbackslash" not in out


def test_code_block_renders_verbatim_not_escaped():
    """A code_block Paragraph (a <pre>/<code> body, e.g. a BibTeX box) must emit
    <pre> so html2latex renders it verbatim — NOT escaped prose."""
    from htmldrill.project_latex import document_to_html
    bib = "@misc{x2026, title={{T}}, note=$\\sim$99\\%}"
    doc = _Doc([_Obj("c", "Paragraph",
                     {"flow_index": 0, "text": bib, "code_block": True})],
               {"title": "D"})
    html = document_to_html(doc)
    assert f"<pre>" in html and "</pre>" in html
    assert "<p>@misc" not in html                 # NOT a normal paragraph
    # a plain paragraph with the same text WOULD be a <p>
    doc2 = _Doc([_Obj("p", "Paragraph", {"flow_index": 0, "text": bib})], {"title": "D"})
    assert "<pre>" not in document_to_html(doc2)


def test_renovate_wraps_code_in_lstlisting():
    """The renovation converts html2latex verbatim → wrapping lstlisting, adds the
    listings package, keeps code content byte-for-byte, and stays idempotent."""
    from htmldrill.latex_renovate import renovate
    raw = ("\\documentclass{article}\n\\begin{document}\n"
           "Intro para\\par\n"
           "\\begin{verbatim}@misc{x, title={{T}}, note=$\\sim$99\\%}\\end{verbatim}\n"
           "\\end{document}\n")
    out = renovate(raw)
    assert "\\begin{lstlisting}" in out and "\\begin{verbatim}" not in out
    assert "\\usepackage{listings}" in out and "\\lstset{" in out
    # code content survives unescaped and intact
    assert "@misc{x, title={{T}}, note=$\\sim$99\\%}" in out
    # the whitespace transforms did NOT touch inside the code block
    assert renovate(out) == out                    # idempotent


def test_serializer_escapes_content():
    """Model text can never inject markup — angle brackets are escaped."""
    doc = _Doc([_Obj("p", "Paragraph", {"flow_index": 0, "text": "a < b & c > d"})],
               {"title": "T"})
    html = document_to_html(doc)
    assert "&lt; b &amp; c &gt;" in html
    assert "<p>a <" not in html


def test_latex_projects_model_to_tex():
    """End-to-end: fetch (offline file) → model → latex, producing a standalone
    .tex with a documentclass and the reconstructed section title."""
    if not _have("html2latex"):
        print("    (skip: no html2latex)", end="")
        return
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_fetch(ctx)
        C.cmd_model(ctx)
        out = C.cmd_latex(ctx)
        assert "standalone LaTeX" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("LATEX_BUILT")
        tex = sc.read_blob("out.tex") or ""
        assert "\\documentclass" in tex and "\\begin{document}" in tex
        assert "\\end{document}" in tex
        # the fixture's headings survive into sectioning commands
        assert "Main Title" in tex
        # NO raw data: URI may survive in \includegraphics — that wouldn't compile
        assert "data:image" not in tex, "a base64 data URI leaked into out.tex"


def test_latex_missing_dep_is_actionable():
    """If html2latex is absent, cmd_latex raises a clear install hint (not a bare
    ImportError). Only meaningful to assert when the dep is genuinely missing."""
    if _have("html2latex"):
        print("    (skip: html2latex present)", end="")
        return
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_fetch(ctx)
        C.cmd_model(ctx)
        try:
            C.cmd_latex(ctx)
            assert False, "expected a RuntimeError when html2latex is absent"
        except RuntimeError as e:
            assert "pip install" in str(e)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  ✓ {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(fns)} passed")
    return 0 if passed == len(fns) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
