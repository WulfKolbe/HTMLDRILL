"""capture — scroll-materialize a real page, then grab DOM + PDF + screenshot.

The load-bearing claims, tested for real on a local lazy-loading fixture:
  1. scrolling materializes lazy content a naive capture drops (601 rows, not ~51)
  2. the browser profile is ISOLATED — never the user's real profile
  3. the profile-isolation flags are present in the Chrome paths too (unit check)

The browser tests are gated on Firefox/geckodriver/selenium and skip cleanly
(counted as pass) when absent — capture is an escalation, not a base dependency.

    python3 -m pytest tests/test_capture.py
    PYTHONPATH=src python3 tests/test_capture.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C                    # noqa: E402
from htmldrill.sources import capture as CAP           # noqa: E402
from htmldrill.sources import render as R              # noqa: E402
from htmldrill.sidecar import Sidecar                  # noqa: E402

LAZY = ROOT / "tests" / "corpus" / "lazytable.html"    # 600 rows appended on scroll


def _firefox() -> bool:
    if not CAP._find_geckodriver():
        return False
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        return False


def test_render_chrome_flags_are_isolated():
    """Unit, no browser: the Chrome flag builder MUST pin an isolated
    --user-data-dir so headless Chrome never attaches to the real profile."""
    flags = R._flags("/tmp/some-throwaway-profile")
    assert any(f.startswith("--user-data-dir=") for f in flags)
    assert "--no-default-browser-check" in flags


def test_static_markup_misses_lazy_rows():
    """Baseline: the raw markup only has the header row — proving the lazy
    content genuinely isn't there without a browser."""
    rows = len(re.findall(r"<tr\b", LAZY.read_text(), re.I))
    assert rows <= 2, f"fixture should start with ~1 row, has {rows}"


def test_capture_materializes_all_lazy_rows():
    """Real: capture the lazy fixture; the scroll loop must recover ALL rows,
    and the profile must be isolated (never ~/.mozilla / ~/.config)."""
    if not _firefox():
        print("    (skip: no firefox/geckodriver/selenium)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(LAZY), work=work, engine="firefox", timeout=90.0)
        out = C.cmd_capture(ctx)
        assert "captured" in out and "materialized" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("CAPTURED")
        dom = sc.read_blob("captured.html") or ""
        rows = len(re.findall(r"<tr\b", dom, re.I))
        assert rows >= 600, f"lazy rows not materialized: got {rows}, want >=600"
        # profile isolation — the whole safety point
        prof = sc.get_evidence("capture_profile") or ""
        assert ".mozilla" not in prof and ".config" not in prof, f"real profile touched: {prof}"
        # the PDF captured them too, with a usable text layer
        pdf = sc.blob_path("capture.pdf")
        assert pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-"
        if shutil.which("pdftotext"):
            txt = subprocess.run(["pdftotext", str(pdf), "-"],
                                 capture_output=True, text=True).stdout
            assert txt.count("SENSOR_") >= 500, "PDF lost the lazy rows"


def test_capture_feeds_model():
    """model must prefer the captured DOM as its source once CAPTURED holds."""
    if not _firefox():
        print("    (skip: no firefox/geckodriver/selenium)", end="")
        return
    from htmldrill._core import pdfdrill_src
    if not (pdfdrill_src() / "docmodel").exists():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(LAZY), work=work, engine="firefox", timeout=90.0)
        C.cmd_capture(ctx)
        out = C.cmd_model(ctx)
        assert "captured DOM" in out


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
