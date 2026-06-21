"""L0 tests — drive the parse layer + the fetch→snapshot→introspect loop offline.

Runnable two ways:
    python3 -m pytest tests/test_l0.py          # if pytest is installed
    PYTHONPATH=src python3 tests/test_l0.py     # plain stdlib runner (no deps)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.parse import html as H          # noqa: E402
from htmldrill import commands as C            # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample.html"
SAMPLE = FIXTURE.read_text(encoding="utf-8")


def test_meta_and_title():
    c = H.collect(SAMPLE)
    meta = H.extract_meta(c)
    assert c.title == "Sample Page — htmldrill"
    assert meta.get("charset") == "utf-8"
    assert meta.get("description", "").startswith("A sample page")


def test_opengraph():
    og = H.extract_opengraph(H.collect(SAMPLE))
    assert og.get("og:title") == "Sample Page"
    assert "twitter:card" in og


def test_canonical_and_feeds():
    c = H.collect(SAMPLE)
    assert H.extract_canonical(c)["canonical"] == "https://example.com/sample"
    feeds = H.extract_feeds(c)
    assert feeds and feeds[0]["href"] == "https://example.com/feed.xml"


def test_links_internal_external():
    c = H.collect(SAMPLE)
    links = H.extract_links(c, base_url="https://example.com/sample")
    internal = {u for u, _ in links["internal"]}
    external = {u for u, _ in links["external"]}
    assert "https://example.com/about" in internal
    assert "https://external.example.org/page" in external


def test_jsonld_ok_and_error():
    blocks = H.extract_jsonld(H.collect(SAMPLE))
    assert len(blocks) == 2
    assert blocks[0]["ok"] and blocks[0]["data"]["@type"] == "Article"
    assert blocks[1]["ok"] is False          # malformed block surfaced, not raised


def test_microdata():
    items = H.extract_microdata(H.collect(SAMPLE))
    assert items and items[0]["itemtype"] == "https://schema.org/Product"
    assert "name" in items[0]["props"] and "price" in items[0]["props"]


def test_outline():
    heads = H.extract_outline(H.collect(SAMPLE))
    assert (1, "Main Title") in heads
    assert sum(1 for lvl, _ in heads if lvl == 2) == 2


def test_fetch_snapshot_and_hidden_links():
    """End-to-end on the real command layer: fetch a local file, then links
    must surface the URLs hidden in data-* and JS string literals."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        out = C.cmd_fetch(ctx)
        assert "fetched" in out
        # snapshot commands now work with no network
        assert "Sample Page" in C.cmd_meta(ctx)
        links_out = C.cmd_links(ctx)
        assert "hidden.example.net" in links_out      # JS string literal
        assert "api.example.com" in links_out         # data-api attribute
        assert "visible anchors" in links_out          # "… NOT visible anchors"
        assert "Article" in C.cmd_jsonld(ctx)


def test_text_static_no_chrome():
    """`text` must work off the static snapshot with no browser involved."""
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_fetch(ctx)
        out = C.cmd_text(ctx)
        assert "static" in out and "Main Title" in out


def test_render_dom_compare():
    """M1 end-to-end — skipped (counts as pass) when no Chrome is installed."""
    from htmldrill.sources import render as R
    if not R.find_chrome():
        print("    (skip: no chrome)", end="")
        return
    with tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=str(FIXTURE), work=work)
        C.cmd_fetch(ctx)
        rep = C.cmd_render(ctx)
        assert "rendered" in rep
        assert "Main Title" in C.cmd_text(ctx)        # now from the rendered DOM
        dom = C.cmd_dom(ctx)
        assert "rendered DOM" in dom and "vs static" in dom
        cmp = C.cmd_compare(ctx)
        assert "fidelity" in cmp and "verdict" in cmp


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
