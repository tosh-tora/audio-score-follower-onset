#!/usr/bin/env python3
"""
slide_controller.py - Google Slides Browser Automation via Playwright

Owns a Chromium instance running a Google Slides presentation. Key-press
commands are received from other threads via a Queue and applied to the
browser page from the dedicated worker thread (sync Playwright is
single-threaded by design).

Typical lifecycle::

    sc = SlideController(slide_url="https://docs.google.com/presentation/d/.../present")
    sc.start()
    sc.wait_ready(timeout=30.0)
    sc.press("right")           # advance one slide
    sc.press("left")            # go back
    sc.stop()

The Chromium window is launched non-headless so the user can drag it to the
projector monitor and switch to fullscreen (F11). The /present URL form
auto-enters Google Slides' built-in presentation mode.

Action mapping
--------------
The existing config.json uses short action names ("right", "left", ...).
These are mapped to Playwright key identifiers (ArrowRight, ArrowLeft, ...).
Unknown actions are passed through verbatim so any Playwright-valid key name
also works (e.g. "Space", "Enter", "F5").
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

logger = logging.getLogger(__name__)


# config.json `action` strings → Playwright key identifiers.
# https://playwright.dev/python/docs/api/class-keyboard#keyboard-press
_KEY_MAP = {
    "right": "ArrowRight",
    "left": "ArrowLeft",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "space": "Space",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "pgdn": "PageDown",
    "pagedown": "PageDown",
    "pgup": "PageUp",
    "pageup": "PageUp",
}


class SlideController:
    """
    Thread-backed Playwright wrapper for slide control.

    Browser lifetime is bound to the worker thread. Commands are FIFO via a
    queue; key presses are non-blocking from the caller's perspective.
    """

    def __init__(
        self,
        slide_url: str,
        *,
        headless: bool = False,
        viewport: Optional[dict] = None,
    ) -> None:
        """
        Args:
            slide_url: Google Slides URL (recommended: the ``/present?...``
                variant so the page opens in presentation mode automatically).
            headless: Run Chromium without a UI window. Default False; set
                True only for automated tests.
            viewport: Optional viewport dict (``{"width": ..., "height": ...}``).
                Default uses the OS window size.
        """
        self.slide_url = slide_url
        self.headless = headless
        self.viewport = viewport

        self._command_queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._fatal_error: Optional[BaseException] = None

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        """Spawn the Playwright worker thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("SlideController.start() called but thread already running")
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self._fatal_error = None

        self._thread = threading.Thread(
            target=self._run,
            name="slide-controller",
            daemon=True,
        )
        self._thread.start()
        logger.info("SlideController worker thread started")

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Block until Chromium has loaded the slide URL (or has failed)."""
        return self._ready_event.wait(timeout)

    @property
    def last_error(self) -> Optional[BaseException]:
        """Fatal error from the worker, if any."""
        return self._fatal_error

    def press(self, action: str) -> None:
        """Queue a key press. Returns immediately; press happens in worker thread."""
        key = _KEY_MAP.get(action.lower(), action)
        self._command_queue.put(("press", key))
        logger.debug("Queued key press: %s → %s", action, key)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal worker to close the browser and exit."""
        self._stop_event.set()
        # Wake the queue.get() if it's blocked
        self._command_queue.put(None)

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "SlideController worker did not stop within %.1fs", timeout
                )
        self._thread = None
        logger.info("SlideController stopped")

    # ----------------------------------------------------------------- private
    def _run(self) -> None:
        """Worker thread main loop: own Playwright + process commands."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            self._fatal_error = exc
            logger.error(
                "Playwright is not installed. Inside WSL2, run:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            )
            self._ready_event.set()
            return

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--start-maximized",
                        # Reduce automation banner / detection so Google Slides
                        # behaves like a normal user session.
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context_kwargs = {"no_viewport": self.viewport is None}
                if self.viewport is not None:
                    context_kwargs = {"viewport": self.viewport}
                context = browser.new_context(**context_kwargs)
                page = context.new_page()

                logger.info("Navigating Chromium to slide URL: %s", self.slide_url)
                # ``domcontentloaded`` is enough for Google Slides; ``load``
                # can hang waiting for analytics requests.
                page.goto(self.slide_url, wait_until="domcontentloaded")
                # Give Slides a moment to set up the presentation viewer and
                # take keyboard focus.
                page.wait_for_timeout(1500)
                # Click once on the slide area so subsequent key events land
                # on the presentation viewer rather than (e.g.) the URL bar.
                try:
                    page.locator("body").click(timeout=2000)
                except Exception:  # noqa: BLE001 — non-fatal
                    logger.debug("Initial body click failed; continuing")

                self._ready_event.set()
                logger.info(
                    "SlideController ready. Drag the Chromium window to the "
                    "projector monitor and press F11 / F5 for fullscreen."
                )

                while not self._stop_event.is_set():
                    try:
                        cmd = self._command_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    if cmd is None:
                        # Sentinel from stop()
                        break

                    op, arg = cmd
                    if op == "press":
                        try:
                            page.keyboard.press(arg)
                            logger.info("Sent key press: %s", arg)
                        except Exception as exc:  # noqa: BLE001
                            logger.error("Key press %s failed: %s", arg, exc, exc_info=True)
                    else:
                        logger.warning("Unknown slide controller command: %s", op)

                try:
                    context.close()
                    browser.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Browser teardown raised: %s", exc)

        except Exception as exc:  # noqa: BLE001
            self._fatal_error = exc
            logger.error("SlideController fatal: %s", exc, exc_info=True)
        finally:
            self._ready_event.set()  # unblock waiters even on failure
            logger.info("SlideController worker exiting")

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        alive = self._thread is not None and self._thread.is_alive()
        return f"SlideController(url={self.slide_url!r}, running={alive})"


class NullSlideController:
    """No-op slide controller used when --slide-url is omitted (dry-run / test mode)."""

    def start(self) -> None:
        logger.info("[dry-run] SlideController: start (no browser)")

    def wait_ready(self, timeout: float = 30.0) -> bool:  # noqa: ARG002
        return True

    def stop(self) -> None:
        logger.info("[dry-run] SlideController: stop")

    def press(self, action: str) -> None:
        # No log here — the canonical "slide press" log is emitted by
        # AudioScoreFollowerApp._execute_action so it can include the
        # source tag (manual/auto) and the triggering measure.
        return None

    @property
    def last_error(self) -> None:
        return None
