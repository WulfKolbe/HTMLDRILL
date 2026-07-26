"""U2 — the classifier.

The property under test is TOTALITY: no input, however empty or however
strange, may produce a vector with a missing dimension. `unknown` is a value.
"""
import random
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import pagekind as PK           # noqa: E402
from htmldrill.detectors import NOT_OBSERVED   # noqa: E402

SPEC = yaml.safe_load((ROOT / "src" / "htmldrill" / "pagekind.yaml").read_text("utf-8"))
DIMS = list(SPEC["dimensions"])


# -- (a) an empty feature dict still yields a complete vector -----------------

def test_empty_features_yield_complete_all_unknown_vector():
    v = PK.classify({})
    assert set(v) == set(DIMS)
    for dim, verdict in v.items():
        assert verdict.value == "unknown", f"{dim} invented {verdict.value!r} from nothing"
        assert verdict.confidence == 0.0
        assert verdict.rule_id.endswith(".default")


def test_classify_with_no_argument_is_the_same_as_empty():
    assert PK.classify() == PK.classify({})


def test_every_dimension_value_is_in_its_declared_value_set():
    v = PK.classify({})
    for dim, verdict in v.items():
        assert verdict.value in SPEC["dimensions"][dim]


# -- (b) NOT_OBSERVED never satisfies a `when:` ------------------------------

def test_not_observed_never_satisfies_a_constraint():
    assert PK.satisfies(NOT_OBSERVED, True) is False
    assert PK.satisfies(NOT_OBSERVED, False) is False       # the important one
    assert PK.satisfies(NOT_OBSERVED, "html") is False
    assert PK.satisfies(NOT_OBSERVED, [200, 404]) is False
    assert PK.satisfies(NOT_OBSERVED, {"lt": 200}) is False


def test_rule_level_empty_when_is_unconditional_even_with_no_features():
    """The fallback rules carry ``when: {}``. That is the RULE level, and it must
    fire regardless of what was observed — it is what makes each dimension total."""
    assert PK.rule_fires({}, {}) is True
    assert PK.rule_fires({}, {"status": NOT_OBSERVED}) is True


def test_an_empty_comparator_dict_is_an_authoring_error_not_a_wildcard():
    """``feature: {}`` would silently match every measured number. Refuse it."""
    with pytest.raises(ValueError):
        PK.satisfies(500, {})


def test_a_features_dict_of_all_not_observed_classifies_to_all_unknown():
    """The exact shape ``detectors.collect`` returns for an empty page."""
    feats = {fid: NOT_OBSERVED for fid in _all_feature_ids()}
    v = PK.classify(feats)
    assert all(x.value == "unknown" for x in v.values())


def test_a_false_observation_is_not_the_same_as_no_observation():
    """dl.thinshell needs ``fw_marker: false`` OBSERVED. An unmeasured
    fw_marker must not satisfy it — that conflation is the original defect."""
    measured = {"fw_marker": False, "static_text_chars": 10, "script_ratio": 0.9}
    unmeasured = {"fw_marker": NOT_OBSERVED, "static_text_chars": 10, "script_ratio": 0.9}
    assert PK.classify(measured)["delivery"].rule_id == "dl.thinshell"
    assert PK.classify(unmeasured)["delivery"].value == "unknown"


# -- type discipline: bool must not satisfy a numeric or list constraint -----

def test_bool_does_not_masquerade_as_a_number():
    assert PK.satisfies(True, {"gte": 1}) is False
    assert PK.satisfies(True, [200, 401]) is False
    assert PK.satisfies(1, True) is False


def test_numeric_comparators_behave():
    assert PK.satisfies(199, {"lt": 200}) is True
    assert PK.satisfies(200, {"lt": 200}) is False
    assert PK.satisfies(200, {"gte": 200}) is True
    assert PK.satisfies("200", {"gte": 200}) is False       # a string is not a number


# -- (c) first-match-wins, verified on a real two-rule conflict --------------

def test_first_match_wins_on_a_two_rule_conflict():
    """A 404 page whose markup is also thin satisfies BOTH ri.error and
    ri.thin. ri.error is listed first, so it must win."""
    feats = {"status": 404, "static_text_chars": 10, "link_count": 0}
    verdict = PK.classify(feats)["richness"]
    assert verdict.rule_id == "ri.error" and verdict.value == "error"


def test_first_match_wins_reflects_yaml_order_not_code_order():
    """Reorder the rule list in memory and the winner must follow the YAML."""
    feats = {"status": 404, "static_text_chars": 10, "link_count": 0}
    spec = PK.load_spec()
    original = spec["rules"]["richness"]
    try:
        spec["rules"]["richness"] = [r for r in original if r["id"] != "ri.error"]
        assert PK.classify(feats)["richness"].rule_id == "ri.thin"
    finally:
        spec["rules"]["richness"] = original
    assert PK.classify(feats)["richness"].rule_id == "ri.error"


def test_shell_and_hydrated_are_separated_by_the_text_threshold():
    shell = {"fw_marker": True, "static_text_chars": 12}
    hydrated = {"fw_marker": True, "static_text_chars": 4000}
    assert PK.classify(shell)["delivery"] == ("client_rendered", 0.9, "dl.shell")
    assert PK.classify(hydrated)["delivery"] == ("hydrated", 0.8, "dl.hydrate")


# -- (d) 200 randomised feature dicts all yield complete vectors -------------

def _all_feature_ids() -> list[str]:
    ids: set[str] = set()
    for rules in SPEC["rules"].values():
        for rule in rules:
            ids.update(rule["when"].keys())
    return sorted(ids)


def _random_value(rng):
    return rng.choice([
        NOT_OBSERVED, True, False, 0, 1, 199, 200, 5000, 0.0, 0.5, 1.0,
        "html", "json", "feed", "image", "archive", "other", "application/pdf",
        "", None, [], {},
    ])


def test_two_hundred_random_feature_dicts_all_classify_completely():
    rng = random.Random(20260726)
    ids = _all_feature_ids()
    for _ in range(200):
        feats = {fid: _random_value(rng) for fid in ids if rng.random() < 0.7}
        v = PK.classify(feats)
        assert set(v) == set(DIMS)
        for dim, verdict in v.items():
            assert verdict.value in SPEC["dimensions"][dim]
            assert 0.0 <= verdict.confidence <= 1.0
            assert verdict.rule_id


def test_random_junk_never_raises_and_never_omits_a_dimension():
    rng = random.Random(7)
    for _ in range(200):
        feats = {f"bogus_feature_{rng.randrange(50)}": _random_value(rng)
                 for _ in range(rng.randrange(10))}
        assert set(PK.classify(feats)) == set(DIMS)
