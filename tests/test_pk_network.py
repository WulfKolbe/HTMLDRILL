"""U7/U8/U10 — the refusal and payload terminals, against a real HTTP origin.

Every terminal that says "stop" used to be untested: `fetch` raised on any
non-2xx, so `ac.401` / `ac.429` could never be produced through the real path,
and `payload` trusted the declared Content-Type, so a PDF served as
`binary/octet-stream` never reached `po.pdf`.

These run against a localhost origin (tests/httpfixture.py) that reproduces
behaviour observed on www.ubiquitypress.com — no network, no live host.
"""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from htmldrill import commands as C            # noqa: E402
from htmldrill import pagekind as PK           # noqa: E402
from htmldrill.sidecar import Sidecar          # noqa: E402
from htmldrill.sources import fetch as F       # noqa: E402
from httpfixture import Origin, PDF_BYTES      # noqa: E402


# -- U7a: the request must carry Accept-Language ----------------------------

def test_fetch_sends_accept_language():
    """Observed on a live WAF: UA alone -> 403, UA+Accept -> 403,
    UA+Accept-Language -> 200. The header was simply absent."""
    with Origin() as o:
        F.fetch(o.url("/ok"))
        sent = o.seen_headers[-1]
        assert "Accept-Language" in sent, f"headers sent: {sorted(sent)}"


def test_a_waf_that_requires_accept_language_now_returns_200():
    with Origin() as o:
        res = F.fetch(o.url("/waf"))
        assert res.status == 200
        assert b"A Page" in res.body


# -- U7b: a non-2xx response is a RESULT, not an exception ------------------

@pytest.mark.parametrize("path,status", [
    ("/forbidden", 403), ("/unauthorized", 401),
    ("/ratelimited", 429), ("/missing", 404),
])
def test_non_2xx_returns_a_fetch_result_instead_of_raising(path, status):
    with Origin() as o:
        res = F.fetch(o.url(path))
        assert res.status == status
        assert res.body, "the error body must be kept — it is what gets classified"
        assert res.headers


def test_a_transport_failure_still_raises():
    """A refused connection is not a response. Only HTTP-level answers become
    results; there is nothing to classify when no server replied."""
    with pytest.raises(Exception):
        F.fetch("http://127.0.0.1:1/nothing", timeout=3)


# -- U7c: the refusal terminals, end to end through the real commands -------

def _fetch_and_classify(work, url):
    ctx = C.Ctx(url=url, work=work)
    C.cmd_fetch(ctx)
    out = C.cmd_classify(ctx)
    return out, Sidecar(C._resolve_id(ctx), work=work)


@pytest.mark.parametrize("path,access,terminal", [
    ("/forbidden", "auth_required", "NOT_RETRIEVABLE_AUTH"),
    ("/unauthorized", "auth_required", "NOT_RETRIEVABLE_AUTH"),
    ("/ratelimited", "rate_limited", "NOT_RETRIEVABLE_BLOCKED"),
])
def test_refusal_terminals_are_reachable_through_the_real_fetch_path(path, access, terminal):
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        out, sc = _fetch_and_classify(work, o.url(path))
        assert sc.get_evidence("pagekind")["access"]["value"] == access
        assert sc.get_evidence("pagekind_terminal") == terminal
        assert sc.get_evidence("pagekind_plan") == [], "a refusal must plan nothing"
        assert terminal in out


def test_a_404_classifies_as_error_and_plans_nothing():
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        _, sc = _fetch_and_classify(work, o.url("/missing"))
        assert sc.get_evidence("pagekind")["richness"]["value"] == "error"
        assert sc.get_evidence("pagekind_row") == "po.error"
        assert sc.get_evidence("pagekind_plan") == []


def test_fetch_reports_a_non_2xx_status_rather_than_implying_success():
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        out = C.cmd_fetch(C.Ctx(url=o.url("/forbidden"), work=work))
        assert "403" in out


# -- U8: magic bytes are authoritative for `payload` ------------------------

def test_a_pdf_served_as_octet_stream_still_classifies_as_pdf():
    """The observed ubiquitypress case: content-type `binary/octet-stream`.
    htmldrill's own content_kind() already calls this a pdf from magic bytes;
    the lattice must consume that verdict rather than the declared header."""
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        _, sc = _fetch_and_classify(work, o.url("/octet.pdf"))
        assert sc.get_evidence("content_type") == "binary/octet-stream"
        assert sc.get_evidence("content_kind") == "pdf"
        assert sc.get_evidence("pagekind")["payload"]["value"] == "pdf"
        assert sc.get_evidence("pagekind_row") == "po.pdf"


def test_the_octet_stream_pdf_no_longer_loops_on_fetch_underlying():
    """Before: payload=unknown, delivery=non_html -> po.nonhtml -> fetch_underlying,
    the very step that produced this resource. A fixed point that never reached
    po.pdf ('hand to pdfdrill')."""
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        _, sc = _fetch_and_classify(work, o.url("/octet.pdf"))
        assert sc.get_evidence("pagekind_plan") == []
        assert sc.get_evidence("pagekind_terminal") == "RETRIEVED"


def test_a_correctly_served_pdf_reaches_the_same_verdict():
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        _, a = _fetch_and_classify(work, o.url("/proper.pdf"))
        _, b = _fetch_and_classify(work, o.url("/octet.pdf"))
        assert a.get_evidence("pagekind")["payload"]["value"] == \
            b.get_evidence("pagekind")["payload"]["value"] == "pdf"


def test_magic_bytes_beat_a_lying_content_type_in_the_rule_order():
    from htmldrill import detectors as D
    v = PK.classify(D.collect("", {"content-type": "text/html"}, "https://x.test/a",
                              200, body_kind="pdf"))
    assert v["payload"].value == "pdf"
    assert v["payload"].rule_id.startswith("pl.magic")


# -- U9: the viewer shell resolves to its underlying resource ---------------

def test_a_viewer_shell_locates_the_pdf_behind_it():
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        out, sc = _fetch_and_classify(work, o.url("/viewer"))
        assert sc.get_evidence("pagekind")["locus"]["value"] == "external_resource"
        assert sc.get_evidence("pagekind_row") == "po.viewer"
        assert sc.get_evidence("pagekind_plan") == ["fetch_underlying"]
        found = C.cmd_underlying(C.Ctx(url=o.url("/viewer"), work=work))
        assert "/files/chapter-01.pdf" in found


def test_underlying_can_retrieve_the_resource_it_located():
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        C.cmd_fetch(C.Ctx(url=o.url("/viewer"), work=work))
        C.cmd_underlying(C.Ctx(url=o.url("/viewer"), work=work, follow=True))
        target = Sidecar(F.local_id_for(o.url("/files/chapter-01.pdf")), work=work)
        assert target.has("FETCHED")
        assert target.get_evidence("content_kind") == "pdf"
        assert target.blob_path("raw.pdf").read_bytes() == PDF_BYTES


def test_the_retrieved_underlying_resource_classifies_as_a_pdf_terminal():
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        C.cmd_fetch(C.Ctx(url=o.url("/viewer"), work=work))
        C.cmd_underlying(C.Ctx(url=o.url("/viewer"), work=work, follow=True))
        pdf_url = o.url("/files/chapter-01.pdf")
        out = C.cmd_classify(C.Ctx(url=pdf_url, work=work))
        sc = Sidecar(F.local_id_for(pdf_url), work=work)
        assert sc.get_evidence("pagekind_row") == "po.pdf"
        assert "pdfdrill" in out
