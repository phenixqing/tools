"""
Fidelity portfolio CSV downloader.

Uses the `fidelity` Python library for browser automation and login
(handles TOTP 2FA, loading spinners, device trust automatically),
then downloads the positions CSV via the authenticated page.

Required env vars
-----------------
FIDELITY_USERNAME      Fidelity customer ID / username
FIDELITY_PASSWORD      Fidelity password

Optional env vars
-----------------
FIDELITY_TOTP_SECRET   Base32 TOTP secret for fully-automated 2FA.
                       Obtain by re-enrolling your authenticator app and
                       copying the "enter key manually" base32 string.
FIDELITY_HEADLESS      Set to "0" to show the browser window.

Usage
-----
As a library (called from FastAPI):
    from fidelity_sync import run_sync, FidelitySession
    await run_sync(output_path=Path("blob/Portfolio_Positions_Latest.csv"))

As a standalone script:
    python fidelity_sync.py [--no-headless]
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional


# ── synchronous core ───────────────────────────────────────────────────────────

def _do_sync(output_path: Path, headless: bool, log) -> None:
    """
    Synchronous implementation — runs the Playwright browser in the current
    thread (required because the `fidelity` library uses sync_playwright).
    """
    try:
        from fidelity import fidelity as fid_lib
    except ImportError:
        raise RuntimeError(
            "The `fidelity` library is required: pip install fidelity"
        )

    username    = os.environ.get("FIDELITY_USERNAME", "").strip()
    password    = os.environ.get("FIDELITY_PASSWORD", "").strip()
    totp_secret = os.environ.get("FIDELITY_TOTP_SECRET", "").strip() or None

    if not username or not password:
        raise RuntimeError(
            "FIDELITY_USERNAME and FIDELITY_PASSWORD environment variables must be set."
        )

    _headless = headless and os.environ.get("FIDELITY_HEADLESS", "1") != "0"
    log(f"Launching {'headless' if _headless else 'visible'} Firefox…")

    browser = fid_lib.FidelityAutomation(headless=_headless, save_state=False)

    try:
        # ── login ─────────────────────────────────────────────────────────────
        log("Logging in to Fidelity…")
        step1, step2 = browser.login(
            username=username,
            password=password,
            totp_secret=totp_secret,
            save_device=True,
        )

        if not step1:
            raise RuntimeError(
                "Fidelity login failed — check FIDELITY_USERNAME and FIDELITY_PASSWORD."
            )

        if not step2:
            if totp_secret:
                raise RuntimeError(
                    "Fidelity 2FA failed — the FIDELITY_TOTP_SECRET may be incorrect.\n"
                    "Re-enroll your authenticator app to get the correct base32 key."
                )
            elif not _headless:
                # Visible browser — user can complete 2FA manually; just wait
                log("⚠  2FA required — please complete it in the browser window.")
                log("Waiting up to 3 minutes…")
                deadline = time.time() + 180
                completed = False
                while time.time() < deadline:
                    time.sleep(3)
                    url = browser.page.url.lower()
                    if "signin" not in url and "login" not in url:
                        completed = True
                        break
                if not completed:
                    raise RuntimeError("Timed out waiting for manual 2FA.")
            else:
                raise RuntimeError(
                    "Fidelity requires 2FA but FIDELITY_TOTP_SECRET is not set.\n"
                    "Set FIDELITY_TOTP_SECRET to your authenticator base32 key, or\n"
                    "set FIDELITY_HEADLESS=0 to complete 2FA in the browser window."
                )

        log("Login successful.")

        # ── navigate to positions page ─────────────────────────────────────
        log("Navigating to Portfolio Positions page…")
        browser.page.goto(
            "https://digital.fidelity.com/ftgw/digital/portfolio/positions"
        )
        browser.wait_for_loading_sign()
        browser.page.wait_for_timeout(1_000)
        browser.wait_for_loading_sign(timeout=int(2.5 * 60 * 1_000))

        # ── download CSV ───────────────────────────────────────────────────
        log("Looking for Download button…")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = False

        # New Fidelity UI: "Available Actions" → "Download"
        try:
            browser.page.get_by_role("button", name="Available Actions").click(timeout=8_000)
            with browser.page.expect_download(timeout=30_000) as dl_info:
                browser.page.get_by_role("menuitem", name="Download").click()
            dl_info.value.save_as(str(output_path))
            downloaded = True
            log("Downloaded via 'Available Actions' menu.")
        except Exception:
            pass

        # Old Fidelity UI: "Download Positions" label/button
        if not downloaded:
            try:
                with browser.page.expect_download(timeout=30_000) as dl_info:
                    browser.page.get_by_label("Download Positions").click(timeout=8_000)
                dl_info.value.save_as(str(output_path))
                downloaded = True
                log("Downloaded via 'Download Positions' button.")
            except Exception:
                pass

        if not downloaded:
            # Save screenshot to help debug
            try:
                shot = output_path.parent / "fidelity_error.png"
                browser.page.screenshot(path=str(shot))
                log(f"Error screenshot → {shot}")
            except Exception:
                pass
            raise RuntimeError(
                "Could not find a Download button on the Positions page.\n"
                "Fidelity may have updated its UI — run with FIDELITY_HEADLESS=0 to inspect."
            )

        size = output_path.stat().st_size
        log(f"Saved → {output_path}  ({size:,} bytes)")

    except Exception:
        # Best-effort error screenshot
        try:
            shot = output_path.parent / "fidelity_error.png"
            browser.page.screenshot(path=str(shot))
            log(f"Error screenshot → {shot}")
        except Exception:
            pass
        raise
    finally:
        browser.close_browser()


# ── session-reuse class ────────────────────────────────────────────────────────

class FidelitySession:
    """
    Keeps a FidelityAutomation browser alive across multiple syncs so that
    each subsequent download only navigates to the positions page and clicks
    Download — no repeated login.

    On session expiry (redirect to login page) the session re-logins
    automatically.  A threading.Lock ensures only one download at a time.
    """

    def __init__(self) -> None:
        self._browser = None          # FidelityAutomation instance or None
        self._lock    = threading.Lock()

    # ── internal ────────────────────────────────────────────────────────────

    def _is_alive(self) -> bool:
        """Return True if the existing browser looks usable."""
        if self._browser is None:
            return False
        try:
            url = self._browser.page.url.lower()
            # Consider alive if NOT on any login/error page
            return "signin" not in url and "login" not in url and "error" not in url
        except Exception:
            return False

    def _login(self, headless: bool, log) -> None:
        """Create a fresh browser and log in."""
        try:
            from fidelity import fidelity as fid_lib
        except ImportError:
            raise RuntimeError("The `fidelity` library is required: pip install fidelity")

        username    = os.environ.get("FIDELITY_USERNAME", "").strip()
        password    = os.environ.get("FIDELITY_PASSWORD", "").strip()
        totp_secret = os.environ.get("FIDELITY_TOTP_SECRET", "").strip() or None

        if not username or not password:
            raise RuntimeError(
                "FIDELITY_USERNAME and FIDELITY_PASSWORD environment variables must be set."
            )

        _headless = headless and os.environ.get("FIDELITY_HEADLESS", "1") != "0"
        log(f"Starting new Fidelity session ({'headless' if _headless else 'visible'})…")

        browser = fid_lib.FidelityAutomation(headless=_headless, save_state=False)
        step1, step2 = browser.login(
            username=username,
            password=password,
            totp_secret=totp_secret,
            save_device=True,
        )

        if not step1:
            browser.close_browser()
            raise RuntimeError(
                "Fidelity login failed — check FIDELITY_USERNAME and FIDELITY_PASSWORD."
            )
        if not step2:
            browser.close_browser()
            if totp_secret:
                raise RuntimeError(
                    "Fidelity 2FA failed — check FIDELITY_TOTP_SECRET."
                )
            raise RuntimeError(
                "Fidelity requires 2FA but FIDELITY_TOTP_SECRET is not set."
            )

        self._browser = browser
        log("Fidelity session established.")

    def _close_browser(self) -> None:
        """Close and discard the current browser (best-effort)."""
        if self._browser is not None:
            try:
                self._browser.close_browser()
            except Exception:
                pass
            self._browser = None

    def _download_to_path(self, output_path: Path, log) -> None:
        """Navigate to positions page and download CSV (session must already be live)."""
        log("Navigating to Portfolio Positions page…")
        self._browser.page.goto(
            "https://digital.fidelity.com/ftgw/digital/portfolio/positions"
        )
        self._browser.wait_for_loading_sign()
        self._browser.page.wait_for_timeout(1_000)
        self._browser.wait_for_loading_sign(timeout=int(2.5 * 60 * 1_000))

        # After navigation, verify we're not on a login page
        url = self._browser.page.url.lower()
        if "signin" in url or "login" in url:
            raise RuntimeError("Session expired — redirected to login page.")

        log("Looking for Download button…")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = False

        # New Fidelity UI: "Available Actions" → "Download"
        try:
            self._browser.page.get_by_role("button", name="Available Actions").click(timeout=8_000)
            with self._browser.page.expect_download(timeout=30_000) as dl_info:
                self._browser.page.get_by_role("menuitem", name="Download").click()
            dl_info.value.save_as(str(output_path))
            downloaded = True
            log("Downloaded via 'Available Actions' menu.")
        except Exception:
            pass

        # Old Fidelity UI: "Download Positions" label/button
        if not downloaded:
            try:
                with self._browser.page.expect_download(timeout=30_000) as dl_info:
                    self._browser.page.get_by_label("Download Positions").click(timeout=8_000)
                dl_info.value.save_as(str(output_path))
                downloaded = True
                log("Downloaded via 'Download Positions' button.")
            except Exception:
                pass

        if not downloaded:
            try:
                shot = output_path.parent / "fidelity_error.png"
                self._browser.page.screenshot(path=str(shot))
                log(f"Error screenshot → {shot}")
            except Exception:
                pass
            raise RuntimeError(
                "Could not find a Download button on the Positions page."
            )

        size = output_path.stat().st_size
        log(f"Saved → {output_path}  ({size:,} bytes)")

    # ── public API ───────────────────────────────────────────────────────────

    def get_csv_to_path(self, output_path: Path, headless: bool = True, log=print) -> None:
        """
        Download positions CSV to output_path, reusing the existing browser
        session where possible.  Re-logins automatically on session expiry.
        """
        with self._lock:
            # Attempt 1: reuse existing session (or create fresh one if none)
            try:
                if self._is_alive():
                    log("Reusing existing Fidelity session.")
                else:
                    self._close_browser()
                    self._login(headless, log)
                self._download_to_path(output_path, log)
                return
            except Exception as first_err:
                log(f"First attempt failed ({first_err}). Re-logging in…")

            # Attempt 2: force fresh login
            self._close_browser()
            self._login(headless, log)
            self._download_to_path(output_path, log)

    def get_csv_memory(self, headless: bool = True, log=print) -> str:
        """
        Download positions CSV and return its content as a string.
        Uses a temp file internally; nothing is written to persistent storage.
        """
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            self.get_csv_to_path(tmp_path, headless, log)
            return tmp_path.read_text(encoding="utf-8-sig")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def close(self) -> None:
        """Close the browser session (call on shutdown)."""
        with self._lock:
            self._close_browser()


# ── public async API ───────────────────────────────────────────────────────────

def _do_sync_to_memory(headless: bool, log) -> str:
    """Run a standalone sync (fresh login each time) and return CSV as string."""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp_path = Path(f.name)
    try:
        _do_sync(tmp_path, headless, log)
        return tmp_path.read_text(encoding="utf-8-sig")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


async def run_sync_memory(headless: bool = True, log=print) -> str:
    """Async: run Fidelity sync and return CSV as string (no persistent disk file)."""
    return await asyncio.to_thread(_do_sync_to_memory, headless, log)


async def run_sync(
    output_path: Path,
    headless: bool = True,
    log=print,
) -> None:
    """
    Async wrapper around _do_sync().

    Runs the synchronous Playwright code in a thread pool so it doesn't
    block the FastAPI event loop.

    Reads credentials from environment variables:
      FIDELITY_USERNAME, FIDELITY_PASSWORD, FIDELITY_TOTP_SECRET (optional)
    """
    await asyncio.to_thread(_do_sync, output_path, headless, log)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download Fidelity portfolio CSV")
    ap.add_argument("--no-headless", action="store_true",
                    help="Show browser window (useful for debugging / manual 2FA)")
    ap.add_argument("--output", default=None,
                    help="Output CSV path (default: blob/Portfolio_Positions_Latest.csv)")
    args = ap.parse_args()

    out = Path(args.output) if args.output else (
        Path(__file__).parent.parent / "blob" / "Portfolio_Positions_Latest.csv"
    )

    t0 = time.time()
    _do_sync(
        output_path=out,
        headless=not args.no_headless,
        log=lambda msg: print(f"[fidelity_sync] {msg}"),
    )
    print(f"Done in {time.time() - t0:.1f}s  →  {out}")
