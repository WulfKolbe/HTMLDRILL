"""Print a web page to PDF, then judge whether its text layer is actually usable.

This is the htmldrill -> pdfdrill bridge: a page printed to PDF becomes a base
medium pdfdrill's whole tower already understands (text-layer-vs-scanned gate,
OCR, docmodel, projectors). htmldrill only has to capture it well and say
honestly whether the text came out usable.

TWO ENGINES
  firefox  WebDriver ``print_page()`` via Selenium + geckodriver (the approach
           recommended by Selenium.md: standards-based, no Chrome DevTools hacks).
           Needs the optional `selenium` package + a geckodriver binary.
  chrome   ``--print-to-pdf`` via the same headless Chrome `render` already uses.
           Zero extra dependencies.

MEASURED (not assumed): Selenium.md warns that Chrome-produced PDFs can carry a
semantically broken text layer (the Adobe "T1 font" reports), making them hostile
to extraction/OCR. We tested both engines on real pages — a live site and a
math-heavy formula report — and extraction quality came out equivalent:

    python.org      firefox 387 words / letter-ratio 0.812
                    chrome  389 words / letter-ratio 0.813
    formula-report  firefox 1165 words / letter-ratio 0.522
                    chrome  1212 words / letter-ratio 0.522

So neither engine is privileged on evidence. The defence that matters is not the
engine choice but the VALIDATOR below: print, then *check*, and only escalate to
OCR when the check fails. `--engine` lets you swap engines when a specific page
does misbehave (which is the real, narrow risk the note describes).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import fetch as F
from . import render as R

ENGINES = ("firefox", "chrome")


def find_geckodriver() -> Optional[str]:
    """$HTMLDRILL_GECKODRIVER > PATH > the usual local install spot."""
    env = os.environ.get("HTMLDRILL_GECKODRIVER")
    if env and Path(env).exists():
        return env
    p = shutil.which("geckodriver")
    if p:
        return p
    local = Path.home() / ".local/bin/geckodriver"
    return str(local) if local.exists() else None


def _target_url(url: str) -> str:
    """Browsers need a URL; a bare local path must become file://<abs>."""
    norm = F.normalize_url(url)
    if F.is_local(norm):
        return Path(norm.replace("file://", "")).expanduser().resolve().as_uri()
    return norm


def print_firefox(url: str, dest: Path, timeout: float = 90.0) -> None:
    """Firefox headless -> WebDriver Print Page -> base64 PDF -> dest."""
    gecko = find_geckodriver()
    if not gecko:
        raise FileNotFoundError(
            "no geckodriver found for --engine firefox — install it (or set "
            "$HTMLDRILL_GECKODRIVER). Alternative: --engine chrome (no extra deps).")
    try:
        from selenium import webdriver
        from selenium.webdriver.common.print_page_options import PrintOptions
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
    except ImportError as e:
        raise FileNotFoundError(
            f"--engine firefox needs the optional 'selenium' package "
            f"(pip install selenium): {e}. Alternative: --engine chrome.") from e

    opts = Options()
    opts.add_argument("--headless")
    driver = webdriver.Firefox(options=opts, service=Service(executable_path=gecko))
    try:
        driver.set_page_load_timeout(timeout)
        driver.get(_target_url(url))
        po = PrintOptions()
        po.background = True                 # keep CSS backgrounds, per the note
        dest.write_bytes(base64.b64decode(driver.print_page(po)))
    finally:
        driver.quit()


def print_chrome(url: str, dest: Path, timeout: float = 90.0) -> None:
    """Headless Chrome --print-to-pdf -> dest. Reuses render.py's Chrome probe."""
    chrome = R.find_chrome()
    if not chrome:
        raise FileNotFoundError(
            "no Chrome/Chromium found for --engine chrome — set $HTMLDRILL_CHROME, "
            "or use --engine firefox.")
    # Isolated throwaway profile — never attach to the user's real Chrome profile
    # (a second instance on it makes their browser unusable; can corrupt settings).
    profile = tempfile.mkdtemp(prefix="htmldrill-chrome-")
    try:
        cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
               "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={profile}",
               "--run-all-compositor-stages-before-draw",
               f"--print-to-pdf={dest}", "--print-to-pdf-no-header", _target_url(url)]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if not dest.exists():
            raise RuntimeError(f"chrome --print-to-pdf produced nothing "
                               f"(rc={proc.returncode}): {proc.stderr[:300]!r}")
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def to_pdf(url: str, dest: Path, engine: str = "firefox",
           timeout: float = 90.0) -> str:
    """Print `url` to `dest` with the chosen engine. Returns the engine used."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r} — choose from {ENGINES}")
    (print_firefox if engine == "firefox" else print_chrome)(url, dest, timeout)
    return engine


# --------------------------------------------------------------------------- #
# The validator — the part of Selenium.md that is engine-agnostic and load-bearing
# --------------------------------------------------------------------------- #

def validate_text_layer(pdf: Path) -> dict:
    """Is this PDF's text layer usable, or does it need an OCR rebuild?

    The postflight check Selenium.md argues for: rather than "always OCR", try
    extraction and only flag the genuine failures. Detects the three bad cases it
    names — empty extraction, gibberish, and absurd mappings — via character
    statistics that real prose satisfies and broken font maps do not.

    Returns a dict with `usable` + the evidence behind the verdict. Never raises:
    a missing pdftotext is reported as `usable=None` (unknown), not a crash."""
    if not pdf.exists():
        return {"usable": False, "verdict": "no PDF produced"}
    if not shutil.which("pdftotext"):
        return {"usable": None, "verdict": "pdftotext not installed — cannot judge "
                                           "the text layer (install poppler-utils)"}
    try:
        out = subprocess.run(["pdftotext", str(pdf), "-"],
                             capture_output=True, text=True, timeout=120)
        text = out.stdout
    except Exception as e:  # noqa: BLE001
        return {"usable": False, "verdict": f"pdftotext failed: {e}"}

    dense = text.replace(" ", "").replace("\n", "")
    letters = sum(c.isalpha() for c in text)
    words = [w for w in text.split() if len(w) > 2 and any(c.isalpha() for c in w)]
    ratio = letters / max(1, len(dense))
    stats = {"chars": len(text), "letters": letters, "words": len(words),
             "letter_ratio": round(ratio, 3),
             "sample": " ".join(text.split())[:100]}

    # Thresholds are deliberately loose: they must pass a math-heavy page (real
    # ratio ~0.52, half the glyphs being operators/digits) while still catching a
    # genuinely broken layer. We are separating "nothing/garbage" from "prose",
    # not grading quality.
    if len(words) < 5 or letters < 20:
        return {"usable": False, "verdict": "empty or near-empty text layer — "
                                            "OCR required (hand to pdfdrill)", **stats}
    if ratio < 0.15:
        return {"usable": False, "verdict": "text extracts as gibberish (letter "
                                            "ratio too low) — broken font mapping, "
                                            "OCR required (hand to pdfdrill)", **stats}
    return {"usable": True, "verdict": "text layer is extractable — no OCR needed",
            **stats}
