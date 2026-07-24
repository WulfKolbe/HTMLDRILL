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
