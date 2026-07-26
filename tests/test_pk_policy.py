"""U3 — policy resolution.

Three properties, in descending order of how much they would hurt if false:

  * the totality gate (T1/T2/T3) runs here, not only in CI;
  * the runtime matcher AGREES with the matcher T2's proof was computed under
    — otherwise the exhaustive coverage proof is about a different program;
  * an underdetermined vector names the cheapest probe that would close a gap,
    and a terminal vector plans no network at all.
"""
import importlib.util
import itertools
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import pagekind as PK           # noqa: E402

SPEC = yaml.safe_load((ROOT / "src" / "htmldrill" / "pagekind.yaml").read_text("utf-8"))
DIMS = list(SPEC["dimensions"])
CAPS = SPEC["capabilities"]


def _load_validator():
    """Import tools/pagekind_validate.py as a module (it is a script, not a package)."""
    path = ROOT / "tools" / "pagekind_validate.py"
    spec = importlib.util.spec_from_file_location("pagekind_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load_validator()


# -- (a) T1 / T2 / T3 as pytest cases ---------------------------------------

def test_t1_every_dimension_rule_list_is_total():
    ok, problems = V.t1_rules_total(V.load())
    assert ok, "\n".join(problems)


def test_t2_policy_covers_every_fully_determined_vector():
    ok, problems, n = V.t2_policy_total(V.load())
    assert ok, "\n".join(problems)
    assert n == 94500, f"the lattice changed size ({n}); re-read the coverage claim"


def test_t3_every_dimension_is_recoverable_by_some_capability():
    ok, problems = V.t3_dimensions_recoverable(V.load())
    assert ok, "\n".join(problems)


def test_runtime_matcher_agrees_with_the_validator_over_the_whole_lattice():
    """T2 proves coverage using ``pagekind_validate.matches``. If ``pagekind.
    row_matches`` disagreed anywhere, the proof would be about a program that
    is not the one that ships. Brute-forced, no sampling."""
    value_sets = [[v for v in SPEC["dimensions"][d] if v != "unknown"] for d in DIMS]
    rows = SPEC["policy"]
    checked = 0
    for combo in itertools.product(*value_sets):
        vector = dict(zip(DIMS, combo))
        for row in rows:
            assert V.matches(row["when"], vector) == PK.row_matches(row["when"], vector), \
                f"matcher disagreement on {row['id']} for {vector}"
        checked += 1
    assert checked == 94500


def test_no_fully_determined_vector_reaches_the_catch_all_at_runtime():
    """T2 restated through the shipping code path."""
    value_sets = [[v for v in SPEC["dimensions"][d] if v != "unknown"] for d in DIMS]
    for combo in itertools.product(*value_sets):
        plan = PK.resolve(dict(zip(DIMS, combo)))
        assert plan.matched_row != PK.CATCH_ALL, f"uncovered: {dict(zip(DIMS, combo))}"
        assert plan.terminal != "UNDERDETERMINED"
        assert plan.unresolved_dims == []


def test_a_dimension_a_row_does_not_name_is_a_wildcard():
    """po.auth names only `access`. It must match whatever the other six say —
    including a vector that does not mention them at all."""
    assert PK.row_matches({"access": "auth_required"}, {"access": "auth_required"}) is True
    assert PK.row_matches({}, {}) is True
    assert PK.row_matches({"access": "auth_required"}, {"access": "open"}) is False
    assert PK.row_matches({"access": ["blocked", "rate_limited"]},
                          {"access": "rate_limited"}) is True


def test_a_dimension_the_caller_omitted_reads_as_unknown_not_as_absent():
    values = PK.vector_values({"access": "open"})
    assert set(values) == set(DIMS)
    assert values["access"] == "open"
    assert all(values[d] == "unknown" for d in DIMS if d != "access")


def test_a_junk_dimension_value_degrades_to_unknown_not_to_a_finding():
    values = PK.vector_values({"access": 17, "payload": None, "delivery": ["static"]})
    assert values["access"] == "unknown"
    assert values["payload"] == "unknown"
    assert values["delivery"] == "unknown"


def test_a_classifier_vector_and_a_bare_value_dict_resolve_identically():
    feats = {"fw_marker": True, "static_text_chars": 12}
    from_vector = PK.resolve(PK.classify(feats))
    from_values = PK.resolve({d: v.value for d, v in PK.classify(feats).items()})
    assert from_vector == from_values


# -- (b) the cheapest-probe property, one case per dimension -----------------

def test_cheapest_probe_per_dimension():
    """For every dimension there is a probe, and it is the cheapest one that
    resolves that dimension. One case per dimension, read from the YAML."""
    for dim in DIMS:
        chosen = PK.cheapest_probe({dim})
        assert chosen is not None, f"no capability resolves {dim!r}"
        assert dim in CAPS[chosen]["resolves"]
        best = min(meta["cost"] for meta in CAPS.values() if dim in meta["resolves"])
        assert CAPS[chosen]["cost"] == best, \
            f"{dim}: chose {chosen} (cost {CAPS[chosen]['cost']}), cheapest is {best}"


def test_cheapest_probe_is_deterministic_on_a_cost_tie():
    """Two capabilities can share a cost; the plan must not flap between runs."""
    assert PK.cheapest_probe({"identity"}) == PK.cheapest_probe({"identity"})
    assert PK.cheapest_probe({"payload"}) == "fetch"          # cost 1, name-ordered


def test_an_all_unknown_vector_is_underdetermined_and_names_a_probe():
    plan = PK.resolve({})
    assert plan.terminal == "UNDERDETERMINED"
    assert plan.matched_row == PK.CATCH_ALL
    assert sorted(plan.unresolved_dims) == sorted(DIMS)
    assert plan.steps == ["static_parse"]                     # cost 0, resolves 3 dims


def test_the_named_probe_always_resolves_something_and_is_minimal():
    """Every underdetermined vector's first step must intersect the unresolved
    set, at minimal cost among the capabilities that do."""
    for hidden in itertools.combinations(DIMS, 3):
        vector = {d: SPEC["dimensions"][d][0] for d in DIMS if d not in hidden}
        plan = PK.resolve(vector)
        if not plan.is_underdetermined:
            continue
        assert plan.steps, f"underdetermined with no probe: {vector}"
        step = plan.steps[0]
        unresolved = set(plan.unresolved_dims)
        assert set(CAPS[step]["resolves"]) & unresolved
        best = min(meta["cost"] for meta in CAPS.values()
                   if set(meta["resolves"]) & unresolved)
        assert CAPS[step]["cost"] == best


def test_excluding_already_run_probes_terminates_instead_of_looping():
    """A zero-cost probe that fails to resolve its dimension must not be
    proposed forever. T3 guarantees a probe exists, not that it succeeds."""
    already: set[str] = set()
    for _ in range(len(CAPS) + 1):
        plan = PK.resolve({}, exclude=already)
        if not plan.steps:
            break
        already.add(plan.steps[0])
    else:                                                     # pragma: no cover
        raise AssertionError("probe selection never exhausted — it can loop")
    assert PK.resolve({}, exclude=set(CAPS)).steps == []


# -- (c) terminals plan nothing ---------------------------------------------

def test_auth_required_is_a_terminal_and_never_plans_a_fetch():
    for payload in SPEC["dimensions"]["payload"]:
        for delivery in SPEC["dimensions"]["delivery"]:
            plan = PK.resolve({"access": "auth_required", "payload": payload,
                               "delivery": delivery})
            assert plan.terminal == "NOT_RETRIEVABLE_AUTH"
            assert plan.matched_row == "po.auth"
            assert plan.steps == [], f"planned {plan.steps} against an auth wall"


def test_blocked_and_rate_limited_are_terminals_with_no_steps():
    for access in ("blocked", "rate_limited"):
        plan = PK.resolve({"access": access})
        assert plan.terminal == "NOT_RETRIEVABLE_BLOCKED" and plan.steps == []


def test_no_terminal_row_with_an_empty_plan_ever_acquires_steps():
    """A row that declares ``plan: []`` must produce zero steps even when the
    vector has unknown dimensions — the probe logic belongs to the catch-all
    alone, or a determined 'cannot retrieve' would quietly become a retry."""
    for row in SPEC["policy"]:
        if row["id"] == PK.CATCH_ALL or row.get("plan"):
            continue
        vector = {dim: (want[0] if isinstance(want, list) else want)
                  for dim, want in row["when"].items()}
        plan = PK.resolve(vector)
        assert plan.steps == [], f"{row['id']} produced steps {plan.steps}"


def test_underdetermined_vectors_report_their_gaps_by_name():
    plan = PK.resolve({"access": "open", "payload": "html"})
    assert plan.is_underdetermined
    assert "delivery" in plan.unresolved_dims and "locus" in plan.unresolved_dims
    assert "access" not in plan.unresolved_dims


# -- a finding worth keeping visible ----------------------------------------

def test_session_bound_identity_has_a_consequence_and_is_not_a_dead_dimension():
    """`identity` used to be classified, probed for, and consumed by nothing.
    It now earns its place: `session_bound` means re-running `fetch` will NOT
    reproduce this snapshot, which contradicts htmldrill's premise that re-runs
    are deterministic and cumulative. That deserves its own terminal."""
    consuming = [r["id"] for r in SPEC["policy"] if "identity" in r["when"]]
    assert consuming == ["po.session"], f"identity is consumed by {consuming}"
    plan = PK.resolve({d: SPEC["dimensions"][d][0] for d in DIMS if d != "identity"}
                      | {"identity": "session_bound"})
    assert plan.terminal == "RETRIEVED_VOLATILE"
    assert plan.matched_row == "po.session"


def test_a_session_bound_page_is_still_refused_when_it_is_also_walled():
    """po.session must not shadow the refusal terminals — a session cookie on
    an auth wall is still an auth wall."""
    assert PK.resolve({"identity": "session_bound",
                       "access": "auth_required"}).matched_row == "po.auth"
    assert PK.resolve({"identity": "session_bound",
                       "access": "blocked"}).matched_row == "po.blocked"
    assert PK.resolve({"identity": "session_bound",
                       "payload": "pdf"}).matched_row == "po.pdf"


def test_blocked_is_unreachable_from_markup_alone():
    """`access: blocked` may only come from an observed fetch-level refusal.
    No combination of page markup can produce it."""
    import htmldrill.detectors as DET
    feats = DET.collect('<html><head><meta name="robots" content="noindex">'
                        "</head><body>text</body></html>",
                        {"content-type": "text/html", "x-robots-tag": "noindex"},
                        "https://x.test/p", 200)
    assert PK.classify(feats)["access"].value == "open"
    with_verdict = DET.collect("<html><body>t</body></html>",
                               {"content-type": "text/html"}, "https://x.test/p", 200,
                               robots_txt_disallow=True)
    assert PK.classify(with_verdict)["access"].value == "blocked"


def test_po_session_shadows_1200_vectors_including_every_consent_wall():
    """MEASURED CONSEQUENCE of placing po.session above the retrieval rows.

    `identity` describes how REPRODUCIBLE a snapshot is; it does not describe
    how to retrieve one. Sitting in the first-match-wins chain above po.consent
    / po.infinite / po.paged / po.spa, it replaces the retrieval STRATEGY with
    `static_parse` for every session-bound page — so a session-bound infinite
    feed returns page one, and a session-bound SPA returns the empty shell.

    Counted exhaustively so the cost of the placement is a number, not an
    opinion. If po.session moves (or volatility becomes an annotation on the
    matched row rather than a competing row), this test fails and gets revisited."""
    dims = list(SPEC["dimensions"])
    value_sets = [[v for v in SPEC["dimensions"][d] if v != "unknown"] for d in dims]
    shadowed = {}
    for combo in itertools.product(*value_sets):
        values = dict(zip(dims, combo))
        if values["identity"] != "session_bound":
            continue
        row = PK.resolve(values).matched_row
        if row != "po.session":
            continue
        without = dict(values, identity="stable")
        shadowed[PK.resolve(without).matched_row] = \
            shadowed.get(PK.resolve(without).matched_row, 0) + 1
    assert sum(shadowed.values()) == 1200
    assert shadowed["po.consent"] == 600, "consent walls are the largest bucket lost"
    for strategy in ("po.spa", "po.infinite", "po.paged", "po.windowed"):
        assert shadowed[strategy] > 0, f"{strategy} is shadowed too"
