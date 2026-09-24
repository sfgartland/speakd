#!/usr/bin/env python3
"""Browser check for the Settings panel in clients/gui/pinned.html.

Run with: uv run --with playwright python clients/gui/test-browser/settings_check.py

There is no daemon here and none is started: a plain browser tab always gets
`SimulatedSource` (see `resolveSource()` in shared/speakd-source.js), which is
exactly the environment this window is developed and reviewed in. The script
serves clients/gui itself over `python3 -m http.server`, drives real Chrome
through Playwright, and exits non-zero on the first failed check.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GUI_DIR = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"http.server on {port} never came up")


class Checks:
    """Collects pass/fail rather than raising on the first miss, so one run
    reports everything wrong at once instead of a failure at a time."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.count = 0

    def check(self, ok: bool, description: str) -> None:
        self.count += 1
        mark = "ok" if ok else "FAIL"
        print(f"  [{mark}] {description}")
        if not ok:
            self.failures.append(description)


def run(port: int) -> int:
    checks = Checks()
    console_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page()
        def on_console(msg) -> None:
            # The plain `http.server` has no favicon.ico, so Chrome's own
            # auto-request for one 404s on every run; that is the static
            # server's absence, not a bug in the page, so it is the one
            # console error this check knows to ignore.
            if msg.type != "error":
                return
            if (msg.location or {}).get("url", "").endswith("/favicon.ico"):
                return
            console_errors.append(msg.text)

        page.on("console", on_console)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        page.goto(f"http://127.0.0.1:{port}/pinned.html")
        page.wait_for_selector("#gear")
        # `resolveSource()` is awaited at module scope; give it a tick to
        # settle and expose `window.__speakdSource` before anything below
        # tries to reach it.
        page.wait_for_function("window.__speakdSource !== undefined")

        # ---- the gear opens settings ----
        page.click("#gear")
        page.wait_for_selector("#settings:not([hidden])")
        checks.check(
            page.eval_on_selector("#gear", "el => el.getAttribute('aria-pressed')") == "true",
            "gear reports aria-pressed=true once open",
        )
        checks.check(
            page.eval_on_selector("#win", "el => 'settings' in el.dataset") is True,
            "the window carries data-settings while open",
        )

        # ---- every control type renders ----
        # ids are `set-<key>` with dots replaced by dashes, set in
        # pinned.html's settingRow() — see the fixture schema added to
        # SimulatedSource for what each of these declares.
        control_ids = {
            "bool switch": "set-speech-detect_language",
            "float number input": "set-speech-rate",
            "multiline string textarea": "set-speech-preamble",
            "int number input": "set-zotero-max_snippet",
            "choice select": "set-zotero-citation_style",
            "voice text input": "set-zotero-default_voice",
            "restart-flagged int": "set-http-port",
        }
        for label, control_id in control_ids.items():
            checks.check(
                page.query_selector(f"#{control_id}") is not None,
                f"{label} renders (#{control_id})",
            )
        checks.check(
            page.query_selector("input.switch") is not None, "bool renders as a switch input"
        )
        checks.check(
            page.query_selector("table.vmap") is not None, "voice_map renders as a table"
        )
        checks.check(
            page.query_selector('.set-restart') is not None,
            "the restart-flagged setting shows its note",
        )

        # ---- changing a number commits and persists in the sim ----
        page.fill("#set-zotero-max_snippet", "500")
        page.dispatch_event("#set-zotero-max_snippet", "change")
        page.wait_for_function(
            "document.querySelector('#set-zotero-max_snippet')"
            ".closest('.set-row').querySelector('.set-error').textContent === ''"
        )
        checks.check(
            page.eval_on_selector("#set-zotero-max_snippet", "el => el.value") == "500",
            "the number input shows the committed value",
        )
        # Close and reopen the panel: `loadSettings()` re-reads `settings`
        # from the sim, so this proves the value actually landed there and
        # was not just left sitting in the DOM.
        page.click("#gear")
        # The closed panel is hidden, not removed — wait for attachment with
        # the attribute, not visibility, or this would wait forever.
        page.wait_for_selector("#settings[hidden]", state="attached")
        page.click("#gear")
        page.wait_for_selector("#settings:not([hidden])")
        checks.check(
            page.eval_on_selector("#set-zotero-max_snippet", "el => el.value") == "500",
            "the committed number survives closing and reopening the panel",
        )

        # ---- an out-of-range value shows an error and reverts ----
        # zotero.max_snippet's declared max is 4000.
        page.fill("#set-zotero-max_snippet", "9000")
        page.dispatch_event("#set-zotero-max_snippet", "change")
        page.wait_for_function(
            "document.querySelector('#set-zotero-max_snippet')"
            ".closest('.set-row').querySelector('.set-error').textContent !== ''"
        )
        error_text = page.eval_on_selector(
            "#set-zotero-max_snippet",
            "el => el.closest('.set-row').querySelector('.set-error').textContent",
        )
        checks.check("maximum" in error_text, f"the error names the rule ({error_text!r})")
        checks.check(
            page.eval_on_selector("#set-zotero-max_snippet", "el => el.value") == "500",
            "the control reverts to the last good value after a refusal",
        )

        # ---- a `setting` event updates an unfocused control ----
        page.evaluate(
            "document.activeElement && document.activeElement.blur"
            " && document.activeElement.blur()"
        )
        page.evaluate(
            "() => window.__speakdSource.send('set_setting',"
            " { key: 'zotero.citation_style', value: 'apa' })"
        )
        page.wait_for_function(
            "document.querySelector('#set-zotero-citation_style').value === 'apa'"
        )
        checks.check(True, "an externally-originated `setting` event updates an unfocused control")

        # A focused control must NOT be clobbered by the same event.
        page.click("#set-zotero-default_voice")
        page.fill("#set-zotero-default_voice", "mid-edit")
        page.evaluate(
            "() => window.__speakdSource.send('set_setting',"
            " { key: 'zotero.default_voice', value: 'if_sara_alt' })"
        )
        page.wait_for_timeout(100)
        checks.check(
            page.eval_on_selector("#set-zotero-default_voice", "el => el.value") == "mid-edit",
            "a `setting` event leaves a focused control's in-progress edit alone",
        )

        browser.close()

    checks.check(len(console_errors) == 0, f"no page errors (got {console_errors!r})")

    print(f"\n{checks.count - len(checks.failures)}/{checks.count} checks passed")
    return 1 if checks.failures else 0


def main() -> int:
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=GUI_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_server(port)
        return run(port)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
