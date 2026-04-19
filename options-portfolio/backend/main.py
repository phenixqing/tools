"""Options Portfolio Analyzer – FastAPI backend v2."""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import time
import traceback
import uuid
from datetime import date, datetime as _dt, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _PACIFIC = _ZoneInfo("America/Los_Angeles")
except Exception:
    _PACIFIC = None   # fallback: no timezone restriction

import httpx
import numpy as np
import openai as _openai
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from black_scholes import bs_greeks, implied_vol
from portfolio_parser import load_portfolio, load_portfolio_from_string

# ── constants ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
BLOB_DIR   = Path("/Users/phenixqing/claude/options-portfolio/blob")
BLOB_CSV   = BLOB_DIR / "Portfolio_Positions_Latest.csv"
DATA_CSV   = BASE_DIR / "data" / "Portfolio_Positions_Apr-12-2026.csv"
STATIC_DIR = BASE_DIR / "static"

DEBUG_LOG_FILE    = BLOB_DIR / "debug.log"
DEBUG_MAX_BYTES   = 1024 * 1024     # 1 MB per file
DEBUG_KEEP_DAYS   = 7

RISK_FREE_RATE       = 0.045
AVGO_DIV_YIELD       = 0.013
PRICE_POLL_SECS      = 5
FILE_WATCH_SECS      = 5
ALERT_CHECK_SECS     = 60
AI_CACHE_TTL_SECS    = 1800   # 30 min

# ── Debug logger (file-backed, 1 MB / 7-day retention) ────────────────────────

def _setup_debug_logger() -> logging.Logger:
    """Configure the 'portfolio' logger to write to DEBUG_LOG_FILE."""
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("portfolio")
    lg.setLevel(logging.DEBUG)
    if lg.handlers:
        return lg   # already configured (e.g. reload)
    handler = logging.handlers.RotatingFileHandler(
        str(DEBUG_LOG_FILE), maxBytes=DEBUG_MAX_BYTES, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    lg.addHandler(handler)
    return lg


def _purge_old_debug_logs() -> None:
    """Strip log lines older than DEBUG_KEEP_DAYS from the debug log file at startup."""
    if not DEBUG_LOG_FILE.exists():
        return
    try:
        cutoff = _dt.now() - timedelta(days=DEBUG_KEEP_DAYS)
        raw    = DEBUG_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        kept   = []
        for line in raw.splitlines():
            try:
                ts = _dt.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f")
                if ts >= cutoff:
                    kept.append(line)
            except (ValueError, IndexError):
                kept.append(line)   # keep un-parseable lines
        if len(kept) < len(raw.splitlines()):
            DEBUG_LOG_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except Exception:
        pass


_dbg = _setup_debug_logger()
_purge_old_debug_logs()

# Telegram
TG_TOKEN    = "8663670593:AAFcRGQQ7-6MxSiNlyitRicvYcSgidTw7L4"
TG_CHAT_ID  = "8472985790"
TG_BASE_URL = f"https://api.telegram.org/bot{TG_TOKEN}"

# Yahoo Finance
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/AVGO"

# ── application state ──────────────────────────────────────────────────────────
class _State:
    def __init__(self):
        self.portfolio: dict       = {"options": [], "stocks": [], "cash": []}
        self._csv_avgo: float      = 371.55
        self.underlying_prices: dict = {}
        self.file_mtime: float     = 0.0
        # live price
        self.live_enabled: bool    = False
        self.live_price: Optional[float]     = None
        self.open_price: Optional[float]     = None
        self.change_pct: Optional[float]     = None

    def load_csv(self, path: str):
        self.portfolio = load_portfolio(path)
        prices = {s["symbol"]: s["market_price"] for s in self.portfolio["stocks"]}
        self.underlying_prices = prices
        self._csv_avgo = prices.get("AVGO", 371.55)

    def load_csv_content(self, content: str):
        """Load portfolio from a CSV string — pure in-memory, zero disk I/O."""
        self.portfolio = load_portfolio_from_string(content)
        prices = {s["symbol"]: s["market_price"] for s in self.portfolio["stocks"]}
        self.underlying_prices = prices
        self._csv_avgo = prices.get("AVGO", 371.55)

    @property
    def avgo_price(self) -> float:
        """Effective AVGO price: live if enabled, else CSV."""
        if self.live_enabled and self.live_price:
            return self.live_price
        return self._csv_avgo


_st = _State()

# ── AI cache ───────────────────────────────────────────────────────────────────
_ai_cache: Dict[str, Any] = {
    "status":    "idle",   # idle | running | done | error
    "result":    None,
    "timestamp": None,
    "lang":      "zh",
    "trigger":   None,
}

# ── alert idempotency ──────────────────────────────────────────────────────────
_alert_sent: set = set()

# ── Fidelity one-time sync state ──────────────────────────────────────────────
_sync_state: Dict[str, Any] = {
    "status":     "idle",   # idle | running | done | error
    "last_sync":  None,     # unix timestamp
    "last_error": None,
    "message":    None,
}

# ── Fidelity ongoing sync state ────────────────────────────────────────────────
_ongoing_state: Dict[str, Any] = {
    "enabled":       False,
    "interval_secs": 300,     # default 5 minutes; min 30 s
    "status":        "idle",  # idle | running | done | error
    "last_sync":     None,
    "last_error":    None,
    "allow_anytime": False,   # bypass Mon–Fri 06:00–13:00 PT restriction (debug)
}
_ongoing_task: Optional[asyncio.Task] = None

# ── Fidelity reusable browser session (lazy-init, shared across syncs) ─────────
_fidelity_session = None  # FidelitySession instance; created on first use

# ── In-memory CSV snapshots (keyed by dated filename, no disk writes) ──────────
_memory_snapshots: Dict[str, str] = {}

def _get_fidelity_session():
    """Return the singleton FidelitySession, creating it if needed."""
    global _fidelity_session
    if _fidelity_session is None:
        from fidelity_sync import FidelitySession
        _fidelity_session = FidelitySession()
    return _fidelity_session

# ── notification settings ──────────────────────────────────────────────────────
_settings: Dict[str, Any] = {
    "warning_pct":    70.0,   # ⚠️  warn when |gain_loss_pct| exceeds this
    "alert_pct":     100.0,   # 🚨 alert when |gain_loss_pct| exceeds this
    "avgo_change_pct": 3.0,   # 📊 alert when AVGO daily |change| exceeds this
}

# ── event log ──────────────────────────────────────────────────────────────────
_event_log: List[Dict] = []
_MAX_LOG = 500

def _log(type_: str, message: str) -> None:
    # Sync logs: sweep all previous sync entries so only 1 remains at a time
    if type_ == "sync":
        _event_log[:] = [e for e in _event_log if e["type"] != "sync"]
    _event_log.insert(0, {
        "time":    _dt.utcnow().isoformat(timespec="seconds") + "Z",
        "type":    type_,    # csv_load | alert | warning | test | error | sync
        "message": message,
    })
    if len(_event_log) > _MAX_LOG:
        _event_log.pop()

# ── app ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Options Portfolio Analyzer", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── helpers ────────────────────────────────────────────────────────────────────
def _today() -> date:
    return date.today()


def _compute_iv(opt: dict) -> float:
    S = _st.avgo_price
    T = max((opt["expiry"] - _today()).days, 0) / 365.0
    return implied_vol(
        market_price=opt["market_price"],
        S=S, K=opt["strike"], T=T,
        r=RISK_FREE_RATE, q=AVGO_DIV_YIELD,
        option_type=opt["option_type"],
        fallback=0.40,
    )


def _enrich_options(ref_date: Optional[date] = None) -> List[dict]:
    if ref_date is None:
        ref_date = _today()
    result = []
    for opt in _st.portfolio["options"]:
        S   = _st.avgo_price
        K   = opt["strike"]
        exp = opt["expiry"]
        T   = max((exp - ref_date).days, 0) / 365.0
        iv  = _compute_iv(opt)
        g   = bs_greeks(S, K, T, RISK_FREE_RATE, iv, AVGO_DIV_YIELD, opt["option_type"])

        gain_loss     = opt.get("total_gain_loss")
        gain_loss_pct = opt.get("total_gain_pct")   # from CSV (preferred)
        if gain_loss is None and opt.get("cost_basis"):
            # Compute gain_loss from current_value / cost_basis as fallback
            cb = opt["cost_basis"]
            qty_val = opt["quantity"]
            gain_loss = opt["current_value"] + cb if qty_val < 0 else opt["current_value"] - cb
            # Only compute gain_loss_pct if CSV didn't provide it — never overwrite the CSV value
            if gain_loss_pct is None:
                gain_loss_pct = (gain_loss / abs(cb) * 100) if cb else None

        result.append({
            "symbol":          opt["symbol"],
            "description":     opt["description"],
            "account":         opt["account"],
            "underlying":      opt["underlying"],
            "strike":          opt["strike"],
            "option_type":     opt["option_type"],
            "expiry":          exp.isoformat(),
            "quantity":        opt["quantity"],
            "market_price":    opt["market_price"],
            "current_value":   opt["current_value"],
            "cost_basis":      opt["cost_basis"],
            "gain_loss":       round(gain_loss, 2) if gain_loss is not None else None,
            "gain_loss_pct":   round(gain_loss_pct, 2) if gain_loss_pct is not None else None,
            "days_to_expiry":  (exp - ref_date).days,
            "implied_vol_pct": round(iv * 100, 2),
            "greeks":          {k: round(v, 6) for k, v in g.items()},
        })
    return result


def _build_prompt(enriched: List[dict], avgo_price: float, lang: str) -> str:
    avgo_shares = sum(s["quantity"] for s in _st.portfolio["stocks"] if s["symbol"] == "AVGO")
    positions_txt = []
    for o in enriched:
        g = o["greeks"]
        positions_txt.append(
            f"- {o['description']} | qty {o['quantity']} | "
            f"Strike ${o['strike']} | Expiry {o['expiry']} | DTE {o['days_to_expiry']}d | "
            f"IV {o['implied_vol_pct']:.1f}% | Mkt ${o['market_price']:.2f} | "
            f"Value ${o['current_value']:.0f} | G/L ${o.get('gain_loss') or 0:.0f} "
            f"({o.get('gain_loss_pct') or 0:.1f}%) | "
            f"Δ {g['delta']:.3f} Γ {g['gamma']:.5f} Θ ${g['theta']:.2f}/day "
            f"Vega ${g['vega']:.2f}/1%"
        )
    lang_instr = (
        "Please respond entirely in Simplified Chinese (简体中文)."
        if lang == "zh" else "Please respond in English."
    )
    return f"""You are an expert options trader and risk analyst. Analyze the following AVGO options portfolio and provide a concise but thorough analysis.

**Current Market Context:**
- AVGO current price: ${avgo_price:.2f}
- Reference date: {_today().isoformat()}
- AVGO shares held: {avgo_shares:,}

**Option Positions:**
{chr(10).join(positions_txt) if positions_txt else "No positions."}

Please provide:

## 1. Position Risk Analysis
For each position, assess: current risk/reward profile, key risk levels (breakeven, max loss triggers), theta decay impact, and whether the position benefits from or is hurt by IV changes.

## 2. Portfolio-Level Risks
Identify concentration risks, directional bias, and how the positions interact (natural hedges or compounding risks).

## 3. AVGO Short-Term Outlook (1–2 weeks)
Key price levels to watch, implied move from IV, what the options market is pricing in.

## 4. AVGO Medium-Term Outlook (1–3 months)
Broader technical and fundamental considerations — sector dynamics, earnings, macro factors.

## 5. Key Actions to Consider
Specific, actionable suggestions for managing risk or optimizing these positions.

Be direct and specific. Use dollar amounts and percentages. Keep each section concise.

{lang_instr}"""


def _openclaw_ok() -> bool:
    profiles = Path.home() / ".openclaw/agents/main/agent/auth-profiles.json"
    if not shutil.which("openclaw") or not profiles.exists():
        return False
    try:
        data = json.loads(profiles.read_text())
        prof = data.get("profiles", {}).get("openai-codex:default", {})
        return bool(prof.get("access")) and time.time() * 1000 < prof.get("expires", 0) - 60_000
    except Exception:
        return False


def _run_openclaw(prompt: str) -> str:
    sid = f"pf-{uuid.uuid4().hex[:12]}"
    r = subprocess.run(
        ["openclaw", "agent", "--local", "--message", prompt,
         "--session-id", sid, "--json"],
        capture_output=True, text=True, timeout=300,
    )
    raw = r.stdout
    start = raw.find("{")
    if start == -1:
        raise RuntimeError(f"No JSON from openclaw: {r.stderr[:300]}")
    data = json.loads(raw[start:])
    text = next((p["text"] for p in data.get("payloads", []) if p.get("text")), "")
    if not text:
        raise RuntimeError("Empty payload from openclaw")
    return text


# ── background: live price ────────────────────────────────────────────────────
async def _price_loop():
    while True:
        if _st.live_enabled:
            try:
                async with httpx.AsyncClient(timeout=8) as c:
                    r = await c.get(YF_URL,
                        params={"interval": "1m", "range": "1d"},
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    meta = r.json()["chart"]["result"][0]["meta"]
                    price = float(meta["regularMarketPrice"])
                    prev  = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
                    _st.live_price  = price
                    _st.open_price  = prev
                    _st.change_pct  = round((price - prev) / prev * 100, 3) if prev else 0.0
            except Exception:
                pass
        await asyncio.sleep(PRICE_POLL_SECS)


# ── background: file watcher ──────────────────────────────────────────────────
async def _file_loop():
    # Record initial mtime without triggering reload (already loaded at startup)
    if BLOB_CSV.exists():
        _st.file_mtime = BLOB_CSV.stat().st_mtime

    while True:
        await asyncio.sleep(FILE_WATCH_SECS)
        try:
            if BLOB_CSV.exists():
                mtime = BLOB_CSV.stat().st_mtime
                if mtime != _st.file_mtime:
                    _st.file_mtime = mtime
                    _st.load_csv(str(BLOB_CSV))
                    _log("csv_load", f"文件变更自动重载: {BLOB_CSV.name}  "
                         f"({len(_st.portfolio['options'])} 期权, {len(_st.portfolio['stocks'])} 股票)")
                    asyncio.create_task(_trigger_cache("file_change"))
        except Exception:
            pass


# ── background: AI cache ──────────────────────────────────────────────────────
async def _trigger_cache(trigger: str, lang: str = "zh"):
    if _ai_cache["status"] == "running":
        return
    if (
        _ai_cache["status"] == "done"
        and _ai_cache["timestamp"]
        and time.time() - _ai_cache["timestamp"] < AI_CACHE_TTL_SECS
    ):
        return  # still fresh

    _ai_cache.update({"status": "running", "trigger": trigger, "lang": lang})
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _cache_worker, lang)


def _cache_worker(lang: str):
    try:
        enriched = _enrich_options()
        prompt   = _build_prompt(enriched, _st.avgo_price, lang)
        if _openclaw_ok():
            text = _run_openclaw(prompt)
        else:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("No openclaw auth and no OPENAI_API_KEY")
            client = _openai.OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model="o4-mini",
                reasoning_effort="high",
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content or ""
        _ai_cache.update({"status": "done", "result": text, "timestamp": time.time(), "lang": lang})
    except Exception as exc:
        _ai_cache.update({"status": "error", "result": str(exc)})


# ── background: Telegram alerts ───────────────────────────────────────────────
async def _send_tg(msg: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{TG_BASE_URL}/sendMessage", json={
                "chat_id": TG_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            })
    except Exception:
        pass


def _is_market_hours() -> bool:
    """True if current Pacific time is Mon–Fri, 06:00–13:00, or allow_anytime is set."""
    if _ongoing_state.get("allow_anytime"):
        return True
    if _PACIFIC is None:
        return True   # no zoneinfo: always allow
    now = _dt.now(_PACIFIC)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return 6 <= now.hour < 13


async def _check_alerts_once() -> None:
    """Single-pass alert check. Called by both the periodic loop and ongoing sync."""
    today_str = _today().isoformat()
    enriched  = _enrich_options()
    warn_pct  = _settings["warning_pct"]
    alert_pct = _settings["alert_pct"]

    # 1. Contract gain/loss thresholds
    for o in enriched:
        pct = o.get("gain_loss_pct")
        if pct is None:
            continue
        abs_pct = abs(pct)
        label   = "亏损" if pct < 0 else "盈利"
        acct    = o.get("account", "")

        if abs_pct > alert_pct:
            key = f"{o['symbol']}:{acct}:alert:{today_str}"
            if key not in _alert_sent:
                _alert_sent.add(key)
                msg = (f"🚨 *期权高风险警报*\n"
                       f"合约: `{o['description']}`\n"
                       f"账户: {acct}\n"
                       f"{label}: *{pct:.1f}%* (超过 {alert_pct:.0f}% 阈值)\n"
                       f"当前价值: ${o['current_value']:.0f}\n"
                       f"AVGO: ${_st.avgo_price:.2f}")
                _log("alert", f"[{acct}] {o['description']} {label} {pct:.1f}%，已发送告警")
                asyncio.create_task(_send_tg(msg))
        elif abs_pct > warn_pct:
            key = f"{o['symbol']}:{acct}:warn:{today_str}"
            if key not in _alert_sent:
                _alert_sent.add(key)
                msg = (f"⚠️ *期权预警*\n"
                       f"合约: `{o['description']}`\n"
                       f"账户: {acct}\n"
                       f"{label}: *{pct:.1f}%* (超过 {warn_pct:.0f}% 阈值)\n"
                       f"当前价值: ${o['current_value']:.0f}\n"
                       f"AVGO: ${_st.avgo_price:.2f}")
                _log("warning", f"[{acct}] {o['description']} {label} {pct:.1f}%，已发送预警")
                asyncio.create_task(_send_tg(msg))

    # 2. AVGO daily change threshold
    chg      = _st.change_pct
    avgo_thr = _settings["avgo_change_pct"]
    if chg is not None and abs(chg) > avgo_thr:
        direction = "上涨" if chg > 0 else "下跌"
        key = f"AVGO:chg:{direction}:{today_str}"
        if key not in _alert_sent:
            _alert_sent.add(key)
            msg = (f"📊 *AVGO 当日大幅{direction}*\n"
                   f"涨跌幅: *{chg:+.2f}%* (阈值 {avgo_thr:.1f}%)\n"
                   f"当前价格: ${_st.avgo_price:.2f}")
            _log("alert", f"AVGO 当日{direction} {chg:+.2f}%，已发送告警")
            asyncio.create_task(_send_tg(msg))


async def _alert_loop():
    while True:
        await asyncio.sleep(ALERT_CHECK_SECS)
        await _check_alerts_once()


# ── startup sync ───────────────────────────────────────────────────────────────

async def _run_startup_sync() -> None:
    """
    Background task: sync from Fidelity at server startup.
    Falls back to local CSV if Fidelity sync fails.
    """
    _dbg.info("Startup sync: begin")
    try:
        session = _get_fidelity_session()
        csv_content = await asyncio.to_thread(
            session.get_csv_memory, True,
            lambda msg: _dbg.debug(f"[fidelity] {msg}"),
        )
        _st.load_csv_content(csv_content)
        n_opts = len(_st.portfolio["options"])
        _sync_state.update({
            "status":     "done",
            "last_sync":  time.time(),
            "message":    f"启动同步完成 — {n_opts} 期权",
            "last_error": None,
        })
        _dbg.info(f"Startup sync: success — {n_opts} options")
        _log("sync", f"✓ 启动同步 — {n_opts} 期权 (in-memory)")
    except Exception as exc:
        err = str(exc)
        _dbg.error(f"Startup sync failed: {err}\n{traceback.format_exc()}")
        _sync_state.update({
            "status":     "error",
            "last_sync":  time.time(),
            "last_error": err,
            "message":    None,
        })
        _log("sync", f"✗ 启动同步失败: {err[:80]}")
        # Fall back to local CSV
        for csv_path in (BLOB_CSV, DATA_CSV):
            if csv_path.exists():
                try:
                    _st.load_csv(str(csv_path))
                    _dbg.info(f"Startup sync fallback: loaded {csv_path.name}")
                    _log("csv_load",
                         f"回退至本地: {csv_path.name}  "
                         f"({len(_st.portfolio['options'])} 期权, "
                         f"{len(_st.portfolio['stocks'])} 股票)")
                except Exception as e2:
                    _dbg.error(f"Startup fallback also failed: {e2}")
                break


# ── startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    """
    On startup: launch a background Fidelity sync (if creds are configured).
    If creds are absent, fall back to loading the local CSV immediately.
    """
    asyncio.create_task(_price_loop())
    asyncio.create_task(_file_loop())
    asyncio.create_task(_alert_loop())

    if os.environ.get("FIDELITY_USERNAME") and os.environ.get("FIDELITY_PASSWORD"):
        _dbg.info("Startup: Fidelity creds found — launching background sync")
        _log("sync", "启动: 正在从 Fidelity 同步…")
        _sync_state.update({"status": "running", "message": "启动同步中…", "last_error": None})
        asyncio.create_task(_run_startup_sync())
    else:
        # No credentials configured: load local CSV immediately
        csv_path = BLOB_CSV if BLOB_CSV.exists() else DATA_CSV
        _dbg.info(f"Startup: no Fidelity creds — loading local CSV {csv_path.name}")
        _st.load_csv(str(csv_path))
        _log("csv_load",
             f"启动加载: {csv_path.name}  "
             f"({len(_st.portfolio['options'])} 期权, {len(_st.portfolio['stocks'])} 股票)")


# ── upload ─────────────────────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files accepted.")
    raw = await file.read()
    _dbg.info(f"CSV upload: {file.filename} ({len(raw):,} bytes)")
    try:
        content = raw.decode("utf-8-sig")
        _st.load_csv_content(content)
    except Exception as exc:
        _dbg.error(f"CSV upload parse error: {exc}\n{traceback.format_exc()}")
        raise HTTPException(status_code=422, detail=f"Parse error: {exc}")
    n_opts = len(_st.portfolio["options"])
    _dbg.info(f"CSV upload success: {n_opts} options, {len(_st.portfolio['stocks'])} stocks")
    _log("csv_load", f"手动上传: {file.filename}  "
         f"({n_opts} 期权, {len(_st.portfolio['stocks'])} 股票)")
    return {"status": "ok", "filename": file.filename,
            "options_count": n_opts,
            "stocks_count":  len(_st.portfolio["stocks"]),
            "avgo_price":    _st.avgo_price}


# ── live price ─────────────────────────────────────────────────────────────────
@app.get("/api/price/status")
def price_status():
    return {
        "live_enabled": _st.live_enabled,
        "price":        _st.avgo_price,
        "live_price":   _st.live_price,
        "change_pct":   _st.change_pct,
    }


@app.post("/api/price/live")
async def toggle_live_price(enabled: bool = Query(...)):
    """Enable or disable live AVGO price polling."""
    _st.live_enabled = enabled
    if not enabled:
        _st.live_price = None
        _st.change_pct = None
    return {"live_enabled": _st.live_enabled, "price": _st.avgo_price}


@app.get("/api/price/stream")
async def price_stream():
    """SSE stream: sends AVGO price every 2s while connected."""
    async def gen():
        try:
            while True:
                payload = json.dumps({
                    "price":      round(_st.avgo_price, 2),
                    "live":       _st.live_enabled,
                    "change_pct": _st.change_pct,
                })
                yield f"data: {payload}\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── AI cache ───────────────────────────────────────────────────────────────────
@app.get("/api/ai/cache")
def get_ai_cache():
    return {
        "status":    _ai_cache["status"],
        "lang":      _ai_cache["lang"],
        "trigger":   _ai_cache["trigger"],
        "timestamp": _ai_cache["timestamp"],
        "age_secs":  round(time.time() - _ai_cache["timestamp"], 0) if _ai_cache["timestamp"] else None,
        "result":    _ai_cache["result"] if _ai_cache["status"] in ("done", "error") else None,
    }


@app.post("/api/ai/cache/refresh")
async def refresh_ai_cache(lang: str = Query(default="zh")):
    """Force a fresh background AI analysis."""
    _ai_cache["status"] = "idle"   # reset TTL guard
    asyncio.create_task(_trigger_cache("manual", lang))
    return {"status": "triggered"}


# ── holdings ───────────────────────────────────────────────────────────────────
@app.get("/api/holdings")
def get_holdings():
    stocks = [{"symbol": s["symbol"], "description": s["description"], "account": s["account"],
               "quantity": s["quantity"], "market_price": s["market_price"],
               "current_value": s["current_value"]} for s in _st.portfolio["stocks"]]
    options = [{"symbol": o["symbol"], "description": o["description"], "account": o["account"],
                "option_type": o["option_type"], "strike": o["strike"],
                "expiry": o["expiry"].isoformat(), "quantity": o["quantity"],
                "market_price": o["market_price"], "current_value": o["current_value"]}
               for o in _st.portfolio["options"]]
    cash = [{"symbol": c["symbol"], "account": c["account"], "current_value": c["current_value"]}
            for c in _st.portfolio["cash"]]
    return {"stocks": stocks, "options": options, "cash": cash}


# ── summary ────────────────────────────────────────────────────────────────────
@app.get("/api/portfolio/summary")
def portfolio_summary():
    opt_val  = sum(o["current_value"] for o in _st.portfolio["options"])
    stk_val  = sum(s["current_value"] for s in _st.portfolio["stocks"])
    csh_val  = sum(c["current_value"] for c in _st.portfolio["cash"])
    opt_cost = sum(
        (o["cost_basis"] or 0) * (-1 if o["quantity"] < 0 else 1)
        for o in _st.portfolio["options"]
    )
    accounts: dict = {}
    for lst in (_st.portfolio["options"], _st.portfolio["stocks"], _st.portfolio["cash"]):
        for pos in lst:
            acc = pos["account"]
            accounts[acc] = accounts.get(acc, 0) + pos["current_value"]
    return {
        "reference_date":         _today().isoformat(),
        "underlying":             "AVGO",
        "underlying_price":       _st.avgo_price,
        "live_price_enabled":     _st.live_enabled,
        "live_price":             _st.live_price,
        "change_pct":             _st.change_pct,
        "total_value":            round(opt_val + stk_val + csh_val, 2),
        "options_value":          round(opt_val, 2),
        "stocks_value":           round(stk_val, 2),
        "cash_value":             round(csh_val, 2),
        "options_unrealized_pnl": round(opt_val - opt_cost, 2) if opt_cost else 0,
        "positions_count":        {"options": len(_st.portfolio["options"]),
                                   "stocks":  len(_st.portfolio["stocks"])},
        "accounts":               {k: round(v, 2) for k, v in accounts.items()},
    }


@app.get("/api/options")
def options_list():
    return _enrich_options()


@app.get("/api/options/pnl_point")
def pnl_point(price: float = Query(...), target_date: str = Query(...)):
    try:
        td = date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    S_curr   = _st.avgo_price
    enriched = _enrich_options()
    opt_details, agg_pnl = [], 0.0

    for opt in enriched:
        expiry = date.fromisoformat(opt["expiry"])
        T      = max((expiry - td).days, 0) / 365.0
        iv     = opt["implied_vol_pct"] / 100.0
        K, qty = opt["strike"], opt["quantity"]
        cb     = opt.get("cost_basis") or 0
        ref_val = -cb if qty < 0 else cb

        g     = bs_greeks(price, K, T, RISK_FREE_RATE, iv, AVGO_DIV_YIELD, opt["option_type"])
        opt_p = g["price"]
        pnl   = qty * opt_p * 100.0 - ref_val
        opt_details.append({"symbol": opt["symbol"], "pnl": round(pnl, 2),
                             "scen_price": round(opt_p, 4),
                             "scen_value": round(qty * opt_p * 100.0, 2)})
        agg_pnl += pnl

    avgo_shares = sum(s["quantity"] for s in _st.portfolio["stocks"] if s["symbol"] == "AVGO")
    stock_delta = (price - S_curr) * avgo_shares
    return {
        "agg_pnl":    round(agg_pnl, 2),
        "port_pnl":   round(agg_pnl + stock_delta, 2),
        "stock_delta": round(stock_delta, 2),
        "opt_details": opt_details,
    }


# ── scenarios ──────────────────────────────────────────────────────────────────
@app.get("/api/options/scenarios")
def options_scenarios(price_steps: int = Query(default=21, ge=5, le=41)):
    today    = _today()
    S_curr   = _st.avgo_price
    pct_arr  = np.linspace(-0.10, 0.10, price_steps)
    price_range = [round(S_curr * (1 + p), 2) for p in pct_arr]
    pct_labels  = [f"{p:+.1%}" for p in pct_arr]

    enriched    = _enrich_options(today)
    options_out = []

    for opt in enriched:
        expiry  = date.fromisoformat(opt["expiry"])
        dte     = max((expiry - today).days, 0)
        K, qty  = opt["strike"], opt["quantity"]
        iv      = opt["implied_vol_pct"] / 100.0
        cb      = opt.get("cost_basis") or 0
        ref_val = -cb if qty < 0 else cb

        dates_list  = [today + timedelta(days=d) for d in range(dte + 1)]
        date_labels = [d.isoformat() for d in dates_list]
        pnl_matrix, opt_price_matrix, greeks_over_time = [], [], []

        for d in dates_list:
            T = max((expiry - d).days, 0) / 365.0
            g = bs_greeks(S_curr, K, T, RISK_FREE_RATE, iv, AVGO_DIV_YIELD, opt["option_type"])
            greeks_over_time.append({k: round(v, 6) for k, v in g.items()})
            pnl_row, price_row = [], []
            for S_new in price_range:
                g_s = bs_greeks(S_new, K, T, RISK_FREE_RATE, iv, AVGO_DIV_YIELD, opt["option_type"])
                opt_p = g_s["price"]
                pnl_row.append(round(qty * opt_p * 100.0 - ref_val, 2))
                price_row.append(round(opt_p, 4))
            pnl_matrix.append(pnl_row)
            opt_price_matrix.append(price_row)

        exp_str   = expiry.strftime("%m/%d")
        short_lbl = f"${opt['strike']} {'P' if opt['option_type']=='put' else 'C'} {exp_str} (x{int(qty)})"
        options_out.append({
            "symbol": opt["symbol"], "short_label": short_lbl,
            "description": opt["description"], "account": opt["account"],
            "expiry": opt["expiry"], "strike": opt["strike"],
            "option_type": opt["option_type"], "quantity": opt["quantity"],
            "current_value": opt["current_value"], "cost_basis": opt["cost_basis"],
            "implied_vol_pct": opt["implied_vol_pct"], "days_to_expiry": opt["days_to_expiry"],
            "dates": date_labels, "option_price_matrix": opt_price_matrix,
            "pnl_matrix": pnl_matrix, "greeks_over_time": greeks_over_time,
        })

    all_expiries  = [date.fromisoformat(o["expiry"]) for o in enriched]
    max_expiry    = max(all_expiries) if all_expiries else today
    all_dates     = [today + timedelta(days=d) for d in range((max_expiry - today).days + 1)]
    all_date_lbls = [d.isoformat() for d in all_dates]

    agg_pnl = [[0.0] * len(price_range) for _ in range(len(all_dates))]
    for o in options_out:
        date_map     = {d: i for i, d in enumerate(o["dates"])}
        last_pnl_row = o["pnl_matrix"][-1]
        for ai, dl in enumerate(all_date_lbls):
            src_row = o["pnl_matrix"][date_map[dl]] if dl in date_map else last_pnl_row
            for pi in range(len(price_range)):
                agg_pnl[ai][pi] += src_row[pi]
    agg_pnl = [[round(v, 2) for v in row] for row in agg_pnl]

    avgo_shares = sum(s["quantity"] for s in _st.portfolio["stocks"] if s["symbol"] == "AVGO")
    stock_delta = [round((p - S_curr) * avgo_shares, 2) for p in price_range]
    port_pnl    = [[round(agg_pnl[d][p] + stock_delta[p], 2)
                    for p in range(len(price_range))] for d in range(len(all_dates))]

    return {
        "reference_date":   today.isoformat(),
        "underlying":       "AVGO",
        "underlying_price": S_curr,
        "live_price_enabled": _st.live_enabled,
        "avgo_shares":      avgo_shares,
        "stock_delta":      stock_delta,
        "price_range":      price_range,
        "pct_labels":       pct_labels,
        "options":          options_out,
        "aggregate": {
            "dates":                all_date_lbls,
            "pnl_matrix":           agg_pnl,
            "portfolio_pnl_matrix": port_pnl,
        },
    }


# ── AI analysis (streaming, manual) ───────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    selected_symbols: List[str]
    avgo_price: Optional[float] = None
    lang: str = "zh"


@app.post("/api/ai/analyze")
async def ai_analyze(req: AnalyzeRequest):
    enriched   = _enrich_options()
    selected   = [o for o in enriched if o["symbol"].strip() in req.selected_symbols]
    avgo_price = req.avgo_price or _st.avgo_price

    prompt = _build_prompt(selected, avgo_price, req.lang)
    use_openclaw = _openclaw_ok()

    if not use_openclaw:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(status_code=503,
                detail="No valid auth. Re-authenticate openclaw or set OPENAI_API_KEY.")

    def stream():
        try:
            if use_openclaw:
                text  = _run_openclaw(prompt)
                words = text.split(" ")
                for i, word in enumerate(words):
                    yield word + (" " if i < len(words) - 1 else "")
            else:
                client = _openai.OpenAI(api_key=api_key)
                resp = client.chat.completions.create(
                    model="o4-mini", reasoning_effort="high", stream=True,
                    messages=[{"role": "user", "content": prompt}],
                )
                for chunk in resp:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        yield delta
        except Exception as exc:
            yield f"\n\n**Error:** {exc}"

    return StreamingResponse(stream(), media_type="text/plain")


# ── server status ─────────────────────────────────────────────────────────────
@app.get("/api/status")
def server_status():
    return {
        "csv_path":       str(BLOB_CSV),
        "csv_exists":     BLOB_CSV.exists(),
        "csv_mtime":      _dt.fromtimestamp(_st.file_mtime).isoformat() if _st.file_mtime else None,
        "avgo_price":     _st.avgo_price,
        "live_enabled":   _st.live_enabled,
        "live_price":     _st.live_price,
        "change_pct":     _st.change_pct,
        "options_count":  len(_st.portfolio["options"]),
        "ai_cache_status": _ai_cache["status"],
        "ai_cache_age":   round(time.time() - _ai_cache["timestamp"]) if _ai_cache["timestamp"] else None,
        "alert_sent_count": len(_alert_sent),
        "alert_sent_today": sorted(k for k in _alert_sent if _today().isoformat() in k),
    }


# ── settings ───────────────────────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    return _settings.copy()


class SettingsUpdate(BaseModel):
    warning_pct:    Optional[float] = None
    alert_pct:      Optional[float] = None
    avgo_change_pct: Optional[float] = None


@app.post("/api/settings")
def update_settings(body: SettingsUpdate):
    if body.warning_pct    is not None: _settings["warning_pct"]    = body.warning_pct
    if body.alert_pct      is not None: _settings["alert_pct"]      = body.alert_pct
    if body.avgo_change_pct is not None: _settings["avgo_change_pct"] = body.avgo_change_pct
    _log("setting", f"阈值更新 — 预警 {_settings['warning_pct']:.0f}% / "
                    f"告警 {_settings['alert_pct']:.0f}% / "
                    f"AVGO变动 {_settings['avgo_change_pct']:.1f}%")
    return _settings.copy()


# ── event log ──────────────────────────────────────────────────────────────────
@app.get("/api/logs")
def get_logs(limit: int = Query(default=200, ge=1, le=500)):
    return _event_log[:limit]


# ── test notification ──────────────────────────────────────────────────────────
@app.post("/api/notifications/test")
async def test_notification():
    msg = (f"🔔 *测试通知*\n"
           f"来自: Options Portfolio Analyzer\n"
           f"时间: {_dt.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
           f"AVGO: ${_st.avgo_price:.2f}\n"
           f"期权: {len(_st.portfolio['options'])} 个合约\n"
           f"预警阈值: {_settings['warning_pct']:.0f}% · "
           f"告警阈值: {_settings['alert_pct']:.0f}%")
    channels = []
    try:
        await _send_tg(msg)
        channels.append({"name": "Telegram", "status": "ok",
                          "detail": f"Chat ID {TG_CHAT_ID[:4]}****"})
    except Exception as exc:
        channels.append({"name": "Telegram", "status": "error", "detail": str(exc)})
    _log("test", f"手动测试通知 — {', '.join(c['name'] for c in channels)}")
    return {"channels": channels, "message": msg}


# ── Fidelity sync ──────────────────────────────────────────────────────────────

def _dated_csv_name() -> str:
    """Return 'Portfolio_Positions_Mon-D-YYYY.csv' for today (no leading zero on day)."""
    now = _dt.now()
    return f"Portfolio_Positions_{now.strftime('%b')}-{int(now.strftime('%d'))}-{now.strftime('%Y')}.csv"


async def _run_fidelity_sync_once() -> None:
    """Background task: one-time sync — in-memory via DOM scraping, emits 1 log entry."""
    _dbg.info("One-time Fidelity sync: begin")
    try:
        session = _get_fidelity_session()
        csv_content = await asyncio.to_thread(
            session.get_csv_memory, True,
            lambda msg: _dbg.debug(f"[fidelity] {msg}"),
        )
        _st.load_csv_content(csv_content)
        n_opts = len(_st.portfolio["options"])
        _sync_state.update({
            "status":     "done",
            "last_sync":  time.time(),
            "message":    f"Synced {n_opts} options",
            "last_error": None,
        })
        _dbg.info(f"One-time sync success — {n_opts} options")
        _log("sync", f"✓ Sync — {n_opts} options (in-memory)")
    except Exception as exc:
        err = str(exc)
        _dbg.error(f"One-time sync failed: {err}\n{traceback.format_exc()}")
        _sync_state.update({
            "status":     "error",
            "last_sync":  time.time(),
            "last_error": err,
            "message":    None,
        })
        _log("sync", f"✗ Sync failed: {err[:120]}")


async def _run_ongoing_sync() -> None:
    """One pass of the ongoing sync — in-memory, no disk write, no AI trigger.
    Reuses the persistent FidelitySession browser so no repeated login."""
    _ongoing_state["status"] = "running"
    _dbg.debug("Ongoing sync: begin")
    try:
        session = _get_fidelity_session()
        csv_content = await asyncio.to_thread(
            session.get_csv_memory, True,
            lambda msg: _dbg.debug(f"[fidelity] {msg}"),
        )
        _st.load_csv_content(csv_content)
        _ongoing_state.update({
            "status":     "done",
            "last_sync":  time.time(),
            "last_error": None,
        })
        n_opts = len(_st.portfolio["options"])
        ts = _dt.utcnow().strftime("%H:%M UTC")
        _dbg.info(f"Ongoing sync success {ts} — {n_opts} options")
        _log("sync", f"✓ Ongoing sync {ts}  ({n_opts} options, in-memory)")
        # Alert check only — skip AI analysis
        asyncio.create_task(_check_alerts_once())
    except Exception as exc:
        err = str(exc)
        _dbg.error(f"Ongoing sync failed: {err}\n{traceback.format_exc()}")
        _ongoing_state.update({
            "status":     "error",
            "last_sync":  time.time(),
            "last_error": err,
        })
        _log("sync", f"✗ Ongoing sync failed: {err[:120]}")


async def _ongoing_sync_loop() -> None:
    """Ongoing background loop — respects interval & Mon–Fri 06:00–13:00 PT."""
    while _ongoing_state["enabled"]:
        # Sleep for the configured interval, checking every 30 s for disable
        interval_secs = _ongoing_state["interval_secs"]
        slept = 0
        while slept < interval_secs:
            await asyncio.sleep(min(30, interval_secs - slept))
            slept += 30
            if not _ongoing_state["enabled"]:
                return
        if not _ongoing_state["enabled"]:
            break
        # Only run during Mon–Fri 06:00–13:00 PT
        if not _is_market_hours():
            continue
        await _run_ongoing_sync()


@app.post("/api/sync/fidelity")
async def trigger_fidelity_sync():
    """Start a one-time background Fidelity portfolio sync. Returns immediately."""
    if _sync_state["status"] == "running":
        return {"status": "already_running", "message": "Sync is already in progress."}

    if not os.environ.get("FIDELITY_USERNAME") or not os.environ.get("FIDELITY_PASSWORD"):
        raise HTTPException(
            status_code=503,
            detail="FIDELITY_USERNAME and FIDELITY_PASSWORD environment variables are not set.",
        )

    _sync_state.update({"status": "running", "last_error": None, "message": "Connecting to Fidelity…"})
    asyncio.create_task(_run_fidelity_sync_once())
    return {"status": "triggered"}


@app.get("/api/sync/status")
def fidelity_sync_status():
    """Poll the current state of the one-time Fidelity sync."""
    creds_set = bool(os.environ.get("FIDELITY_USERNAME") and os.environ.get("FIDELITY_PASSWORD"))
    totp_set  = bool(os.environ.get("FIDELITY_TOTP_SECRET"))
    return {
        **_sync_state,
        "age_secs":  round(time.time() - _sync_state["last_sync"])
                     if _sync_state["last_sync"] else None,
        "creds_configured": creds_set,
        "totp_configured":  totp_set,
    }


class OngoingSyncUpdate(BaseModel):
    enabled:       Optional[bool] = None
    interval_secs: Optional[int]  = None   # seconds (preferred)
    interval_mins: Optional[int]  = None   # minutes (backward compat → converted to secs)
    allow_anytime: Optional[bool] = None   # bypass market-hours restriction (debug)


@app.get("/api/sync/ongoing")
def get_ongoing_sync():
    """Return current ongoing sync settings and status."""
    return {
        **_ongoing_state,
        "age_secs":     round(time.time() - _ongoing_state["last_sync"])
                        if _ongoing_state["last_sync"] else None,
        "in_market_hours": _is_market_hours(),
        "creds_configured": bool(
            os.environ.get("FIDELITY_USERNAME") and os.environ.get("FIDELITY_PASSWORD")
        ),
    }


@app.post("/api/sync/ongoing")
async def update_ongoing_sync(body: OngoingSyncUpdate):
    """Enable/disable ongoing sync or change the interval."""
    global _ongoing_task

    if body.interval_secs is not None:
        _ongoing_state["interval_secs"] = max(30, body.interval_secs)
    elif body.interval_mins is not None:
        _ongoing_state["interval_secs"] = max(30, body.interval_mins * 60)

    if body.allow_anytime is not None:
        _ongoing_state["allow_anytime"] = body.allow_anytime
        label = "开启" if body.allow_anytime else "关闭"
        _dbg.info(f"allow_anytime toggled: {body.allow_anytime}")
        _log("setting", f"允许随时同步 {label} (绕过交易时段限制)")

    if body.enabled is not None:
        prev = _ongoing_state["enabled"]
        _ongoing_state["enabled"] = body.enabled

        if body.enabled and not prev:
            # Newly enabled — start the loop
            if _ongoing_task and not _ongoing_task.done():
                _ongoing_task.cancel()
            _ongoing_task = asyncio.create_task(_ongoing_sync_loop())
            iv = _ongoing_state['interval_secs']
            iv_str = f"{iv//60}m {iv%60}s" if iv % 60 else f"{iv//60}m"
            _log("setting", f"Ongoing sync 已开启  (间隔 {iv_str}，交易时段 Mon–Fri 06:00–13:00 PT)")
        elif not body.enabled and prev:
            # Disabled — the loop will exit naturally on its next 30 s check
            if _ongoing_task and not _ongoing_task.done():
                _ongoing_task.cancel()
            _ongoing_state["status"] = "idle"
            _log("setting", "Ongoing sync 已关闭")

    return {**_ongoing_state, "in_market_hours": _is_market_hours()}


@app.post("/api/sync/now")
async def sync_now():
    """
    Manual one-time sync triggered from Settings → Fidelity Sync → Sync Now.
    Extracts positions via DOM scraping (no download button, no disk write),
    stores snapshot in memory, and reloads the portfolio.
    """
    if not os.environ.get("FIDELITY_USERNAME") or not os.environ.get("FIDELITY_PASSWORD"):
        raise HTTPException(
            status_code=503,
            detail="FIDELITY_USERNAME and FIDELITY_PASSWORD environment variables are not set.",
        )

    filename = _dated_csv_name()
    _dbg.info(f"Sync Now: begin — {filename}")
    try:
        session = _get_fidelity_session()
        csv_content = await asyncio.to_thread(
            session.get_csv_memory, True,
            lambda msg: _dbg.debug(f"[fidelity] {msg}"),
        )
        # Keep snapshot in memory (no disk write)
        _memory_snapshots[filename] = csv_content
        _st.load_csv_content(csv_content)
        n_opts = len(_st.portfolio["options"])
        _dbg.info(f"Sync Now success — {n_opts} options")
        _log("sync", f"✓ Sync Now — {filename}  ({n_opts} options, in-memory)")
        return {
            "status":        "ok",
            "filename":      filename,
            "options_count": n_opts,
            "stocks_count":  len(_st.portfolio["stocks"]),
        }
    except Exception as exc:
        err = str(exc)
        _dbg.error(f"Sync Now failed: {err}\n{traceback.format_exc()}")
        _log("sync", f"✗ Sync Now failed: {err[:120]}")
        raise HTTPException(status_code=500, detail=err)


# ── debug log ──────────────────────────────────────────────────────────────────
@app.get("/api/logs/debug")
def get_debug_logs(lines: int = Query(default=500, ge=1, le=5000)):
    """
    Return the last N lines from the persistent debug log file.
    The file is capped at 1 MB and entries older than 7 days are purged at startup.
    """
    if not DEBUG_LOG_FILE.exists():
        return {
            "lines": [], "total_lines": 0,
            "size_bytes": 0, "path": str(DEBUG_LOG_FILE),
        }
    try:
        content   = DEBUG_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        all_lines = [l for l in content.splitlines() if l.strip()]
        return {
            "lines":       all_lines[-lines:],
            "total_lines": len(all_lines),
            "size_bytes":  DEBUG_LOG_FILE.stat().st_size,
            "path":        str(DEBUG_LOG_FILE),
        }
    except Exception as exc:
        _dbg.error(f"Failed to read debug log: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── static ─────────────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
