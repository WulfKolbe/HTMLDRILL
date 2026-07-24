# Design: `single` (monolith archive) + `latex` (html2latex projector)

Date: 2026-07-24
Status: Approved (design forks confirmed by user via structured questions)

## Motivation

Two capabilities, connected by one idea — turn a live web page into a faithful,
self-contained artifact, then project that artifact into LaTeX the same way
pdfdrill already ingests gold author LaTeX into its docmodel.

1. **`single`** — use the `monolith` CLI (`~/.cargo/bin/monolith`, v2.10.1) to
   download a URL and inline *all* assets (CSS, images, fonts) into one
   self-contained `single.html`. This is both a portable archive deliverable and
   a superior source for the offline `model` pipeline (figures resolve to
   `data:` URIs instead of dangling network `src`s).

2. **`latex`** — a full-document `.tex` projector over the shared docmodel,
   parallel to `tiddlers`/`md`/`llmtext`, powered by
   [html2latex](https://github.com/pankaj28843/html2latex)
   (`render_document()`), which converts HTML fragments → LaTeX. It reconstructs
   HTML from the docmodel objects and emits one standalone `.tex`.

This bridges to PDFDRILL's existing LaTeX ingestion (`cmd_injectlatex`,
`merge_latex.merge_latex_prose`, the `MathPass`/`CitationPass` enhancement
passes that read `doc.meta['latex_source_dir']`): the same per-block html2latex
conversion is the raw material those passes consume. This projector is the first
half of "fill the docmodel with LaTeX."

## Constraints discovered

- htmldrill's core is **stdlib-only** (sole runtime dep: pyyaml). The one
  external bridge is `_core.ensure_pdfdrill()` (docmodel/docops via sys.path).
- **Block text is collapsed plain text.** `parse.html.Block.text` is the
  *collapsed visible text* of a block; inline markup (bold/italic/links/math) is
  flattened at L0 parse time. So the docmodel Paragraphs carry plain text, and
  the emitted `.tex` is **structurally faithful but inline-plain** this round.
  Preserving per-block inner HTML (to unlock html2latex's inline/math strengths)
  is a documented future enhancement, **not built now** (YAGNI).
- Commands are registered in three synchronized places: `commands.yaml`
  (manifest), the `HANDLERS` dict, and a `cmd_*` function. `tools/skillsync.py
  all .` regenerates `_help_generated.txt` + the SKILL.md tables; CI fails on
  drift.
- `html2latex` is **not** stdlib (Python 3.10+, deps `justhtml`). Treated as an
  optional dependency, doctor-gated — exactly how `print` treats selenium.

## Component 1 — `single` (network tier)

**Manifest** (`commands.yaml`): new section *Single-file archive (network)*:
```yaml
- name: single
  section: Single-file archive (network)
  summary: Download the URL with monolith and inline every asset into one self-contained single.html (a portable archive AND the preferred offline model source). Records SINGLE.
  network: true
  done_when: fact:SINGLE
```

**Handler** `cmd_single(ctx)` in `commands.py`:
- Resolve the monolith binary: `$HTMLDRILL_MONOLITH` → `~/.cargo/bin/monolith` →
  `shutil.which("monolith")`. Clear error with an install hint if none.
- Run `monolith <url> -e -o <blob>/single.html`, wiring
  `--user-agent <ctx.ua>` and `--timeout <int(ctx.timeout)>`. `-e` tolerates
  individual asset fetch failures. Optional passthrough flags: `--no-js`
  (`-j`), `--isolate` (`-I`).
- On success: the blob `single.html` already exists (monolith wrote it); set
  evidence (`single_bytes`, `single_blob="single.html"`), add fact `SINGLE`, log
  the transition, return a deterministic report (bytes + path + next-step hint),
  mirroring `_fetch_report`.
- Cached path: if `SINGLE` present and not `--force`, report cached.

**Config/doctor**:
- `cmd_config` gains a `monolith:` line (resolved path + `$HTMLDRILL_MONOLITH`).
- `cmd_doctor` gains an optional check: monolith presence + `--version`.

## Component 2 — `model` prefers `single.html`

Surgical change to `cmd_model`'s HTML-source selection. New precedence:

```
single.html  →  rendered DOM  →  raw.html
```

When `single.html` exists it is read and passed to `ingest_dom.build_document`
in place of the rendered/raw DOM. Fully backward-compatible: absent
`single.html`, behaviour is unchanged. Record the chosen source in the model
report / evidence (`model_source`) so `status` shows which tier fed the model.

## Component 3 — `latex` (offline projector, `requires: [model]`)

**Manifest**:
```yaml
- name: latex
  section: Projectors (offline)
  summary: Project the docmodel to a standalone LaTeX .tex via html2latex (reconstructs HTML from the model, then render_document). Records LATEX_BUILT.
  requires: [model]
  done_when: fact:LATEX_BUILT
```

**Handler** `cmd_latex(ctx)`:
1. Load `model.docmodel.json` (auto-ensured via `requires: [model]`).
2. Reconstruct a clean HTML document from the docmodel objects, in `flow_index`
   order, honoring the Section hierarchy:
   - Section → `<h{level}>` (clamped 1–6)
   - Paragraph → `<p>` (plain text; `continuation` paragraphs included)
   - ListItem → grouped into `<ul><li>…</li></ul>`
   - Table → `<table>` rebuilt from `props['rows']`
   - Picture → `<figure><img src alt><figcaption></figure>`
   HTML is escaped so model text can't inject markup.
3. Lazy `import html2latex`; if absent, return a clear error with
   `pip install html2latex`. Call `html2latex.render_document(html)` (fallback to
   `html2latex.html2latex(...)` wrapped in a minimal document if the document API
   differs), write `<blob>/out.tex`.
4. Record fact `LATEX_BUILT`, evidence (`latex_blob="out.tex"`, byte size), log
   transition, return a report (path + object counts + a compile hint).

**A small reconstruction helper** lives in a new focused module
`src/htmldrill/project_latex.py` (docmodel → HTML string) so `commands.py`
doesn't grow another large inline block and the serializer is unit-testable in
isolation. `cmd_latex` stays thin.

**Dependency plumbing**: `cmd_doctor` reports html2latex presence (optional, like
selenium); the command itself is the enforcement point when absent.

## Bridge note (future, not this round)

The docmodel→HTML→html2latex per-block conversion is exactly the input shape for
PDFDRILL's `merge_latex.merge_latex_prose(doc, latex_prose)` and the
`provenance="tex"` `latex_candidate` realizations. A future
`latex --enrich` mode could attach per-object LaTeX realizations instead of (or
alongside) emitting a whole-document `.tex`, completing "fill the docmodel with
LaTeX." Documented here so the projector is a stepping stone, not a dead end.

## Sync & tests

- After editing `commands.yaml` + `HANDLERS` + `cmd_*`: run
  `python3 tools/skillsync.py all .`, commit regenerated `_help_generated.txt` +
  `SKILL.md`. CI drift gate must pass.
- `tests/test_single.py`: gate on monolith availability (skip if absent, like
  `test_print.py`/`test_capture.py` gate their binaries). Assert the `SINGLE`
  fact, a non-trivial `single.html` blob, and that `model` reports
  `model_source=single`.
- `tests/test_latex.py`: gate on `html2latex` importability. Assert `LATEX_BUILT`,
  a non-empty `out.tex` containing `\documentclass` and reconstructed section
  titles. A dependency-free unit test covers `project_latex.py`'s docmodel→HTML
  serializer directly (no html2latex needed).

## Manual acceptance (test URL: https://schema-harness.github.io/)

All commands logged to the user:
```
./htmldrill fetch  https://schema-harness.github.io/ --force
./htmldrill single https://schema-harness.github.io/
./htmldrill model  https://schema-harness.github.io/      # → model_source=single
./htmldrill latex  https://schema-harness.github.io/      # → out.tex
./htmldrill doctor                                        # monolith ✓ / html2latex status
```

## Out of scope (YAGNI)

- Preserving per-block inner HTML for inline-rich LaTeX.
- `latex --enrich` per-object docmodel realizations.
- Any refactor beyond the `model` source-selection tweak.
