"""U6 — the lattice corpus. One fixture per distinct REGION of the lattice.

Every case asserts the VECTOR (and the policy row it lands on), never a branch
and never a page genre. The fixtures are named by their lattice coordinates for
the same reason: naming one after a retail or editorial page TYPE would smuggle
a genre back into the repository as a filename.

The last case is the important one. A page that is genuinely underdetermined
must SAY SO and name its next probe, rather than guessing — a corpus of only
confidently-classifiable pages would prove nothing about the property this
whole design exists to provide.
"""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import detectors as D           # noqa: E402
from htmldrill import pagekind as PK           # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus" / "pagekind"
SPEC = yaml.safe_load((ROOT / "src" / "htmldrill" / "pagekind.yaml").read_text("utf-8"))

HTML_200 = ({"content-type": "text/html; charset=utf-8"}, 200)


def vector_for(fixture: str | None, headers: dict, status: int, url: str = "https://x.test/p"):
    html = (CORPUS / fixture).read_text("utf-8") if fixture else ""
    return PK.classify(D.collect(html, headers, url, status))


def values(fixture, headers=HTML_200[0], status=HTML_200[1], url="https://x.test/p"):
    return {d: v.value for d, v in vector_for(fixture, headers, status, url).items()}


# (fixture, headers, status, expected dimension values, expected policy row, expected plan)
CASES = [
    ("delivery-client-rendered-shell.html", *HTML_200,
     {"delivery": "client_rendered", "payload": "html", "access": "open"},
     "po.spa", ["render"]),

    ("delivery-hydrated-jsonld.html", *HTML_200,
     {"delivery": "hydrated", "locus": "structured_data", "payload": "html"},
     "po.hydrate", ["structured_parse", "static_parse"]),

    ("completeness-paginated-list.html", *HTML_200,
     {"completeness": "paginated", "delivery": "templated"},
     "po.paged", ["crawl_pages"]),

    ("completeness-infinite-feed.html", *HTML_200,
     {"completeness": "infinite"},
     "po.infinite", ["capture_scroll"]),

    ("completeness-collapsed-details.html", *HTML_200,
     {"completeness": "collapsed"},
     "po.collapse", ["static_parse"]),

    ("richness-hub.html", *HTML_200,
     {"richness": "hub"},
     "po.hub", ["static_parse"]),

    ("richness-soft-error.html", *HTML_200,
     {"richness": "error", "access": "open"},
     "po.error", []),

    ("access-consent-wall.html", *HTML_200,
     {"access": "consent_wall"},
     "po.consent", ["static_parse", "structured_parse"]),

    ("access-auth-wall.html", {"content-type": "text/html"}, 401,
     {"access": "auth_required"},
     "po.auth", []),

    (None, {"content-type": "application/pdf"}, 200,
     {"payload": "pdf", "delivery": "non_html"},
     "po.pdf", []),

    ("locus-external-resource-viewer.html", *HTML_200,
     {"locus": "external_resource"},
     "po.viewer", ["fetch_underlying"]),
]


@pytest.mark.parametrize("fixture,headers,status,expected,row,plan",
                         CASES, ids=[c[4] for c in CASES])
def test_lattice_region(fixture, headers, status, expected, row, plan):
    vector = vector_for(fixture, headers, status)
    got = {d: v.value for d, v in vector.items()}
    for dim, want in expected.items():
        assert got[dim] == want, (
            f"{fixture}: {dim} is {got[dim]!r}, expected {want!r} "
            f"(rule {vector[dim].rule_id}); full vector {got}")
    resolved = PK.resolve(vector)
    assert resolved.matched_row == row, f"{fixture}: landed on {resolved.matched_row}"
    assert resolved.steps == plan


@pytest.mark.parametrize("fixture,headers,status,expected,row,plan",
                         CASES, ids=[c[4] for c in CASES])
def test_every_region_is_fully_traceable(fixture, headers, status, expected, row, plan):
    """Every dimension of every corpus vector cites a real rule from the YAML."""
    known = {r["id"] for rules in SPEC["rules"].values() for r in rules}
    for dim, v in vector_for(fixture, headers, status).items():
        assert v.rule_id in known
        assert v.value in SPEC["dimensions"][dim]


def test_the_regions_are_actually_distinct():
    """A corpus whose fixtures collapse onto one region proves nothing."""
    rows = {c[4] for c in CASES}
    assert len(rows) == len(CASES), "two fixtures share a policy row"


# -- the case that matters --------------------------------------------------

def test_a_genuinely_underdetermined_page_says_so_and_names_its_next_probe():
    vector = vector_for("underdetermined.html", *HTML_200)
    plan = PK.resolve(vector)
    assert plan.terminal == "UNDERDETERMINED"
    assert plan.matched_row == PK.CATCH_ALL
    assert "delivery" in plan.unresolved_dims
    assert plan.steps, "reported UNDERDETERMINED without naming a next probe"
    assert set(SPEC["capabilities"][plan.steps[0]]["resolves"]) & set(plan.unresolved_dims)


def test_the_underdetermined_page_does_not_guess_a_delivery():
    """The old heuristic would have called this 'no render needed' with
    confidence. The lattice reports `unknown` at confidence 0.0 instead."""
    vector = vector_for("underdetermined.html", *HTML_200)
    assert vector["delivery"].value == "unknown"
    assert vector["delivery"].confidence == 0.0
    assert vector["delivery"].rule_id == "dl.default"


# -- MEASURED CONSEQUENCE of dl.lowjs, kept as a test rather than a paragraph --

def test_dl_lowjs_claims_a_delivery_for_a_page_with_no_content_at_all():
    """`dl.lowjs` fires on {fw_marker: false, script_ratio < 0.25}. A page with
    no scripts has ratio 0.0, so an EMPTY document satisfies it and is reported
    `static` at confidence 0.5, routed to a RETRIEVED terminal.

    A ratio is only scale-free where its denominator is meaningful. Over ~13
    characters of markup, script_ratio measures nothing — so this rule converts
    "no evidence" into a confident value, which is the failure mode the whole
    lattice exists to remove, wearing a ratio instead of a magic number.

    Asserted so the defect is visible and so fixing it FAILS here rather than
    passing silently. Not a rule I invented a threshold to paper over."""
    empty = PK.classify(D.collect("<html></html>", *HTML_200, "https://x.test/p"))
    assert empty["delivery"].value == "static"
    assert empty["delivery"].rule_id == "dl.lowjs"
    assert PK.resolve(empty).terminal == "RETRIEVED"


def test_the_underdetermined_region_survives_only_as_a_script_ratio_band():
    """After dl.lowjs, a fetched HTML page can only be underdetermined on
    `delivery` inside 0.25 <= script_ratio < 0.5 with thin text. Below 0.25 it
    is `static` (dl.lowjs), at/above 0.5 it is `client_rendered` (dl.thinshell).
    The corpus fixture sits in that band deliberately."""
    def delivery_at(script_len):
        # static text deliberately < 200 chars, so the thin-text precondition of
        # dl.thinshell holds and only script_ratio decides
        html = ("<!doctype html><html><head><title>N</title></head><body>"
                "<p>A short note.</p><script>" + "x" * script_len
                + "</script></body></html>")
        return PK.classify(D.collect(html, *HTML_200, "https://x.test/p"))["delivery"].value
    assert delivery_at(0) == "static"                     # ratio 0.00 -> dl.lowjs
    assert delivery_at(60) == "unknown"                   # ratio ~0.36 -> the band
    assert delivery_at(4000) == "client_rendered"         # ratio ~0.97 -> dl.thinshell


def test_the_original_defect_does_not_reproduce_on_the_corpus():
    """A page with NO recognised framework, thin static text and script-dominated
    markup is client_rendered. Under the old rule (`fw != "none detected" and
    ...`) it routed to 'no render needed' — the exact conflation being fixed."""
    html = (ROOT / "tests" / "corpus" / "noscript-spa.html").read_text("utf-8")
    feats = D.collect(html, *HTML_200)
    assert feats["fw_marker"] is False, "this fixture must have NO framework marker"
    vector = PK.classify(feats)
    assert vector["delivery"].value == "client_rendered"
    assert vector["delivery"].rule_id == "dl.thinshell"
    assert PK.resolve(vector).steps == ["render"]
