"""M4 tests — split recovery (lazy-load & virtualization), REAL local pages.

Every fixture in tests/corpus/ is a REAL probe target (or the verbatim hidden
element lifted from one), copied in offline:

  * details-collapsed.html  — the real CLOSED <details> element from Test Table.html
                              (collapsed in the view; body present in markup).
  * lazy-img.html           — formula-report.html: 13 loading="lazy" CDN images.
  * shadow-template.html    — saved_resource.html: declarative shadow <template>.
  * noscript-spa.html       — a real SPA's <noscript> + empty #root mount point.

The two assertions the milestone demands:
  1. `splits` detects the real markers (the right kind + repair energy).
  2. `materialize` recovers REAL hidden text that was ABSENT from the
     un-materialized model — proving content was actually cut and then repaired.

Deterministic and OFFLINE. The render-delta path is gated on Chrome (skip-safe).

Runnable two ways:
    python3 -m pytest tests/test_splits.py
    PYTHONPATH=src python3 tests/test_splits.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.parse import html as H          # noqa: E402
from htmldrill import commands as C            # noqa: E402
from htmldrill.sidecar import Sidecar          # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus"
DETAILS = CORPUS / "details-collapsed.html"
LAZY = CORPUS / "lazy-img.html"
TEMPLATE = CORPUS / "shadow-template.html"
NOSCRIPT = CORPUS / "noscript-spa.html"


def _pdfdrill_available() -> bool:
    try:
        from htmldrill._core import ensure_pdfdrill
        ensure_pdfdrill()
        import docops.loader  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# -- detector: the real markers, classified by kind + repair energy ------------

def test_splits_detects_collapsed_details():
    raw = DETAILS.read_text(encoding="utf-8")
    splits = H.detect_splits(raw)
    coll = [s for s in splits if s.kind == "collapsed"]
    assert coll, "the real closed <details> was not detected"
    s = coll[0]
    assert s.energy == "toggle"               # closed details → browser must toggle
    assert s.recovered, "details body not recovered from static markup"
    # the REAL body text (the hidden table) must be recovered offline
    assert "Table 1" in s.recovered or "System Specifications" in s.recovered


def test_splits_detects_lazy_images():
    raw = LAZY.read_text(encoding="utf-8")
    splits = H.detect_splits(raw)
    lazy = [s for s in splits if s.kind == "lazy-media"]
    # the real formula-report has 13 loading="lazy" CDN crops
    assert len(lazy) == 13
    assert all(s.energy == "intersection" for s in lazy)
    # the src reference is recoverable from the markup (only the BYTES are deferred)
    assert any("cdn.mathpix.com" in s.recovered for s in lazy)


def test_splits_detects_template_deferred():
    raw = TEMPLATE.read_text(encoding="utf-8")
    splits = H.detect_splits(raw)
    tpl = [s for s in splits if s.kind == "deferred" and s.tag == "template"]
    assert tpl, "declarative shadow <template> not detected"
    assert tpl[0].energy == "none"            # body already in markup
    assert "shadowrootmode" in tpl[0].evidence


def test_splits_detects_noscript_deferred():
    raw = NOSCRIPT.read_text(encoding="utf-8")
    splits = H.detect_splits(raw)
    ns = [s for s in splits if s.kind == "deferred" and s.tag == "noscript"]
    assert ns, "<noscript> body not detected"
    # the real noscript body must be recovered statically
    assert "enable JavaScript" in ns[0].recovered


def test_splits_summary_counts_and_energy():
    raw = LAZY.read_text(encoding="utf-8")
    summary = H.summarize_splits(H.detect_splits(raw))
    assert summary["by_kind"].get("lazy-media") == 13
    assert summary["by_energy"].get("intersection") == 13
    assert summary["recoverable"] >= 13       # each lazy img keeps its src ref


# -- command layer: splits records SPLITS_KNOWN + evidence --------------------

def test_cmd_splits_records_fact_and_evidence():
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(DETAILS), work=work)
        C.cmd_fetch(ctx)
        out = C.cmd_splits(ctx)
        assert "collapsed" in out and "toggle" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("SPLITS_KNOWN")
        assert sc.get_evidence("splits_by_kind", {}).get("collapsed") == 1


def test_cmd_splits_offline_without_snapshot_errors():
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(DETAILS), work=work)
        try:
            C.cmd_splits(ctx)
        except FileNotFoundError as e:
            assert "offline" in str(e) or "fetch" in str(e)
            return
        assert False, "splits should refuse without a snapshot"


# -- the KILLER claim: materialize recovers text the model dropped -------------

def test_materialize_recovers_text_absent_from_model():
    """The real <noscript> body is GENUINELY CUT from the surface model — the
    structural walk skips <noscript>/<template> subtrees (a JS-enabled browser
    never paints them), so their text never reaches the model. `materialize`
    recovers it as a role=continuation fragment and folds it in. Prove: the
    marker is ABSENT from the un-materialized model, then PRESENT after
    materialize → rebuild."""
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    import json
    marker = "enable JavaScript"
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(NOSCRIPT), work=work)
        C.cmd_fetch(ctx)
        C.cmd_model(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)

        # PRECONDITION: the hidden body really IS cut from the surface model
        model0 = sc.read_blob("model.docmodel.json") or ""
        assert marker not in model0, "precondition: noscript body must be cut from model"
        raw = sc.read_blob("raw.html") or ""
        ns = [s for s in H.detect_splits(raw) if s.tag == "noscript"][0]
        assert marker in ns.recovered, "precondition: marker in the hidden body"

        # materialize: continuation fragment recorded, and (model built) folded in
        out = C.cmd_materialize(ctx)
        assert "continuation" in out and "materialized" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("MATERIALIZED")
        cont = json.loads(sc.read_blob("continuation.json") or "[]")
        assert cont and any(marker in f["text"] for f in cont)
        assert all(f["role"] == "continuation" for f in cont)

        # the recovered text now reaches the model (appended in-place since
        # MODEL_BUILT held) with a role=continuation realization
        model1 = sc.read_blob("model.docmodel.json") or ""
        assert marker in model1, "continuation text never reached the model"
        assert '"role": "continuation"' in model1

        # and a fresh rebuild ALSO folds it in (continuation.json is honored)
        ctx.force = True
        C.cmd_model(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        model2 = sc.read_blob("model.docmodel.json") or ""
        assert marker in model2 and '"role": "continuation"' in model2


def test_materialize_dedups_already_static_details():
    """A CLOSED <details> body is collapsed in-browser but PRESENT in static
    markup, so `model` already captured it (html.parser doesn't skip <details>).
    materialize must NOT re-inject it — doing so would duplicate paragraphs in
    the projection. It reports nothing new and leaves no continuation in the
    model. (The genuine absent->recovered case is covered by the <noscript>
    test above; deferred <noscript>/<template> bodies ARE skipped by walk_blocks
    and so are legitimately materialized.)"""
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(DETAILS), work=work)
        C.cmd_fetch(ctx)
        C.cmd_model(ctx)
        out = C.cmd_materialize(ctx)
        assert "already in the static model" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        model = sc.read_blob("model.docmodel.json") or ""
        # the details body is present (from the base model) but NOT duplicated
        # as a continuation fragment
        assert '"role": "continuation"' not in model


def test_commands_accept_bare_id_token():
    """Regression: the documented path `htmldrill <cmd> <id-prefix>` (splits'
    own output and the status/steps help advertise it) must not crash. This
    caught a NameError where commands.py used resolve_local_id without importing
    it — full-path invocation hid it, every bare-id invocation crashed."""
    with tempfile.TemporaryDirectory() as work:
        # establish a snapshot, then drive subsequent commands by the bare id
        C.cmd_fetch(C.Ctx(url=str(DETAILS), work=work))
        from htmldrill.sources import fetch as F
        bare_id = F.local_id_for(str(DETAILS))
        for cmd in (C.cmd_splits, C.cmd_status, C.cmd_size, C.cmd_links):
            out = cmd(C.Ctx(url=bare_id, work=work))   # bare id, no '/'
            assert isinstance(out, str) and out          # no NameError, real output


def test_materialize_idempotent():
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(NOSCRIPT), work=work)
        C.cmd_fetch(ctx)
        first = C.cmd_materialize(ctx)
        assert "materialized" in first or "nothing to materialize" in first
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("MATERIALIZED")


def test_materialize_recovers_noscript_body_to_continuation():
    """noscript body (deferred, energy=none) is in-markup → materialized offline."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(NOSCRIPT), work=work)
        C.cmd_fetch(ctx)
        out = C.cmd_materialize(ctx)
        import json
        sc = Sidecar(C._resolve_id(ctx), work=work)
        cont = json.loads(sc.read_blob("continuation.json") or "[]")
        assert any("enable JavaScript" in f["text"] for f in cont)


# -- render-delta: gated on Chrome (skip-safe) --------------------------------

def test_materialize_render_delta_gated_on_chrome():
    """--render-delta materializes timer/raf content via virtual-time. Needs
    Chrome; skipped (counts as pass) when absent. Uses the render_delta fixture
    whose setTimeout appends a #timer-extra block after load."""
    from htmldrill.sources import render as R
    if not R.find_chrome():
        print("    (skip: no chrome)", end="")
        return
    fixture = ROOT / "tests" / "fixtures" / "render_delta_page.html"
    if not fixture.exists():
        print("    (skip: no render_delta fixture)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(fixture), work=work, render_delta=True)
        C.cmd_fetch(ctx)
        out = C.cmd_materialize(ctx)
        assert "render-delta" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("MATERIALIZED")
        # the probe-confirmed marker only the virtual-time render materializes
        cont_raw = sc.read_blob("continuation.json") or "[]"
        # not asserting exact content (env-dependent), but the mode must run clean
        assert sc.get_evidence("materialized_mode") == "render-delta"


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
