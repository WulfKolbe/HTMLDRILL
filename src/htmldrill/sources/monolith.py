"""Save a whole web page as ONE self-contained HTML file, via the `monolith` CLI.

`monolith` (https://github.com/Y2Z/monolith) downloads a URL and inlines every
asset — CSS, images, fonts, JS — as `data:` URIs, producing a single portable
`.html` that renders offline with no network. htmldrill uses that twice over:

  * as a durable ARCHIVE deliverable (a page frozen in one file), and
  * as the preferred SOURCE for the offline `model` pipeline — because inlined
    assets mean `ingest_dom` sees real image `src`s and complete markup instead
    of dangling network references it can never resolve offline.

This module is the thin heavy-tool wrapper (sibling of `render`/`print_pdf`):
binary resolution + one subprocess call. It shells out; it never parses HTML.
`monolith` is an OPTIONAL dependency — `find_monolith()` returns None when it
isn't installed and the caller turns that into an actionable error, exactly how
`print_pdf.find_geckodriver()` gates the firefox engine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import fetch as F


def find_monolith() -> Optional[str]:
    """$HTMLDRILL_MONOLITH > the usual cargo install spot > PATH."""
    env = os.environ.get("HTMLDRILL_MONOLITH")
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    cargo = Path.home() / ".cargo/bin/monolith"
    if cargo.exists():
        return str(cargo)
    return shutil.which("monolith")


def monolith_version(binary: Optional[str] = None) -> Optional[str]:
    """The `monolith --version` string (e.g. "monolith 2.10.1"), or None."""
    b = binary or find_monolith()
    if not b:
        return None
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True,
                             timeout=10)
        return (out.stdout or out.stderr).strip() or None
    except Exception:
        return None


@dataclass
class SingleResult:
    out_path: Path            # the written single.html
    bytes: int               # size of the written file
    binary: str              # the monolith binary used
    final_url: str           # the normalized target that was archived


def save_single(url: str, out_path: Path, *, ua: Optional[str] = None,
                timeout: float = F.DEFAULT_TIMEOUT, no_js: bool = False,
                isolate: bool = False, binary: Optional[str] = None) -> SingleResult:
    """Run monolith on ``url`` and write one self-contained HTML to ``out_path``.

    ``-e`` (ignore-errors) tolerates individual asset fetch failures so a single
    missing image never sinks the whole archive. ``no_js`` drops scripts (a
    cleaner, more deterministic model source); ``isolate`` cuts the result off
    from the network entirely (``-I``). Raises RuntimeError with monolith's own
    stderr on a non-zero exit, and FileNotFoundError if the binary is absent.
    """
    b = binary or find_monolith()
    if not b:
        raise FileNotFoundError(
            "monolith not found — install it (`cargo install monolith`) or set "
            "$HTMLDRILL_MONOLITH to the binary. It powers `htmldrill single`.")
    target = F.normalize_url(url)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [b, "-e", "-o", str(out_path)]
    if ua:
        cmd += ["-u", ua]
    if timeout:
        cmd += ["-t", str(int(timeout))]
    if no_js:
        cmd.append("-j")
    if isolate:
        cmd.append("-I")
    cmd.append(target)

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=max(timeout * 3, 120))
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"monolith failed ({proc.returncode}) on {target}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}")
    return SingleResult(out_path=out_path, bytes=out_path.stat().st_size,
                        binary=b, final_url=target)
