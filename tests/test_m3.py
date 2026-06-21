"""M3 tests — the docops projector payoff: build the docmodel Document from the
fixture, then run pdfdrill's REAL projectors (TiddlyWiki / LLMCompact / PlainText)
over it through the htmldrill command layer.

Skip-safe: if pdfdrill's docmodel/docops can't be imported (no checkout, env var
unset), every test prints "(skip: …)" and counts as a pass — exactly like the
no-Chrome guard in test_l0.py, so this file is runnable anywhere.

Runnable two ways:
    python3 -m pytest tests/test_m3.py
    PYTHONPATH=src python3 tests/test_m3.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C            # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample.html"


def _pdfdrill_available() -> bool:
    """True when docmodel/docops import — otherwise the whole milestone is skipped."""
    try:
        from htmldrill._core import ensure_pdfdrill
        ensure_pdfdrill()
        import docops.loader  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _built_ctx(work: str) -> C.Ctx:
    """fetch (offline file) + model, returning a ready Ctx pointing at the drill."""
    ctx = C.Ctx(url=str(FIXTURE), work=work)
    C.cmd_fetch(ctx)
    C.cmd_model(ctx)
    return ctx


def test_model_builds_document():
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_fetch(ctx)
        out = C.cmd_model(ctx)
        assert "docmodel Document" in out
        # the persisted model must be valid JSON with objects
        from htmldrill.sidecar import Sidecar
        sc = Sidecar(C._resolve_id(ctx), work=work)
        doc = json.loads(sc.read_blob("model.docmodel.json"))
        assert doc.get("objects"), "model has no objects"


def test_tiddlers_projector():
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = _built_ctx(work)
        ctx.out = str(Path(work) / "tiddlers")     # keep .tid files inside the tmp
        out = C.cmd_tiddlers(ctx)
        assert "TiddlyWikiProjector" in out
        from htmldrill.sidecar import Sidecar
        sc = Sidecar(C._resolve_id(ctx), work=work)
        tids = json.loads(sc.read_blob("tiddlers.json"))
        assert isinstance(tids, list) and tids, "tiddlers.json empty / not a list"
        assert all("title" in t for t in tids)


def test_md_and_llmtext_projectors():
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = _built_ctx(work)
        assert "LLMCompactProjector" in C.cmd_md(ctx)
        assert "PlainTextProjector" in C.cmd_llmtext(ctx)
        from htmldrill.sidecar import Sidecar
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert (sc.read_blob("md.md") or "").strip(), "md.md empty"
        assert (sc.read_blob("llm.txt") or "").strip(), "llm.txt empty"


def test_projector_idempotent_and_force():
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = _built_ctx(work)
        ctx.out = str(Path(work) / "tiddlers")
        first = C.cmd_tiddlers(ctx)
        assert "TiddlyWikiProjector" in first
        again = C.cmd_tiddlers(ctx)              # no --force → cached
        assert "cached" in again
        ctx.force = True
        forced = C.cmd_tiddlers(ctx)
        assert "TiddlyWikiProjector" in forced and "cached" not in forced


def test_model_never_fetches():
    """The planner contract: model on a fresh (no-snapshot) drill ERRORS rather
    than touching the network — independent of pdfdrill availability."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        try:
            C.cmd_model(ctx)
        except FileNotFoundError as e:
            assert "offline" in str(e)
            return
        assert False, "model should refuse without a snapshot"


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
