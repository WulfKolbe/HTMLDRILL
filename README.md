# HTMLDRILL

A token-economical, shallow-first **drill-down toolkit for HTML / live web
documents** — a structural twin of [`pdfdrill`](../../PDFDRILL) and
[`chatdrill`](../../../CHATDRILL). Same sidecar state machine, same L0–L8 stratified
standoff graph, same `commands.yaml`-as-single-source-of-truth + skillsync drift
gate. The HTML-specific work lives in the L0–L4 producers (static markup,
headless render, CSS regions); L5–L8 are shared with the rest of the *drill
family.

## Quick start

```bash
./htmldrill doctor                         # environment self-check
./htmldrill fetch https://example.com      # the only network step; snapshots raw HTML
./htmldrill size   https://example.com     # bytes/tags/framework + render-needed verdict
./htmldrill meta   https://example.com     # <meta> + <title>
./htmldrill links  https://example.com     # anchors + URLs hidden in the markup
./htmldrill jsonld https://example.com     # structured data lifted from <script ld+json>
./htmldrill outline https://example.com    # h1–h6 tree
./htmldrill status https://example.com     # what's been learned so far
```

Or without the wrapper: `PYTHONPATH=src python3 -m htmldrill <command> <url>`.

Only dependency: **pyyaml** (the planner manifest). Everything else is stdlib —
`urllib` for fetch, `html.parser` for parsing. No headless browser is required
for the L0 tier; that arrives at M1 (`render`), gated by `size`'s verdict.

## How it works

`fetch` captures an immutable snapshot (raw HTML + headers) under `drills/`,
keyed by a URL-derived id. Every other L0 command reads that snapshot — never the
live network — so re-runs are deterministic and cumulative. State lives in a
sidecar (`<id>.htmldrill.json`): facts (what's known), evidence (the values),
and a transition log (the audit trail).

The **killer case**: `htmldrill links` surfaces URLs that exist in the raw markup
(in `data-*` attributes, inline JSON, `<link>` tags, or JS string literals) but
never appear as a visible `<a href>` anchor — the HTML analog of pdfdrill reading
invisible links out of a PDF's annotation layer.

## The pdfdrill bridge: `print`

`htmldrill print <url>` renders a page to PDF, then **judges whether the text
layer is actually usable** — turning a web page into a base medium pdfdrill's
whole tower already understands:

```bash
./htmldrill print https://example.com            # --engine firefox (default)
./htmldrill print https://example.com --engine chrome
# → "text layer:✓ extractable — no OCR needed"
# → hand it over:  pdfdrill size <…>/print.pdf
```

Two engines: **firefox** (Selenium + geckodriver `print_page()`; needs the
optional `selenium` package + a geckodriver binary) and **chrome**
(`--print-to-pdf` via the headless Chrome `render` already uses; no extra deps).

*Measured, not assumed:* the engines extract equivalently on real pages —
python.org (387 vs 389 words, letter-ratio 0.812/0.813) and a math-heavy formula
report (1165 vs 1212 words, ratio 0.522/0.522). So the engine is a fallback lever
for pages that misbehave, not a quality choice. The load-bearing step is the
**validator**: print, then *check*, and escalate to OCR only when the check fails
— never "always OCR everything".

## Status

**M0–M5 complete.** 31 commands: L0 free tier · M1 headless render gate · M2
`ingest_dom` → shared `Document` · M3 projector payoff (`tiddlers`/`md`/`llmtext`
via pdfdrill's own `docops`) · M4 split-recovery (`splits`/`materialize`) · M5
bounded `crawl` + `retrieve`/`chatlog` + `drillui --tool htmldrill` · plus the
`print` bridge. 59 tests, real-corpus hardened; CI enforces manifest↔HANDLERS.
