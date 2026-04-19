"""Parse Fidelity portfolio CSV into structured position data."""
import re
import csv
from datetime import date
from typing import Optional


# ── helpers ──────────────────────────────────────────────────────────────────

def _clean_num(raw) -> Optional[float]:
    """Parse a dollar/percent string like '+$1,234.56', '-$4,308.00', '($64,020.00)', '--' → float.

    Handles Fidelity's parenthetical negative notation: ($64,020.00) → -64020.0
    """
    s = str(raw).strip()
    if s in ("", "nan", "--", "N/A"):
        return None
    # Parenthetical negatives: ($64,020.00) or (5,387.25)
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace("+", "").replace("%", "")
    try:
        result = float(s)
        return -result if negative else result
    except ValueError:
        return None


def _parse_option_symbol(raw_symbol: str) -> Optional[dict]:
    """Parse an option symbol like ' -AVGO260417P375' or ' -AVGO260508C330'.

    OCC-style: UNDERLYING + YYMMDD + P/C + STRIKE (no leading zeros on strike)
    """
    sym = raw_symbol.strip().lstrip("-").strip()  # remove leading '-' and spaces
    pattern = r"^([A-Z]+)(\d{6})([PC])([\d.]+)$"
    m = re.match(pattern, sym)
    if not m:
        return None

    underlying, date_str, pc, strike_str = m.groups()
    yy, mm, dd = int(date_str[:2]), int(date_str[2:4]), int(date_str[4:6])
    expiry = date(2000 + yy, mm, dd)

    return {
        "underlying": underlying,
        "expiry": expiry,
        "option_type": "call" if pc == "C" else "put",
        "strike": float(strike_str),
    }


# ── main parser ───────────────────────────────────────────────────────────────

# Symbols that are clearly money-market / ETF / stock (not options)
_NON_OPTION_SYMS = {"FZFXX**", "FZDXX", "FDRXX**", "AVGO", "VOO", "SPY",
                    "NHFSMKX98", "Pending activity"}


def load_portfolio(csv_path: str) -> dict:
    """Return {'options': [...], 'stocks': [...], 'cash': [...]}."""
    options, stocks, cash = [], [], []

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_sym = row.get("Symbol", "")
            if not raw_sym or raw_sym.strip() in ("", "nan"):
                continue
            sym_clean = raw_sym.strip()

            # Stop at Fidelity disclaimer lines (they start with a quote block)
            desc = row.get("Description", "").strip()
            if "Brokerage services" in desc or "data and information" in desc.lower():
                break

            qty = _clean_num(row.get("Quantity"))
            market_price = _clean_num(row.get("Last Price"))
            current_value = _clean_num(row.get("Current Value")) or 0.0
            cost_basis = _clean_num(row.get("Cost Basis Total"))
            total_gain_loss = _clean_num(row.get("Total Gain/Loss Dollar"))
            total_gain_pct  = _clean_num(row.get("Total Gain/Loss Percent"))
            account = row.get("Account Number", "").strip()

            # ── Option contract (starts with ' -') ────────────────────────
            if raw_sym.startswith(" -") or raw_sym.startswith("-"):
                info = _parse_option_symbol(sym_clean)
                if info:
                    options.append({
                        **info,
                        "symbol": sym_clean,
                        "description": desc,
                        "account": account,
                        "quantity": qty or 0,
                        "market_price": market_price or 0.0,
                        "current_value": current_value,
                        "cost_basis": cost_basis,
                        "total_gain_loss": total_gain_loss,
                        "total_gain_pct":  total_gain_pct,
                    })
                continue

            # ── Stock / ETF ───────────────────────────────────────────────
            if sym_clean in ("AVGO", "VOO", "SPY", "NHFSMKX98"):
                stocks.append({
                    "symbol": sym_clean,
                    "description": desc,
                    "account": account,
                    "quantity": qty or 0,
                    "market_price": market_price or 0.0,
                    "current_value": current_value,
                    "cost_basis": cost_basis,
                })
                continue

            # ── Cash / money-market ────────────────────────────────────────
            if current_value != 0:
                cash.append({
                    "symbol": sym_clean,
                    "description": desc,
                    "account": account,
                    "current_value": current_value,
                })

    return {"options": options, "stocks": stocks, "cash": cash}
