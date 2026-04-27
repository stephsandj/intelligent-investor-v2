"""
Growth & Quality ETF Screener
Graham × Buffett framework applied to a curated universe of ~50 ETFs.

Graham criteria  (5 pts): expense ratio, adequate size (AUM), earnings
                           stability (consistent returns), dividend record,
                           not excessive price (beta / volatility proxy)
Buffett criteria (5 pts): Sharpe ratio (quality at right price),
                           long-term compounding (5yr return), predictable
                           earnings (return consistency), honest mgmt (low cost),
                           economic moat proxy (category quality score)
Total: 10 pts → grade A+ … F
"""

import math
import time
import warnings
import requests
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Universe ──────────────────────────────────────────────────────────────────
# Curated list of growth / quality ETFs that a Graham-Buffett investor
# might consider.  Excludes: leveraged, inverse, commodity, crypto.
GROWTH_ETF_UNIVERSE: List[str] = [
    # S&P 500 / Broad Market (Buffett's #1 recommendation)
    "SPY", "VOO", "IVV", "VTI", "SCHB",
    # Large Cap Growth
    "QQQ", "VUG", "IVW", "SCHG", "SPYG", "MGK",
    # Quality Factor (Graham loves)
    "QUAL", "SPHQ", "MOAT", "VFQY",
    # Dividend Growth (Buffett loves — income + quality signal)
    "VIG", "DGRO", "DGRW", "NOBL", "SDY", "DVY",
    # Technology Growth
    "VGT", "XLK", "IGV", "SOXX", "SMH",
    # Healthcare (defensive growth)
    "XLV", "VHT",
    # Consumer Staples (Buffett's circle of competence)
    "XLP", "VDC",
    # Financials (Buffett's largest sector)
    "XLF", "VFH",
    # Free Cash Flow (Graham approves — earnings quality)
    "COWZ", "CALF",
    # Momentum (trend confirmation)
    "MTUM", "PDP",
    # Mid Cap Growth
    "IWP", "VOT",
    # International Developed (diversification)
    "EFG", "VEA",
    # Small Cap Quality
    "IWO", "VBK",
]

# Category quality weights — how much a Buffett moat score is given
# based on the fund's stated category (from yfinance info['category'])
_CATEGORY_MOAT_SCORE: Dict[str, float] = {
    "large growth":          1.0,
    "large blend":           1.0,
    "large value":           0.75,
    "mid-cap growth":        0.75,
    "mid-cap blend":         0.75,
    "small growth":          0.5,
    "technology":            0.75,
    "health":                0.75,
    "consumer staples":      1.0,   # Buffett's favourite sector
    "financial":             0.75,
    "dividend":              1.0,   # dividend = quality signal
    "quality":               1.0,
    "momentum":              0.5,
    "international":         0.5,
    "diversified emerging":  0.25,
}

# ── Risk-free rate (FRED) ─────────────────────────────────────────────────────
def _get_risk_free_rate() -> float:
    """3-month US Treasury yield from FRED API (free, no key)."""
    try:
        r = requests.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        for line in reversed(r.text.strip().split("\n")):
            val = line.split(",")[-1].strip()
            if val and val != ".":
                return float(val) / 100.0
    except Exception:
        pass
    return 0.045   # fallback: 4.5%

# ── Helpers ───────────────────────────────────────────────────────────────────
def _sf(val, divisor: float = 1.0) -> Optional[float]:
    """Safe float conversion — returns None on NaN/Inf."""
    if val is None:
        return None
    try:
        v = float(val) / divisor
        return None if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return None

def _ann_return(prices: pd.Series, years: float) -> Optional[float]:
    s = prices.dropna()
    if len(s) < 20 or s.iloc[0] <= 0:
        return None
    total = s.iloc[-1] / s.iloc[0] - 1
    return (1 + total) ** (1.0 / years) - 1

def _sharpe(prices: pd.Series, rf: float, years: float = 3.0) -> Optional[float]:
    s = prices.dropna()
    if len(s) < 60:
        return None
    daily = s.pct_change().dropna()
    ann_ret = _ann_return(s, years)
    ann_std = float(daily.std()) * (252 ** 0.5)
    if ann_ret is None or ann_std == 0:
        return None
    return (ann_ret - rf) / ann_std

def _beta(etf_prices: pd.Series, spy_prices: pd.Series) -> Optional[float]:
    er = etf_prices.pct_change().dropna()
    sr = spy_prices.pct_change().dropna()
    df = pd.concat([er, sr], axis=1).dropna()
    if len(df) < 60:
        return None
    cov = float(df.cov().iloc[0, 1])
    var = float(df.iloc[:, 1].var())
    return cov / var if var != 0 else None

def _yearly_returns(prices: pd.Series) -> List[float]:
    """Returns list of calendar-year returns (most recent 5 years)."""
    s = prices.dropna()
    results = []
    for yr in range(datetime.now().year - 1, datetime.now().year - 6, -1):
        yr_data = s[s.index.year == yr]
        if len(yr_data) >= 20:
            results.append(float(yr_data.iloc[-1] / yr_data.iloc[0] - 1))
    return results

def _category_moat(category: str) -> float:
    """Map yfinance category string to a 0–1 moat score."""
    if not category:
        return 0.5
    cat_lower = category.lower()
    for key, score in _CATEGORY_MOAT_SCORE.items():
        if key in cat_lower:
            return score
    return 0.5

# ── Graham × Buffett Scoring ──────────────────────────────────────────────────
def _score(info: Dict, hist_1y: pd.Series, hist_3y: pd.Series,
           hist_5y: pd.Series, spy_3y: pd.Series, rf: float) -> Dict:
    """
    Graham Criteria (5 pts):
      G1. Expense ratio ≤ 0.20% — low cost (Intelligent Investor ch.14)
      G2. AUM ≥ $500M — adequate enterprise, liquid market
      G3. 3yr annualised return ≥ 0% — earnings stability
      G4. Dividend/income component — income record (bonus for ≥1%)
      G5. Not excessively volatile — beta ≤ 1.2 (margin of safety)

    Buffett Criteria (5 pts):
      B1. Sharpe ratio ≥ 0.5 — quality return per unit of risk
      B2. 5yr annualised return ≥ 8% — long-term compounding power
      B3. Return consistency — positive in ≥ 3 of last 5 calendar years
      B4. Expense ratio ≤ 0.10% — managers aligned with investors (extra)
      B5. Category moat score — economic moat of underlying holdings
    """
    checklist = []

    def _check(label: str, status: str, value: str, desc: str):
        checklist.append({"label": label, "status": status, "value": value, "desc": desc})

    # ── Shared metrics ───────────────────────────────────────────────────────
    expense  = _sf(info.get("expenseRatio") or info.get("annualReportExpenseRatio"))
    aum      = _sf(info.get("totalAssets") or info.get("netAssets"))
    div_yld  = _sf(info.get("yield") or info.get("trailingAnnualDividendYield"))
    category = info.get("category") or ""

    ret_1y = _ann_return(hist_1y, 1.0)
    ret_3y = _ann_return(hist_3y, 3.0)
    ret_5y = _ann_return(hist_5y, 5.0)
    sharpe = _sharpe(hist_3y, rf, 3.0)
    beta   = _beta(hist_3y, spy_3y)
    yr_rets = _yearly_returns(hist_5y)
    positive_years = sum(1 for r in yr_rets if r > 0) if yr_rets else 0
    moat   = _category_moat(category)

    # ── Graham Scores ────────────────────────────────────────────────────────
    # G1: Expense ratio
    if expense is not None and expense <= 0.0020:          # ≤ 0.20%
        g1, g1v = 1.0, f"{expense*100:.2f}%"
        _check("G1 Expense Ratio", "PASS", g1v, "≤ 0.20% — low drag on returns")
    elif expense is not None and expense <= 0.0050:        # ≤ 0.50%
        g1, g1v = 0.5, f"{expense*100:.2f}%"
        _check("G1 Expense Ratio", "COND", g1v, "0.20–0.50% — acceptable")
    else:
        g1, g1v = 0.0, f"{expense*100:.2f}%" if expense else "N/A"
        _check("G1 Expense Ratio", "FAIL", g1v, "> 0.50% — erodes long-term compounding")

    # G2: AUM
    if aum is not None and aum >= 5e9:
        g2, g2v = 1.0, f"${aum/1e9:.1f}B"
        _check("G2 Fund Size (AUM)", "PASS", g2v, "≥ $5B — institutional grade liquidity")
    elif aum is not None and aum >= 5e8:
        g2, g2v = 0.5, f"${aum/1e9:.2f}B"
        _check("G2 Fund Size (AUM)", "COND", g2v, "$500M–$5B — adequate")
    else:
        g2, g2v = 0.0, f"${aum/1e6:.0f}M" if aum else "N/A"
        _check("G2 Fund Size (AUM)", "FAIL", g2v, "< $500M — liquidity risk")

    # G3: 3yr return stability
    if ret_3y is not None and ret_3y >= 0.08:
        g3, g3v = 1.0, f"{ret_3y*100:.1f}%/yr"
        _check("G3 Earnings Stability", "PASS", g3v, "≥ 8%/yr annualised — consistent growth")
    elif ret_3y is not None and ret_3y >= 0.0:
        g3, g3v = 0.5, f"{ret_3y*100:.1f}%/yr"
        _check("G3 Earnings Stability", "COND", g3v, "0–8%/yr — modest but positive")
    else:
        g3, g3v = 0.0, f"{ret_3y*100:.1f}%/yr" if ret_3y is not None else "N/A"
        _check("G3 Earnings Stability", "FAIL", g3v, "Negative 3yr return")

    # G4: Dividend/income record
    if div_yld is not None and div_yld >= 0.01:
        g4, g4v = 1.0, f"{div_yld*100:.2f}%"
        _check("G4 Dividend Record", "PASS", g4v, "≥ 1% yield — income signal")
    elif div_yld is not None and div_yld > 0:
        g4, g4v = 0.5, f"{div_yld*100:.2f}%"
        _check("G4 Dividend Record", "COND", g4v, "Some income but < 1%")
    else:
        g4, g4v = 0.0, "0%"
        _check("G4 Dividend Record", "FAIL", g4v, "No dividend — pure price-return fund")

    # G5: Not excessively volatile (beta)
    if beta is not None and beta <= 0.9:
        g5, g5v = 1.0, f"{beta:.2f}"
        _check("G5 Stability (Beta)", "PASS", g5v, "≤ 0.9 vs S&P — defensive quality")
    elif beta is not None and beta <= 1.2:
        g5, g5v = 0.5, f"{beta:.2f}"
        _check("G5 Stability (Beta)", "COND", g5v, "0.9–1.2 — market-like volatility")
    else:
        g5, g5v = 0.0, f"{beta:.2f}" if beta is not None else "N/A"
        _check("G5 Stability (Beta)", "FAIL", g5v, "> 1.2 — above-market volatility")

    # ── Buffett Scores ───────────────────────────────────────────────────────
    # B1: Sharpe ratio
    if sharpe is not None and sharpe >= 1.0:
        b1, b1v = 1.0, f"{sharpe:.2f}"
        _check("B1 Sharpe Ratio", "PASS", b1v, "≥ 1.0 — excellent risk-adjusted return")
    elif sharpe is not None and sharpe >= 0.5:
        b1, b1v = 0.5, f"{sharpe:.2f}"
        _check("B1 Sharpe Ratio", "COND", b1v, "0.5–1.0 — adequate risk/reward")
    else:
        b1, b1v = 0.0, f"{sharpe:.2f}" if sharpe is not None else "N/A"
        _check("B1 Sharpe Ratio", "FAIL", b1v, "< 0.5 — poor risk-adjusted return")

    # B2: 5yr compounding
    if ret_5y is not None and ret_5y >= 0.12:
        b2, b2v = 1.0, f"{ret_5y*100:.1f}%/yr"
        _check("B2 Long-Term Compounding", "PASS", b2v, "≥ 12%/yr — Buffett-grade compounding")
    elif ret_5y is not None and ret_5y >= 0.08:
        b2, b2v = 0.5, f"{ret_5y*100:.1f}%/yr"
        _check("B2 Long-Term Compounding", "COND", b2v, "8–12%/yr — solid long-term growth")
    else:
        b2, b2v = 0.0, f"{ret_5y*100:.1f}%/yr" if ret_5y is not None else "N/A"
        _check("B2 Long-Term Compounding", "FAIL", b2v, "< 8%/yr 5yr return")

    # B3: Return consistency
    if positive_years >= 4:
        b3, b3v = 1.0, f"{positive_years}/5 yrs"
        _check("B3 Return Consistency", "PASS", b3v, "≥ 4 of last 5 years positive")
    elif positive_years >= 3:
        b3, b3v = 0.5, f"{positive_years}/5 yrs"
        _check("B3 Return Consistency", "COND", b3v, "3 of last 5 years positive")
    else:
        b3, b3v = 0.0, f"{positive_years}/5 yrs" if yr_rets else "N/A"
        _check("B3 Return Consistency", "FAIL", b3v, "< 3 of last 5 years positive")

    # B4: Ultra-low cost (Buffett: managers aligned with investors)
    if expense is not None and expense <= 0.0010:          # ≤ 0.10%
        b4, b4v = 1.0, f"{expense*100:.2f}%"
        _check("B4 Ultra-Low Cost", "PASS", b4v, "≤ 0.10% — near-zero fee drag")
    elif expense is not None and expense <= 0.0025:        # ≤ 0.25%
        b4, b4v = 0.5, f"{expense*100:.2f}%"
        _check("B4 Ultra-Low Cost", "COND", b4v, "0.10–0.25% — reasonable")
    else:
        b4, b4v = 0.0, f"{expense*100:.2f}%" if expense else "N/A"
        _check("B4 Ultra-Low Cost", "FAIL", b4v, "> 0.25% — fee drag compounds over decades")

    # B5: Economic moat of underlying holdings
    if moat >= 0.9:
        b5, b5v = 1.0, category or "Quality"
        _check("B5 Holdings Moat", "PASS", b5v, "Category associated with wide-moat companies")
    elif moat >= 0.5:
        b5, b5v = 0.5, category or "Mixed"
        _check("B5 Holdings Moat", "COND", b5v, "Some moat characteristics in holdings")
    else:
        b5, b5v = 0.0, category or "Unknown"
        _check("B5 Holdings Moat", "FAIL", b5v, "Category has limited moat characteristics")

    graham_score  = g1 + g2 + g3 + g4 + g5
    buffett_score = b1 + b2 + b3 + b4 + b5
    total         = graham_score + buffett_score

    # Grade
    if   total >= 9.0: grade = "A+"
    elif total >= 8.0: grade = "A"
    elif total >= 7.0: grade = "B+"
    elif total >= 6.0: grade = "B"
    elif total >= 5.0: grade = "C+"
    elif total >= 4.0: grade = "C"
    elif total >= 2.5: grade = "D"
    else:              grade = "F"

    return {
        "graham_score":  graham_score,
        "buffett_score": buffett_score,
        "score":         round(total, 1),
        "grade":         grade,
        "checklist":     checklist,
        # raw metrics for display
        "metrics": {
            "ret_1y":   round(ret_1y * 100, 1) if ret_1y is not None else None,
            "ret_3y":   round(ret_3y * 100, 1) if ret_3y is not None else None,
            "ret_5y":   round(ret_5y * 100, 1) if ret_5y is not None else None,
            "sharpe":   round(sharpe, 2) if sharpe is not None else None,
            "beta":     round(beta, 2)   if beta   is not None else None,
            "expense":  round((expense or 0) * 100, 3),
            "aum_b":    round((aum or 0) / 1e9, 2),
            "div_yld":  round((div_yld or 0) * 100, 2),
        },
    }

# ── Main screen function ──────────────────────────────────────────────────────
def run_screen(on_progress=None) -> Dict:
    """
    Screen GROWTH_ETF_UNIVERSE using Graham×Buffett criteria.
    Returns dict with keys: results, top5, screened, eligible,
                            run_date, risk_free_rate, duration_secs
    """
    t0   = time.time()
    rf   = _get_risk_free_rate()
    if on_progress: on_progress(f"Risk-free rate (3M Treasury): {rf*100:.2f}%")

    # Download SPY for beta calculation (3yr)
    if on_progress: on_progress("Downloading SPY benchmark (3yr)…")
    spy_hist = yf.download("SPY", period="3y", auto_adjust=True,
                           progress=False, threads=False)
    spy_close = spy_hist["Close"].squeeze() if not spy_hist.empty else pd.Series(dtype=float)

    results = []
    total   = len(GROWTH_ETF_UNIVERSE)

    for i, symbol in enumerate(GROWTH_ETF_UNIVERSE):
        if on_progress:
            on_progress(f"  [{i+1}/{total}] Fetching {symbol}…")
        try:
            ticker   = yf.Ticker(symbol)
            info     = ticker.info or {}
            name     = info.get("longName") or info.get("shortName") or symbol
            category = info.get("category") or ""

            # Skip leveraged / inverse
            if any(k in (category + name).lower()
                   for k in ["leveraged", "inverse", "bear", "2x", "3x", "ultra"]):
                if on_progress: on_progress(f"    {symbol} skipped — leveraged/inverse")
                continue

            # Price history
            h5 = yf.download(symbol, period="5y", auto_adjust=True,
                              progress=False, threads=False)
            if h5.empty:
                continue
            close5 = h5["Close"].squeeze()
            close3 = close5[close5.index >= (datetime.now() - pd.DateOffset(years=3)).strftime("%Y-%m-%d")]
            close1 = close5[close5.index >= (datetime.now() - pd.DateOffset(years=1)).strftime("%Y-%m-%d")]

            # Align SPY with this ETF's 3yr window
            spy3 = spy_close[spy_close.index.isin(close3.index)] if len(spy_close) else pd.Series(dtype=float)

            ev = _score(info, close1, close3, close5, spy3, rf)

            results.append({
                "symbol":         symbol,
                "name":           name[:45],
                "category":       category,
                "score":          ev["score"],
                "grade":          ev["grade"],
                "graham_score":   ev["graham_score"],
                "buffett_score":  ev["buffett_score"],
                "checklist":      ev["checklist"],
                "metrics":        ev["metrics"],
            })
        except Exception as e:
            if on_progress: on_progress(f"    {symbol} error: {e}")
        time.sleep(0.3)

    # Sort best-first
    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "results":        results,
        "top5":           results[:5],
        "screened":       total,
        "eligible":       len(results),
        "run_date":       datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "risk_free_rate": round(rf * 100, 2),
        "duration_secs":  round(time.time() - t0),
    }


if __name__ == "__main__":
    import json, sys
    out_dir = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
    print("=" * 60)
    print("GROWTH ETF SCREENER — Graham × Buffett Framework")
    print("=" * 60)
    data = run_screen(on_progress=print)
    out  = __import__("os").path.join(out_dir, "etf_results.json")
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nTop 5 ETFs:")
    for i, r in enumerate(data["top5"], 1):
        m = r["metrics"]
        print(f"  {i}. {r['symbol']:<6} {r['name'][:35]:<35} "
              f"Score {r['score']:.1f}/10  Grade {r['grade']}  "
              f"3yr {m['ret_3y']}%  Sharpe {m['sharpe']}  Exp {m['expense']}%")
    print(f"\nResults saved → {out}")
    print(f"Run time: {data['duration_secs']}s")
