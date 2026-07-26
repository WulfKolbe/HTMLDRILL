# CR: pagekind lattice — replace open-world verdicts with a total state machine

**Paste everything below into Claude Code, in the htmldrill repo root.**

---

## Context you must read before writing any line

Read, in this order: `SKILL.md`, `src/htmldrill/commands.yaml`,
`src/htmldrill/planner.py`, `src/htmldrill/sidecar.py`, `commands.py` lines
276–320 (`_guess_framework`, `cmd_size`), `commands.py` `cmd_splits`,
`src/htmldrill/sources/known_hosts.py`. Then read
`src/htmldrill/pagekind.yaml` and `tools/pagekind_validate.py`, which already
exist and already pass.

## The defect (do not re-derive it, do not re-litigate it)

`commands.py:305`:

```python
needs_render = bool(fw != "none detected" and len(c.headings) <= 1 and visible_text < 200)
```

An **unrecognised** framework and a **genuinely absent** framework collapse to the
same value, and that value routes to "no render needed". Not-yet-measured is
conflated with measured-and-absent. `KNOWN_HOSTS` (arxiv only) and
`_guess_framework`'s `"none detected"` have the same shape. Every unknown page
gets a confident wrong answer; the wrong answer looks like a missing special
case; the repair looks like another branch. That loop is the thing being fixed.

## The target architecture

Four strictly separated stages. No stage may do another stage's job.

1. **Detectors** — pure `(html, headers, url, status) -> dict[feature_id, value]`.
   Detectors observe. They never decide, never name a page genre, never
   mention `render`. A detector that cannot determine its feature returns
   `NOT_OBSERVED`, which is a distinct sentinel from `False`.
2. **Classifier** — `features -> Vector`, one value per dimension in
   `pagekind.yaml`, each with `(value, confidence, rule_id)`. Total: an empty
   feature dict must still yield a complete vector, all `unknown`.
3. **Policy** — `Vector -> (plan: list[capability], terminal)`. Table lookup,
   first match wins.
4. **Execution** — maps capability names onto the existing commands
   (`static_parse` → the existing snapshot parsers, `render` → `cmd_render`,
   `capture_scroll` → `cmd_capture`, …). Executing does not reclassify.

`pagekind.yaml` is the only place knowledge lives. Detection rules and policy
rows are **data**. Adding a page genre is a row.

## Hard prohibitions — these are checkable, and I will check them

- **No page-genre identifier may appear in any `.py` file.** Not `cart`, not
  `faq`, not `product_page`, not `search_results`. Genres are points in the
  lattice, never names in code.
- **No regex literal may appear in `pagekind.py` or `classify.py`.** Every
  pattern comes from the YAML. If a detector needs a regex it lives in the
  detector module and is registered by feature id.
- **No new `elif` in any classification path.** If a case needs handling, it
  needs a YAML row. If the row cannot express it, the *dimension set* is wrong —
  stop and say so rather than branching.
- **Forbidden outright**: `except: pass`, commenting out a failing test,
  `# TODO` standing in for a rule, writing a function you have not run.
- Do not weaken `tools/pagekind_validate.py` to make anything pass.

## Units. One at a time. Do not start N+1 before N's tests run green.

For each unit: write the interface contract first, then the test, then the
implementation. After each unit, **re-run every previous unit's tests plus the
existing 59** and report the count.

### U1 — detector registry
Contract: `detectors.collect(html, headers, url, status) -> dict[str, object]`.
Every feature id referenced by any `when:` in `pagekind.yaml` has exactly one
registered detector. Unresolvable → `NOT_OBSERVED`.
Tests: `test_pk_detectors.py` — (a) every YAML feature id has a detector
(introspect the YAML, do not hardcode the list); (b) each detector on its
corpus fixture; (c) empty input yields all-`NOT_OBSERVED`, no exception.

### U2 — classifier
Contract: `classify(features) -> dict[dim, (value, conf, rule_id)]`.
Tests: `test_pk_classify.py` — (a) empty features → every dimension present,
all `unknown`, conf 0.0; (b) `NOT_OBSERVED` never satisfies a `when:`;
(c) first-match-wins order verified on a two-rule conflict; (d) 200 randomised
feature dicts all yield complete vectors.

### U3 — policy resolution
Contract: `resolve(vector) -> Plan(steps, terminal, matched_row, unresolved_dims)`.
When terminal is `UNDERDETERMINED`, `steps[0]` must be the **cheapest**
capability whose `resolves:` covers some unresolved dimension.
Tests: `test_pk_policy.py` — (a) re-run T1/T2/T3 from
`tools/pagekind_validate.py` as pytest cases; (b) the UNDERDETERMINED
cheapest-probe property, one case per dimension; (c) `access=auth_required`
yields empty steps and the auth terminal — it must never plan a fetch.

### U4 — the `classify` command
New command in `commands.yaml` (`done_when: fact:PAGEKIND_KNOWN`), handler in
`commands.py`, sidecar evidence = the full vector. Prose output **must** print,
per dimension: value, confidence, and the firing `rule_id`; then the plan with
each step's cost; then unresolved dimensions named explicitly.
Run `python3 tools/skillsync.py all .` and commit the regenerated files.
Tests: `test_pk_command.py` — output names a rule id for every dimension; an
underdetermined page's prose contains the unresolved dimension names.

### U5 — rewire `size`, do not delete it
`cmd_size` keeps its output shape. `needs_render` becomes **derived**:
`vector.delivery == client_rendered`. `_guess_framework` survives as one
*feature detector* among many — demoted, not deleted.
Tests: re-run the full existing suite. Any existing test whose expectation
changes must be re-baselined **explicitly in the report**, with the old and new
value and why the new one is correct. Silently updating an expected value counts
as a failure of this unit.

### U6 — corpus
One fixture per distinct region of the lattice, asserting the **vector**, never
a branch. Minimum: SPA shell, hydrated+JSON-LD, paginated list, infinite feed,
collapsed `<details>`, link hub, soft-404, consent wall, auth wall, PDF payload,
document-viewer shell, and one page that is *genuinely underdetermined*.
The last one is the important test: assert it reports UNDERDETERMINED and names
its next probe, rather than guessing.

## Reporting rules

Never write "done", "working", "complete", or "production-ready". Report only
`Test <name> passed, proceeding to <next>`. Assume nothing works at the start;
confidence comes only from accumulated passing tests. End with two lists:
**tests passed, by name** and **assumptions that remain unverified**.
