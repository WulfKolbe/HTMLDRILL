"""known_hosts / arxiv — the URL-specific routing copied from pdfdrill.

The pure parsers are deterministic and offline (the bulk of the coverage). One
live check hits real arXiv and skips cleanly when offline.

    python3 -m pytest tests/test_arxiv.py
    PYTHONPATH=src python3 tests/test_arxiv.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.sources import known_hosts as K       # noqa: E402
from htmldrill import commands as C                   # noqa: E402
from htmldrill.sidecar import Sidecar                 # noqa: E402

# A minimal abs page carrying exactly the structure parse_arxiv_abs_html reads.
ABS_FIXTURE = """
<h1 class="title mathjax"><span class="descriptor">Title:</span>Attention Is All You Need</h1>
<div class="authors"><span class="descriptor">Authors:</span>
  <a href="/a/vaswani">Ashish Vaswani</a>, <a href="/a/shazeer">Noam Shazeer</a></div>
<blockquote class="abstract mathjax"><span class="descriptor">Abstract:</span>
  The dominant sequence transduction models are based on complex networks.</blockquote>
<td class="tablecell subjects"><span class="primary-subject">Computation and Language (cs.CL)</span>;
  Machine Learning (cs.LG)</td>
"""


def test_parse_arxiv_id_spellings():
    cases = {
        "https://arxiv.org/abs/2510.11170v2": "2510.11170v2",
        "arXiv:2312.11532": "2312.11532",
        "2305.04710v1": "2305.04710v1",
        "math/0309136": "math/0309136",
        "https://arxiv.org/pdf/1706.03762.pdf": "1706.03762",
        "https://example.com/foo": None,
    }
    for s, want in cases.items():
        assert K.parse_arxiv_id(s) == want, f"{s} -> {K.parse_arxiv_id(s)} != {want}"


def test_bare_arxiv_id_only_whole_bare_ids():
    assert K.bare_arxiv_id("2510.11170v2") == "2510.11170v2"
    assert K.bare_arxiv_id("arXiv:2312.11532") == "2312.11532"
    # embedded in a path or a URL is NOT a bare id
    assert K.bare_arxiv_id("data/2312.11532.pdf") is None
    assert K.bare_arxiv_id("https://arxiv.org/abs/1706.03762") is None


def test_known_host():
    assert K.known_host("https://arxiv.org/abs/x") == "arxiv"
    assert K.known_host("https://www.arxiv.org/abs/x") == "arxiv"
    assert K.known_host("https://export.arxiv.org/abs/x") == "arxiv"
    assert K.known_host("https://example.com") is None
    assert K.known_host("1706.03762") is None            # not a URL


def test_arxiv_urls():
    u = K.arxiv_urls("1706.03762")
    assert u["abs"] == "https://arxiv.org/abs/1706.03762"
    assert u["pdf"] == "https://arxiv.org/pdf/1706.03762"
    assert u["eprint"] == "https://arxiv.org/e-print/1706.03762"


def test_parse_arxiv_abs_html_offline():
    m = K.parse_arxiv_abs_html(ABS_FIXTURE)
    assert m["title"] == "Attention Is All You Need"
    assert m["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert m["abstract"].startswith("The dominant sequence")
    assert m["primary_category"] == "cs.CL"


def _online() -> bool:
    try:
        K.F.fetch("https://arxiv.org/abs/1706.03762", timeout=8)
        return True
    except Exception:
        return False


def test_arxiv_command_live():
    """Real network: the command reads the abstract and converges spellings to one
    sidecar. Skips cleanly offline."""
    if not _online():
        print("    (skip: offline)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        out1 = C.cmd_arxiv(C.Ctx(url="https://arxiv.org/abs/1706.03762", work=work))
        assert "Attention Is All You Need" in out1 and "cs.CL" in out1
        # the bare id must resolve to the SAME sidecar (one file)
        C.cmd_arxiv(C.Ctx(url="1706.03762", work=work))
        sidecars = list(Path(work).glob("*.htmldrill.json"))
        assert len(sidecars) == 1, f"spellings did not converge: {sidecars}"
        assert sidecars[0].name == "arxiv-1706.03762.htmldrill.json"


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
