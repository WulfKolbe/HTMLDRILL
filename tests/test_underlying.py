"""U9 — locating the resource behind a viewer shell. Pure, offline.

The pdf.js-in-an-iframe pattern is not one site's quirk; it is how most
"read online" pages are built. These cover the shapes that actually occur,
including the ones where the answer must be NO — a page that merely mentions
a PDF is not a shell around one, and a false positive here sends the whole
pipeline off to fetch the wrong thing.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import underlying as U          # noqa: E402

BASE = "https://host.test/reader/chapters/pdf/10.5334/bbc.l"


def top(html, base=BASE):
    c = U.best(html, base)
    return c.url if c else None


# -- the observed case ------------------------------------------------------

def test_pdfjs_hypothesis_iframe_file_param():
    """Verbatim shape observed on www.ubiquitypress.com, 2026-07."""
    html = ('<iframe title="Hypothesis" class="brUrFo" '
            'src="/assets/hypothesis/web/viewer.html'
            '?file=/chapters/e/32/files/f25dff0c-9aa5-4b29-bed7-84c9ce5d371e.pdf">'
            "</iframe>")
    assert top(html) == ("https://host.test/chapters/e/32/files/"
                         "f25dff0c-9aa5-4b29-bed7-84c9ce5d371e.pdf")
    assert U.best(html, BASE).rule == "embed.param:iframe?file"


# -- the other shapes that occur in the wild --------------------------------

@pytest.mark.parametrize("html,expected", [
    # plain pdf.js viewer
    ('<iframe src="/pdfjs/web/viewer.html?file=/docs/a.pdf"></iframe>',
     "https://host.test/docs/a.pdf"),
    # absolute target
    ('<iframe src="/pdfjs/web/viewer.html?file=https://cdn.test/x/b.pdf"></iframe>',
     "https://cdn.test/x/b.pdf"),
    # percent-encoded target
    ('<iframe src="/viewer.html?file=%2Ffiles%2Fc%20d.pdf"></iframe>',
     "https://host.test/files/c%20d.pdf"),
    # Google Docs viewer, which uses ?url=
    ('<iframe src="https://docs.google.com/viewer?url=https://x.test/e.pdf&embedded=true">'
     "</iframe>", "https://x.test/e.pdf"),
    # <embed> pointing straight at the document
    ('<embed type="application/pdf" src="/files/f.pdf">', "https://host.test/files/f.pdf"),
    # <object data=…>
    ('<object data="/files/g.pdf" type="application/pdf"></object>',
     "https://host.test/files/g.pdf"),
    # explicit download link, no viewer at all
    ('<a download href="/files/h.pdf">Download</a>', "https://host.test/files/h.pdf"),
    # single-quoted attributes
    ("<iframe src='/viewer.html?file=/files/i.pdf'></iframe>", "https://host.test/files/i.pdf"),
    # unquoted attribute
    ("<iframe src=/files/j.pdf></iframe>", "https://host.test/files/j.pdf"),
    # a relative param resolves beside the VIEWER document (the base URL's last
    # segment `bbc.l` is a file, so `10.5334/` is the directory)
    ('<iframe src="viewer.html?file=k.pdf"></iframe>',
     "https://host.test/reader/chapters/pdf/10.5334/k.pdf"),
])
def test_common_viewer_shapes(html, expected):
    assert top(html) == expected


def test_epub_and_djvu_count_as_documents_too():
    assert top('<embed src="/files/a.epub">') == "https://host.test/files/a.epub"
    assert top('<a download href="/files/b.djvu">get</a>') == "https://host.test/files/b.djvu"


# -- the answers that must be NO --------------------------------------------

def test_a_page_that_merely_mentions_a_pdf_is_not_a_shell():
    assert top("<html><body><p>We also publish a PDF edition.</p></body></html>") is None


def test_an_ordinary_iframe_is_not_a_viewer():
    assert top('<iframe src="https://www.youtube.com/embed/xyz"></iframe>') is None
    assert top('<iframe src="/ads/banner.html"></iframe>') is None


def test_no_candidates_on_an_empty_or_broken_page():
    assert U.candidates("", BASE) == []
    assert U.candidates("<iframe><embed><object", BASE) == []


# -- ordering, tracing, and idempotence -------------------------------------

def test_the_viewer_parameter_outranks_a_bare_download_link():
    """Both point somewhere; the parameter is the more direct statement of
    'this is the document being displayed'."""
    html = ('<iframe src="/pdfjs/web/viewer.html?file=/real/chapter.pdf"></iframe>'
            '<a download href="/promo/brochure.pdf">Brochure</a>')
    assert top(html) == "https://host.test/real/chapter.pdf"
    rules = [c.rule for c in U.candidates(html, BASE)]
    assert rules[0].startswith("embed.param")


def test_a_recognised_viewer_bundle_scores_above_an_unknown_one():
    known = U.best('<iframe src="/pdfjs/web/viewer.html?file=/a.pdf"></iframe>', BASE)
    unknown = U.best('<iframe src="/custom/show.html?file=/a.pdf"></iframe>', BASE)
    assert known.confidence > unknown.confidence


def test_every_candidate_names_the_rule_that_found_it():
    html = ('<iframe src="/pdfjs/web/viewer.html?file=/a.pdf"></iframe>'
            '<embed src="/b.pdf"><a download href="/c.pdf">c</a>')
    found = U.candidates(html, BASE)
    assert len(found) == 3
    for c in found:
        assert c.rule and 0.0 < c.confidence <= 1.0
        assert c.url.startswith("https://host.test/")


def test_duplicate_targets_are_reported_once():
    html = ('<iframe src="/viewer.html?file=/a.pdf"></iframe>'
            '<a download href="/a.pdf">same</a>')
    assert [c.url for c in U.candidates(html, BASE)] == ["https://host.test/a.pdf"]


def test_extraction_is_pure_and_repeatable():
    html = '<iframe src="/pdfjs/web/viewer.html?file=/a.pdf"></iframe>'
    assert U.candidates(html, BASE) == U.candidates(html, BASE)


def test_no_base_url_leaves_targets_unresolved_rather_than_inventing_an_origin():
    html = '<iframe src="/pdfjs/web/viewer.html?file=/a.pdf"></iframe>'
    assert U.best(html, "").url == "/a.pdf"
