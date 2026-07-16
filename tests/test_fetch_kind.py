"""fetch must not text-decode (and destroy) a non-HTML body.

Regression for the arXiv-/pdf-URL bug: fetch stored an application/pdf response
as raw.html via utf-8 decode, replacing every non-utf8 byte with U+FFFD (811k of
them in a 7-page PDF) — leaving neither valid HTML nor a valid PDF. Now non-HTML
is stored losslessly as raw bytes and labelled, and snapshot commands refuse it.

    python3 -m pytest tests/test_fetch_kind.py
    PYTHONPATH=src python3 tests/test_fetch_kind.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.sources import fetch as F              # noqa: E402
from htmldrill import commands as C                   # noqa: E402
from htmldrill.sidecar import Sidecar                 # noqa: E402

# A minimal but real PDF (valid header + %%EOF). The bytes after %PDF are the
# binary comment a real PDF carries — the ones utf-8 decoding would clobber.
_PDF = (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<</Type/Catalog>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n")
_HTML = b"<!doctype html><title>hi</title><h1>Real HTML</h1>"


def test_content_kind_by_magic():
    assert F.content_kind("application/pdf", _PDF) == "pdf"
    assert F.content_kind("text/html", _HTML) == "html"
    # magic bytes WIN over a wrong/missing content-type (the local-file case,
    # where fetch hardcodes text/html) — a PDF served as text/html is still a PDF.
    assert F.content_kind("text/html; charset=utf-8", _PDF) == "pdf"
    assert F.content_kind("", b"PK\x03\x04zip") == "zip"
    assert F.content_kind("", b"\x89PNG\r\n\x1a\n") == "image"


def test_fetch_pdf_stored_losslessly():
    """fetch a local .pdf: it must land as raw.pdf bytes-intact, NOT raw.html."""
    with tempfile.TemporaryDirectory() as work:
        pdf = Path(work) / "doc.pdf"
        pdf.write_bytes(_PDF)
        ctx = C.Ctx(url=str(pdf), work=work)
        out = C.cmd_fetch(ctx)
        assert "PDF, not HTML" in out or "kind: pdf" in out
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.get_evidence("content_kind") == "pdf"
        assert sc.get_evidence("raw_blob") == "raw.pdf"
        # bytes are byte-identical to the source — no corruption
        assert sc.blob_path("raw.pdf").read_bytes() == _PDF
        assert not sc.has_blob("raw.html")


def test_snapshot_command_refuses_pdf():
    """meta/links etc. must refuse a PDF snapshot with a clear route, not parse it."""
    with tempfile.TemporaryDirectory() as work:
        pdf = Path(work) / "doc.pdf"
        pdf.write_bytes(_PDF)
        ctx = C.Ctx(url=str(pdf), work=work)
        C.cmd_fetch(ctx)
        for cmd in (C.cmd_meta, C.cmd_links, C.cmd_outline):
            try:
                cmd(ctx)
                assert False, f"{cmd.__name__} should refuse a PDF"
            except ValueError as e:
                assert "not HTML" in str(e) and "pdfdrill" in str(e)


def test_html_still_works():
    """The common path is unchanged: an HTML body is still text raw.html."""
    with tempfile.TemporaryDirectory() as work:
        page = Path(work) / "p.html"
        page.write_bytes(_HTML)
        ctx = C.Ctx(url=str(page), work=work)
        C.cmd_fetch(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.get_evidence("content_kind") == "html"
        assert "Real HTML" in (sc.read_blob("raw.html") or "")
        assert "Real HTML" in C.cmd_meta(ctx) or "p-" in C.cmd_meta(ctx)


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
