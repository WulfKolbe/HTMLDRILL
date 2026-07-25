"""inline — the pure-Python self-contained archiver (the stdlib alternative to the
monolith Rust binary for `single`).

All tests run OFFLINE via an injected fetcher — the inliner never touches the
network here. They pin the load-bearing claims: stylesheets/images/CSS-url become
data: URIs, external scripts are dropped, failures are skipped (not fatal), and no
external http(s) asset reference survives a successful inline.

    python3 -m pytest tests/test_inline.py
    PYTHONPATH=src python3 tests/test_inline.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.sources import inline as INLINE      # noqa: E402


def _fake_fetch(table):
    def _f(url):
        if url not in table:
            raise FileNotFoundError(url)
        return table[url]
    return _f


def test_inlines_image_stylesheet_and_css_url():
    base = "https://ex.com/page.html"
    fetch = _fake_fetch({
        "https://ex.com/logo.png": (b"\x89PNG\r\n\x1a\nDATA", "image/png"),
        "https://ex.com/app.css": (b"body{background:url(bg.jpg)}", "text/css"),
        "https://ex.com/bg.jpg": (b"\xff\xd8\xffJPEG", "image/jpeg"),
    })
    html = ('<link rel="stylesheet" href="app.css">'
            '<img src="logo.png" alt="x">'
            '<p>hi</p>')
    out, stats = INLINE.inline_assets(html, base, fetch=fetch)
    assert "data:image/png;base64," in out            # image inlined
    assert "<style>" in out and 'href="app.css"' not in out   # link → style
    assert "data:image/jpeg;base64," in out           # css url() inlined recursively
    # no external http asset refs left behind
    assert "logo.png" not in out and "app.css" not in out and "bg.jpg" not in out
    assert stats["assets"] >= 2 and stats["stylesheets"] == 1 and stats["failed"] == 0


def test_at_import_is_followed():
    base = "https://ex.com/"
    fetch = _fake_fetch({
        "https://ex.com/main.css": (b'@import "sub.css"; p{color:red}', "text/css"),
        "https://ex.com/sub.css": (b"h1{font-weight:bold}", "text/css"),
    })
    out, stats = INLINE.inline_assets('<link rel="stylesheet" href="main.css">', base, fetch=fetch)
    assert "font-weight:bold" in out and "color:red" in out    # both levels inlined
    assert "@import" not in out


def test_external_scripts_dropped_inline_kept():
    base = "https://ex.com/"
    fetch = _fake_fetch({})
    html = ('<script src="https://cdn/x.js"></script>'
            '<script>var kept=1;</script><p>body</p>')
    out, _ = INLINE.inline_assets(html, base, fetch=fetch)
    assert 'src="https://cdn/x.js"' not in out          # external script dropped
    assert "var kept=1;" in out                          # inline script kept
    # with strip_scripts, inline is removed too
    out2, _ = INLINE.inline_assets(html, base, fetch=fetch, strip_scripts=True)
    assert "var kept=1;" not in out2


def test_failed_asset_is_skipped_not_fatal():
    base = "https://ex.com/"
    fetch = _fake_fetch({})                              # every fetch 404s
    html = '<img src="missing.png"><p>still here</p>'
    out, stats = INLINE.inline_assets(html, base, fetch=fetch)
    assert "still here" in out                           # no exception raised
    assert 'src="missing.png"' in out                    # original ref left in place
    assert stats["failed"] == 1 and stats["assets"] == 0


def test_existing_data_uris_untouched():
    base = "https://ex.com/"
    fetch = _fake_fetch({})
    html = '<img src="data:image/gif;base64,R0lGOD">'
    out, stats = INLINE.inline_assets(html, base, fetch=fetch)
    assert out == html and stats["failed"] == 0          # no re-fetch of a data: URI


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
