"""capture — drive a REAL browser to fully materialize a page, then grab
everything: the loaded DOM, a print-to-PDF, and a full-page screenshot.

Why a real browser and not `render`'s one-shot ``--dump-dom``: lazy content
(infinite-scroll feeds, IntersectionObserver-gated tables, deferred images) is
NOT in the initial render. A naive capture silently loses it — e.g. a 600-row
lazy table comes back with 50 rows. So capture SCROLLS until the page stops
growing, then snapshots. The completion rule is general (height + node count
stable for N rounds), not tuned to any one page.

PROFILE ISOLATION IS MANDATORY (see the module note): the automation browser
must never touch the user's real browser profile. Doing so makes their running
browser unusable (Chrome refuses a second instance on the same profile) and can
corrupt their settings. So:
  * firefox (default): Selenium/geckodriver auto-creates an EPHEMERAL profile
    under /tmp and deletes it on quit — it never opens ~/.mozilla. Isolated by
    construction; we only assert headless.
  * chrome: we pass an explicit throwaway ``--user-data-dir`` under a temp dir
    (removed after), plus --no-first-run/--no-default-browser-check, so it never
    attaches to ~/.config/google-chrome.
One launch per capture, clean quit — no polling, no relaunch loop.

Login tokens: a fresh isolated profile is NOT logged in, so gated pages capture
only what a logged-out visit sees. Reusing the user's session is deferred by
design; capture what we can today.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import fetch as F
from . import render as R


class CaptureResult:
    def __init__(self, dom: str, pdf: Optional[bytes], screenshot: Optional[bytes],
                 engine: str, rounds: int, final_url: str, profile: str):
        self.dom = dom
        self.pdf = pdf
        self.screenshot = screenshot
        self.engine = engine
        self.rounds = rounds          # how many scroll steps ran
        self.final_url = final_url
        self.profile = profile        # the isolated profile dir used (for the audit trail)


def _target_url(url: str) -> str:
    norm = F.normalize_url(url)
    if F.is_local(norm):
        return Path(norm.replace("file://", "")).expanduser().resolve().as_uri()
    return norm


# --------------------------------------------------------------------------- #
# The general "materialization complete" scroll loop — shared by both engines.
# --------------------------------------------------------------------------- #

_SCROLL_JS = "window.scrollTo(0, document.body.scrollHeight);"
_HEIGHT_JS = "return document.body.scrollHeight"
_NODES_JS = "return document.getElementsByTagName('*').length"


def _scroll_until_stable(driver, max_rounds: int, settle: float,
                         stable_needed: int = 2) -> int:
    """Scroll to the bottom repeatedly; stop when (scroll height, node count) is
    unchanged for `stable_needed` consecutive rounds, or `max_rounds` is hit.
    Returns the number of rounds run. General — knows nothing about the page."""
    last = (-1, -1)
    stable = 0
    rounds = 0
    for rounds in range(1, max_rounds + 1):
        driver.execute_script(_SCROLL_JS)
        time.sleep(settle)                       # let IO callbacks / timers fire
        state = (driver.execute_script(_HEIGHT_JS),
                 driver.execute_script(_NODES_JS))
        if state == last:
            stable += 1
            if stable >= stable_needed:
                break
        else:
            stable = 0
            last = state
    driver.execute_script("window.scrollTo(0, 0);")   # back to top for the print/shot
    return rounds


def capture_firefox(url: str, *, max_rounds: int = 60, settle: float = 0.35,
                    page_timeout: float = 60.0, screenshot: bool = True) -> CaptureResult:
    """Firefox headless via Selenium (ephemeral profile — never ~/.mozilla)."""
    gecko = _find_geckodriver()
    if not gecko:
        raise FileNotFoundError(
            "no geckodriver for capture --engine firefox (set $HTMLDRILL_GECKODRIVER "
            "or install it). Alternative: --engine chrome.")
    try:
        from selenium import webdriver
        from selenium.webdriver.common.print_page_options import PrintOptions
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
    except ImportError as e:
        raise FileNotFoundError(
            f"capture --engine firefox needs 'selenium' (pip install selenium): {e}. "
            f"Alternative: --engine chrome.") from e

    opts = Options()
    opts.add_argument("--headless")
    # Selenium creates a fresh temp profile per session and removes it on quit;
    # it does NOT touch the user's default Firefox profile. We assert nothing
    # else so we can't accidentally point it at a real profile.
    driver = webdriver.Firefox(options=opts, service=Service(executable_path=gecko))
    profile = getattr(getattr(driver, "capabilities", {}), "get", lambda *_: "")(
        "moz:profile") if hasattr(driver, "capabilities") else ""
    try:
        driver.set_page_load_timeout(page_timeout)
        driver.get(_target_url(url))
        rounds = _scroll_until_stable(driver, max_rounds, settle)
        dom = driver.page_source
        po = PrintOptions(); po.background = True
        import base64
        pdf = base64.b64decode(driver.print_page(po))
        shot = driver.get_screenshot_as_png() if screenshot else None
        final = driver.current_url
    finally:
        driver.quit()
    return CaptureResult(dom, pdf, shot, "firefox", rounds, final,
                         profile or "(ephemeral selenium profile)")


def capture_chrome(url: str, *, max_rounds: int = 60, settle: float = 0.35,
                   page_timeout: float = 60.0, screenshot: bool = True) -> CaptureResult:
    """Chrome headless via Selenium with an EXPLICIT throwaway --user-data-dir."""
    chrome = R.find_chrome()
    if not chrome:
        raise FileNotFoundError("no Chrome for capture --engine chrome "
                                "(set $HTMLDRILL_CHROME) — or use --engine firefox.")
    try:
        from selenium import webdriver
        from selenium.webdriver.common.print_page_options import PrintOptions
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError as e:
        raise FileNotFoundError(f"capture --engine chrome via selenium needs "
                                f"'selenium': {e}") from e

    profile = tempfile.mkdtemp(prefix="htmldrill-chrome-")
    opts = Options()
    opts.binary_location = chrome
    for a in ("--headless=new", "--no-sandbox", "--disable-gpu",
              "--disable-dev-shm-usage", "--hide-scrollbars",
              "--no-first-run", "--no-default-browser-check",
              f"--user-data-dir={profile}"):     # <- the isolation that matters
        opts.add_argument(a)
    driver = webdriver.Chrome(options=opts, service=Service())
    try:
        driver.set_page_load_timeout(page_timeout)
        driver.get(_target_url(url))
        rounds = _scroll_until_stable(driver, max_rounds, settle)
        dom = driver.page_source
        po = PrintOptions(); po.background = True
        import base64
        pdf = base64.b64decode(driver.print_page(po))
        shot = driver.get_screenshot_as_png() if screenshot else None
        final = driver.current_url
    finally:
        driver.quit()
        shutil.rmtree(profile, ignore_errors=True)   # never leave the profile behind
    return CaptureResult(dom, pdf, shot, "chrome", rounds, final, profile)


def _find_geckodriver() -> Optional[str]:
    env = os.environ.get("HTMLDRILL_GECKODRIVER")
    if env and Path(env).exists():
        return env
    p = shutil.which("geckodriver")
    if p:
        return p
    local = Path.home() / ".local/bin/geckodriver"
    return str(local) if local.exists() else None


def capture(url: str, engine: str = "firefox", **kw) -> CaptureResult:
    if engine == "firefox":
        return capture_firefox(url, **kw)
    if engine == "chrome":
        return capture_chrome(url, **kw)
    raise ValueError(f"unknown engine {engine!r} — choose firefox or chrome")
