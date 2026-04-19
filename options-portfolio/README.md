# Options Portfolio Analyzer

A FastAPI + vanilla-JS web application for analyzing AVGO (Broadcom) options positions loaded from a Fidelity brokerage CSV export. Provides Greeks-based P&L scenario projections, live price streaming, AI risk analysis, Telegram alerts, and a multi-tab dashboard.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Server](#running-the-server)
- [CSV Format](#csv-format)
- [UI Guide](#ui-guide)
- [API Reference](#api-reference)
- [Alert System](#alert-system)
- [AI Analysis](#ai-analysis)
- [Black-Scholes Model](#black-scholes-model)

---

## Features

| Category | Details |
|----------|---------|
| **Portfolio loading** | Auto-loads CSV from `blob/Portfolio_Positions_Latest.csv` at startup; file-watches and reloads automatically every 5 s when the file changes |
| **Manual upload** | Drag-and-drop or click to upload a Fidelity CSV via the browser |
| **Live price** | Fetches AVGO price from Yahoo Finance every 5 s via SSE stream; toggle on/off; portfolio charts update in real-time |
| **Greeks** | Black-Scholes Delta, Gamma, Theta, Vega, Rho computed for every option; implied volatility solved via Brent's method |
| **Scenario analysis** | Daily P&L curves × AVGO price ±10 % (5–41 price steps), payoff-at-expiry bar chart, Date × Price heatmap |
| **Scenario comparison** | Add arbitrary (date, price) points and compare P&L side-by-side |
| **AI risk analysis** | o4-mini (high reasoning) analysis of selected positions; 30-min in-memory cache; EN/中文 toggle; streams to the browser |
| **Telegram alerts** | Configurable warning / alert % thresholds; per-day per-account deduplication; AVGO intraday move alerts |
| **Event log** | Timestamped log of CSV loads, alerts, threshold changes — visible in the Logs tab with live 5 s auto-refresh |
| **Settings** | Warning %, Alert %, AVGO daily-move % thresholds editable in the Settings tab; persisted in memory |

---

## Architecture

```
options-portfolio/
├── backend/
│   ├── main.py              # FastAPI application, all API endpoints, background loops
│   ├── black_scholes.py     # Black-Scholes pricing, Greeks, implied volatility
│   ├── portfolio_parser.py  # Fidelity CSV parser
│   └── requirements.txt
├── static/
│   ├── index.html           # Single-page app shell (3 tabs)
│   ├── app.js               # All frontend logic (no framework)
│   └── styles.css           # Dark-mode design system
├── blob/                    # Hot-watch directory (gitignored)
│   └── Portfolio_Positions_Latest.csv
├── data/                    # Fallback sample data (gitignored)
├── run_server.sh            # Startup script (sources ~/.zshrc for env vars)
└── start.sh
```

**Backend loops (all async, started at server startup):**

| Loop | Interval | Purpose |
|------|----------|---------|
| `_price_loop` | 5 s | Polls Yahoo Finance for live AVGO price (only when live mode is ON) |
| `_file_loop` | 5 s | Checks `blob/` CSV mtime; reloads portfolio on change |
| `_alert_loop` | 60 s | Evaluates option gain/loss against thresholds; sends Telegram messages |

---

## Installation

### Prerequisites

- Python 3.10+
- pip

### Steps

```bash
# 1. Clone the repo
git clone git@github.com:phenixqing/tools.git
cd tools/options-portfolio

# 2. Create a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r backend/requirements.txt

# 4. Create the blob directory for your CSV
mkdir -p blob
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework |
| `uvicorn[standard]` | ASGI server |
| `numpy` / `scipy` | Black-Scholes math (Brent's method for IV) |
| `pandas` | (available for future CSV extensions) |
| `httpx` | Async HTTP — Yahoo Finance + Telegram |
| `openai` | AI analysis via o4-mini |
| `python-multipart` | File upload support |

---

## Configuration

All configuration lives in `backend/main.py`. Edit the constants at the top of the file:

```python
# ── File paths ─────────────────────────────────────────────────────────────────
BLOB_CSV = Path("/path/to/your/blob/Portfolio_Positions_Latest.csv")
DATA_CSV = BASE_DIR / "data" / "fallback.csv"   # used if blob not found

# ── Model parameters ───────────────────────────────────────────────────────────
RISK_FREE_RATE   = 0.045   # risk-free rate (4.5 %)
AVGO_DIV_YIELD   = 0.013   # AVGO dividend yield (1.3 %)

# ── Polling intervals ──────────────────────────────────────────────────────────
PRICE_POLL_SECS  = 5       # Yahoo Finance polling frequency
FILE_WATCH_SECS  = 5       # CSV file-watch frequency
ALERT_CHECK_SECS = 60      # Telegram alert evaluation frequency
AI_CACHE_TTL_SECS = 1800   # AI analysis cache TTL (30 min)

# ── Telegram ───────────────────────────────────────────────────────────────────
TG_TOKEN   = "YOUR_BOT_TOKEN"
TG_CHAT_ID = "YOUR_CHAT_ID"

# ── Default alert thresholds (also editable at runtime via Settings tab) ───────
_settings = {
    "warning_pct":    70.0,   # ⚠️  warn when |gain_loss_pct| > this
    "alert_pct":     100.0,   # 🚨 alert when |gain_loss_pct| > this
    "avgo_change_pct": 3.0,   # 📊 alert on intraday AVGO move > this
}
```

### OpenAI / AI Analysis

Set your API key as an environment variable before starting the server:

```bash
export OPENAI_API_KEY="sk-..."
```

The server sources `~/.zshrc` (or `~/.bashrc`) automatically via `run_server.sh`, so you can also export it there.

---

## Running the Server

```bash
# Standard start
./run_server.sh

# Or directly
cd options-portfolio
python3 backend/main.py
```

The app is served at **http://localhost:8000**.

To use a different port, edit `main.py`:

```python
uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
```

---

## CSV Format

The parser expects a standard **Fidelity "Portfolio Positions"** CSV export. Go to Fidelity → Accounts & Trade → Portfolio → Download (CSV).

Required columns (order-independent, matched by header name):

| Column | Example | Notes |
|--------|---------|-------|
| `Symbol` | ` -AVGO260424C420` | Options start with ` -`; OCC format |
| `Description` | `AVGO APR 24 2026 $420 CALL` | Human-readable label |
| `Account Number` | `ACCT001` | Used in alert dedup keys |
| `Quantity` | `-1` | Negative = short |
| `Last Price` | `$3.55` | Per-share option price |
| `Current Value` | `($2,130.00)` | Parenthetical = negative |
| `Cost Basis Total` | `$2,022.93` | Total premium paid/received |
| `Total Gain/Loss Dollar` | `($107.07)` | Absolute P&L |
| `Total Gain/Loss Percent` | `-5.30%` | Used for alert thresholds |
| `Average Cost Basis` | `$3.37` | Per-share cost |

**Supported position types:**

| Type | Detection |
|------|-----------|
| Option | Symbol starts with ` -` (OCC format: `AVGO260424C420`) |
| Stock / ETF | Symbol in `{AVGO, VOO, SPY, NHFSMKX98}` |
| Cash / money-market | Anything else with a non-zero current value |

**OCC symbol format parsed:**  
`AVGO 26 04 24 C 420` → underlying=AVGO, expiry=2026-04-24, type=CALL, strike=420.00

Drop your exported CSV at:
```
options-portfolio/blob/Portfolio_Positions_Latest.csv
```
The server detects the file change within 5 seconds and reloads automatically.

---

## UI Guide

### Tab: Portfolio

- **Summary cards** — Total Value, AVGO Price, AVGO Shares, Options Value, Options P&L, Stocks Value, Cash
- **Current Holdings** — stocks/ETFs grid + options table with full Greeks columns
  - Checkbox per row — controls which positions are included in Aggregate P&L and AI analysis
- **Scenario Comparison** — enter a (date, price) pair and click **+ Add** to pin a scenario card; shows P&L at that point for each included position
- **P&L Scenario Analysis**
  - *P&L by Date* — Plotly line chart; X = date, each line = a different AVGO price scenario (±10 %)
  - *Scenario Preview* — hover the chart above to see a live breakdown by position at any date
  - *AI Risk Analysis* — click **✦ Analyze Selected** to run o4-mini analysis on checked positions
  - *P&L at Expiry* — bar chart showing hold-to-expiry P&L at each price scenario
  - *P&L Heatmap* — 2-D grid: Date (Y) × AVGO Price (X), colored green/red by P&L

### Tab: Settings

- **Warning threshold** (default 70 %) — sends ⚠️ Telegram message when a contract's `Total Gain/Loss %` exceeds this (absolute value)
- **Alert threshold** (default 100 %) — sends 🚨 Telegram message
- **AVGO daily move** (default 3 %) — sends 📊 message when live intraday AVGO change exceeds this
- **Send Test Notification** — sends a real Telegram message and shows a preview of its content

### Tab: Logs

Auto-refreshes every 5 seconds while active. Events:

| Badge | Type | Trigger |
|-------|------|---------|
| 📂 CSV 加载 | `csv_load` | Startup load, file-watch reload, manual upload |
| ⚠️ 预警 | `warning` | Contract gain/loss crosses warning threshold |
| 🚨 告警 | `alert` | Contract gain/loss crosses alert threshold, or AVGO daily move |
| 🔔 测试 | `test` | Test notification button |
| ⚙️ 设置 | `setting` | Threshold saved via Settings tab |

---

## API Reference

Base URL: `http://localhost:8000`

---

### Portfolio

#### `GET /api/portfolio/summary`
Returns portfolio-wide totals and AVGO price metadata.

**Response:**
```json
{
  "reference_date": "2026-04-18",
  "underlying": "AVGO",
  "underlying_price": 405.60,
  "live_price_enabled": false,
  "live_price": null,
  "change_pct": null,
  "total_value": 255000.00,
  "options_value": -4500.00,
  "stocks_value": 121500.00,
  "cash_value": 138000.00,
  "options_unrealized_pnl": -850.00,
  "positions_count": { "options": 5, "stocks": 5 },
  "accounts": { "ACCT001": 123456.78 }
}
```

---

#### `GET /api/holdings`
Returns raw position data without Greeks.

**Response:**
```json
{
  "stocks": [
    { "symbol": "AVGO", "description": "BROADCOM INC COM", "account": "ACCT001",
      "quantity": 300, "market_price": 405.60, "current_value": 121680.0 }
  ],
  "options": [
    { "symbol": "-AVGO260424C420", "description": "AVGO APR 24 2026 $420 CALL",
      "account": "ACCT001", "option_type": "call", "strike": 420.0,
      "expiry": "2026-04-24", "quantity": -1,
      "market_price": 3.55, "current_value": -355.0 }
  ],
  "cash": [
    { "symbol": "FZFXX**", "account": "ACCT001", "current_value": 126168.15 }
  ]
}
```

---

#### `GET /api/options`
Returns all option positions enriched with Black-Scholes Greeks and P&L.

**Response (array):**
```json
[
  {
    "symbol": "-AVGO260424C420",
    "description": "AVGO APR 24 2026 $420 CALL",
    "account": "ACCT001",
    "underlying": "AVGO",
    "strike": 420.0,
    "option_type": "call",
    "expiry": "2026-04-24",
    "quantity": -1,
    "market_price": 3.55,
    "current_value": -355.0,
    "cost_basis": 337.16,
    "gain_loss": -17.84,
    "gain_loss_pct": -5.3,
    "days_to_expiry": 6,
    "implied_vol_pct": 38.42,
    "greeks": {
      "price": 3.55,
      "delta": -0.1823,
      "gamma": 0.00412,
      "theta": -1.2340,
      "vega": 0.4821,
      "rho": -0.0312
    }
  }
]
```

---

### Scenario Analysis

#### `GET /api/options/scenarios?price_steps=21`
Returns full P&L scenario matrix: every option × every date × every price scenario.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `price_steps` | int | 21 | Number of price scenarios (5–41, odd numbers give symmetric ±10 %) |

**Response (abbreviated):**
```json
{
  "reference_date": "2026-04-18",
  "underlying": "AVGO",
  "underlying_price": 405.60,
  "price_range": [365.04, 369.10, "...", 446.16],
  "pct_labels": ["-10.0%", "-9.0%", "...", "+10.0%"],
  "options": [
    {
      "symbol": "-AVGO260424C420",
      "short_label": "$420 C 04/24 (x-1)",
      "dates": ["2026-04-18", "2026-04-19", "...", "2026-04-24"],
      "pnl_matrix": [[...], [...], "..."],
      "option_price_matrix": [[...], "..."],
      "greeks_over_time": [{ "delta": -0.18, "gamma": 0.004, "theta": -1.23, "vega": 0.48, "rho": -0.03, "price": 3.55 }, "..."]
    }
  ],
  "aggregate": {
    "dates": ["2026-04-18", "...", "2026-04-24"],
    "pnl_matrix": [[...], "..."],
    "portfolio_pnl_matrix": [[...], "..."]
  },
  "avgo_shares": 300,
  "stock_delta": [-12016.8, "...", 12169.2]
}
```

`pnl_matrix[date_index][price_index]` = P&L in dollars for that date and price scenario.  
`portfolio_pnl_matrix` adds the stock position delta (AVGO shares × price change).

---

#### `GET /api/options/pnl_point?price=380&target_date=2026-04-22`
Calculates P&L for all options at a specific price and date. Used by the Scenario Comparison feature.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `price` | float | ✓ | AVGO price scenario |
| `target_date` | string | ✓ | ISO date `YYYY-MM-DD` |

**Response:**
```json
{
  "agg_pnl": -570.25,
  "port_pnl": -8250.25,
  "stock_delta": -7680.0,
  "opt_details": [
    { "symbol": "-AVGO260424C420", "pnl": 1840.20, "scen_price": 0.29, "scen_value": -174.0 }
  ]
}
```

---

### Live Price

#### `GET /api/price/status`
Returns current live-price state.

**Response:**
```json
{
  "live_enabled": false,
  "price": 405.60,
  "live_price": null,
  "change_pct": null
}
```

---

#### `POST /api/price/live?enabled=true`
Toggles live AVGO price polling.

| Parameter | Type | Description |
|-----------|------|-------------|
| `enabled` | bool | `true` to start polling Yahoo Finance, `false` to revert to CSV price |

**Response:**
```json
{ "live_enabled": true, "price": 403.21 }
```

---

#### `GET /api/price/stream`
Server-Sent Events (SSE) stream. Emits one event every 2 s.

**Event payload:**
```json
{ "price": 405.60, "live": false, "change_pct": null }
```

Connect with:
```javascript
const es = new EventSource("/api/price/stream");
es.onmessage = e => { const d = JSON.parse(e.data); console.log(d.price); };
```

---

### AI Analysis

#### `GET /api/ai/cache`
Returns the current state of the background AI analysis cache.

**Response:**
```json
{
  "status": "done",
  "lang": "zh",
  "trigger": "file_change",
  "timestamp": 1713456789.0,
  "age_secs": 432.0,
  "result": "## 1. 持仓风险分析\n..."
}
```

`status` values: `idle` | `running` | `done` | `error`

---

#### `POST /api/ai/cache/refresh?lang=zh`
Forces a fresh background AI analysis (ignores cache TTL).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lang` | string | `zh` | Response language: `zh` (Chinese) or `en` (English) |

**Response:** `{ "status": "triggered" }`

---

#### `POST /api/ai/analyze`
Runs an on-demand streaming AI analysis of selected positions. Streams plain-text chunks.

**Request body:**
```json
{
  "selected_symbols": ["-AVGO260424C420", "-AVGO260424C300"],
  "avgo_price": 405.60,
  "lang": "zh"
}
```

**Response:** `text/plain` stream — chunks of the AI's markdown response as they arrive.

---

### Settings

#### `GET /api/settings`
Returns current alert thresholds.

**Response:**
```json
{
  "warning_pct": 70.0,
  "alert_pct": 100.0,
  "avgo_change_pct": 3.0
}
```

---

#### `POST /api/settings`
Updates one or more thresholds. Fields are optional — only provided fields are updated.

**Request body:**
```json
{
  "warning_pct": 50.0,
  "alert_pct": 80.0,
  "avgo_change_pct": 5.0
}
```

**Response:** Updated settings object (same shape as `GET /api/settings`).

---

### Notifications

#### `POST /api/notifications/test`
Sends a test Telegram message and returns the result.

**Response:**
```json
{
  "channels": [
    { "name": "Telegram", "status": "ok", "detail": "Chat ID 1234****" }
  ],
  "message": "🔔 *测试通知*\n来自: Options Portfolio Analyzer\n..."
}
```

---

### Logs

#### `GET /api/logs?limit=200`
Returns the in-memory event log, newest first.

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `limit` | int | 200 | 1–500 | Max entries to return |

**Response (array):**
```json
[
  {
    "time": "2026-04-18T17:10:30Z",
    "type": "csv_load",
    "message": "启动加载: Portfolio_Positions_Latest.csv  (5 期权, 5 股票)"
  },
  {
    "time": "2026-04-18T17:08:58Z",
    "type": "warning",
    "message": "[ACCT001] AVGO APR 24 2026 $300 CALL 亏损 -79.2%，已发送预警"
  }
]
```

`type` values: `csv_load` | `warning` | `alert` | `test` | `setting` | `error`

---

### Upload

#### `POST /api/upload`
Uploads a Fidelity CSV file to replace the current portfolio (in-memory only; does not write to `blob/`).

**Request:** `multipart/form-data` with field `file`.

**Response:**
```json
{
  "status": "ok",
  "filename": "Portfolio_Positions_Apr-18-2026.csv",
  "options_count": 5,
  "stocks_count": 5,
  "avgo_price": 405.60
}
```

---

### Status

#### `GET /api/status`
Internal server state dump — useful for debugging.

**Response:**
```json
{
  "csv_path": "/path/to/blob/Portfolio_Positions_Latest.csv",
  "csv_exists": true,
  "csv_mtime": "2026-04-18T17:10:28",
  "avgo_price": 405.60,
  "live_enabled": false,
  "live_price": null,
  "change_pct": null,
  "options_count": 5,
  "ai_cache_status": "done",
  "ai_cache_age": 432,
  "alert_sent_count": 2,
  "alert_sent_today": [
    "-AVGO260424C300:ACCT001:warn:2026-04-18"
  ]
}
```

---

## Alert System

Alerts are evaluated every 60 seconds against all enriched option positions.

### Thresholds

| Level | Default | Telegram prefix | Condition |
|-------|---------|-----------------|-----------|
| Warning | 70 % | ⚠️ *期权预警* | `70 < |gain_loss_pct| ≤ 100` |
| Alert | 100 % | 🚨 *期权高风险警报* | `|gain_loss_pct| > 100` |
| AVGO move | 3 % | 📊 *AVGO 当日大幅* | `|intraday_change_pct| > 3` (live only) |

`gain_loss_pct` comes directly from the CSV `Total Gain/Loss Percent` column (Fidelity-calculated).

### Deduplication

Each alert is keyed as:

```
{symbol}:{account}:{level}:{YYYY-MM-DD}
```

Example: `-AVGO260424C300:ACCT001:warn:2026-04-18`

Keys are stored in an in-memory `set`. Once a key is in the set, no further message is sent for that contract-account-level combination that calendar day. **The set resets on server restart** (no disk persistence by design).

### Telegram Message Format

```
⚠️ *期权预警*
合约: `-AVGO260424C300`
账户: ACCT001
亏损: *-79.2%* (超过 70% 阈值)
当前价值: $-14003
AVGO: $405.60
```

---

## AI Analysis

The AI analysis prompt is built from the enriched option positions and sent to **o4-mini** with `reasoning_effort="high"`.

**Analysis sections (in both EN and 中文):**
1. Position Risk Analysis — per-contract risk/reward, breakeven, theta decay
2. Portfolio-Level Risks — concentration, directional bias, hedges
3. AVGO Short-Term Outlook (1–2 weeks) — key levels, implied move
4. AVGO Medium-Term Outlook (1–3 months) — technical/fundamental
5. Key Actions — specific, actionable suggestions

**Caching behavior:**
- Background cache triggered on: startup CSV load, file-watch reload
- Cache TTL: 30 minutes
- Manual refresh: click "✦ Analyze Selected" in the UI or `POST /api/ai/cache/refresh`
- If `OPENAI_API_KEY` is not set and `openclaw` CLI is not authenticated, the analyze button returns a 503 error

---

## Black-Scholes Model

Implemented in `backend/black_scholes.py` using the Merton (1973) model with continuous dividend yield.

**Parameters used:**
- Risk-free rate (`r`): 4.5 % (configurable via `RISK_FREE_RATE`)
- Dividend yield (`q`): 1.3 % (configurable via `AVGO_DIV_YIELD`)
- Implied volatility: solved per-option via Brent's method from the market price

**Greeks returned per option:**

| Greek | Formula | Unit |
|-------|---------|------|
| `delta` | ∂V/∂S | $ per $1 move in AVGO |
| `gamma` | ∂²V/∂S² | delta per $1 move in AVGO |
| `theta` | ∂V/∂t | $ per calendar day |
| `vega` | ∂V/∂σ | $ per 1 % point move in IV |
| `rho` | ∂V/∂r | $ per 1 % point move in rate |

For expired options (T = 0), intrinsic value is returned and all Greeks are zero.
