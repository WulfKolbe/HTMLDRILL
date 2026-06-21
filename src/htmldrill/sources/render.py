"""L1 render — headless materialization via a system Chrome, zero Python deps.

This is the expensive, escalation-gated layer (the OCR analog: `size` decides
static-sufficient vs. JS-rendered, the way pdfdrill decides text-layer vs.
scanned). Rather than pull in Playwright + a downloaded Chromium, htmldrill
shells out to whatever Chrome/Chromium is already on the system:

    chrome --headless=new --dump-dom   <url>   → the computed/rendered DOM (stdout)
    chrome --headless=new --screenshot ...     → a PNG of the painted page

Both run against the live URL (or a file://). The rendered DOM + screenshot are
snapshotted as blobs, so downstream commands replay against them, never re-render.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import fetch as F

# Probe order — env override first, then the usual binary names.
_CANDIDATES = [
    "google-chrome-beta", "google-chrome-stable", "google-chrome",
    "chromium", "chromium-browser", "chrome",
]


def find_chrome() -> Optional[str]:
    env = os.environ.get("HTMLDRILL_CHROME")
    if env and Path(env).exists():
        return env
    for name in _CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def _as_chrome_url(url: str) -> str:
    norm = F.normalize_url(url)
    if F.is_local(norm):
        return Path(norm.replace("file://", "")).expanduser().resolve().as_uri()
    return norm


def _flags() -> list[str]:
    return ["--headless=new", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars", "--disable-dev-shm-usage"]


class RenderResult:
    def __init__(self, dom: str, screenshot: Optional[bytes], chrome: str, final_url: str):
        self.dom = dom
        self.screenshot = screenshot
        self.chrome = chrome
        self.final_url = final_url


def render(url: str, timeout: float = 45.0, window: str = "1280,900",
           screenshot: bool = True) -> RenderResult:
    """Materialize `url` once: rendered DOM (+ optional screenshot). Raises
    FileNotFoundError if no Chrome is found, or RuntimeError on a failed render."""
    chrome = find_chrome()
    if not chrome:
        raise FileNotFoundError(
            "no Chrome/Chromium found — set $HTMLDRILL_CHROME or install one "
            "(tried: " + ", ".join(_CANDIDATES) + ")")
    target = _as_chrome_url(url)

    dom_cmd = [chrome, *_flags(), f"--window-size={window}", "--dump-dom", target]
    proc = subprocess.run(dom_cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"chrome --dump-dom failed (rc={proc.returncode}): "
                           f"{proc.stderr.strip()[:300]}")
    dom = proc.stdout

    shot: Optional[bytes] = None
    if screenshot:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "shot.png"
            shot_cmd = [chrome, *_flags(), f"--window-size={window}",
                        f"--screenshot={png}", target]
            try:
                subprocess.run(shot_cmd, capture_output=True, timeout=timeout)
                if png.exists():
                    shot = png.read_bytes()
            except Exception:
                shot = None          # screenshot is best-effort; DOM is the point

    return RenderResult(dom=dom, screenshot=shot, chrome=chrome, final_url=target)


class MaterializeResult:
    """The render-delta payload: the virtual-time-materialized DOM, the plain DOM
    (for the diff), and which Chrome produced them."""

    def __init__(self, dom: str, plain_dom: str, chrome: str, final_url: str):
        self.dom = dom
        self.plain_dom = plain_dom
        self.chrome = chrome
        self.final_url = final_url


#: the probe-documented render-delta flags — let timers/microtasks/animation
#: frames run to completion before the DOM is dumped, materializing JS-injected
#: nodes (e.g. a setTimeout-appended block) that a plain --dump-dom never sees.
_VIRTUAL_TIME_FLAGS = ["--virtual-time-budget=5000",
                       "--run-all-compositor-stages-before-draw"]


def render_materialize(url: str, timeout: float = 45.0,
                       window: str = "1280,900") -> MaterializeResult:
    """Render twice — plain, then with the virtual-time-budget flag — so the
    caller can diff the two DOMs and recover the blocks JS materialized after
    load (timer/microtask/raf content). NETWORK + headless.

    Raises FileNotFoundError with a clear message if no Chrome is found.

    LIMIT (honest): virtual-time materializes timer/microtask/animation-frame
    content but NOT scroll-intersection content — IntersectionObserver-gated
    infinite-scroll needs CDP input events (an explicit scroll/setViewport step)
    which this plain-flag path does not drive."""
    chrome = find_chrome()
    if not chrome:
        raise FileNotFoundError(
            "no Chrome/Chromium found for --render-delta — set $HTMLDRILL_CHROME "
            "or install one (tried: " + ", ".join(_CANDIDATES) + ")")
    target = _as_chrome_url(url)
    size = [f"--window-size={window}"]

    plain_cmd = [chrome, *_flags(), *size, "--dump-dom", target]
    plain = subprocess.run(plain_cmd, capture_output=True, text=True, timeout=timeout)
    if plain.returncode != 0 and not plain.stdout:
        raise RuntimeError(f"chrome plain --dump-dom failed (rc={plain.returncode}): "
                           f"{plain.stderr.strip()[:300]}")

    vt_cmd = [chrome, *_flags(), *size, *_VIRTUAL_TIME_FLAGS, "--dump-dom", target]
    vt = subprocess.run(vt_cmd, capture_output=True, text=True, timeout=timeout)
    if vt.returncode != 0 and not vt.stdout:
        raise RuntimeError(f"chrome virtual-time --dump-dom failed (rc={vt.returncode}): "
                           f"{vt.stderr.strip()[:300]}")

    return MaterializeResult(dom=vt.stdout, plain_dom=plain.stdout,
                             chrome=chrome, final_url=target)
