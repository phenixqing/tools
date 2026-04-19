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


# ── DOM-based extraction (no download button) ─────────────────────────────────

def _extract_positions_from_page(page, log) -> str:
    """
    Extract portfolio positions directly from the Fidelity positions page DOM.
    Returns a CSV string in Fidelity's standard 16-column download format.

    No download button or file download needed.
    Tries three selectors in order:
        1. tr.pvd-table__row  (confirmed Fidelity class from library source)
        2. table tbody tr     (standard HTML table fallback)
        3. [role="row"]       (ARIA fallback)

    Account context is tracked by detecting section header rows that match
    the Fidelity account-number pattern ([A-Z]\\d{5,} or \\d{7,}).
    """
    import csv as _csv
    import io  as _io

    log("Waiting for positions table…")

    loaded = False
    for sel in ["tr.pvd-table__row", "table tbody tr", "[role='row']"]:
        try:
            page.wait_for_selector(sel, timeout=12_000)
            loaded = True
            log(f"Table ready (selector: {sel})")
            break
        except Exception:
            continue
    if not loaded:
        raise RuntimeError(
            "Positions table did not appear within 12 s — page may still be loading."
        )

    log("Extracting table data from DOM…")

    # --- JavaScript runs inside the browser page ----------------------------
    raw = page.evaluate(r"""
() => {
    const clean = s =>
        (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    const ACT_RE = /([A-Z]\d{5,}|\d{7,})/;

    // ── headers ──────────────────────────────────────────────────────────
    let hdrEls = document.querySelectorAll('tr.pvd-table__header-row th');
    if (!hdrEls.length) hdrEls = document.querySelectorAll('thead th');
    if (!hdrEls.length) hdrEls = document.querySelectorAll('[role="columnheader"]');
    const headers = Array.from(hdrEls).map(el =>
        clean(el.innerText || el.textContent)
    );

    // ── data rows ─────────────────────────────────────────────────────────
    let rowEls = document.querySelectorAll('tr.pvd-table__row');
    if (!rowEls.length) rowEls = document.querySelectorAll('table tbody tr');
    if (!rowEls.length) rowEls = document.querySelectorAll('[role="row"]');

    let curAccNum = '', curAccName = '';
    const rows = [];

    Array.from(rowEls).forEach(row => {
        const tds       = Array.from(row.querySelectorAll('td, [role="cell"]'));
        const cellTexts = tds.map(td => clean(td.innerText || td.textContent || ''));
        const rowText   = cellTexts.join(' ');

        // Account section header: very few cells + contains an account number
        if (tds.length <= 2) {
            const m = rowText.match(ACT_RE);
            if (m) {
                curAccNum  = m[1];
                curAccName = rowText.replace(m[1], '').replace(/\s*[-–—,]\s*/g, ' ').trim();
            }
            return;
        }

        // Some layouts put the account number in the first short cell
        if (cellTexts.length >= 2) {
            const firstM = cellTexts[0].match(ACT_RE);
            if (firstM && cellTexts[0].length <= 12) {
                curAccNum  = firstM[1];
                curAccName = cellTexts[1] || '';
                return;
            }
        }

        // Regular position row
        rows.push({ accNum: curAccNum, accName: curAccName, cells: cellTexts });
    });

    return { headers, rows };
}
""")

    headers = raw.get("headers", [])
    rows    = raw.get("rows",    [])

    if not rows:
        try:
            page.screenshot(path="/tmp/fidelity_no_rows.png")
            log("Debug screenshot → /tmp/fidelity_no_rows.png")
        except Exception:
            pass
        raise RuntimeError(
            "No position rows found in page DOM.\n"
            "Fidelity may have updated its page structure — "
            "run with FIDELITY_HEADLESS=0 to inspect."
        )

    log(f"Found {len(rows)} position rows, {len(headers)} header columns.")

    # ── Map UI column names → CSV field indices ────────────────────────────
    FIELD_ALIASES: dict = {
        "Symbol":                    ["symbol"],
        "Description":               ["description", "name", "security name"],
        "Quantity":                  ["quantity", "qty", "shares"],
        "Last Price":                ["last price", "price", "market price", "last"],
        "Last Price Change":         ["last price change", "price change", "change $", "chg"],
        "Current Value":             ["current value", "value", "market value", "mkt value"],
        "Today's Gain/Loss Dollar":  [
            "today's gain/loss $", "today's g/l $", "day g/l $",
            "today gain/loss $", "today's gain/loss dollar",
        ],
        "Today's Gain/Loss Percent": [
            "today's gain/loss %", "today's g/l %", "day g/l %",
            "today gain/loss %", "today's gain/loss percent",
        ],
        "Total Gain/Loss Dollar":    [
            "total gain/loss $", "total g/l $", "unrealized gain/loss $",
            "gain/loss $", "total gain/loss dollar",
        ],
        "Total Gain/Loss Percent":   [
            "total gain/loss %", "total g/l %", "unrealized gain/loss %",
            "gain/loss %", "total gain/loss percent",
        ],
        "Percent Of Account":        ["percent of account", "% of account", "% account"],
        "Cost Basis Total":          ["cost basis total", "cost basis", "total cost basis", "total cost"],
        "Average Cost Basis":        ["average cost basis", "avg cost basis", "average cost", "avg cost"],
        "Type":                      ["type", "account type"],
    }

    h_lower = [h.lower() for h in headers]
    col_idx: dict = {}
    for csv_field, aliases in FIELD_ALIASES.items():
        all_aliases = [csv_field.lower()] + aliases
        for i, h in enumerate(h_lower):
            if h in all_aliases:
                col_idx[csv_field] = i
                break

    # Positional fallback if header detection failed
    # Typical Fidelity column order (UI table, without account columns):
    POSITIONAL = [
        "Symbol", "Description", "Quantity", "Last Price", "Last Price Change",
        "Current Value", "Today's Gain/Loss Dollar", "Today's Gain/Loss Percent",
        "Total Gain/Loss Dollar", "Total Gain/Loss Percent",
        "Percent Of Account", "Cost Basis Total", "Average Cost Basis", "Type",
    ]
    use_positional = not col_idx

    def _get(cells: list, field: str) -> str:
        if use_positional:
            try:
                idx = POSITIONAL.index(field)
            except ValueError:
                return ""
        else:
            idx = col_idx.get(field)
            if idx is None:
                return ""
        return cells[idx] if idx < len(cells) else ""

    # ── Build CSV string ───────────────────────────────────────────────────
    CSV_COLS = [
        "Account Number", "Account Name", "Symbol", "Description",
        "Quantity", "Last Price", "Last Price Change", "Current Value",
        "Today's Gain/Loss Dollar", "Today's Gain/Loss Percent",
        "Total Gain/Loss Dollar", "Total Gain/Loss Percent",
        "Percent Of Account", "Cost Basis Total", "Average Cost Basis", "Type",
    ]
    DATA_COLS = CSV_COLS[2:]   # everything after the two account columns

    out    = _io.StringIO()
    writer = _csv.writer(out)
    writer.writerow(CSV_COLS)

    for row in rows:
        cells = row["cells"]
        writer.writerow([
            row["accNum"], row["accName"],
            *[_get(cells, f) for f in DATA_COLS],
        ])

    return out.getvalue()


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

    def _navigate_to_positions(self, log) -> None:
        """Navigate to the portfolio positions page and wait for it to fully load."""
        log("Navigating to Portfolio Positions page…")
        self._browser.page.goto(
            "https://digital.fidelity.com/ftgw/digital/portfolio/positions"
        )
        self._browser.wait_for_loading_sign()
        self._browser.page.wait_for_timeout(1_500)
        self._browser.wait_for_loading_sign(timeout=int(2.5 * 60 * 1_000))

        url = self._browser.page.url.lower()
        if "signin" in url or "login" in url:
            raise RuntimeError("Session expired — redirected to login page.")

    def _extract_from_page(self, log) -> str:
        """Navigate to positions page and extract CSV data via DOM scraping."""
        self._navigate_to_positions(log)
        return _extract_positions_from_page(self._browser.page, log)

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
        Extract positions CSV data and return it as a string.

        Uses DOM scraping — no download button, no disk I/O.
        Reuses the existing browser session; re-logins automatically on expiry.
        """
        with self._lock:
            try:
                if self._is_alive():
                    log("Reusing existing Fidelity session.")
                else:
                    self._close_browser()
                    self._login(headless, log)
                return self._extract_from_page(log)
            except Exception as first_err:
                log(f"First attempt failed ({first_err}). Re-logging in…")

            # Attempt 2: force fresh login
            self._close_browser()
            self._login(headless, log)
            return self._extract_from_page(log)

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
