"""_core — the ONE place htmldrill reaches outside its own tree.

htmldrill's L0/L1 tiers (fetch, render, the parse extractors) are pure stdlib and
import nothing external — exactly like CHATDRILL. M2 changes that: the ``model``
command lifts the parsed DOM into the shared **docmodel** Document — the same
intermediate representation pdfdrill builds — so the downstream projectors
(PlainText, TiddlyWiki, …) and any cross-drill tooling can consume HTML the same
way they consume PDFs. Rather than vendor a copy of docmodel, htmldrill borrows
pdfdrill's canonical implementation by putting its ``src`` on ``sys.path``.

This is deliberately isolated here so the divergence from CHATDRILL (which bridged
to nothing) is auditable in a single file: only ``model`` / ``ingest_dom`` import
docmodel, and they do it through :func:`ensure_pdfdrill`. Everything else stays
dependency-free.

Path resolution: ``$HTMLDRILL_PDFDRILL`` if set, else the default checkout at
``/home/wkolbe/MX/PDFDRILL/src``. The insert is idempotent, and a missing/broken
checkout raises a clear, actionable error instead of a bare ``ImportError``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_PDFDRILL_SRC = "/home/wkolbe/MX/PDFDRILL/src"


def pdfdrill_src() -> Path:
    """The pdfdrill ``src`` dir: ``$HTMLDRILL_PDFDRILL`` or the default checkout."""
    return Path(os.environ.get("HTMLDRILL_PDFDRILL") or DEFAULT_PDFDRILL_SRC).expanduser()


def ensure_pdfdrill() -> Path:
    """Put pdfdrill's ``src`` on ``sys.path`` (idempotently) so ``docmodel`` /
    ``docops`` import, and confirm the import works.

    Returns the resolved ``src`` path. Raises ``ModuleNotFoundError`` with a
    pointed message (naming the env var) if docmodel still can't be imported —
    this is the only external dependency htmldrill has, so its absence must be
    obvious rather than surfacing as a cryptic failure deep inside ``model``.
    """
    src = pdfdrill_src()
    s = str(src)
    if s not in sys.path:
        # Front-insert so pdfdrill's modules win over any same-named stragglers.
        sys.path.insert(0, s)
    try:
        import docmodel  # noqa: F401
    except Exception as e:  # noqa: BLE001 — re-raise with actionable context
        raise ModuleNotFoundError(
            f"htmldrill `model` needs pdfdrill's docmodel, but importing it from "
            f"{s!r} failed ({e}). Point $HTMLDRILL_PDFDRILL at a pdfdrill checkout's "
            f"src dir (currently {'set' if os.environ.get('HTMLDRILL_PDFDRILL') else 'unset'})."
        ) from e
    return src
