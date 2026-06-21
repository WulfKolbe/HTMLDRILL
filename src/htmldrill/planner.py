"""planner — the prerequisite state machine (mirrors PDFDRILL/CHATDRILL planner.py).

Each command declares ``requires:`` + a ``done_when:`` detector in commands.yaml.
``plan()`` computes, from the current sidecar/artifact state, the ordered list of
missing steps to run before a target. Idempotency is structural: a command whose
``done_when`` already holds is simply skipped.

  htmldrill steps <cmd> <id>      show the chain: what's done, what would run
  htmldrill <cmd> <id> --ensure   auto-run the missing prerequisites, then <cmd>

SAFETY: ``ensure`` runs whatever a target lists in ``requires:`` — it does NOT
inspect offline-ness. So NETWORK steps (``fetch``, ``render``) are deliberately
NEVER placed in any command's ``requires:``; snapshot commands instead hard-check
the ``FETCHED`` fact and tell the user to run ``fetch`` first. Only offline,
idempotent steps (parsing, ``model`` ingestion) are ever declared as prerequisites.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .sidecar import Sidecar

MANIFEST_PATH = Path(__file__).resolve().parent / "commands.yaml"


@lru_cache(maxsize=1)
def load_manifest() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_graph(manifest: dict) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(requires, done_when) maps from the manifest."""
    requires: dict[str, list[str]] = {}
    done: dict[str, str] = {}
    for c in manifest.get("commands", []):
        if c.get("requires"):
            requires[c["name"]] = list(c["requires"])
        if c.get("done_when"):
            done[c["name"]] = c["done_when"]
    return requires, done


def plan(target: str, requires: dict[str, list[str]], satisfied: set[str]) -> list[str]:
    """Ordered steps to satisfy `target`: each UNSATISFIED transitive prerequisite
    (deepest first), then `target` itself (which always runs). Cycle-safe."""
    out: list[str] = []

    def add(cmd: str, stack: frozenset) -> None:
        if cmd in stack:                      # cycle guard
            return
        for dep in requires.get(cmd, []):
            if dep in satisfied or dep in out:
                continue
            add(dep, stack | {cmd})
            if dep not in out:
                out.append(dep)

    add(target, frozenset())
    out.append(target)
    return out


def detect(spec: str, sc: Sidecar) -> bool:
    """Is a `done_when` spec satisfied for this target?
      fact:NAME    the sidecar carries that fact
      model        the model.docmodel.json artifact exists
      artifact:REL a blob at REL exists"""
    if spec == "model":
        return sc.has_blob("model.docmodel.json")
    if spec.startswith("fact:"):
        return sc.has(spec[5:])
    if spec.startswith("artifact:"):
        return sc.has_blob(spec[len("artifact:"):])
    return False


def satisfied_set(done: dict[str, str], sc: Sidecar) -> set[str]:
    return {cmd for cmd, spec in done.items() if detect(spec, sc)}


def resolve_steps(target: str, sc: Sidecar) -> tuple[list[str], set[str]]:
    """(ordered steps incl. target, satisfied set) for `target` on this target."""
    man = load_manifest()
    requires, done = load_graph(man)
    sat = satisfied_set(done, sc)
    return plan(target, requires, sat), sat


def describe(target: str, sc: Sidecar) -> str:
    steps, sat = resolve_steps(target, sc)
    prereqs = steps[:-1]
    tid = sc.local_id[:12]
    if not prereqs:
        return (f"`{target}` for {tid}…: prerequisites satisfied "
                f"({', '.join(sorted(sat)) or 'none required'}) — runs directly.")
    return (f"`{target}` for {tid}… would run, in order:\n  "
            + " → ".join(steps)
            + f"\n  (missing prerequisites auto-inserted by --ensure: "
            f"{', '.join(prereqs)}; already done: {', '.join(sorted(sat)) or 'none'})")


def ensure(target: str, sc: Sidecar, handlers: dict, ctx) -> list[str]:
    """Run the missing OFFLINE prerequisites of `target` (not `target` itself) via
    their handlers, in order. Each handler is idempotent, so this is safe even if
    a step turns out to be already done."""
    steps, _ = resolve_steps(target, sc)
    ran: list[str] = []
    for step in steps[:-1]:                   # everything except the target
        fn = handlers.get(step)
        if fn is None:
            continue
        out = fn(ctx)
        if out:
            print(out)
        ran.append(step)
    return ran
