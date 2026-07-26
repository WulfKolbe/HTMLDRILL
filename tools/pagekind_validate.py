#!/usr/bin/env python3
"""pagekind_validate — the totality gate for pagekind.yaml.

This is the instrument that makes "no uncovered case" a MEASURED property
instead of a claim. It answers exactly three questions:

  T1  Is every dimension's rule list total? (ends with an unconditional rule
      whose value is `unknown`, and every rule value is in the dimension's
      declared value set.)
  T2  Is the policy table total over fully-determined vectors? (every vector
      with NO `unknown` component matches a row that is not the catch-all.)
      Brute-forced over the entire cross-product -- no sampling.
  T3  Is every dimension recoverable? (for each dimension there exists at
      least one capability declaring it in `resolves:`, so an UNDERDETERMINED
      vector always has a next probe to name.)

Exit 0 only if all three hold. Run it in CI next to skillsync.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parent.parent / "src" / "htmldrill" / "pagekind.yaml"
CATCH_ALL = "po.underdetermined"


def load() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def matches(row_when: dict, vector: dict) -> bool:
    """A policy row matches when every constraint it names is satisfied.
    An absent key is a wildcard; a list is a membership test."""
    for dim, want in row_when.items():
        if want == "*":
            continue
        got = vector.get(dim)
        if isinstance(want, list):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


def t1_rules_total(spec: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    dims = spec["dimensions"]
    for dim, rules in spec["rules"].items():
        if dim not in dims:
            problems.append(f"T1: rules for undeclared dimension {dim!r}")
            continue
        allowed = set(dims[dim])
        for r in rules:
            if r["value"] not in allowed:
                problems.append(
                    f"T1: rule {r['id']} yields {r['value']!r}, not in {dim} value set")
        last = rules[-1]
        if last["when"]:
            problems.append(
                f"T1: dimension {dim!r} last rule {last['id']} is CONDITIONAL "
                f"-- an unmatched page would fall through with no value")
        if last["value"] != "unknown":
            problems.append(
                f"T1: dimension {dim!r} fallback yields {last['value']!r}, "
                f"must be 'unknown' (not-measured must not masquerade as a finding)")
    for dim in dims:
        if dim not in spec["rules"]:
            problems.append(f"T1: dimension {dim!r} has no rule list at all")
    return not problems, problems


def t2_policy_total(spec: dict) -> tuple[bool, list[str], int]:
    dims = spec["dimensions"]
    policy = spec["policy"]
    names = list(dims)
    # fully-determined == no `unknown` component
    value_sets = [[v for v in dims[d] if v != "unknown"] for d in names]
    total = 0
    uncovered: list[str] = []
    for combo in itertools.product(*value_sets):
        total += 1
        vector = dict(zip(names, combo))
        hit = None
        for row in policy:
            if matches(row["when"], vector):
                hit = row["id"]
                break
        if hit is None or hit == CATCH_ALL:
            if len(uncovered) < 5:
                uncovered.append(f"T2: {vector} -> {hit or 'NO MATCH'}")
    if uncovered:
        uncovered.append(f"T2: (showing first 5 of the uncovered vectors)")
    return not uncovered, uncovered, total


def t3_dimensions_recoverable(spec: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    caps = spec["capabilities"]
    for dim in spec["dimensions"]:
        resolvers = [c for c, meta in caps.items() if dim in meta.get("resolves", [])]
        if not resolvers:
            problems.append(
                f"T3: dimension {dim!r} is resolved by NO capability -- an "
                f"UNDERDETERMINED vector on {dim} could never name a next probe")
    return not problems, problems


def main() -> int:
    spec = load()
    failures: list[str] = []

    ok1, p1 = t1_rules_total(spec)
    print(f"T1 rule totality       : {'PASS' if ok1 else 'FAIL'} "
          f"({len(spec['rules'])} dimensions checked)")
    failures += p1

    ok2, p2, n = t2_policy_total(spec)
    print(f"T2 policy coverage     : {'PASS' if ok2 else 'FAIL'} "
          f"({n} fully-determined vectors enumerated, exhaustive)")
    failures += p2

    ok3, p3 = t3_dimensions_recoverable(spec)
    print(f"T3 probe recoverability: {'PASS' if ok3 else 'FAIL'} "
          f"({len(spec['capabilities'])} capabilities checked)")
    failures += p3

    if failures:
        print("\n".join("  " + f for f in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
