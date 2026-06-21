"""M5 tests — crawl, retrieve, chatlog, and the drillui --once smoke dispatch.

All deterministic + OFFLINE: the crawl runs over a local file:// interlinked site
(tests/corpus/site/), retrieve reuses pdfdrill's lexical retriever over a built
model (skip-safe if pdfdrill is absent), chatlog round-trips a JSONL transcript,
and the drillui smoke test launches tools/drillui_chat.py with --once to dispatch
ONE htmldrill verb non-interactively. No network, no Chrome, no LLM.

Runnable two ways:
    python3 -m pytest tests/test_m5.py
    PYTHONPATH=src python3 tests/test_m5.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C            # noqa: E402
from htmldrill.sidecar import Sidecar          # noqa: E402
from htmldrill.sources import fetch as F       # noqa: E402

SITE = Path(__file__).resolve().parent / "corpus" / "site"
INDEX = SITE / "index.html"
PAGE_A = SITE / "page-a.html"
PAGE_B = SITE / "page-b.html"
DRILLUI = ROOT / "tools" / "drillui_chat.py"


def _pdfdrill_available() -> bool:
    try:
        from htmldrill._core import ensure_pdfdrill
        ensure_pdfdrill()
        import docmodel  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# -- the fixture itself must be the interlinked multi-page site ---------------

def test_site_fixture_shape():
    """The crawl fixture: index links to two siblings + one external link, and the
    leaves link back. Guards the deterministic graph the crawl test relies on."""
    for p in (INDEX, PAGE_A, PAGE_B):
        assert p.exists(), f"missing crawl fixture {p}"
    idx = INDEX.read_text(encoding="utf-8")
    assert "page-a.html" in idx and "page-b.html" in idx
    assert "https://external.example.org" in idx           # the external link
    assert "index.html" in PAGE_A.read_text(encoding="utf-8")   # back-link


# -- A. crawl: bounded same-origin frontier over file:// ----------------------

def test_crawl_visits_internal_pages_only():
    """Depth-1 crawl from index discovers EXACTLY the 2 internal siblings (index
    + a + b = 3 pages); the external https link is excluded; 2 links followed."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(INDEX), work=work, depth=1, max_pages=20)
        out = C.cmd_crawl(ctx)
        assert "3 page(s)" in out
        assert "2 link(s) followed" in out
        sc = Sidecar(F.local_id_for(str(INDEX)), work=work)
        summary = json.loads(sc.read_blob("crawl.json") or "{}")
        ids = {p["id"] for p in summary["pages"]}
        assert F.local_id_for(PAGE_A.resolve().as_uri()) in ids
        assert F.local_id_for(PAGE_B.resolve().as_uri()) in ids
        # the external target was never fetched as a page
        for p in summary["pages"]:
            assert "external.example.org" not in p["final_url"]


def test_crawl_bounded_by_max():
    """--max caps the pages visited even when more internal links exist."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(INDEX), work=work, depth=3, max_pages=2)
        C.cmd_crawl(ctx)
        sc = Sidecar(F.local_id_for(str(INDEX)), work=work)
        summary = json.loads(sc.read_blob("crawl.json") or "{}")
        assert summary["pages_visited"] <= 2


def test_crawl_cycle_safe():
    """a<->b<->index is a cycle; the crawl must terminate and never revisit a page
    (each final_url appears at most once)."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(INDEX), work=work, depth=5, max_pages=20)
        C.cmd_crawl(ctx)
        sc = Sidecar(F.local_id_for(str(INDEX)), work=work)
        summary = json.loads(sc.read_blob("crawl.json") or "{}")
        finals = [p["final_url"] for p in summary["pages"]]
        assert len(finals) == len(set(finals)), "a page was visited twice"
        assert summary["pages_visited"] == 3   # index + a + b, no more


def test_crawl_idempotent():
    """A second crawl returns the cached summary (CRAWLED fact), not a re-walk."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(INDEX), work=work, depth=1)
        C.cmd_crawl(ctx)
        out2 = C.cmd_crawl(ctx)
        assert out2.startswith("cached crawl")


# -- B. retrieve: lexical ranking over a real model ---------------------------

def test_retrieve_finds_relevant_unit():
    """retrieve over page-b's model returns the ADS-B paragraph for an ADS-B
    query — the answer is in the page. Skip-safe if pdfdrill is absent."""
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        page_ctx = C.Ctx(url=str(PAGE_B), work=work)
        C.cmd_fetch(page_ctx)
        C.cmd_model(page_ctx)
        q = C.Ctx(url=str(PAGE_B), work=work,
                  query="decode ADS-B aviation transponder message", k=8)
        out = C.cmd_retrieve(q)
        assert "ADS-B" in out
        assert "Paragraph" in out


def test_retrieve_json_shape():
    """--json emits {question, units, prompt, title, subjects} for the wrapper."""
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        page_ctx = C.Ctx(url=str(PAGE_B), work=work)
        C.cmd_fetch(page_ctx)
        C.cmd_model(page_ctx)
        q = C.Ctx(url=str(PAGE_B), work=work, query="ADS-B transponder",
                  k=8, as_json=True)
        obj = json.loads(C.cmd_retrieve(q))
        assert set(obj) >= {"question", "units", "prompt", "title", "subjects"}
        assert obj["question"] == "ADS-B transponder"
        assert isinstance(obj["units"], list)


def test_retrieve_requires_model():
    """retrieve with no model is a clear error (offline; never fetches)."""
    with tempfile.TemporaryDirectory() as work:
        page_ctx = C.Ctx(url=str(PAGE_B), work=work)
        C.cmd_fetch(page_ctx)                  # fetched but NOT modelled
        q = C.Ctx(url=str(PAGE_B), work=work, query="anything")
        try:
            C.cmd_retrieve(q)
            assert False, "expected a FileNotFoundError for the missing model"
        except FileNotFoundError as e:
            assert "model" in str(e).lower()


# -- C. chatlog: append + show round-trip -------------------------------------

def test_chatlog_append_and_show():
    """An appended turn round-trips through chat.jsonl: the show view reflects the
    question, answer, and grounding units; the line shape mirrors pdfdrill."""
    with tempfile.TemporaryDirectory() as work:
        page_ctx = C.Ctx(url=str(PAGE_B), work=work)
        C.cmd_fetch(page_ctx)
        ask = C.Ctx(url=str(PAGE_B), work=work, ask="What does page B decode?",
                    answer="ADS-B and Mode S messages.", units="obj_1,obj_2",
                    model_name="claude")
        out = C.cmd_chatlog(ask)
        assert "appended turn 1" in out
        # raw JSONL line has the pdfdrill-shaped keys
        sc = Sidecar(F.local_id_for(str(PAGE_B)), work=work)
        line = json.loads((sc.blob_path("chat.jsonl")).read_text().strip())
        assert set(line) == {"question", "answer", "units", "model", "ts"}
        assert line["units"] == ["obj_1", "obj_2"]
        # show view
        show = C.cmd_chatlog(C.Ctx(url=str(PAGE_B), work=work))
        assert "1 turn(s)" in show
        assert "What does page B decode?" in show
        assert "obj_1, obj_2" in show


def test_chatlog_appends_multiple():
    """Two appends produce two turns; counts and order are stable."""
    with tempfile.TemporaryDirectory() as work:
        C.cmd_fetch(C.Ctx(url=str(PAGE_A), work=work))
        for i in (1, 2):
            C.cmd_chatlog(C.Ctx(url=str(PAGE_A), work=work,
                                ask=f"Q{i}", answer=f"A{i}"))
        show = C.cmd_chatlog(C.Ctx(url=str(PAGE_A), work=work))
        assert "2 turn(s)" in show
        assert "Q1" in show and "Q2" in show


def test_chatlog_show_empty():
    """Showing a target with no log is graceful prose, not a crash."""
    with tempfile.TemporaryDirectory() as work:
        C.cmd_fetch(C.Ctx(url=str(PAGE_A), work=work))
        out = C.cmd_chatlog(C.Ctx(url=str(PAGE_A), work=work))
        assert "no chat log yet" in out


# -- D. drillui --once: launch + single-dispatch smoke test -------------------

def _drillui_once(work: str, doc: str, once: str, tool: str = "htmldrill"):
    env = dict(os.environ)
    env["HTMLDRILL_WORK"] = work
    env["DRILLUI_HTMLDRILL"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(DRILLUI), doc, "--tool", tool, "--once", once],
        capture_output=True, text=True, env=env, timeout=120)


def test_drillui_once_dispatches_one_htmldrill_command():
    """drillui --tool htmldrill --once status launches, dispatches ONE htmldrill
    verb non-interactively, and returns its prose (exit 0). Does NOT exercise the
    TUI or the LLM call (out of scope by design)."""
    uri = PAGE_B.resolve().as_uri()
    with tempfile.TemporaryDirectory() as work:
        # build a target the dispatched verb can report on
        C.cmd_fetch(C.Ctx(url=uri, work=work))
        p = _drillui_once(work, uri, "status")
        assert p.returncode == 0, f"drillui --once failed: {p.stderr}"
        assert "facts:" in p.stdout and "FETCHED" in p.stdout


def test_drillui_once_autodetects_htmldrill_for_html_url():
    """With no --tool, an http(s)/file/.html doc auto-detects the htmldrill
    backend and the verb still runs."""
    uri = PAGE_A.resolve().as_uri()
    with tempfile.TemporaryDirectory() as work:
        C.cmd_fetch(C.Ctx(url=uri, work=work))
        env = dict(os.environ)
        env["HTMLDRILL_WORK"] = work
        env["DRILLUI_HTMLDRILL"] = str(ROOT)
        p = subprocess.run(
            [sys.executable, str(DRILLUI), uri, "--once", "status"],
            capture_output=True, text=True, env=env, timeout=120)
        assert p.returncode == 0, f"auto-detect dispatch failed: {p.stderr}"
        assert "FETCHED" in p.stdout


def test_drillui_once_retrieve_returns_prose():
    """The smoke path can also run a no-LLM `retrieve` verb end-to-end."""
    if not _pdfdrill_available():
        print("    (skip: no pdfdrill docmodel)", end="")
        return
    uri = PAGE_B.resolve().as_uri()
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=uri, work=work)
        C.cmd_fetch(ctx)
        C.cmd_model(ctx)
        p = _drillui_once(work, uri, "retrieve ADS-B transponder")
        assert p.returncode == 0, f"drillui --once retrieve failed: {p.stderr}"
        assert "ADS-B" in p.stdout


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
