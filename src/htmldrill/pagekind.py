"""pagekind — the classifier and the policy resolver. YAML-driven, total.

CONTRACT (stages 2 and 3 of four; see docs/CR-pagekind-lattice.md)

    classify(features)          -> dict[dim, Verdict(value, confidence, rule_id)]
    resolve(vector, exclude=()) -> Plan(steps, terminal, matched_row, unresolved_dims)

Both are TOTAL. ``classify({})`` yields a complete vector — every dimension
present, every value ``unknown``, every confidence 0.0. ``resolve`` always
returns a Plan; when the vector is underdetermined the plan names the CHEAPEST
capability that would resolve a missing dimension, and says which dimensions
are missing. "I do not know yet, and here is the cheapest way to find out" is
a first-class result, not a fallthrough.

This module contains NO regex literals and NO page-genre identifiers. Every
pattern lives in ``detectors.py``; every genre is a point in the lattice
described by ``pagekind.yaml``. Adding a page genre is a YAML row — if a case
cannot be expressed as a row, the DIMENSION SET is wrong and that is a design
conversation, not a branch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import yaml

from .detectors import NOT_OBSERVED

SPEC_PATH = Path(__file__).resolve().parent / "pagekind.yaml"

UNKNOWN = "unknown"
CATCH_ALL = "po.underdetermined"

#: comparator keys a ``when:`` clause may use on a numeric feature
_COMPARATORS = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}


class Verdict(NamedTuple):
    """One dimension's reading. Tuple-compatible with ``(value, conf, rule_id)``."""
    value: str
    confidence: float
    rule_id: str


@dataclass(frozen=True)
class Plan:
    """What to do about a vector, and what remains unknown about it."""
    steps: list[str] = field(default_factory=list)
    terminal: str = "UNDERDETERMINED"
    matched_row: str = CATCH_ALL
    unresolved_dims: list[str] = field(default_factory=list)

    @property
    def is_underdetermined(self) -> bool:
        return self.terminal == "UNDERDETERMINED"


@lru_cache(maxsize=1)
def load_spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def dimensions() -> list[str]:
    return list(load_spec()["dimensions"])


# ---------------------------------------------------------------------------
# Constraint satisfaction (stage 2's only primitive)
# ---------------------------------------------------------------------------

def _is_number(v: Any) -> bool:
    """A real number. ``bool`` is excluded: True == 1 in Python, and letting a
    boolean satisfy a numeric threshold is exactly the kind of silent type
    coercion this design exists to prevent."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scalar_equal(observed: Any, want: Any) -> bool:
    """Equality with the bool/int conflation closed off in both directions."""
    if isinstance(want, bool) or isinstance(observed, bool):
        return isinstance(want, bool) and isinstance(observed, bool) and observed == want
    return observed == want


def satisfies(observed: Any, want: Any) -> bool:
    """Does an OBSERVED feature value satisfy one ``when:`` constraint?

    ``NOT_OBSERVED`` satisfies nothing, ever — including a constraint that asks
    for ``false``. Not-measured is not a measurement.
    """
    if observed is NOT_OBSERVED:
        return False
    if want == "*":
        return True
    if isinstance(want, dict):
        if not want:
            # `feature: {}` is not a wildcard — it is a typo that would match
            # every measured number silently. Only a rule-level empty `when:`
            # is unconditional, and that is handled in `rule_fires`.
            raise ValueError("empty comparator dict in pagekind.yaml — a "
                             "constraint must name at least one of "
                             f"{sorted(_COMPARATORS)}")
        if not _is_number(observed):
            return False
        for op, threshold in want.items():
            fn = _COMPARATORS.get(op)
            if fn is None:
                raise ValueError(f"unknown comparator {op!r} in pagekind.yaml")
            if not fn(observed, threshold):
                return False
        return True
    if isinstance(want, list):
        return any(_scalar_equal(observed, w) for w in want)
    return _scalar_equal(observed, want)


def rule_fires(when: dict, features: dict) -> bool:
    """A rule fires when EVERY constraint it names is satisfied. An empty
    ``when:`` is unconditional — that is what makes each dimension total."""
    for feature_id, want in when.items():
        if not satisfies(features.get(feature_id, NOT_OBSERVED), want):
            return False
    return True


# ---------------------------------------------------------------------------
# Stage 2 — classify
# ---------------------------------------------------------------------------

def classify(features: dict | None = None) -> dict[str, Verdict]:
    """features -> one Verdict per declared dimension. First matching rule wins.

    Total by construction: T1 (enforced by ``tools/pagekind_validate.py``)
    guarantees every dimension's rule list ends with an unconditional rule
    yielding ``unknown``, so no dimension can ever be absent from the result.
    """
    spec = load_spec()
    feats = features or {}
    vector: dict[str, Verdict] = {}
    for dim in spec["dimensions"]:
        for rule in spec["rules"][dim]:
            if rule_fires(rule["when"], feats):
                vector[dim] = Verdict(rule["value"], float(rule.get("conf", 0.0)), rule["id"])
                break
        else:                                    # pragma: no cover - T1 forbids this
            raise AssertionError(
                f"dimension {dim!r} has no unconditional fallback rule — "
                f"pagekind.yaml violates T1; run tools/pagekind_validate.py")
    return vector


# ---------------------------------------------------------------------------
# Stage 3 — policy resolution
# ---------------------------------------------------------------------------

def vector_values(vector: dict) -> dict[str, str]:
    """Accept either a classifier vector (Verdicts) or a bare dim->value dict.
    Any dimension the caller omitted reads as ``unknown``, never as absent."""
    out: dict[str, str] = {}
    for dim in load_spec()["dimensions"]:
        got = vector.get(dim, UNKNOWN)
        if isinstance(got, tuple):               # Verdict or plain 3-tuple
            got = got[0]
        out[dim] = got if isinstance(got, str) else UNKNOWN
    return out


def row_matches(row_when: dict, values: dict[str, str]) -> bool:
    """A policy row matches when every dimension it names is satisfied. An
    absent key is a wildcard; a list is a membership test.

    Deliberately identical in semantics to ``tools/pagekind_validate.py``'s
    ``matches`` — T2's exhaustive coverage proof only transfers to runtime if
    runtime matching agrees with the matching the proof was computed under.
    ``test_pk_policy.py`` asserts that agreement over all 94,500 vectors.
    """
    for dim, want in row_when.items():
        if want == "*":
            continue
        got = values.get(dim)
        satisfied = got in want if isinstance(want, list) else got == want
        if not satisfied:
            return False
    return True


def cheapest_probe(unresolved: Iterable[str],
                   exclude: Iterable[str] = ()) -> str | None:
    """The lowest-cost capability that would resolve at least one unresolved
    dimension. Ties break on capability name so the plan is deterministic.

    ``exclude`` is the set of capabilities already run for this target. Without
    it a zero-cost probe that fails to resolve its dimension would be proposed
    forever; T3 guarantees a probe EXISTS, not that it succeeds.
    """
    wanted, skip = set(unresolved), set(exclude)
    candidates = [
        (meta.get("cost", 0), name)
        for name, meta in load_spec()["capabilities"].items()
        if name not in skip and wanted & set(meta.get("resolves", []))
    ]
    return min(candidates)[1] if candidates else None


def resolve(vector: dict, exclude: Iterable[str] = ()) -> Plan:
    """vector -> Plan. First matching policy row wins.

    Reaching the catch-all is not a failure and not a default answer: it means
    the vector is underdetermined, and the plan says so out loud, names the
    dimensions that are missing, and leads with the cheapest probe that would
    close one of them.
    """
    spec = load_spec()
    values = vector_values(vector)
    unresolved = [d for d in spec["dimensions"] if values[d] == UNKNOWN]

    for row in spec["policy"]:
        if not row_matches(row["when"], values):
            continue
        if row["id"] == CATCH_ALL:
            break
        return Plan(steps=list(row.get("plan") or []),
                    terminal=row["terminal"],
                    matched_row=row["id"],
                    unresolved_dims=unresolved)

    probe = cheapest_probe(unresolved, exclude)
    return Plan(steps=[probe] if probe else [],
                terminal="UNDERDETERMINED",
                matched_row=CATCH_ALL,
                unresolved_dims=unresolved)


def capability_cost(name: str) -> int:
    return int(load_spec()["capabilities"].get(name, {}).get("cost", 0))
