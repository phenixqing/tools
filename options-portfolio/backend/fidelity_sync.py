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
import logging as _logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

# Use the same named logger that main.py sets up; NullHandler = silent when run standalone
_dbg = _logging.getLogger("portfolio")
_dbg.addHandler(_logging.NullHandler())

_BOT_DETECTION_MSG = (
    "Fidelity blocked the automated login (bot detection — "
    "'Sorry, we can't complete this action').\n\n"
    "Run this one-time setup command to log in with a visible browser and\n"
    "save the session for future headless syncs:\n\n"
    "  python backend/fidelity_sync.py --setup\n\n"
    "A Firefox window will open.  Log in normally (including TOTP if prompted)\n"
    "and the session will be saved automatically when the window closes."
)


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

    Uses Python Playwright APIs — NOT JavaScript querySelectorAll — because
    Fidelity's positions table is rendered inside Angular Shadow DOM components
    (pvd-* custom elements).  Playwright's get_by_role() and .locator() calls
    pierce Shadow DOM automatically; document.querySelectorAll() does not.

    Account context is tracked by detecting section header rows that match
    the Fidelity account-number pattern ([A-Z]\\d{5,} or \\d{7,}).
    """
    import csv as _csv
    import io  as _io
    import re  as _re

    log("Waiting for positions table…")

    # Poll until at least one row is visible — Playwright's role selector
    # pierces Shadow DOM, so this works where wait_for_selector("tr") fails.
    loaded = False
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if page.get_by_role("row").count() > 1:
                loaded = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not loaded:
        raise RuntimeError(
            "Positions table did not appear within 30 s — page may still be loading."
        )

    log("Extracting table data from DOM…")

    ACT_RE = _re.compile(r'([A-Z]\d{5,}|\d{7,})')

    def _clean(s: str) -> str:
        return (s or '').replace('\u00a0', ' ').replace('\u200b', '').strip()

    # ── Walk every row using Playwright Python API (pierces Shadow DOM) ───────
    all_rows = page.get_by_role("row").all()
    _dbg.debug(f"_extract: {len(all_rows)} total rows via get_by_role('row')")

    headers    = []
    data_rows  = []
    curAccNum  = ''
    curAccName = ''

    for row in all_rows:
        # ── Header row? ──────────────────────────────────────────────────────
        hdr_cells = row.locator("[role='columnheader'], th").all()
        if hdr_cells:
            texts = [_clean(c.inner_text()) for c in hdr_cells]
            if any(texts):           # ignore empty phantom header rows
                headers = texts
                _dbg.debug(f"_extract: header row: {headers!r}")
            continue

        # ── Data cells ───────────────────────────────────────────────────────
        cells = row.locator("td, [role='gridcell'], [role='cell']").all()
        if not cells:
            continue

        cell_texts = [_clean(c.inner_text()) for c in cells]
        row_text   = ' '.join(cell_texts)

        # Account section header row: ≤ 2 cells + contains an account number
        if len(cells) <= 2:
            m = ACT_RE.search(row_text)
            if m:
                curAccNum  = m.group(1)
                curAccName = _re.sub(
                    r'\s*[-\u2013\u2014,]\s*', ' ',
                    row_text.replace(m.group(1), ''),
                ).strip()
                _dbg.debug(f"_extract: account header: {curAccNum!r} / {curAccName!r}")
            continue

        # Some layouts put the account number in the first short cell only
        if len(cell_texts) >= 2:
            fm = ACT_RE.match(cell_texts[0])
            if fm and len(cell_texts[0]) <= 12:
                curAccNum  = fm.group(1)
                curAccName = cell_texts[1]
                continue

        # Skip spacer / total / summary rows with too few non-empty cells
        if sum(1 for t in cell_texts if t) < 2:
            continue

        data_rows.append({
            'accNum':  curAccNum,
            'accName': curAccName,
            'cells':   cell_texts,
        })

    if not data_rows:
        _dbg.error(
            f"DOM extraction: no rows found. "
            f"headers={headers!r}  page_url={page.url!r}"
        )
        try:
            page.screenshot(path="/tmp/fidelity_no_rows.png")
            log("Debug screenshot → /tmp/fidelity_no_rows.png")
            _dbg.error("Debug screenshot saved to /tmp/fidelity_no_rows.png")
        except Exception:
            pass
        raise RuntimeError(
            "No position rows found in page DOM.\n"
            "Fidelity may have updated its page structure — "
            "run with FIDELITY_HEADLESS=0 to inspect."
        )

    _dbg.info(f"DOM extraction: {len(data_rows)} rows, {len(headers)} header cols, headers={headers!r}")
    log(f"Found {len(data_rows)} position rows, {len(headers)} header columns.")

    # ── Map UI column names → CSV field indices ────────────────────────────────
    # Aliases include both the full download-CSV names and the shorter UI names
    # that Fidelity's table actually displays (e.g. "Today's gain/loss" without $).
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
            "today's gain/loss",        # UI short form (no $ / %)
        ],
        "Today's Gain/Loss Percent": [
            "today's gain/loss %", "today's g/l %", "day g/l %",
            "today gain/loss %", "today's gain/loss percent",
        ],
        "Total Gain/Loss Dollar":    [
            "total gain/loss $", "total g/l $", "unrealized gain/loss $",
            "gain/loss $", "total gain/loss dollar",
            "total gain/loss",          # UI short form
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

    _dbg.debug(f"_extract: column mapping: {col_idx!r}")

    # Positional fallback when header matching yields nothing.
    # Typical Fidelity UI column order (9-col table as of 2024):
    #   Symbol | Last price | Today's gain/loss | Total gain/loss |
    #   Current value | % of account | Quantity | Cost basis | 52-week range
    POSITIONAL_UI = [
        "Symbol", "Last Price", "Today's Gain/Loss Dollar", "Total Gain/Loss Dollar",
        "Current Value", "Percent Of Account", "Quantity", "Cost Basis Total",
    ]
    # Fallback to the full download-CSV column order if needed
    POSITIONAL_DOWNLOAD = [
        "Symbol", "Description", "Quantity", "Last Price", "Last Price Change",
        "Current Value", "Today's Gain/Loss Dollar", "Today's Gain/Loss Percent",
        "Total Gain/Loss Dollar", "Total Gain/Loss Percent",
        "Percent Of Account", "Cost Basis Total", "Average Cost Basis", "Type",
    ]

    use_positional = not col_idx
    # Pick the positional list whose length is closest to our actual cell count
    if use_positional and data_rows:
        sample_len = len(data_rows[0]['cells'])
        if abs(sample_len - len(POSITIONAL_UI)) <= abs(sample_len - len(POSITIONAL_DOWNLOAD)):
            POSITIONAL = POSITIONAL_UI
        else:
            POSITIONAL = POSITIONAL_DOWNLOAD
        _dbg.debug(f"_extract: using positional fallback ({len(POSITIONAL)}-col), sample_len={sample_len}")
    else:
        POSITIONAL = POSITIONAL_DOWNLOAD

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

    # ── Build CSV string ───────────────────────────────────────────────────────
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

    for row in data_rows:
        cells = row["cells"]
        writer.writerow([
            row["accNum"], row["accName"],
            *[_get(cells, f) for f in DATA_COLS],
        ])

    return out.getvalue()


# ── session-reuse class ────────────────────────────────────────────────────────

class FidelitySession:
    """
    Keeps a Fidelity browser session alive across multiple syncs.

    Design
    ------
    * Uses ``fidelity.FidelityAutomation`` for browser setup, stealth, and login
      (Firefox + playwright_stealth, same as the fidelity pip package).
    * Adds session persistence: browser storage state (cookies + localStorage)
      is saved to a JSON file after every successful login and restored on the
      next startup so the login page is skipped entirely.
    * Bot-detection handling: if Fidelity shows "Sorry, we can't complete this
      action" in headless mode, a clear error is raised instructing the user to
      run once with ``FIDELITY_HEADLESS=0`` for a manual visible-browser login.
    * On session expiry the session re-logins automatically.
    * A ``threading.Lock`` ensures only one operation runs at a time.

    Parameters
    ----------
    session_dir : Path | None
        Directory where the browser storage state is kept.  The fidelity
        library creates ``{session_dir}/Fidelity.json`` inside it.
        If ``None``, no disk caching.
    """

    def __init__(self, session_dir: Optional[Path] = None) -> None:
        self._fid         = None        # FidelityAutomation instance, or None
        self._lock        = threading.Lock()
        self._session_dir = session_dir
        # Actual JSON file the fidelity library creates inside session_dir
        self._session_file = (session_dir / "Fidelity.json") if session_dir else None

    # ── internal helpers ─────────────────────────────────────────────────────

    def _is_alive(self) -> bool:
        """Return True if the existing browser looks usable."""
        if self._fid is None:
            return False
        try:
            url = self._fid.page.url.lower()
            return not any(kw in url for kw in ("signin", "login", "error"))
        except Exception:
            return False

    def _wait_for_loading(self, timeout: int = 30_000) -> None:
        """Delegate to FidelityAutomation.wait_for_loading_sign()."""
        if self._fid is not None:
            try:
                self._fid.wait_for_loading_sign(timeout=timeout)
            except Exception:
                pass

    def _close_browser(self) -> None:
        """Close FidelityAutomation browser (saves state if save_state=True)."""
        if self._fid is not None:
            try:
                self._fid.close_browser()
            except Exception:
                pass
            self._fid = None

    # ── login ────────────────────────────────────────────────────────────────

    def _login(self, headless: bool, log) -> None:
        """
        Create a FidelityAutomation browser and log in.

        Session-restore-first flow
        --------------------------
        1. If a saved session file exists, load it and navigate directly to the
           positions page.  If we are NOT redirected to a login page, the session
           is still valid → skip the login form entirely.
        2. Otherwise run the normal login sequence via
           ``FidelityAutomation.login()``.
        3. If headless login is blocked (Fidelity "Sorry" bot-detection page),
           raise a clear ``RuntimeError`` instructing the user to run once with
           ``FIDELITY_HEADLESS=0`` for a manual setup.
        4. After any successful login save the storage state for next time.
        """
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
        _dbg.debug(f"_login: headless={_headless}, totp={'set' if totp_secret else 'not set'}")

        # The fidelity library treats profile_path as a DIRECTORY and always
        # appends "Fidelity.json" inside it.  Pass session_dir (the directory),
        # not the JSON file itself.
        _use_session = self._session_dir is not None
        _profile     = str(self._session_dir) if _use_session else "."

        fid = fid_lib.FidelityAutomation(
            headless=_headless,
            save_state=_use_session,
            profile_path=_profile,
        )

        try:
            # ── attempt to restore from saved session ──────────────────────
            if _use_session and self._session_file.exists():
                log("Checking saved Fidelity session…")
                _dbg.debug("_login: session file exists — trying direct navigation")
                try:
                    fid.page.goto(
                        "https://digital.fidelity.com/ftgw/digital/portfolio/positions",
                        timeout=30_000,
                    )
                    fid.wait_for_loading_sign()
                    fid.page.wait_for_timeout(1_500)
                    fid.wait_for_loading_sign()
                    url = fid.page.url.lower()
                    _dbg.debug(f"_login: session-restore URL={url!r}")

                    # Check for bot-detection "Sorry" page even during restore
                    _sorry_restore = False
                    try:
                        _sorry_restore = fid.page.get_by_text(
                            "Sorry, we can't complete this action"
                        ).is_visible(timeout=1_000)
                    except Exception:
                        pass
                    if _sorry_restore:
                        _dbg.error("_login: bot-detection during session restore")
                        fid.save_state = False
                        fid.close_browser()
                        raise RuntimeError(_BOT_DETECTION_MSG)

                    if not any(kw in url for kw in ("signin", "login")):
                        log("Fidelity session restored from saved state.")
                        _dbg.info(f"_login: session restored, URL={url!r}")
                        self._fid = fid
                        return
                    _dbg.debug("_login: saved session expired — clearing cookies for fresh login")
                    log("Saved session expired — doing fresh login…")
                    # Clear the stale cookies so they don't poison the fresh login
                    try:
                        fid.page.context.clear_cookies()
                        _dbg.debug("_login: stale cookies cleared")
                    except Exception as _ce:
                        _dbg.warning(f"_login: could not clear cookies ({_ce})")
                except RuntimeError:
                    raise  # re-raise bot-detection error
                except Exception as e:
                    _dbg.warning(f"_login: session-restore check failed ({e}) — clearing cookies for fresh login")
                    try:
                        fid.page.context.clear_cookies()
                    except Exception:
                        pass

            # ── fresh login — drive fid.page directly ─────────────────────
            # We do NOT call fid.login() because it detects the TOTP prompt by
            # a specific heading text that Fidelity has changed.  Instead we
            # navigate and fill forms ourselves using fid.page (which has all
            # the Firefox + stealth setup from FidelityAutomation already applied).
            from playwright.sync_api import TimeoutError as _PwTimeout

            log("Navigating to Fidelity login page…")
            fid.page.goto(
                "https://digital.fidelity.com/prgw/digital/login/full-page",
                timeout=60_000,
            )
            # Wait for the JS-rendered login form
            fid.page.get_by_label("Username", exact=True).wait_for(
                timeout=15_000, state="visible"
            )
            fid.page.wait_for_timeout(800)
            _dbg.debug(f"_login: login page ready, URL={fid.page.url!r}")

            # ── fill credentials ─────────────────────────────────────────────
            fid.page.get_by_label("Username", exact=True).click()
            fid.page.wait_for_timeout(250)
            fid.page.get_by_label("Username", exact=True).type(username, delay=80)
            fid.page.get_by_label("Password", exact=True).click()
            fid.page.wait_for_timeout(200)
            fid.page.get_by_label("Password", exact=True).type(password, delay=60)
            fid.page.wait_for_timeout(400)
            fid.page.get_by_role("button", name="Log in").click()
            _dbg.debug("_login: credentials submitted")

            # ── wait for spinners ────────────────────────────────────────────
            fid.wait_for_loading_sign()
            fid.page.wait_for_timeout(1_000)
            fid.wait_for_loading_sign()

            url_after = fid.page.url.lower()
            _dbg.debug(f"_login: post-submit URL={url_after!r}")

            # ── bot-detection check ──────────────────────────────────────────
            _is_sorry = False
            try:
                _is_sorry = fid.page.get_by_text(
                    "Sorry, we can't complete this action"
                ).is_visible(timeout=1_000)
            except Exception:
                pass
            if _is_sorry:
                try:
                    fid.page.screenshot(path="/tmp/fidelity_blocked.png")
                    _dbg.error("Bot-detection screenshot → /tmp/fidelity_blocked.png")
                except Exception:
                    pass
                fid.save_state = False
                fid.close_browser()
                raise RuntimeError(_BOT_DETECTION_MSG)

            # ── TOTP / 2FA handling ──────────────────────────────────────────
            # Detect by input placeholder rather than fragile heading text.
            _on_auth = any(kw in url_after for kw in ("login", "signin", "auth", "2fa", "mfa"))
            if _on_auth:
                _dbg.debug(f"_login: on auth/2FA page — attempting TOTP, URL={url_after!r}")
                if totp_secret:
                    import pyotp as _pyotp
                    try:
                        fid.page.get_by_placeholder("XXXXXX").wait_for(
                            timeout=12_000, state="visible"
                        )
                        code = _pyotp.TOTP(totp_secret).now()
                        _dbg.debug(f"_login: submitting TOTP (len={len(code)})")
                        fid.page.get_by_placeholder("XXXXXX").click()
                        fid.page.get_by_placeholder("XXXXXX").type(code, delay=80)
                        # Best-effort: check "Don't ask me again on this device"
                        try:
                            lbl = fid.page.locator("label").filter(
                                has_text="Don't ask me again on this"
                            )
                            if lbl.is_visible(timeout=2_000):
                                lbl.check()
                        except Exception:
                            pass
                        fid.page.get_by_role("button", name="Continue").click()
                        _dbg.debug("_login: TOTP submitted")
                        fid.wait_for_loading_sign()
                        fid.page.wait_for_timeout(1_000)
                        fid.wait_for_loading_sign()
                    except _PwTimeout:
                        _dbg.warning(
                            f"_login: TOTP input not found — URL={fid.page.url!r}. "
                            "Saving screenshot."
                        )
                        try:
                            fid.page.screenshot(path="/tmp/fidelity_2fa_unknown.png")
                            _dbg.warning("2FA screenshot → /tmp/fidelity_2fa_unknown.png")
                        except Exception:
                            pass
                        if _headless:
                            fid.save_state = False
                            fid.close_browser()
                            raise RuntimeError(
                                "Fidelity 2FA prompt not recognized in headless mode.\n"
                                "Run once with FIDELITY_HEADLESS=0 to complete 2FA manually\n"
                                "and save the session for future headless syncs."
                            )
                elif not _headless:
                    # No TOTP secret + visible browser → user completes manually
                    log("⚠  2FA required — complete it in the browser window (3 min timeout)…")
                    deadline = time.time() + 180
                    while time.time() < deadline:
                        time.sleep(3)
                        u = fid.page.url.lower()
                        if not any(kw in u for kw in ("signin", "login", "auth", "2fa")):
                            break

            # ── poll until off auth pages ────────────────────────────────────
            deadline = time.time() + 60
            while time.time() < deadline:
                url_now = fid.page.url.lower()
                if not any(kw in url_now for kw in ("login", "signin", "auth", "2fa")):
                    break
                time.sleep(2)

            url_now = fid.page.url.lower()
            _dbg.debug(f"_login: final URL={url_now!r}")
            if any(kw in url_now for kw in ("login", "signin")):
                try:
                    fid.page.screenshot(path="/tmp/fidelity_login_failed.png")
                    _dbg.error("Login still on auth page → /tmp/fidelity_login_failed.png")
                except Exception:
                    pass
                fid.save_state = False
                fid.close_browser()
                raise RuntimeError(
                    "Fidelity login did not complete — still on auth page after 60 s.\n"
                    "Check FIDELITY_USERNAME, FIDELITY_PASSWORD, FIDELITY_TOTP_SECRET."
                )

            self._fid = fid
            log("Fidelity session established.")
            _dbg.info(f"_login: session established, URL={fid.page.url!r}")

        except Exception:
            # Ensure browser is closed on any failure; don't overwrite good session
            if self._fid is None and fid is not None:
                try:
                    fid.save_state = False
                    fid.close_browser()
                except Exception:
                    pass
            raise

    # ── navigation + extraction ──────────────────────────────────────────────

    def _navigate_to_positions(self, log) -> None:
        """Navigate to the portfolio positions page and wait for full load."""
        log("Navigating to Portfolio Positions page…")
        _dbg.debug("FidelitySession: navigating to positions page")
        self._fid.page.goto(
            "https://digital.fidelity.com/ftgw/digital/portfolio/positions"
        )
        self._wait_for_loading()
        self._fid.page.wait_for_timeout(1_500)
        self._wait_for_loading(timeout=int(2.5 * 60 * 1_000))

        url = self._fid.page.url.lower()
        _dbg.debug(f"FidelitySession: positions page loaded, url={url!r}")
        if "signin" in url or "login" in url:
            _dbg.error(f"FidelitySession: session expired — url={url!r}")
            raise RuntimeError("Session expired — redirected to login page.")

    def _extract_from_page(self, log) -> str:
        """Navigate to positions page and extract CSV via DOM scraping."""
        self._navigate_to_positions(log)
        return _extract_positions_from_page(self._fid.page, log)

    def _download_to_path(self, output_path: Path, log) -> None:
        """Navigate to positions page and download CSV to disk."""
        log("Navigating to Portfolio Positions page…")
        self._fid.page.goto(
            "https://digital.fidelity.com/ftgw/digital/portfolio/positions"
        )
        self._wait_for_loading()
        self._fid.page.wait_for_timeout(1_000)
        self._wait_for_loading(timeout=int(2.5 * 60 * 1_000))

        url = self._fid.page.url.lower()
        if "signin" in url or "login" in url:
            raise RuntimeError("Session expired — redirected to login page.")

        log("Looking for Download button…")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = False

        try:
            self._fid.page.get_by_role("button", name="Available Actions").click(timeout=8_000)
            with self._fid.page.expect_download(timeout=30_000) as dl_info:
                self._fid.page.get_by_role("menuitem", name="Download").click()
            dl_info.value.save_as(str(output_path))
            downloaded = True
            log("Downloaded via 'Available Actions' menu.")
        except Exception:
            pass

        if not downloaded:
            try:
                with self._fid.page.expect_download(timeout=30_000) as dl_info:
                    self._fid.page.get_by_label("Download Positions").click(timeout=8_000)
                dl_info.value.save_as(str(output_path))
                downloaded = True
                log("Downloaded via 'Download Positions' button.")
            except Exception:
                pass

        if not downloaded:
            try:
                shot = output_path.parent / "fidelity_error.png"
                self._fid.page.screenshot(path=str(shot))
                log(f"Error screenshot → {shot}")
            except Exception:
                pass
            raise RuntimeError("Could not find a Download button on the Positions page.")

        size = output_path.stat().st_size
        log(f"Saved → {output_path}  ({size:,} bytes)")

    # ── public API ───────────────────────────────────────────────────────────

    def get_csv_to_path(self, output_path: Path, headless: bool = True, log=print) -> None:
        """Download positions CSV to output_path, reusing session where possible."""
        with self._lock:
            try:
                if self._is_alive():
                    log("Reusing existing Fidelity session.")
                    _dbg.debug("get_csv_to_path: reusing session")
                else:
                    self._close_browser()
                    self._login(headless, log)
                self._download_to_path(output_path, log)
                return
            except Exception as first_err:
                _dbg.warning(f"get_csv_to_path: first attempt failed — {first_err}")
                log(f"First attempt failed ({first_err}). Re-logging in…")
            self._close_browser()
            self._login(headless, log)
            self._download_to_path(output_path, log)

    def get_csv_memory(self, headless: bool = True, log=print) -> str:
        """
        Extract positions CSV as a string via DOM scraping — no disk I/O.
        Reuses existing browser session; re-logins automatically on expiry.
        """
        with self._lock:
            try:
                if self._is_alive():
                    log("Reusing existing Fidelity session.")
                    _dbg.debug("get_csv_memory: reusing session")
                else:
                    _dbg.debug("get_csv_memory: session not alive — logging in")
                    self._close_browser()
                    self._login(headless, log)
                return self._extract_from_page(log)
            except Exception as first_err:
                _dbg.warning(
                    f"get_csv_memory: first attempt failed — {first_err}\n"
                    f"{traceback.format_exc()}"
                )
                log(f"First attempt failed ({first_err}). Re-logging in…")

            _dbg.debug("get_csv_memory: retrying with fresh login")
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

def _cli_setup() -> None:
    """
    One-time visible-browser login to save a session for future headless syncs.

    Credentials + TOTP are filled automatically from env vars.
    Uses save_state=False so no stale cookies are loaded before login,
    then saves the storage state manually on success.
    """
    try:
        from fidelity import fidelity as fid_lib
        from playwright.sync_api import TimeoutError as _PwTimeout
    except ImportError:
        print("ERROR: fidelity library not installed — run: pip install fidelity")
        sys.exit(1)

    username    = os.environ.get("FIDELITY_USERNAME", "").strip()
    password    = os.environ.get("FIDELITY_PASSWORD", "").strip()
    totp_secret = os.environ.get("FIDELITY_TOTP_SECRET", "").strip() or None

    if not username or not password:
        print("ERROR: FIDELITY_USERNAME and FIDELITY_PASSWORD must be set.")
        sys.exit(1)

    session_dir  = Path(__file__).parent.parent / "blob"
    session_file = session_dir / "Fidelity.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    log = lambda msg: print(f"[setup] {msg}")

    print("=" * 60)
    print("Fidelity session setup  (automated credentials)")
    print("=" * 60)
    print(f"Session will be saved to: {session_file}")
    print()

    # save_state=False → clean browser, no stale cookies loaded
    fid = fid_lib.FidelityAutomation(headless=False, save_state=False)

    try:
        log("Navigating to Fidelity login page…")
        fid.page.goto(
            "https://digital.fidelity.com/prgw/digital/login/full-page",
            timeout=60_000,
        )
        fid.page.get_by_label("Username", exact=True).wait_for(timeout=15_000, state="visible")
        fid.page.wait_for_timeout(800)

        fid.page.get_by_label("Username", exact=True).click()
        fid.page.wait_for_timeout(250)
        fid.page.get_by_label("Username", exact=True).type(username, delay=80)
        fid.page.get_by_label("Password", exact=True).click()
        fid.page.wait_for_timeout(200)
        fid.page.get_by_label("Password", exact=True).type(password, delay=60)
        fid.page.wait_for_timeout(400)
        fid.page.get_by_role("button", name="Log in").click()
        log("Credentials submitted — waiting…")

        fid.wait_for_loading_sign()
        fid.page.wait_for_timeout(1_000)
        fid.wait_for_loading_sign()

        url_after = fid.page.url.lower()
        _on_auth = any(kw in url_after for kw in ("login", "signin", "auth", "2fa", "mfa"))
        if _on_auth and totp_secret:
            import pyotp as _pyotp
            try:
                fid.page.get_by_placeholder("XXXXXX").wait_for(timeout=12_000, state="visible")
                fid.page.get_by_placeholder("XXXXXX").type(_pyotp.TOTP(totp_secret).now(), delay=80)
                try:
                    lbl = fid.page.locator("label").filter(has_text="Don't ask me again on this")
                    if lbl.is_visible(timeout=2_000):
                        lbl.check()
                except Exception:
                    pass
                fid.page.get_by_role("button", name="Continue").click()
                fid.wait_for_loading_sign()
            except _PwTimeout:
                log("TOTP input not found — please complete 2FA manually in the browser window.")

        # If still on auth page and no TOTP secret, wait for manual completion
        deadline = time.time() + 180
        while time.time() < deadline:
            url_now = fid.page.url.lower()
            if not any(kw in url_now for kw in ("login", "signin", "auth", "2fa")):
                break
            time.sleep(2)

        url_now = fid.page.url.lower()
        if any(kw in url_now for kw in ("login", "signin")):
            print(f"\n[setup] Still on auth page after waiting ({url_now!r}).")
            fid.close_browser()
            sys.exit(1)

        log("Login successful — saving session…")
        fid.page.context.storage_state(path=str(session_file))
        fid.close_browser()

        if session_file.exists():
            print(f"\n✓ Session saved to {session_file}  ({session_file.stat().st_size:,} bytes)")
            print("  Future syncs will restore this session and skip the login form.")
        else:
            print("\n✗ Session file was not created — check for errors above.")
            sys.exit(1)

    except Exception as e:
        print(f"\n[setup] Error: {e}")
        try:
            fid.close_browser()
        except Exception:
            pass
        sys.exit(1)


def _cli_manual_login() -> None:
    """
    Open a visible Firefox window at the Fidelity login page and wait for the
    user to log in completely by hand.  Once the browser leaves the auth pages
    the storage state is saved to blob/Fidelity.json and the browser closes.

    Uses save_state=False so NO existing stale cookies are loaded — the browser
    starts completely clean.  Storage state is saved manually after login.
    """
    try:
        from fidelity import fidelity as fid_lib
    except ImportError:
        print("ERROR: fidelity library not installed — run: pip install fidelity")
        sys.exit(1)

    # The fidelity library treats profile_path as a directory and writes
    # Fidelity.json inside it — so session_dir is the directory to use.
    session_dir  = Path(__file__).parent.parent / "blob"
    session_file = session_dir / "Fidelity.json"   # actual state file
    session_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Fidelity manual login")
    print("=" * 60)
    print(f"Session will be saved to: {session_file}")
    print()
    print("A Firefox window will open.  Log in however you like")
    print("(password, TOTP, SMS, push notification — anything).")
    print("Once you reach the portfolio page the browser will save")
    print("the session and close automatically.")
    print()
    print("Press Ctrl+C to cancel.")
    print()

    # IMPORTANT: save_state=False so no stale cookies are loaded into the
    # browser.  Loading expired/bad cookies triggers Fidelity's bot detection
    # even before you type anything.
    fid = fid_lib.FidelityAutomation(headless=False, save_state=False)

    try:
        fid.page.goto(
            "https://digital.fidelity.com/prgw/digital/login/full-page",
            timeout=60_000,
        )
        print("Waiting for you to finish logging in (up to 5 minutes)…")

        deadline = time.time() + 300
        logged_in = False
        while time.time() < deadline:
            try:
                url = fid.page.url.lower()
            except Exception:
                break  # browser closed by user
            if not any(kw in url for kw in ("login", "signin", "auth", "2fa", "mfa")):
                logged_in = True
                print(f"\n✓ Detected successful login.")
                break
            time.sleep(2)

        if not logged_in:
            print("\n✗ Timed out — session not saved.")
            fid.close_browser()
            sys.exit(1)

        # Give the page a moment to fully settle before capturing state
        try:
            fid.page.wait_for_timeout(1_500)
            fid.wait_for_loading_sign()
        except Exception:
            pass

        print("Saving session…")
        # Save storage state manually (cookies + localStorage → Fidelity.json)
        fid.page.context.storage_state(path=str(session_file))
        fid.close_browser()   # save_state=False → just closes, no double-write

        if session_file.exists():
            print(f"✓ Session saved  →  {session_file}  ({session_file.stat().st_size:,} bytes)")
            print("  Future syncs will restore this session automatically.")
        else:
            print("✗ Session file was not created — check for errors above.")
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nCancelled.")
        try:
            fid.close_browser()
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        try:
            fid.close_browser()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download Fidelity portfolio CSV")
    ap.add_argument(
        "--setup", action="store_true",
        help=(
            "One-time setup: open a visible browser, log in automatically "
            "(uses env-var credentials + TOTP), and save the session"
        ),
    )
    ap.add_argument(
        "--manual-login", action="store_true",
        help=(
            "One-time setup: open a visible browser at the Fidelity login page "
            "and wait for you to log in fully by hand, then save the session"
        ),
    )
    ap.add_argument("--no-headless", action="store_true",
                    help="Show browser window (useful for debugging / manual 2FA)")
    ap.add_argument("--output", default=None,
                    help="Output CSV path (default: blob/Portfolio_Positions_Latest.csv)")
    args = ap.parse_args()

    if args.manual_login:
        _cli_manual_login()
        sys.exit(0)

    if args.setup:
        _cli_setup()
        sys.exit(0)

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
