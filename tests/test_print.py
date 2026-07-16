"""print (page -> PDF) + the text-layer validator — real, deterministic, offline.

Runs against a real local fixture with headless Chrome (no network). The Firefox
engine (Selenium + geckodriver) is exercised only when both are installed, and
skips cleanly otherwise — it is an OPTIONAL dependency, so its absence must never
fail the suite.

    python3 -m pytest tests/test_print.py
    PYTHONPATH=src python3 tests/test_print.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C                    # noqa: E402
from htmldrill.sources import print_pdf as P           # noqa: E402
from htmldrill.sidecar import Sidecar                  # noqa: E402

PAGE = ROOT / "tests" / "corpus" / "site" / "index.html"


def _chrome() -> bool:
    from htmldrill.sources import render as R
    return bool(R.find_chrome())


def _firefox() -> bool:
    if not P.find_geckodriver():
        return False
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        return False


def test_validator_rejects_missing_pdf():
    v = P.validate_text_layer(Path("/nonexistent/nope.pdf"))
    assert v["usable"] is False and "no PDF" in v["verdict"]


def test_validator_flags_empty_text_layer():
    """A PDF-less/te0xt-less file must be judged unusable, not crash."""
    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty.pdf"
        empty.write_bytes(b"%PDF-1.4\n%%EOF\n")     # structurally a PDF, no text
        v = P.validate_text_layer(empty)
        # usable=False (no words) or None if pdftotext is unavailable — never True
        assert v["usable"] is not True


def test_print_chrome_and_validate():
    """Real: print a real local page with headless Chrome, judge the text layer."""
    if not _chrome():
        print("    (skip: no chrome)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(PAGE), work=work, engine="chrome", timeout=90.0)
        out = C.cmd_print(ctx)
        assert "printed" in out and "chrome" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("PRINTED")
        pdf = sc.blob_path("print.pdf")
        assert pdf.exists() and pdf.stat().st_size > 500
        assert pdf.read_bytes()[:5] == b"%PDF-"           # a real PDF
        # the validator must find the page's real text
        if shutil.which("pdftotext"):
            assert sc.get_evidence("print_text_usable") is True
            assert "no OCR needed" in (sc.get_evidence("print_text_verdict") or "")


def test_print_idempotent():
    if not _chrome():
        print("    (skip: no chrome)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(PAGE), work=work, engine="chrome", timeout=90.0)
        C.cmd_print(ctx)
        again = C.cmd_print(ctx)                            # no --force
        assert "cached print" in again


def test_print_firefox_when_available():
    """The Selenium.md path: Firefox WebDriver print_page. Optional dependency."""
    if not _firefox():
        print("    (skip: no geckodriver/selenium)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(PAGE), work=work, engine="firefox", timeout=120.0)
        out = C.cmd_print(ctx)
        assert "printed" in out and "firefox" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        pdf = sc.blob_path("print.pdf")
        assert pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-"


def test_unknown_engine_rejected():
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(PAGE), work=work, engine="netscape")
        try:
            C.cmd_print(ctx)
            assert False, "unknown engine should raise"
        except ValueError as e:
            assert "unknown engine" in str(e)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(fns)} passed")
    return 0 if passed == len(fns) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
