"""U1 — the detector registry.

These tests are written against the YAML contract, not against the detector
implementations: the feature-id list is INTROSPECTED from pagekind.yaml, so a
new ``when:`` clause with no detector behind it fails here rather than silently
degrading to a wrong verdict at classification time.

Runnable two ways:
    python3 -m pytest tests/test_pk_detectors.py
    PYTHONPATH=src python3 -m pytest tests/test_pk_detectors.py
"""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import detectors as D          # noqa: E402

SPEC = yaml.safe_load((ROOT / "src" / "htmldrill" / "pagekind.yaml").read_text("utf-8"))


def yaml_feature_ids() -> set[str]:
    """Every feature id any rule depends on — read from the YAML, never hardcoded."""
    ids: set[str] = set()
    for rules in SPEC["rules"].values():
        for rule in rules:
            ids.update(rule["when"].keys())
    return ids


# -- (a) coverage: the registry is exactly the YAML's demand ------------------

def test_every_yaml_feature_id_has_a_detector():
    missing = yaml_feature_ids() - set(D.REGISTRY)
    assert not missing, f"YAML references features with no detector: {sorted(missing)}"


def test_no_detector_is_unused_by_the_yaml():
    """The other direction: a detector nothing consumes is dead observation."""
    orphans = set(D.REGISTRY) - yaml_feature_ids()
    assert not orphans, f"detectors registered but referenced by no rule: {sorted(orphans)}"


# -- (b) the sentinel is structurally distinct from False --------------------

def test_not_observed_is_not_false():
    assert D.NOT_OBSERVED is not False
    assert D.NOT_OBSERVED is not None
    assert D.NOT_OBSERVED is D._NotObserved()          # singleton


def test_not_observed_refuses_to_be_truth_tested():
    """The defect being replaced was an unmeasured value reading as False in a
    boolean expression. Make that a crash, not a wrong answer."""
    with pytest.raises(TypeError):
        bool(D.NOT_OBSERVED)
    with pytest.raises(TypeError):
        if D.NOT_OBSERVED:                              # pragma: no branch
            pass


# -- (c) totality: empty input observes nothing, and does not explode --------

def test_empty_input_yields_all_not_observed():
    feats = D.collect("", {}, "", None)
    assert set(feats) == set(D.REGISTRY)
    wrong = {k: v for k, v in feats.items() if v is not D.NOT_OBSERVED}
    assert not wrong, f"these detectors invented a finding from no input: {wrong}"


def test_none_input_yields_all_not_observed():
    feats = D.collect(None, None, None, None)
    assert all(v is D.NOT_OBSERVED for v in feats.values())


def test_collect_never_raises_on_hostile_markup():
    junk = "<html><a href=<<<>>>><script>" + ("<" * 5000) + "</p></body>"
    feats = D.collect(junk, {"content-type": "text/html"}, "http://x/", 200)
    assert set(feats) == set(D.REGISTRY)


# -- (b) each detector against a fixture that should trip it ------------------

def _obs(html="", headers=None, url="", status=None, rendered_html=None,
         robots_txt_disallow=None, body_kind=None):
    return D.Observation(html, headers, url, status, rendered_html,
                         robots_txt_disallow, body_kind)


def one(feature: str, **kw):
    """Observe a single feature, through the registry (not the private fn)."""
    return D.REGISTRY[feature](_obs(**kw))


PAGE = "<!doctype html><html><head><title>T</title></head><body><p>hi</p></body></html>"


def test_transport_detectors():
    assert one("status", status=404) == 404
    assert one("status", status=None) is D.NOT_OBSERVED
    assert one("status", status=True) is D.NOT_OBSERVED       # bool is not a status
    assert one("content_type", headers={"Content-Type": "text/html; charset=utf-8"}) \
        == "text/html"
    assert one("content_type_class", headers={"content-type": "application/pdf"}) == "other"
    assert one("content_type_class", headers={"content-type": "application/json"}) == "json"
    assert one("content_type_class", headers={"content-type": "application/atom+xml"}) == "feed"
    assert one("content_type_class", headers={"content-type": "image/png"}) == "image"
    assert one("content_type_class", headers={"content-type": "application/zip"}) == "archive"
    assert one("content_type_class", headers={"content-type": "text/html"}) == "html"
    assert one("payload_not_html", headers={"content-type": "application/pdf"}) is True
    assert one("payload_not_html", headers={"content-type": "text/html"}) is False
    assert one("payload_not_html") is D.NOT_OBSERVED
    assert one("payload_is_data", headers={"content-type": "application/json"}) is True
    assert one("payload_is_data", headers={"content-type": "text/html"}) is False


def test_body_kind_carries_htmldrills_own_magic_byte_verdict():
    """Observed live: a 32-page PDF served as `binary/octet-stream`. The sniff
    already exists in fetch.content_kind(); the lattice just had to read it."""
    assert one("body_kind") is D.NOT_OBSERVED
    assert one("body_kind", body_kind="pdf") == "pdf"
    assert one("body_kind", body_kind="PDF") == "pdf"          # normalised
    assert one("body_kind", body_kind="html") == "html"
    # and it OUTRANKS a lying content-type for the not-html question
    assert one("payload_not_html", headers={"content-type": "text/html"},
               body_kind="pdf") is True
    assert one("payload_not_html", headers={"content-type": "binary/octet-stream"},
               body_kind="html") is False


def test_session_and_identity_detectors():
    assert one("set_cookie_session", headers={"set-cookie": "PHPSESSID=ab; Path=/"}) is True
    assert one("set_cookie_session",
               headers={"set-cookie": "pref=1; Max-Age=99999"}) is False
    assert one("set_cookie_session") is D.NOT_OBSERVED
    assert one("query_significant", url="https://x.test/a?q=cats") is True
    assert one("query_significant", url="https://x.test/a?utm_source=n") is False
    assert one("query_significant", url="https://x.test/a") is False
    assert one("query_significant") is D.NOT_OBSERVED
    assert one("canonical_present",
               html='<html><head><link rel="canonical" href="https://x.test/a">'
                    '</head><body>b</body></html>') is True
    assert one("canonical_present", html=PAGE) is False


def test_access_detectors():
    # `noindex` is an INDEXING directive and must NOT reach access=blocked.
    # Silently refusing a retrievable page is a worse failure than a wrong
    # verdict, because the refusal reads as correctness.
    assert one("robots_txt_disallow", headers={"X-Robots-Tag": "noindex"}) is D.NOT_OBSERVED
    assert one("robots_txt_disallow",
               html='<html><head><meta name="robots" content="noindex,nofollow">'
                    '</head><body>b</body></html>') is D.NOT_OBSERVED
    assert one("robots_txt_disallow") is D.NOT_OBSERVED
    # only an explicit fetch-level verdict determines it
    assert one("robots_txt_disallow", robots_txt_disallow=True) is True
    assert one("robots_txt_disallow", robots_txt_disallow=False) is False
    assert one("consent_marker",
               html='<html><body><div id="onetrust-banner-sdk">x</div></body></html>') is True
    assert one("consent_marker", html=PAGE) is False
    assert one("login_form",
               html='<html><body><form><input type="password" name="p">'
                    '</form></body></html>') is True
    assert one("login_form", html=PAGE) is False


def test_delivery_detectors():
    assert one("body_starts_html", html=PAGE) is True
    assert one("body_starts_html", html='{"a":1}') is False
    assert one("fw_marker", html='<html><body><script>__NEXT_DATA__={}</script></body></html>') \
        is True
    assert one("fw_marker", html=PAGE) is False
    assert one("static_text_chars", html=PAGE) == len("T hi")
    assert one("script_ratio", html="<html><script>" + "x" * 400 + "</script></html>") > 0.5
    assert one("script_ratio", html=PAGE) == 0.0
    assert one("repeated_blocks",
               html="<html><body>" + '<div class="card">c</div>' * 5 + "</body></html>") is True
    assert one("repeated_blocks", html=PAGE) is False


def test_locus_detectors():
    assert one("embedded_viewer",
               html='<html><body><iframe src="/files/a.pdf"></iframe></body></html>') is True
    assert one("embedded_viewer", html=PAGE) is False
    assert one("jsonld_blocks",
               html='<html><head><script type="application/ld+json">{"@type":"Thing"}'
                    "</script></head><body>b</body></html>") == 1
    assert one("jsonld_blocks", html=PAGE) == 0
    assert one("render_delta_chars", html=PAGE) is D.NOT_OBSERVED
    assert one("render_delta_chars", html=PAGE,
               rendered_html="<html><body><p>" + "x" * 900 + "</p></body></html>") > 500


def test_completeness_detectors():
    assert one("scroll_sentinel",
               html='<html><body><div class="infinite-scroll"></div></body></html>') is True
    assert one("scroll_sentinel", html=PAGE) is False
    assert one("role_feed", html='<html><body><div role="feed"></div></body></html>') is True
    assert one("role_feed", html=PAGE) is False
    assert one("rel_next",
               html='<html><head><link rel="next" href="/p2"></head><body>b</body></html>') \
        is True
    assert one("rel_next", html=PAGE) is False
    assert one("pager_controls",
               html='<html><body><nav class="pagination"></nav></body></html>') is True
    assert one("pager_controls", html=PAGE) is False
    assert one("lazy_media",
               html='<html><body><img src="a.png" loading="lazy"></body></html>') is True
    assert one("lazy_media", html=PAGE) is False
    # a lazy image is ALSO a split, of a different kind — the count must not
    # include it, or `collapsed` silently means "any hidden content at all"
    assert one("collapsed_bodies",
               html="<html><body><details><summary>s</summary>body</details>"
                    '<img src="a.png" loading="lazy" data-src="b.png">'
                    "</body></html>") == 1
    assert one("collapsed_bodies", html=PAGE) == 0


def test_richness_detectors():
    assert one("soft_error_marker",
               html="<html><head><title>404 Not Found</title></head><body>b</body></html>") \
        is True
    assert one("soft_error_marker", html=PAGE) is False
    assert one("link_count",
               html='<html><body><a href="/a">a</a><a href="/b">b</a></body></html>') == 2
    assert one("link_count", html=PAGE) == 0
    hub = "<html><body>" + "".join(f'<a href="/{i}">link{i}</a>' for i in range(25)) \
          + "</body></html>"
    assert one("link_text_ratio", html=hub) >= 0.5
    assert one("link_text_ratio", html=PAGE) == 0.0
    assert one("media_ratio", html="<html><body>" + "<img src=a>" * 9 + "<p>t</p>"
                                   "</body></html>") >= 0.5
    assert one("media_ratio", html=PAGE) == 0.0


def test_every_detector_has_at_least_one_positive_fixture():
    """A detector that no test can trip is unfalsifiable — the gate must have
    teeth on all 29, not just the convenient ones."""
    covered = set()
    for name, fn in sorted(D.REGISTRY.items()):
        covered.add(name)
    src = Path(__file__).read_text("utf-8")
    untested = {n for n in covered if f'"{n}"' not in src}
    assert not untested, f"features with no fixture in this file: {sorted(untested)}"


# -- the substring pre-filter must be a pure optimisation --------------------

PREFILTERED = [
    ("consent_marker", "_CONSENT_TOKENS", "_CONSENT"),
    ("login_form", "_LOGIN_TOKENS", "_LOGIN"),
    ("scroll_sentinel", "_SCROLL_SENTINEL_TOKENS", "_SCROLL_SENTINEL"),
    ("role_feed", "_ROLE_FEED_TOKENS", "_ROLE_FEED"),
    ("pager_controls", "_PAGER_TOKENS", "_PAGER"),
    ("lazy_media", "_LAZY_TOKENS", "_LAZY"),
    ("embedded_viewer", "_VIEWER_TOKENS", "_VIEWER"),
]

CORPUS_PAGES = sorted((ROOT / "tests").rglob("*.html"))


def test_prefilter_never_changes_a_verdict():
    """`_hit` skips the regex when no necessary token is present. A token that
    is not truly necessary would silently turn a real match into False — the
    exact failure class this module exists to prevent. So every guarded pattern
    is re-run UNGUARDED over every corpus page and the two must agree."""
    assert CORPUS_PAGES, "no corpus pages found — this test would be vacuous"
    disagreements = []
    for page in CORPUS_PAGES:
        html = page.read_text("utf-8", errors="replace")
        obs = D.Observation(html, {"content-type": "text/html"}, "https://x.test/p", 200)
        for feature, _tokens, pattern_name in PREFILTERED:
            pattern = getattr(D, pattern_name)
            guarded = D.REGISTRY[feature](obs)
            unguarded = bool(pattern.search(html))
            if guarded != unguarded:
                disagreements.append(f"{page.name}:{feature} guarded={guarded} raw={unguarded}")
    assert not disagreements, "\n".join(disagreements)


def test_prefilter_agrees_on_adversarial_inputs_that_only_the_regex_can_judge():
    """Corpus pages mostly DON'T match; that direction is easy. These inputs
    contain a guard token but must still be judged by the pattern itself."""
    cases = [
        ("consent_marker", "<html><body>I ate a cookie yesterday.</body></html>", False),
        ("consent_marker", '<html><body><div class="cookie-banner">x</div></body></html>', True),
        ("login_form", "<html><body><p>password strength advice</p></body></html>", False),
        ("login_form", '<html><body><input type="password"></body></html>', True),
        ("role_feed", "<html><body><p>an RSS feed</p></body></html>", False),
        ("role_feed", '<html><body><div role="feed"></div></body></html>', True),
        # the token "page=" is present, so the guard defers to the pattern —
        # which requires [?&]page=<digits> and correctly declines
        ("pager_controls", "<html><body><p>turn the page=here</p></body></html>", False),
        ("pager_controls", '<html><body><a href="/x?page=2">2</a></body></html>', True),
        ("embedded_viewer", "<html><body><p>a pdf is a document</p></body></html>", False),
        ("embedded_viewer", '<html><body><iframe src="/a.pdf"></iframe></body></html>', True),
        ("lazy_media", "<html><body><p>a lazy afternoon</p></body></html>", False),
        ("lazy_media", '<html><body><img src="a" loading="lazy"></body></html>', True),
        ("scroll_sentinel", "<html><body><p>the sentinel watched</p></body></html>", False),
        ("scroll_sentinel", '<html><body><div class="infinite-scroll"></div></body></html>', True),
    ]
    for feature, html, expected in cases:
        obs = D.Observation(html, {"content-type": "text/html"}, "https://x.test/p", 200)
        assert D.REGISTRY[feature](obs) is expected, f"{feature} on {html[:50]!r}"


def test_framework_prefilter_agrees_with_the_unguarded_scan():
    import re as _re
    for page in CORPUS_PAGES:
        html = page.read_text("utf-8", errors="replace")
        raw = [n for n, p in D._FRAMEWORK_PATTERNS if _re.search(p, html, _re.I)]
        guarded = D.guess_framework(html)
        expected = ", ".join(raw) if raw else "none detected"
        assert guarded == expected, f"{page.name}: {guarded!r} vs {expected!r}"


def test_every_framework_pattern_has_a_token_tuple():
    assert len(D._FRAMEWORK_TOKENS) == len(D._FRAMEWORK_PATTERNS)
