"""
Bond ETF Screener — Graham Safety-First Framework
Applies Graham's defensive bond principles to a curated universe of ~35
investment-grade bond ETFs.

Graham criteria (5 pts): credit quality, adequate size (AUM), no speculation
                          (excludes HY/junk), income adequacy (SEC yield),
                          capital preservation (drawdown / duration)
Buffett criteria (5 pts): real yield (inflation-adjusted), ultra-low cost,
                           risk-adjusted return (Sharpe), stable price history,
                           short-to-medium duration (capital preservation)

Buffett's view on bonds: "I prefer equities, but when holding bonds own only
short/medium-duration, high-quality paper — never speculate on yield."

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
# Only investment-grade bond ETFs. HY/junk excluded entirely
# (Graham: "The investor should own only high-quality bonds.")
BOND_ETF_UNIVERSE: List[str] = [
    # US Treasury — highest Graham safety (AAA / Government)
    "GOVT", "TLT", "IEF", "SHY", "VGLT", "VGIT", "VGSH", "SCHO", "SCHR",
    # TIPS / Inflation-Protected — real return (Buffett: protect purchasing power)
    "TIP", "VTIP", "STIP", "SCHP",
    # Total Investment-Grade Bond Market
    "AGG", "BND", "SCHZ", "IUSB",
    # Investment-Grade Corporate (A-BBB)
    "LQD", "VCIT", "VCSH", "IGIB", "IGSB", "SPIB", "SPSB",
    # Short-Term IG — Buffett's preferred bond duration
    "NEAR", "JPST", "MINT", "ICSH", "FLOT", "FLRN", "USFR",
    # Municipal (tax-advantaged, AA average quality)
    "MUB", "VTEB", "TFI",
    # International IG
    "BNDX", "IGOV",
]

# Pre-defined metadata for known bond ETFs
# (credit_quality, duration_bucket, description)
# duration_bucket: "ultra-short" <1yr, "short" 1-3yr, "medium" 3-7yr,
#                  "long" 7-15yr, "ultra-long" 15yr+
_BOND_META: Dict[str, Tuple[str, str, str]] = {
    "GOVT":  ("Government",        "medium",      "US Treasury Broad Market"),
    "TLT":   ("Government",        "ultra-long",  "US Treasury 20yr+"),
    "IEF":   ("Government",        "long",        "US Treasury 7–10yr"),
    "SHY":   ("Government",        "short",       "US Treasury 1–3yr"),
    "VGLT":  ("Government",        "ultra-long",  "Vanguard Long-Term Treasury"),
    "VGIT":  ("Government",        "medium",      "Vanguard Intermediate Treasury"),
    "VGSH":  ("Government",        "short",       "Vanguard Short-Term Treasury"),
    "SCHO":  ("Government",        "short",       "Schwab Short-Term US Treasury"),
    "SCHR":  ("Government",        "medium",      "Schwab Intermediate US Treasury"),
    "TIP":   ("Government/TIPS",   "medium",      "iShares TIPS Bond ETF"),
    "VTIP":  ("Government/TIPS",   "short",       "Vanguard Short-Term TIPS"),
    "STIP":  ("Government/TIPS",   "ultra-short", "iShares 0-5yr TIPS Bond"),
    "SCHP":  ("Government/TIPS",   "medium",      "Schwab US TIPS ETF"),
    "AGG":   ("IG Blend",          "medium",      "iShares Core US Agg Bond"),
    "BND":   ("IG Blend",          "medium",      "Vanguard Total Bond Market"),
    "SCHZ":  ("IG Blend",          "medium",      "Schwab US Agg Bond ETF"),
    "IUSB":  ("IG Blend",          "medium",      "iShares Core Total USD Bond"),
    "LQD":   ("Corporate IG",      "long",        "iShares iBoxx $ IG Corporate"),
    "VCIT":  ("Corporate IG",      "medium",      "Vanguard Intermediate Corp Bond"),
    "VCSH":  ("Corporate IG",      "short",       "Vanguard Short-Term Corp Bond"),
    "IGIB":  ("Corporate IG",      "medium",      "iShares IG 5–10yr Corp Bond"),
    "IGSB":  ("Corporate IG",      "short",       "iShares IG 1–5yr Corp Bond"),
    "SPIB":  ("Corporate IG",      "medium",      "SPDR Portfolio Intermediate IG"),
    "SPSB":  ("Corporate IG",      "short",       "SPDR Portfolio Short-Term IG"),
    "NEAR":  ("Corporate IG",      "ultra-short", "iShares Short Maturity Bond"),
    "JPST":  ("Corporate IG",      "ultra-short", "JPMorgan Ultra-Short Income"),
    "MINT":  ("Corporate IG",      "ultra-short", "PIMCO Enhanced Short Maturity"),
    "ICSH":  ("Corporate IG",      "ultra-short", "iShares Ultra Short-Term Bond"),
    "FLOT":  ("Floating Rate IG",  "ultra-short", "iShares Floating Rate Bond"),
    "FLRN":  ("Floating Rate IG",  "ultra-short", "SPDR Bloomberg Investment Grade Float"),
    "USFR":  ("Floating Rate IG",  "ultra-short", "WisdomTree Floating Rate Treasury"),
    "MUB":   ("Municipal",         "medium",      "iShares National Muni Bond"),
    "VTEB":  ("Municipal",         "medium",      "Vanguard Tax-Exempt Bond"),
    "TFI":   ("Municipal",         "medium",      "SPDR Nuveen Bloomberg Muni Bond"),
    "BNDX":  ("International IG",  "medium",      "Vanguard Total International Bond"),
    "IGOV":  ("International IG",  "medium",      "iShares International Treasury Bond"),
}

# Credit quality score (Graham: safety of principal is paramount)
_CREDIT_SCORE: Dict[str, float] = {
    "government":       1.0,   # AAA — no default risk
    "government/tips":  1.0,   # AAA + inflation protection
    "floating rate ig": 0.9,   # short duration + IG
    "municipal":        0.85,  # AA average, tax-advantaged
    "ig blend":         0.8,   # mix of gov + IG corp
    "corporate ig":     0.75,  # A/BBB — some credit risk
    "international ig": 0.65,  # IG but currency + country risk
}

# Duration score (Buffett: short/medium duration preserves capital)
_DURATION_SCORE: Dict[str, float] = {
    "ultra-short": 1.0,   # < 1yr  — negligible rate risk
    "short":       0.85,  # 1–3yr  — low rate risk
    "medium":      0.65,  # 3–7yr  — moderate rate risk
    "long":        0.40,  # 7–15yr — high rate risk
    "ultra-long":  0.20,  # 15yr+  — maximum rate risk
}

# ── FRED helpers ──────────────────────────────────────────────────────────────
def _get_risk_free_rate() -> float:
    try:
        r = requests.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"},
        )
        for line in reversed(r.text.strip().split("\n")):
            val = line.split(",")[-1].strip()
            if val and val != ".":
                return float(val) / 100.0
    except Exception:
        pass
    return 0.045

def _get_inflation_rate() -> float:
    """CPI YoY from FRED (CPIAUCSL series)."""
    try:
        r = requests.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"},
        )
        lines = [l for l in r.text.strip().split("\n") if l and not l.startswith("DATE")]
        if len(lines) >= 13:
            curr = float(lines[-1].split(",")[1])
            prev = float(lines[-13].split(",")[1])
            return (curr - prev) / prev
    except Exception:
        pass
    return 0.033   # fallback: 3.3%

# ── Helpers ───────────────────────────────────────────────────────────────────
def _sf(val, divisor: float = 1.0) -> Optional[float]:
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
    daily   = s.pct_change().dropna()
    ann_ret = _ann_return(s, years)
    ann_std = float(daily.std()) * (252 ** 0.5)
    if ann_ret is None or ann_std == 0:
        return None
    return (ann_ret - rf) / ann_std

def _max_drawdown(prices: pd.Series) -> Optional[float]:
    """Maximum peak-to-trough drawdown (negative float)."""
    s = prices.dropna()
    if len(s) < 20:
        return None
    roll_max = s.cummax()
    dd = (s - roll_max) / roll_max
    return float(dd.min())

# ── Graham × Buffett Scoring ──────────────────────────────────────────────────
def _score(symbol: str, info: Dict, hist_1y: pd.Series,
           hist_3y: pd.Series, rf: float, inflation: float) -> Dict:
    """
    Graham Criteria (5 pts):
      G1. Credit quality — government / IG only, no junk
      G2. Adequate size (AUM ≥ $1B) — no illiquid niche funds
      G3. Income adequacy — SEC yield ≥ 2% (bonds should pay income)
      G4. No speculation — floating-rate and short-duration preferred
          (proxy: duration bucket score)
      G5. Capital preservation — 3yr max drawdown < 15%

    Buffett Criteria (5 pts):
      B1. Real yield (yield − inflation > 0) — true purchasing power gain
      B2. Ultra-low cost (expense ratio ≤ 0.15%)
      B3. Risk-adjusted return (Sharpe ≥ 0.3 for bonds)
      B4. Short-to-medium duration preference (≤ 7yr)
      B5. Consistent positive 1yr return — stable price performance
    """
    checklist = []

    def _check(label, status, value, desc):
        checklist.append({"label": label, "status": status, "value": value, "desc": desc})

    meta    = _BOND_META.get(symbol, ("IG Blend", "medium", symbol))
    cq_key  = meta[0].lower()
    dur_key = meta[1]

    expense = _sf(info.get("expenseRatio") or info.get("annualReportExpenseRatio"))
    aum     = _sf(info.get("totalAssets") or info.get("netAssets"))
    # Bond ETF yield: yfinance 'yield' field is the distribution yield
    yld     = _sf(info.get("yield") or info.get("trailingAnnualDividendYield"))

    ret_1y   = _ann_return(hist_1y, 1.0)
    ret_3y   = _ann_return(hist_3y, 3.0)
    sharpe   = _sharpe(hist_3y, rf, 3.0)
    max_dd   = _max_drawdown(hist_3y)
    real_yld = (yld - inflation) if (yld is not None) else None

    credit_score   = _CREDIT_SCORE.get(cq_key, 0.5)
    duration_score = _DURATION_SCORE.get(dur_key, 0.5)

    # ── Graham Scores ────────────────────────────────────────────────────────
    # G1: Credit quality
    if credit_score >= 0.9:
        g1 = 1.0
        _check("G1 Credit Quality", "PASS", meta[0],
               "Government/AAA — maximum safety of principal (Graham #1 priority)")
    elif credit_score >= 0.75:
        g1 = 0.5
        _check("G1 Credit Quality", "COND", meta[0],
               "Investment-grade — acceptable credit risk")
    else:
        g1 = 0.0
        _check("G1 Credit Quality", "FAIL", meta[0],
               "Below A-average — Graham would avoid")

    # G2: AUM
    if aum is not None and aum >= 10e9:
        g2, g2v = 1.0, f"${aum/1e9:.1f}B"
        _check("G2 Fund Size (AUM)", "PASS", g2v, "≥ $10B — deep liquidity, tightest spreads")
    elif aum is not None and aum >= 1e9:
        g2, g2v = 0.5, f"${aum/1e9:.2f}B"
        _check("G2 Fund Size (AUM)", "COND", g2v, "$1B–$10B — adequate liquidity")
    else:
        g2, g2v = 0.0, f"${aum/1e6:.0f}M" if aum else "N/A"
        _check("G2 Fund Size (AUM)", "FAIL", g2v, "< $1B — potential liquidity risk")

    # G3: Income adequacy
    if yld is not None and yld >= 0.04:
        g3, g3v = 1.0, f"{yld*100:.2f}%"
        _check("G3 Income Yield", "PASS", g3v, "≥ 4% — strong income for bond allocation")
    elif yld is not None and yld >= 0.02:
        g3, g3v = 0.5, f"{yld*100:.2f}%"
        _check("G3 Income Yield", "COND", g3v, "2–4% — moderate income")
    else:
        g3, g3v = 0.0, f"{yld*100:.2f}%" if yld is not None else "N/A"
        _check("G3 Income Yield", "FAIL", g3v, "< 2% — minimal income generation")

    # G4: Duration safety (no speculation on rate movements)
    if duration_score >= 0.85:
        g4 = 1.0
        _check("G4 Duration Safety", "PASS", dur_key.capitalize(),
               "Short/ultra-short — minimal interest rate risk")
    elif duration_score >= 0.60:
        g4 = 0.5
        _check("G4 Duration Safety", "COND", dur_key.capitalize(),
               "Medium duration — moderate rate sensitivity")
    else:
        g4 = 0.0
        _check("G4 Duration Safety", "FAIL", dur_key.capitalize(),
               "Long/ultra-long — high interest rate risk (Graham: avoid speculation)")

    # G5: Capital preservation (max drawdown)
    if max_dd is not None and max_dd >= -0.08:
        g5, g5v = 1.0, f"{max_dd*100:.1f}%"
        _check("G5 Capital Preservation", "PASS", g5v, "< 8% drawdown — principal well protected")
    elif max_dd is not None and max_dd >= -0.15:
        g5, g5v = 0.5, f"{max_dd*100:.1f}%"
        _check("G5 Capital Preservation", "COND", g5v, "8–15% drawdown — acceptable")
    else:
        g5, g5v = 0.0, f"{max_dd*100:.1f}%" if max_dd is not None else "N/A"
        _check("G5 Capital Preservation", "FAIL", g5v, "> 15% drawdown — significant capital loss")

    # ── Buffett Scores ───────────────────────────────────────────────────────
    # B1: Real yield
    if real_yld is not None and real_yld >= 0.01:
        b1, b1v = 1.0, f"{real_yld*100:.2f}%"
        _check("B1 Real Yield", "PASS", b1v,
               "Yield beats inflation — true purchasing power gain")
    elif real_yld is not None and real_yld >= 0.0:
        b1, b1v = 0.5, f"{real_yld*100:.2f}%"
        _check("B1 Real Yield", "COND", b1v,
               "Yield matches inflation — barely preserves purchasing power")
    else:
        b1, b1v = 0.0, f"{real_yld*100:.2f}%" if real_yld is not None else "N/A"
        _check("B1 Real Yield", "FAIL", b1v,
               "Negative real yield — losing purchasing power")

    # B2: Ultra-low cost
    if expense is not None and expense <= 0.0005:          # ≤ 0.05%
        b2, b2v = 1.0, f"{expense*100:.3f}%"
        _check("B2 Ultra-Low Cost", "PASS", b2v, "≤ 0.05% — near-zero cost drag")
    elif expense is not None and expense <= 0.0015:        # ≤ 0.15%
        b2, b2v = 0.5, f"{expense*100:.3f}%"
        _check("B2 Ultra-Low Cost", "COND", b2v, "0.05–0.15% — low cost")
    else:
        b2, b2v = 0.0, f"{expense*100:.3f}%" if expense else "N/A"
        _check("B2 Ultra-Low Cost", "FAIL", b2v, "> 0.15% — unnecessary drag on bond returns")

    # B3: Risk-adjusted return (lower bar for bonds than equities)
    if sharpe is not None and sharpe >= 0.5:
        b3, b3v = 1.0, f"{sharpe:.2f}"
        _check("B3 Sharpe Ratio", "PASS", b3v, "≥ 0.5 — strong risk-adjusted return for bonds")
    elif sharpe is not None and sharpe >= 0.2:
        b3, b3v = 0.5, f"{sharpe:.2f}"
        _check("B3 Sharpe Ratio", "COND", b3v, "0.2–0.5 — acceptable for bond category")
    else:
        b3, b3v = 0.0, f"{sharpe:.2f}" if sharpe is not None else "N/A"
        _check("B3 Sharpe Ratio", "FAIL", b3v, "< 0.2 — poor risk/reward ratio")

    # B4: Duration preference (Buffett: short-to-medium only)
    if duration_score >= 0.85:
        b4 = 1.0
        _check("B4 Duration (Buffett)", "PASS", dur_key.capitalize(),
               "Short duration — Buffett's preferred bond structure")
    elif duration_score >= 0.60:
        b4 = 0.5
        _check("B4 Duration (Buffett)", "COND", dur_key.capitalize(),
               "Medium — acceptable if yield justifies the rate risk")
    else:
        b4 = 0.0
        _check("B4 Duration (Buffett)", "FAIL", dur_key.capitalize(),
               "Long duration — Buffett: 'I would never buy a 30yr bond'")

    # B5: Stable price performance
    if ret_1y is not None and ret_1y >= 0.0:
        b5, b5v = 1.0, f"{ret_1y*100:.1f}%"
        _check("B5 1yr Performance", "PASS", b5v, "Positive 1yr return — stable price")
    elif ret_1y is not None and ret_1y >= -0.03:
        b5, b5v = 0.5, f"{ret_1y*100:.1f}%"
        _check("B5 1yr Performance", "COND", b5v, "Small 1yr decline — within normal range")
    else:
        b5, b5v = 0.0, f"{ret_1y*100:.1f}%" if ret_1y is not None else "N/A"
        _check("B5 1yr Performance", "FAIL", b5v, "Significant 1yr price loss")

    graham_score  = g1 + g2 + g3 + g4 + g5
    buffett_score = b1 + b2 + b3 + b4 + b5
    total         = graham_score + buffett_score

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
        "metrics": {
            "ret_1y":      round(ret_1y * 100, 2) if ret_1y is not None else None,
            "ret_3y":      round(ret_3y * 100, 2) if ret_3y is not None else None,
            "sharpe":      round(sharpe, 2)        if sharpe is not None else None,
            "max_dd":      round(max_dd * 100, 1)  if max_dd is not None else None,
            "expense":     round((expense or 0) * 100, 3),
            "aum_b":       round((aum or 0) / 1e9, 2),
            "yield_pct":   round((yld or 0) * 100, 2),
            "real_yield":  round(real_yld * 100, 2) if real_yld is not None else None,
            "credit":      meta[0],
            "duration":    dur_key,
        },
    }

# ── Main screen function ──────────────────────────────────────────────────────
def run_screen(on_progress=None) -> Dict:
    t0        = time.time()
    rf        = _get_risk_free_rate()
    inflation = _get_inflation_rate()
    if on_progress:
        on_progress(f"Risk-free rate: {rf*100:.2f}%  |  CPI inflation: {inflation*100:.1f}%")

    results = []
    total   = len(BOND_ETF_UNIVERSE)

    for i, symbol in enumerate(BOND_ETF_UNIVERSE):
        if on_progress:
            on_progress(f"  [{i+1}/{total}] Fetching {symbol}…")
        try:
            ticker = yf.Ticker(symbol)
            info   = ticker.info or {}
            name   = info.get("longName") or info.get("shortName") \
                     or _BOND_META.get(symbol, (None, None, symbol))[2]

            # Retry up to 3 times with back-off — guards against transient
            # yfinance rate-limit / network hiccups that silently return empty data
            h3 = pd.DataFrame()
            for _attempt, _period in enumerate(["3y", "3y", "2y"], start=1):
                h3 = yf.download(symbol, period=_period, auto_adjust=True,
                                 progress=False, threads=False)
                if not h3.empty:
                    break
                if on_progress:
                    on_progress(f"    {symbol} empty on attempt {_attempt}, retrying…")
                time.sleep(1.5 * _attempt)  # 1.5s → 3s back-off

            if h3.empty:
                if on_progress:
                    on_progress(f"    {symbol} skipped — no data after retries")
                continue

            close3 = h3["Close"].squeeze()
            close1 = close3[close3.index >= (
                datetime.now() - pd.DateOffset(years=1)).strftime("%Y-%m-%d")]

            ev = _score(symbol, info, close1, close3, rf, inflation)

            results.append({
                "symbol":        symbol,
                "name":          name[:45] if name else symbol,
                "credit":        ev["metrics"]["credit"],
                "duration":      ev["metrics"]["duration"],
                "score":         ev["score"],
                "grade":         ev["grade"],
                "graham_score":  ev["graham_score"],
                "buffett_score": ev["buffett_score"],
                "checklist":     ev["checklist"],
                "metrics":       ev["metrics"],
            })
        except Exception as e:
            if on_progress:
                on_progress(f"    {symbol} error: {e}")
            else:
                import logging as _log
                _log.getLogger(__name__).warning("bond_etf_screener: %s error: %s", symbol, e)
        time.sleep(0.5)  # increased from 0.3 → 0.5s to reduce rate-limit pressure

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "results":        results,
        "top5":           results[:5],
        "screened":       total,
        "eligible":       len(results),
        "run_date":       datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "risk_free_rate": round(rf * 100, 2),
        "inflation_rate": round(inflation * 100, 2),
        "duration_secs":  round(time.time() - t0),
    }


if __name__ == "__main__":
    import json, os
    out_dir = os.path.dirname(os.path.abspath(__file__))
    print("=" * 60)
    print("BOND ETF SCREENER — Graham Safety-First Framework")
    print("=" * 60)
    data = run_screen(on_progress=print)
    out  = os.path.join(out_dir, "bond_results.json")
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nTop 5 Bond ETFs:")
    for i, r in enumerate(data["top5"], 1):
        m = r["metrics"]
        print(f"  {i}. {r['symbol']:<6} {r['name'][:35]:<35} "
              f"Score {r['score']:.1f}/10  Grade {r['grade']}  "
              f"Yield {m['yield_pct']}%  RealYld {m['real_yield']}%  "
              f"Exp {m['expense']}%  Dur {m['duration']}")
    print(f"\nResults saved → {out}")
    print(f"Run time: {data['duration_secs']}s")
