"""U4 — the `classify` command.

The anti-cheating instrument is the TRACE. Every dimension prints the rule id
that fired, so a verdict can be checked against the data that produced it — a
special case bolted on in Python would produce a line whose rule id does not
explain its value. These tests assert the trace is present and complete.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill import commands as C            # noqa: E402
from htmldrill import pagekind as PK           # noqa: E402
from htmldrill.sidecar import Sidecar          # noqa: E402

SPEC = yaml.safe_load((ROOT / "src" / "htmldrill" / "pagekind.yaml").read_text("utf-8"))
DIMS = list(SPEC["dimensions"])

# a page with too little signal to determine delivery / locus / completeness
UNDERDETERMINED_PAGE = ("<!doctype html><html><head><title>Note</title></head>"
                        '<body><p>A short note.</p><a href="/a">a</a></body></html>')
PROSE_PAGE = ("<!doctype html><html><head><title>Essay</title></head><body>"
              "<h1>Essay</h1><p>" + ("word " * 400) + "</p></body></html>")


def _fetched(work: str, html: str, name: str = "p.html"):
    """Establish a real snapshot through the normal fetch path."""
    path = Path(work) / name
    path.write_text(html, encoding="utf-8")
    ctx = C.Ctx(url=str(path), work=work)
    C.cmd_fetch(ctx)
    return ctx


def test_output_names_a_rule_id_for_every_dimension():
    with tempfile.TemporaryDirectory() as work:
        out = C.cmd_classify(_fetched(work, PROSE_PAGE))
        for dim in DIMS:
            assert dim in out, f"{dim} missing from the trace"
        fired = {line.split("rule ")[1].strip()
                 for line in out.splitlines() if "rule " in line}
        assert len(fired) == len(DIMS), f"expected one rule id per dimension, got {fired}"
        known = {r["id"] for rules in SPEC["rules"].values() for r in rules}
        assert fired <= known, f"trace cites rule ids not in the YAML: {fired - known}"


def test_output_prints_a_confidence_for_every_dimension():
    with tempfile.TemporaryDirectory() as work:
        out = C.cmd_classify(_fetched(work, PROSE_PAGE))
        assert out.count("conf ") == len(DIMS)


def test_underdetermined_page_names_its_unresolved_dimensions_in_the_prose():
    with tempfile.TemporaryDirectory() as work:
        out = C.cmd_classify(_fetched(work, UNDERDETERMINED_PAGE))
        assert "UNDERDETERMINED" in out
        assert "unresolved:" in out
        for dim in ("delivery", "locus", "completeness"):
            assert dim in out.split("unresolved:")[1], f"{dim} not named as unresolved"


def test_underdetermined_page_names_a_probe_it_has_not_already_run():
    """Proposing a step already spent is the same dishonesty as a wrong verdict."""
    with tempfile.TemporaryDirectory() as work:
        ctx = _fetched(work, UNDERDETERMINED_PAGE)
        out = C.cmd_classify(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        spent = set(sc.get_evidence("pagekind_spent"))
        proposed = sc.get_evidence("pagekind_plan")
        assert proposed, "underdetermined with no next probe"
        assert not (set(proposed) & spent), f"proposed {proposed}, already spent {spent}"
        assert "already spent:" in out


def test_plan_steps_print_their_cost():
    with tempfile.TemporaryDirectory() as work:
        ctx = _fetched(work, PROSE_PAGE)
        out = C.cmd_classify(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        for step in sc.get_evidence("pagekind_plan"):
            assert f"{step} (cost {PK.capability_cost(step)})" in out


def test_sidecar_evidence_carries_the_whole_vector():
    with tempfile.TemporaryDirectory() as work:
        ctx = _fetched(work, PROSE_PAGE)
        C.cmd_classify(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.has("PAGEKIND_KNOWN")
        vector = sc.get_evidence("pagekind")
        assert set(vector) == set(DIMS)
        for dim, cell in vector.items():
            assert set(cell) == {"value", "conf", "rule_id"}
            assert cell["value"] in SPEC["dimensions"][dim]
        assert sc.get_evidence("pagekind_terminal") in SPEC["terminals"]
        assert isinstance(sc.get_evidence("pagekind_unresolved"), list)


def test_evidence_survives_a_round_trip_to_disk():
    with tempfile.TemporaryDirectory() as work:
        ctx = _fetched(work, PROSE_PAGE)
        C.cmd_classify(ctx)
        raw = json.loads((Path(work) / f"{C._resolve_id(ctx)}.htmldrill.json")
                         .read_text("utf-8"))
        assert set(raw["evidence"]["pagekind"]) == set(DIMS)


def test_classify_is_offline_and_refuses_to_fetch():
    with tempfile.TemporaryDirectory() as work:
        with pytest.raises(FileNotFoundError) as e:
            C.cmd_classify(C.Ctx(url="https://example.invalid/x", work=work))
        assert "fetch" in str(e.value)


def test_a_non_html_payload_classifies_instead_of_erroring():
    """`payload: pdf` is a DETERMINED result — 'hand it to pdfdrill'. The old
    snapshot loader raised on any non-HTML kind; classification must not."""
    with tempfile.TemporaryDirectory() as work:
        sc = Sidecar("pdf-target", work=work)
        sc.write_blob_bytes("raw.pdf", b"%PDF-1.7\n%%EOF\n")
        sc.write_blob("headers.json", json.dumps({"content-type": "application/pdf"}))
        sc.set_evidence("status", 200)
        sc.set_evidence("content_type", "application/pdf")
        sc.set_evidence("content_kind", "pdf")
        sc.set_evidence("raw_blob", "raw.pdf")
        sc.set_evidence("final_url", "https://x.test/a.pdf")
        sc.add_fact("FETCHED")
        sc.save()
        out = C.cmd_classify(C.Ctx(url="pdf-target", work=work))
        assert "payload" in out and "pdf" in out
        after = Sidecar("pdf-target", work=work)
        assert after.get_evidence("pagekind")["payload"]["value"] == "pdf"
        assert after.get_evidence("pagekind_row") == "po.pdf"
        assert after.get_evidence("pagekind_plan") == []


def test_an_auth_wall_reports_the_reason_and_plans_nothing():
    with tempfile.TemporaryDirectory() as work:
        sc = Sidecar("walled", work=work)
        sc.write_blob("raw.html", "<html><body>Sign in to continue</body></html>")
        sc.write_blob("headers.json", json.dumps({"content-type": "text/html"}))
        sc.set_evidence("status", 401)
        sc.set_evidence("content_type", "text/html")
        sc.set_evidence("content_kind", "html")
        sc.set_evidence("final_url", "https://x.test/account")
        sc.add_fact("FETCHED")
        sc.save()
        out = C.cmd_classify(C.Ctx(url="walled", work=work))
        assert "NOT_RETRIEVABLE_AUTH" in out
        assert "no steps" in out
        assert Sidecar("walled", work=work).get_evidence("pagekind_plan") == []


def test_the_command_is_registered_everywhere_it_must_be():
    manifest = yaml.safe_load(
        (ROOT / "src" / "htmldrill" / "commands.yaml").read_text("utf-8"))
    entry = next(c for c in manifest["commands"] if c["name"] == "classify")
    assert entry["done_when"] == "fact:PAGEKIND_KNOWN"
    assert "classify" in C.HANDLERS
    assert C.HANDLERS["classify"] is C.cmd_classify


def test_the_planner_treats_pagekind_known_as_the_completion_detector():
    from htmldrill import planner
    with tempfile.TemporaryDirectory() as work:
        ctx = _fetched(work, PROSE_PAGE)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert "classify" not in planner.satisfied_set(
            planner.load_graph(planner.load_manifest())[1], sc)
        C.cmd_classify(ctx)
        sc2 = Sidecar(C._resolve_id(ctx), work=work)
        assert "classify" in planner.satisfied_set(
            planner.load_graph(planner.load_manifest())[1], sc2)
