"""single — monolith archive: freeze the page + all assets into ONE self-contained
HTML, and let `model` prefer it.

The load-bearing claims:
  1. `single` runs monolith and writes a self-contained single.html + SINGLE fact
  2. `model` prefers single.html as its source (model_source == "single")
  3. find_monolith honors $HTMLDRILL_MONOLITH (unit, no monolith needed)

The monolith tests skip cleanly (counted as pass) when monolith isn't installed —
`single` is an escalation/optional tool, not a base dependency.

    python3 -m pytest tests/test_single.py
    PYTHONPATH=src python3 tests/test_single.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C                    # noqa: E402
from htmldrill.sources import monolith as MONO         # noqa: E402
from htmldrill.sidecar import Sidecar                  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample.html"


def _have_monolith() -> bool:
    return bool(MONO.find_monolith())


def _pdfdrill_available() -> bool:
    try:
        from htmldrill._core import ensure_pdfdrill
        ensure_pdfdrill()
        import docmodel  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def test_find_monolith_honors_env():
    """Unit, no monolith needed: $HTMLDRILL_MONOLITH pointing at an existing file
    wins over PATH discovery. (Uses sys.executable as a guaranteed-present file.)"""
    old = os.environ.get("HTMLDRILL_MONOLITH")
    try:
        os.environ["HTMLDRILL_MONOLITH"] = sys.executable
        assert MONO.find_monolith() == sys.executable
    finally:
        if old is None:
            os.environ.pop("HTMLDRILL_MONOLITH", None)
        else:
            os.environ["HTMLDRILL_MONOLITH"] = old


def test_single_archives_local_fixture():
    """Real monolith: archive the local fixture into one self-contained HTML."""
    if not _have_monolith():
        print("    (skip: no monolith)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        out = C.cmd_single(ctx)
        assert "self-contained" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("SINGLE")
        html = sc.read_blob("single.html") or ""
        assert "<!DOCTYPE html" in html or "<!doctype html" in html
        assert "Main Title" in html, "archive lost the page's real content"
        assert int(sc.get_evidence("single_bytes") or 0) > 200


def test_single_is_idempotent():
    """A second `single` without --force returns the cached view, no re-archive."""
    if not _have_monolith():
        print("    (skip: no monolith)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_single(ctx)
        again = C.cmd_single(ctx)
        assert "cached single" in again


def test_single_feeds_model():
    """model must PREFER the single.html source once SINGLE holds."""
    if not _have_monolith():
        print("    (skip: no monolith)", end="")
        return
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_single(ctx)
        out = C.cmd_model(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.get_evidence("model_source") == "single", out
        assert sc.has("MODEL_BUILT")


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
