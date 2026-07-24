"""Optional headless-browser rendering for JavaScript-heavy sites.

Plain HTTP requests only see a site's initial HTML. Single-page apps
(React/Vue/Angular) ship an almost-empty shell and build the real content
and links in the browser with JavaScript, so a requests-based crawl finds
nothing to follow. When the user opts in to "Render JavaScript", each page
is loaded in a real headless Chromium (via Playwright) and we save the
fully rendered DOM instead.

Chromium itself is NOT bundled with the app; it is downloaded once, on first
use of this mode, into Playwright's per-user browser cache.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

# Re-exec marker used by the frozen executable to run "playwright install".
INSTALL_CHROMIUM_FLAG = "--install-chromium"

# Optional override so a specific Chromium binary can be used instead of the
# one Playwright manages (handy for testing against a pre-provisioned build).
_EXECUTABLE_OVERRIDE = os.environ.get("WD_CHROMIUM_EXECUTABLE") or None


def playwright_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def _launch_kwargs() -> dict:
    kwargs = {"headless": True, "args": ["--no-sandbox"]}
    if _EXECUTABLE_OVERRIDE:
        kwargs["executable_path"] = _EXECUTABLE_OVERRIDE
    return kwargs


def chromium_installed() -> bool:
    """True only if Chromium can actually launch.

    We do a real (headless) launch rather than just checking that a file
    exists: newer Playwright launches a separate ``chrome-headless-shell``
    build, so the main Chromium binary being present isn't enough on its own.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**_launch_kwargs())
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def install_chromium(timeout: int = 900) -> None:
    """Download Chromium into Playwright's browser cache (one-time)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, INSTALL_CHROMIUM_FLAG]
    else:
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    subprocess.run(cmd, check=True, timeout=timeout)


class BrowserRenderer:
    """Renders URLs to their final (post-JavaScript) HTML.

    A Chromium instance is created lazily per calling thread (Playwright's
    sync objects are thread-affine), and all instances are closed together
    via :meth:`close` when the crawl finishes.
    """

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._tls = threading.local()
        self._instances: list = []
        self._lock = threading.Lock()

    def _browser(self):
        tls = self._tls
        if getattr(tls, "browser", None) is None:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.launch(**_launch_kwargs())
            tls.pw = pw
            tls.browser = browser
            with self._lock:
                self._instances.append((pw, browser))
        return tls.browser

    def render(self, url: str) -> tuple[str, str]:
        """Return (rendered_html, final_url) after the page's JS has run."""
        browser = self._browser()
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            # Give client-side rendering a moment to settle; ignore if the
            # network never fully idles (chat widgets, analytics, etc.).
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            return page.content(), page.url
        finally:
            page.close()

    def close(self) -> None:
        with self._lock:
            for pw, browser in self._instances:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._instances.clear()
