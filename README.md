# HTMLDRILL

A token-economical, shallow-first **drill-down toolkit for HTML / live web
documents** — a structural twin of [`pdfdrill`](../MX/PDFDRILL) and
[`chatdrill`](../CHATDRILL). Same sidecar state machine, same L0–L8 stratified
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

## Status

**M0 — scaffold + L0 free tier (current).** Fetch + 14 introspection/diagnostic
commands, all zero-render. Roadmap: M1 headless render gate · M2 DOM/region
producers + `ingest_dom` → shared `Document` · M3 the projector payoff
(`tiddlers`/`md`/`report`/`semantic` over HTML, reusing pdfdrill's `docops`).
