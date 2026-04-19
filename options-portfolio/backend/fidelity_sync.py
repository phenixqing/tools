"""
Fidelity portfolio CSV downloader — Playwright browser automation.

Required env vars
-----------------
FIDELITY_USERNAME      Fidelity customer ID / username
FIDELITY_PASSWORD      Fidelity password

Optional env vars
-----------------
FIDELITY_TOTP_SECRET   Base32 TOTP secret for fully-automated 2FA.
                       Get it by setting up a TOTP authenticator (e.g. Google
                       Authenticator) on your Fidelity account and saving the
                       "manual entry" key shown during setup.
FIDELITY_HEADLESS      Set to "0" to show the browser window (useful for
                       debugging or when bot-detection blocks headless mode).

Usage
-----
As a library (called from FastAPI):
    from fidelity_sync import run_sync
    await run_sync(output_path=Path("blob/Portfolio_Positions_Latest.csv"))

As a standalone script:
    python fidelity_sync.py [--no-headless]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── constants ──────────────────────────────────────────────────────────────────
LOGIN_URL     = "https://digital.fidelity.com/prgw/digital/login/full-page"
POSITIONS_URL = "https://digital.fidelity.com/ftgw/digital/portfolio/positions"

# Timeout in milliseconds
PAGE_TIMEOUT  = 60_000   # 60 s — Fidelity SPA can be slow
NAV_TIMEOUT   = 30_000


# ── helpers ────────────────────────────────────────────────────────────────────

def _totp_code(secret: str) -> str:
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        raise RuntimeError(
            "pyotp is required for TOTP 2FA: pip install pyotp"
        )


async def _try_click(page, *selectors: str, timeout: int = 5_000) -> bool:
    """Try each selector in order; click the first one found. Returns True on success."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


async def _try_fill(page, value: str, *selectors: str, timeout: int = 5_000) -> bool:
    """Fill the first visible input matching any selector."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(value)
            return True
        except Exception:
            continue
    return False


# ── login ──────────────────────────────────────────────────────────────────────

async def _login(page, username: str, password: str,
                 totp_secret: Optional[str], log) -> None:
    """Navigate to Fidelity login and authenticate, handling optional TOTP 2FA."""
    log("Navigating to Fidelity login page…")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)

    # ── username ──────────────────────────────────────────────────────────────
    log("Entering username…")
    ok = await _try_fill(page, username,
                         "#userId-input",
                         "input[name='username']",
                         "input[id*='user']",
                         "input[type='text']")
    if not ok:
        raise RuntimeError("Could not find username input field on Fidelity login page.")

    # Some flows show username then password on separate screens
    await _try_click(page,
                     "#fs-login-button",
                     "button[data-testid='continue-button']",
                     "button:has-text('Continue')",
                     timeout=3_000)
    await page.wait_for_timeout(800)

    # ── password ──────────────────────────────────────────────────────────────
    log("Entering password…")
    ok = await _try_fill(page, password,
                         "#password",
                         "input[name='password']",
                         "input[type='password']")
    if not ok:
        raise RuntimeError("Could not find password input field on Fidelity login page.")

    log("Submitting credentials…")
    ok = await _try_click(page,
                          "#fs-login-button",
                          "button[type='submit']",
                          "button:has-text('Log in')",
                          "button:has-text('Sign in')")
    if not ok:
        raise RuntimeError("Could not find login submit button.")

    await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)

    # ── 2FA detection ─────────────────────────────────────────────────────────
    page_text = (await page.content()).lower()
    two_fa_indicators = [
        "verification code", "security code", "one-time", "authenticator",
        "otc", "two-factor", "2-step", "enter code",
    ]
    needs_2fa = any(k in page_text for k in two_fa_indicators)

    if needs_2fa:
        log("2FA prompt detected.")
        if totp_secret:
            code = _totp_code(totp_secret)
            log(f"Entering TOTP code ({code[:2]}****) …")
            ok = await _try_fill(page, code,
                                 "#otc",
                                 "input[name='otc']",
                                 "input[id*='otp']",
                                 "input[id*='code']",
                                 "input[placeholder*='code' i]",
                                 "input[aria-label*='code' i]")
            if not ok:
                raise RuntimeError(
                    "2FA required but could not find the code input field. "
                    "Try running with FIDELITY_HEADLESS=0 to complete 2FA manually."
                )
            await _try_click(page,
                             "button[type='submit']",
                             "button:has-text('Submit')",
                             "button:has-text('Continue')",
                             "button:has-text('Verify')")
            await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
        else:
            raise RuntimeError(
                "Fidelity requires 2FA but FIDELITY_TOTP_SECRET is not set.\n"
                "Options:\n"
                "  1. Set FIDELITY_TOTP_SECRET to your authenticator's base32 secret.\n"
                "  2. Run with FIDELITY_HEADLESS=0 to complete 2FA in the browser window."
            )

    # ── verify logged in ──────────────────────────────────────────────────────
    current_url = page.url.lower()
    if "login" in current_url or "error" in current_url:
        raise RuntimeError(
            f"Login may have failed — still on: {page.url}\n"
            "Check credentials or run with FIDELITY_HEADLESS=0 to inspect."
        )
    log("Login successful.")


# ── download ───────────────────────────────────────────────────────────────────

async def _download_csv(page, output_path: Path, log) -> None:
    """Navigate to the positions page and download the portfolio CSV."""
    log("Navigating to Portfolio Positions page…")
    await page.goto(POSITIONS_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)

    # Wait for the positions table to render (Fidelity is a heavy SPA)
    log("Waiting for positions table…")
    try:
        await page.wait_for_selector(
            ".p-positions-table, [data-testid*='position'], "
            ".ag-root-wrapper, .account-selector-table, "
            "table",
            timeout=PAGE_TIMEOUT,
        )
    except Exception:
        raise RuntimeError(
            "Positions table did not appear within 60 s. "
            "Try running with FIDELITY_HEADLESS=0 to inspect the page."
        )

    await page.wait_for_timeout(2000)   # allow lazy columns to populate

    # ── find & click the Download button ─────────────────────────────────────
    log("Looking for Download button…")

    # Fidelity sometimes hides the download behind a "..." or toolbar button
    download_selectors = [
        "button:has-text('Download')",
        "a:has-text('Download')",
        "[aria-label*='download' i]",
        "[title*='download' i]",
        "[data-testid*='download']",
        "button.download-btn",
        # older Fidelity UI
        "a[href*='download' i]",
        "a[href*='csv' i]",
    ]

    # Try each selector; some might be inside a dropdown — open it first
    async def _attempt_download():
        for sel in download_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3_000):
                    async with page.expect_download(timeout=30_000) as dl_info:
                        await loc.click()
                    return await dl_info.value
            except Exception:
                continue
        return None

    dl = await _attempt_download()

    # If direct click didn't work, try opening a "..." / actions menu first
    if dl is None:
        log("Direct download button not found — trying actions menu…")
        menu_opened = await _try_click(page,
                                       "button[aria-label*='more' i]",
                                       "button[aria-label*='action' i]",
                                       "button:has-text('...')",
                                       "button[aria-haspopup='menu']",
                                       timeout=5_000)
        if menu_opened:
            await page.wait_for_timeout(500)
            dl = await _attempt_download()

    if dl is None:
        raise RuntimeError(
            "Could not find a Download button on the Positions page.\n"
            "Fidelity may have updated its UI. "
            "Run with FIDELITY_HEADLESS=0 to manually locate the button."
        )

    # ── save ──────────────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await dl.save_as(str(output_path))
    log(f"Saved → {output_path}  ({output_path.stat().st_size:,} bytes)")


# ── public API ─────────────────────────────────────────────────────────────────

async def run_sync(
    output_path: Path,
    headless: bool = True,
    log=print,
) -> None:
    """
    Full sync: login to Fidelity → download positions CSV → save to output_path.

    Reads credentials from environment variables:
      FIDELITY_USERNAME, FIDELITY_PASSWORD, FIDELITY_TOTP_SECRET (optional)
    """
    from playwright.async_api import async_playwright

    username    = os.environ.get("FIDELITY_USERNAME", "").strip()
    password    = os.environ.get("FIDELITY_PASSWORD", "").strip()
    totp_secret = os.environ.get("FIDELITY_TOTP_SECRET", "").strip() or None

    if not username or not password:
        raise RuntimeError(
            "FIDELITY_USERNAME and FIDELITY_PASSWORD environment variables must be set."
        )

    _headless = headless and os.environ.get("FIDELITY_HEADLESS", "1") != "0"
    log(f"Launching {'headless' if _headless else 'visible'} browser…")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=_headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            accept_downloads=True,
        )
        # Mask automation flags
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        """)

        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            await _login(page, username, password, totp_secret, log)
            await _download_csv(page, output_path, log)
        except Exception:
            # Save a screenshot for debugging
            try:
                shot = output_path.parent / "fidelity_error.png"
                await page.screenshot(path=str(shot))
                log(f"Error screenshot saved → {shot}")
            except Exception:
                pass
            raise
        finally:
            await browser.close()


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

    async def _main():
        t0 = time.time()
        await run_sync(
            output_path=out,
            headless=not args.no_headless,
            log=lambda msg: print(f"[fidelity_sync] {msg}"),
        )
        print(f"Done in {time.time()-t0:.1f}s  →  {out}")

    asyncio.run(_main())
