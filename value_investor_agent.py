"""
Intelligent Investor Agent — Daily Stock Analyzer
Graham × Buffett Framework applied to today's worst S&P performers
"""

import warnings
warnings.filterwarnings("ignore")   # MUST be first — suppresses LibreSSL/urllib3 before any 3rd-party imports
import re
import os
import json
import requests
import smtplib
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo
_TZ_EST = ZoneInfo("America/New_York")  # all user-facing times in Eastern
from io import StringIO
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

def _load_dotenv(path: str):
    """Zero-dependency .env loader. Sets env vars from the file; also fills in any empty existing env vars."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key:
                continue
            # Treat an EMPTY existing env var as unset so .env can populate it
            if key not in os.environ or not os.environ.get(key, "").strip():
                os.environ[key] = val

_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
import yfinance as yf

# ── Auto-install anthropic SDK if missing ──────────────────────────────────────
try:
    import anthropic as _anthropic_sdk
except ImportError:
    import subprocess as _sp
    _sp.check_call([__import__("sys").executable, "-m", "pip", "install", "anthropic", "--user", "-q"])
    import anthropic as _anthropic_sdk

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas

# ── EMAIL CONFIGURATION ────────────────────────────────────────────────────────
# Credentials loaded from .env (GMAIL_FROM, GMAIL_APP_PASSWORD)
EMAIL_FROM    = os.environ.get("GMAIL_FROM", "")
EMAIL_APP_PWD = os.environ.get("GMAIL_APP_PASSWORD", "")

# ── CLAUDE AI ANALYSIS ─────────────────────────────────────────────────────────
# Set your Anthropic API key here, or export ANTHROPIC_API_KEY in your shell.
# Get a key at: https://console.anthropic.com
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── DATA MODEL ─────────────────────────────────────────────────────────────────
@dataclass
class StockMetrics:
    symbol: str
    company_name: str
    price: float
    pe_ratio: Optional[float]        # Trailing P/E
    forward_pe: Optional[float]      # Forward P/E
    pb_ratio: Optional[float]
    debt_to_equity: Optional[float]  # ratio (0.74 = 74%)
    current_ratio: Optional[float]
    roe: Optional[float]             # decimal (0.16 = 16%)
    dividend_yield: Optional[float]  # percentage (0.41 = 0.41%)
    market_cap: Optional[float]
    revenue: Optional[float]
    net_income: Optional[float]
    free_cash_flow: Optional[float]
    eps: Optional[float]
    book_value_per_share: Optional[float]
    net_margin: Optional[float]      # decimal (0.06 = 6%)
    beta: Optional[float]
    price_change_percent: float
    sector: Optional[str]
    industry: Optional[str]
    country: str = ""                             # e.g. "United States", "China"
    # Historical verification (populated from 10yr income statements + dividend history)
    hist_profitable_years: Optional[int] = None   # e.g. 8 of last 10 years profitable
    hist_total_years: Optional[int] = None        # how many years of data available
    hist_earnings_source: Optional[str] = None    # "FMP" or "yfinance"
    hist_div_years: Optional[int] = None          # years with dividend payments
    hist_eps_growth_pct: Optional[float] = None   # EPS % change over available history


# ── AGENT CONFIG ────────────────────────────────────────────────────────────────
_AGENT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_AGENT_CONFIG_DEFAULTS = {
    "email_enabled":    True,
    "pdf_enabled":      True,
    "markets":          ["NYSE", "NASDAQ"],
    "loser_period":     "daily100",  # "daily100"|"daily500"|"dailyall"|"weekly100"|"weekly500"|"weeklyall"|"yearly100"|"yearly500"|"yearlyall"|"value100"|"value500"
    "stock_geography":  "all",     # "all" | "usa" | "international"
    "claude_api_key":   "",        # Anthropic API key for AI deep-dive analysis
}

def _load_agent_config() -> dict:
    """Load config with env-var overrides for per-user isolation.

    Priority (highest to lowest):
      1. Env vars injected by the dashboard (AGENT_EMAIL_ENABLED, AGENT_MARKETS, …)
         These are set per-user just before launching the subprocess, eliminating the
         race condition where concurrent scheduled runs overwrite the shared config.json.
      2. Shared config.json on disk (legacy / manual-run fallback).
      3. Built-in defaults.
    """
    cfg = dict(_AGENT_CONFIG_DEFAULTS)
    # --- Layer 1: shared disk config (baseline / legacy) ---
    try:
        if os.path.exists(_AGENT_CONFIG_FILE):
            with open(_AGENT_CONFIG_FILE) as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    # --- Layer 2: per-user env vars set by the dashboard (takes priority) ---
    _ev = os.environ.get
    if _ev("AGENT_EMAIL_ENABLED") is not None:
        cfg["email_enabled"]   = (_ev("AGENT_EMAIL_ENABLED") == "1")
    if _ev("AGENT_PDF_ENABLED") is not None:
        cfg["pdf_enabled"]     = (_ev("AGENT_PDF_ENABLED") == "1")
    if _ev("AGENT_EMAIL_ADDRESS"):
        cfg["email_address"]   = _ev("AGENT_EMAIL_ADDRESS")
    if _ev("AGENT_MARKETS"):
        cfg["markets"]         = [m.strip() for m in _ev("AGENT_MARKETS").split(",") if m.strip()]
    if _ev("AGENT_LOSER_PERIOD"):
        cfg["loser_period"]    = _ev("AGENT_LOSER_PERIOD")
    if _ev("AGENT_STOCK_GEOGRAPHY"):
        cfg["stock_geography"] = _ev("AGENT_STOCK_GEOGRAPHY")
    if _ev("AGENT_CLAUDE_KEY"):
        cfg["claude_api_key"]  = _ev("AGENT_CLAUDE_KEY")
    return cfg


# ── CLAUDE AI DEEP-DIVE ANALYSIS ───────────────────────────────────────────────
_AI_ANALYSIS_DEFAULT = {
    "business_context":        "",
    "metrics_assessment":      "",
    "value_trap_flags":        [],
    "key_signals":             [],
    "disqualifiers":           [],
    "recommendation":          "CONDITIONAL",
    "recommendation_label":    "⚠️ Review Manually",
    "recommendation_color":    "amber",
    "recommendation_narrative": "AI analysis unavailable — API key not configured.",
    "price_target_note":       "",
}

def _generate_enhanced_fallback_analysis(pick: dict) -> dict:
    """Generate V1-like detailed analysis from screening data and checklists (no API key needed)."""
    graham_checklist = pick.get('checklist', [])
    buffett_checklist = pick.get('buffett_checklist', [])

    graham_pass = sum(1 for c in graham_checklist if c.get('status') == 'PASS')
    graham_cond = sum(1 for c in graham_checklist if c.get('status') == 'COND')
    graham_fail = sum(1 for c in graham_checklist if c.get('status') == 'FAIL')

    buffett_pass = sum(1 for c in buffett_checklist if c.get('status') == 'PASS')
    buffett_fail = sum(1 for c in buffett_checklist if c.get('status') == 'FAIL')

    score = pick.get('score', 0)
    grade = pick.get('grade', 'D')
    pe_ratio = float(pick.get('pe_ratio') or 0)
    pb_ratio = float(pick.get('pb_ratio') or 0)
    debt_to_equity = float(pick.get('debt_to_equity') or 0)
    roe = float(pick.get('roe') or 0)
    net_margin = float(pick.get('net_margin') or 0)
    current_ratio = float(pick.get('current_ratio') or 0)
    price_change = float(pick.get('price_change_pct') or 0)

    sector = pick.get('sector', 'Unknown')
    industry = pick.get('industry', 'Unknown')
    profitable_yrs = pick.get('hist_profitable_years') or 0
    div_yrs = pick.get('hist_div_years') or 0

    # ── Business Context ──
    business_context = (
        f"{pick['name']} operates in {industry} ({sector}). "
        f"The company has been profitable in {profitable_yrs} of the last {pick.get('hist_total_years', 10)} years"
        f"{' and maintains a ' + str(div_yrs) + '-year dividend history.' if div_yrs > 0 else '.'}"
    )

    # ── Metrics Assessment ──
    metrics_signals = []
    if pe_ratio > 0 and pe_ratio < 15:
        metrics_signals.append("exceptionally low P/E ratio")
    elif pe_ratio > 25:
        metrics_signals.append("elevated P/E suggesting stretched valuation")

    if pb_ratio > 0 and pb_ratio < 1:
        metrics_signals.append("trading below book value")
    elif pb_ratio > 3:
        metrics_signals.append("trading well above book value")

    if roe > 0.15:
        metrics_signals.append("strong ROE indicating efficient capital use")
    elif roe < 0.08 and roe > 0:
        metrics_signals.append("modest ROE relative to capital base")

    if net_margin > 0.15:
        metrics_signals.append("healthy net margins")
    elif net_margin > 0.05:
        metrics_signals.append("thin profit margins typical of commodity/low-margin industries")

    if current_ratio > 2:
        metrics_signals.append("strong liquidity position")
    elif current_ratio < 1:
        metrics_signals.append("liquidity concerns")

    if debt_to_equity > 1.5:
        metrics_signals.append("elevated leverage that could constrain flexibility")
    elif debt_to_equity < 0.5:
        metrics_signals.append("conservative balance sheet with low leverage")

    metrics_assessment = (
        "Metrics reflect " + (", ".join(metrics_signals) if metrics_signals else "mixed fundamentals") + ". "
        f"Graham score of {graham_pass}/8 and Buffett score of {buffett_pass}/5 provide quantitative validation of underlying quality."
    )

    # ── Value Trap Flags ──
    value_trap_flags = []

    if price_change < -5:
        value_trap_flags.append(f"Significant recent decline ({price_change:.1f}%) may indicate deteriorating fundamentals or mean-reversion opportunity")

    if graham_fail > 0:
        failed_criteria = [c.get('criterion', 'Unknown') for c in graham_checklist if c.get('status') == 'FAIL']
        value_trap_flags.append(f"Graham criteria failures: {', '.join(failed_criteria[:2])}; verify these aren't permanent deterioration signals")

    if buffett_fail > 0:
        failed_criteria = [c.get('criterion', 'Unknown') for c in buffett_checklist if c.get('status') == 'FAIL']
        value_trap_flags.append(f"Buffett competitive moat criteria failures: {', '.join(failed_criteria[:1])}; business may lack sustainable advantages")

    if pe_ratio > 0 and pe_ratio < 8 and profitable_yrs > 0:
        value_trap_flags.append("Extremely low valuation may reflect structural/cyclical headwinds not captured by static metrics")

    if current_ratio < 1 and debt_to_equity > 1:
        value_trap_flags.append("Combined liquidity pressure and leverage create refinancing or distress risk")

    # Always provide baseline assessment if no specific risks identified
    if not value_trap_flags:
        if score < 6:
            value_trap_flags.append("Lower composite score suggests value quality concerns; deeper investigation recommended before investment")
        else:
            value_trap_flags.append("Preliminary screening shows acceptable value characteristics relative to fundamentals and market multiples")

    # ── Key Signals ──
    key_signals = []

    graham_pass_criteria = [c.get('criterion', 'Unknown') for c in graham_checklist if c.get('status') == 'PASS'][:3]
    if graham_pass_criteria:
        key_signals.append(f"Graham framework validates: {', '.join(graham_pass_criteria)}")
    else:
        key_signals.append(f"Graham framework: {graham_pass}/{graham_pass + graham_fail + graham_cond} criteria met ({graham_pass}/8 PASS)")

    buffett_pass_criteria = [c.get('criterion', 'Unknown') for c in buffett_checklist if c.get('status') == 'PASS'][:2]
    if buffett_pass_criteria:
        key_signals.append(f"Buffett quality signals: {', '.join(buffett_pass_criteria)}")
    else:
        key_signals.append(f"Buffett framework: {buffett_pass}/{buffett_pass + buffett_fail} criteria met ({buffett_pass}/5 PASS)")

    if div_yrs >= 20:
        key_signals.append("Dividend aristocrat status (20+ years of payments) suggests stable, mature business with shareholder-friendly management")
    elif div_yrs > 0:
        key_signals.append(f"Consistent dividend history ({div_yrs} years) indicates shareholder-focused management")

    if roe > 0.18 and net_margin > 0.12:
        key_signals.append("Superior profitability with both high ROE and margins indicates competitive strength")
    elif roe > 0.10:
        key_signals.append(f"Above-average ROE ({roe*100:.1f}%) demonstrates efficient capital deployment")

    if profitable_yrs == pick.get('hist_total_years'):
        key_signals.append("Consistent profitability through market cycles demonstrates business resilience")
    elif profitable_yrs > 0:
        key_signals.append(f"Profitable in {profitable_yrs}/{pick.get('hist_total_years', 10)} recent years; track profitability trends")

    # ── Hard Disqualifiers ──
    disqualifiers = []

    if graham_fail >= 3:
        disqualifiers.append("Multiple Graham criteria failures suggest fundamental value screening violations")

    if buffett_fail >= 2:
        disqualifiers.append("Lacks sufficient durable competitive advantages per Buffett framework")

    if debt_to_equity > 3:
        disqualifiers.append("Excessive leverage creates material solvency risk")

    if profitable_yrs < (pick.get('hist_total_years', 10) // 2):
        disqualifiers.append("Inconsistent profitability; majority of recent years unprofitable")

    # Always provide assessment if no hard disqualifiers found
    if not disqualifiers:
        if score >= 9:
            disqualifiers.append("No structural disqualifiers identified; stock passes fundamental quality thresholds")
        elif score >= 6:
            disqualifiers.append("No hard red flags detected; adequate fundamental quality for further investigation")
        else:
            disqualifiers.append("Limited disqualifiers but lower overall score warrants careful due diligence before investment")

    # ── Recommendation ──
    if score >= 11 and graham_pass >= 6 and buffett_pass >= 3:
        recommendation = "INVEST"
        recommendation_label = "✅ INVEST NOW"
        recommendation_color = "green"
        recommendation_narrative = (
            f"Strong fundamental alignment with Graham-Buffett framework ({score}/13 pts). "
            f"Checklist validation ({graham_pass}/8 Graham, {buffett_pass}/5 Buffett) supports thesis. "
            f"Price action and metrics indicate attractive risk-reward."
        )
    elif score >= 9 and graham_pass >= 5:
        recommendation = "CONDITIONAL"
        recommendation_label = "⚠️ INVESTIGATE FURTHER"
        recommendation_color = "amber"
        recommendation_narrative = (
            f"Promising fundamentals ({score}/13 pts) with moderate checklist support ({graham_pass}/8 Graham). "
            f"Requires deeper analysis of value trap risks and cyclical context before committing capital."
        )
        if pe_ratio > 0 and pe_ratio < 12:
            price_target_note = f"Consider initial position below ${float(pick.get('price', 0)) * 0.95:.2f}"
    elif score >= 6:
        recommendation = "WAIT"
        recommendation_label = "⏸️ WAIT FOR BETTER ENTRY"
        recommendation_color = "amber"
        recommendation_narrative = (
            f"Adequate fundamentals ({score}/13 pts) but insufficient checklist validation ({graham_pass}/8 Graham). "
            f"Quality is present but valuation or execution risk suggests patience for improved entry point."
        )
        if pe_ratio > 0:
            target_pe = pe_ratio * 0.85
            target_price = (float(pick.get('price', 100)) * target_pe) / pe_ratio if pe_ratio > 0 else 0
            if target_price > 0:
                price_target_note = f"Target entry: ${target_price:.2f}–${target_price * 1.05:.2f}"
    else:
        recommendation = "SKIP"
        recommendation_label = "❌ SKIP – QUALITY CONCERNS"
        recommendation_color = "red"
        recommendation_narrative = (
            f"Insufficient fundamental quality ({score}/13 pts). "
            f"Checklist failures ({graham_fail} Graham, {buffett_fail} Buffett) suggest value traps or structural challenges. "
            f"Better opportunities exist elsewhere."
        )

    return {
        "business_context": business_context,
        "metrics_assessment": metrics_assessment,
        "value_trap_flags": value_trap_flags[:3],  # Top 3
        "key_signals": key_signals[:5],  # Top 5
        "disqualifiers": disqualifiers[:3],  # Top 3
        "recommendation": recommendation,
        "recommendation_label": recommendation_label,
        "recommendation_color": recommendation_color,
        "recommendation_narrative": recommendation_narrative,
        "price_target_note": price_target_note if 'price_target_note' in locals() else "",
    }


def _generate_ai_analysis(pick: dict) -> dict:
    """Call Claude Haiku API to generate a deep-dive qualitative analysis for a pick."""
    api_key = CLAUDE_API_KEY
    if not api_key:
        # Use enhanced fallback analysis instead of defaults
        return _generate_enhanced_fallback_analysis(pick)

    def _fmt(v, pct=False, mult=False, dollar=False):
        if v is None:
            return "N/A"
        try:
            f = float(v)
            if pct:
                return f"{f * 100:.1f}%"
            if mult:
                return f"{f:.2f}×"
            if dollar:
                if f >= 1e12: return f"${f/1e12:.2f}T"
                if f >= 1e9:  return f"${f/1e9:.1f}B"
                if f >= 1e6:  return f"${f/1e6:.0f}M"
                return f"${f:.0f}"
            return f"{f:.2f}"
        except Exception:
            return str(v)

    checklist_txt = "\n".join(
        f"  [{c.get('status','')}] {c.get('label','')}: {c.get('value','')} — {c.get('desc','')}"
        for c in (pick.get("checklist") or [])
    ) or "  (none)"
    buffett_txt = "\n".join(
        f"  [{c.get('status','')}] {c.get('label','')}: {c.get('value','')} — {c.get('desc','')}"
        for c in (pick.get("buffett_checklist") or [])
    ) or "  (none)"

    user_msg = f"""Analyze {pick['symbol']} ({pick['name']}) — {pick.get('sector','?')} / {pick.get('industry','?')}

Screener score: {pick['score']}/13  Graham: {pick['graham_score']}  Buffett: {pick['buffett_score']}  Grade: {pick['grade']}
Price change today: {pick.get('price_change_pct', 0):.2f}%

Key metrics:
- Trailing P/E:    {_fmt(pick.get('pe_ratio'), mult=True)}
- Forward P/E:     {_fmt(pick.get('forward_pe'), mult=True)}
- P/B:             {_fmt(pick.get('pb_ratio'), mult=True)}
- D/E Ratio:       {_fmt(pick.get('debt_to_equity'), mult=True)}
- Current Ratio:   {_fmt(pick.get('current_ratio'))}
- ROE:             {_fmt(pick.get('roe'), pct=True)}
- Net Margin:      {_fmt(pick.get('net_margin'), pct=True)}
- Dividend Yield:  {_fmt(pick.get('dividend_yield'))}%
- Market Cap:      {_fmt(pick.get('market_cap'), dollar=True)}
- Revenue:         {_fmt(pick.get('revenue'), dollar=True)}
- Net Income:      {_fmt(pick.get('net_income'), dollar=True)}
- Free Cash Flow:  {_fmt(pick.get('free_cash_flow'), dollar=True)}
- EPS:             {_fmt(pick.get('eps'))}
- Beta:            {_fmt(pick.get('beta'))}
- Profitable years (of last {pick.get('hist_total_years', '?')}): {pick.get('hist_profitable_years', 'N/A')}
- Dividend history: {pick.get('hist_div_years', 'N/A')} years with dividends

Graham checklist:
{checklist_txt}

Buffett checklist:
{buffett_txt}

Respond ONLY with valid JSON (no markdown, no text outside the JSON object):
{{
  "business_context": "2–3 sentences: what this company does, its market position, primary revenue drivers, competitive moat",
  "metrics_assessment": "2–3 sentences: are the metrics genuinely strong or distorted by cyclical peaks, one-time items, accounting differences, or industry-specific norms?",
  "value_trap_flags": ["specific risk 1", "specific risk 2"],
  "key_signals": ["signal 1", "signal 2", "signal 3"],
  "disqualifiers": ["hard disqualifier 1"],
  "recommendation": "INVEST",
  "recommendation_label": "✅ INVEST NOW",
  "recommendation_color": "green",
  "recommendation_narrative": "2–3 sentences with specific, actionable rationale for this recommendation",
  "price_target_note": ""
}}

Rules:
- recommendation: exactly one of INVEST, SKIP, WAIT, CONDITIONAL
- recommendation_color: exactly one of green, red, amber
- value_trap_flags: 0–3 strings — specific value trap risks (cyclical peak, revenue decline, debt hidden in subsidiaries, unsustainable payout, etc.); empty [] if none
- key_signals: 3–5 strings — relevant recent context, business dynamics, or news that explains current valuation or metrics
- disqualifiers: 0–3 strings — hard risks NOT captured by screener metrics (structural disruption, governance red flags, regulatory overhang, ESG litigation, etc.); empty [] if none
- price_target_note: if recommendation is WAIT, give a specific entry price range like "$X–$Y"; otherwise ""
"""

    try:
        client = _anthropic_sdk.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            temperature=0,
            system=(
                "You are an expert value investor analyst trained in Benjamin Graham and Warren Buffett "
                "methodologies. You analyze stocks from a fundamental screener, looking beyond raw metrics "
                "to assess business quality, value traps, cyclical distortions, structural risks, and true "
                "investment merit. Your analysis is concise, specific, and actionable. "
                "Respond ONLY with valid JSON — no markdown fences, no explanation outside the JSON."
            ),
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if model adds them
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text.rstrip())
        result = json.loads(text)
        # Validate required fields
        for field in ["recommendation", "recommendation_color", "recommendation_label",
                      "recommendation_narrative", "business_context"]:
            if field not in result:
                result[field] = _AI_ANALYSIS_DEFAULT[field]
        for field in ["value_trap_flags", "key_signals", "disqualifiers"]:
            if not isinstance(result.get(field), list):
                result[field] = []
        return result
    except Exception as exc:
        print(f"    [AI analysis] {pick.get('symbol', '?')} Claude call failed: {exc} — using enhanced fallback")
        return _generate_enhanced_fallback_analysis(pick)


# ── FULL NYSE + NASDAQ SCREENER ─────────────────────────────────────────────────
# Uses the NASDAQ official listing API — free, no API key, ~2 HTTP requests.
# Returns ALL equity stocks on NYSE and NASDAQ with today's price change + market cap.

_NASDAQ_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
_NASDAQ_API_BASE = "https://api.nasdaq.com/api/screener/stocks"

# Regex to exclude warrants (W), units (U), rights (R), preferred (P suffix clusters)
_EQUITY_RE  = re.compile(r'^[A-Z]{1,5}$')
_OTC_SUFFIX = re.compile(r'[WURB]$')     # Warrants, Units, Rights, "B" class warrants


def _parse_market_cap(mc_str: str) -> float:
    """Convert '$1.23B' / '1234567890.00' / 'N/A' → float (0.0 on failure)."""
    if not mc_str:
        return 0.0
    mc_str = mc_str.replace('$', '').replace(',', '').strip()
    try:
        if mc_str.endswith('T'):  return float(mc_str[:-1]) * 1e12
        if mc_str.endswith('B'):  return float(mc_str[:-1]) * 1e9
        if mc_str.endswith('M'):  return float(mc_str[:-1]) * 1e6
        if mc_str.endswith('K'):  return float(mc_str[:-1]) * 1e3
        return float(mc_str)
    except (ValueError, TypeError):
        return 0.0


def _parse_pct(pct_str: str) -> float:
    """Convert '-3.45%' → -3.45 (0.0 on failure)."""
    try:
        return float(str(pct_str).replace('%', '').strip())
    except (ValueError, TypeError):
        return 0.0


def _is_equity(symbol: str) -> bool:
    """True for plain 1-5 letter tickers that look like common stock (not warrants/units)."""
    if not _EQUITY_RE.match(symbol):
        return False
    # 4-5 letter tickers ending in W/U/R are usually warrants/units/rights
    if len(symbol) >= 4 and _OTC_SUFFIX.search(symbol):
        return False
    return True


def fetch_nasdaq_nyse_losers(
    min_market_cap: float = 300_000_000,
    enabled_exchanges: List[str] = None,
    period: str = "daily",
) -> Tuple[List[Dict], int, int]:
    """
    Fetches equity stocks from selected exchanges and returns the worst performers
    for the given period.

    period: "daily100"  – stocks down today, sorted by market cap desc, Step 2 scores top 100
            "daily500"  – same but top 500
            "weekly100" – 5-day losers, sorted by market cap desc, Step 2 scores top 100
            "weekly500" – same but top 500
            "yearly100" – 52-week losers, sorted by market cap desc, Step 2 scores top 100
            "yearly500" – same but top 500
            "value100"  – ALL eligible stocks regardless of price direction; market cap desc; top 100
            "value500"  – same but top 500

    enabled_exchanges: list of exchange names to screen, e.g. ["NYSE", "NASDAQ", "AMEX"]
                       Defaults to ["NYSE", "NASDAQ"] if None or empty.

    Returns:
      (losers, universe_size, losers_count)
        losers         – sorted worst-first list of dicts
        universe_size  – total equity tickers from enabled exchanges
        losers_count   – how many qualified as losers
    """
    if not enabled_exchanges:
        enabled_exchanges = ["NYSE", "NASDAQ"]
    # NASDAQ screener uses lowercase exchange keys ("nasdaq", "nyse", "amex")
    enabled_set = {e.lower() for e in enabled_exchanges}

    # ── Fetch equity list from NASDAQ screener API ────────────────────────────
    _EXCHANGE_KEYS = {"nyse": "nyse", "nasdaq": "nasdaq", "amex": "amex"}
    all_rows: List[Dict] = []
    for exch_key, exch_label in _EXCHANGE_KEYS.items():
        if exch_key not in enabled_set:
            continue
        try:
            params = {
                "tableonly": "true",
                "limit":     10_000,
                "exchange":  exch_label,
                "download":  "true",
            }
            r = requests.get(_NASDAQ_API_BASE, params=params,
                             headers=_NASDAQ_API_HEADERS, timeout=20)
            r.raise_for_status()
            rows = r.json().get("data", {}).get("rows", [])
            for row in rows:
                row["_exchange"] = exch_key.upper()
            all_rows.extend(rows)
            print(f"  {exch_key.upper():<6} listed stocks : {len(rows):,}")
        except Exception as e:
            print(f"  [NASDAQ API] {exch_key} fetch failed: {e}")

    if not all_rows:
        return [], 0, 0

    # Deduplicate on symbol (NASDAQ API can return cross-listed duplicates)
    seen: set = set()
    equity_rows: List[Dict] = []
    for row in all_rows:
        sym = row.get("symbol", "")
        if sym not in seen and _is_equity(sym):
            seen.add(sym)
            equity_rows.append(row)

    universe_size = len(equity_rows)

    # ── Daily mode: use NASDAQ API pctchange directly (fast) ─────────────────
    if period in ("daily100", "daily500", "dailyall"):
        losers: List[Dict] = []
        for row in equity_rows:
            pct = _parse_pct(row.get("pctchange", "0"))
            mc  = _parse_market_cap(row.get("marketCap", ""))
            if pct < 0 and mc >= min_market_cap:
                try:
                    price = float(row.get("lastsale", "0").replace("$", "") or 0)
                except (ValueError, TypeError):
                    price = 0.0
                losers.append({
                    "symbol":           row["symbol"],
                    "name":             row.get("name", ""),
                    "changePercentage": round(pct, 4),
                    "price":            round(price, 4),
                    "marketCap":        mc,
                    "exchange":         row.get("_exchange", ""),
                })
        # Sort by market cap descending — Step 2 analyses the largest companies first
        losers.sort(key=lambda x: x["marketCap"], reverse=True)
        return losers, universe_size, len(losers)

    # ── Value mode: ALL eligible stocks, no price-direction filter ───────────
    if period in ("value100", "value500"):
        candidates = []
        for row in equity_rows:
            mc  = _parse_market_cap(row.get("marketCap", ""))
            sym = row.get("symbol", "").strip()
            if not sym or mc < min_market_cap:
                continue
            try:
                price = float(row.get("lastsale", "0").replace("$", "") or 0)
            except (ValueError, TypeError):
                price = 0.0
            pct = _parse_pct(row.get("pctchange", "0"))
            candidates.append({
                "symbol":           sym,
                "name":             row.get("name", ""),
                "changePercentage": round(pct, 4),
                "price":            round(price, 4),
                "marketCap":        mc,
                "exchange":         row.get("_exchange", ""),
            })
        # Sort largest-first — Step 2 will score the most significant companies first
        candidates.sort(key=lambda x: x["marketCap"], reverse=True)
        eligible = len(candidates)
        cap_n = 100 if period == "value100" else 500
        print(f"  Eligible stocks (mktCap > $300M, all price directions): {eligible:,}")
        print(f"  Scoring top {cap_n} by market cap")
        return candidates, universe_size, eligible

    # ── Weekly / Yearly mode: chunked yfinance download (no thread exhaustion) ─
    yf_period    = "5d" if period.startswith("weekly") else "1y"
    period_label = "5-day" if period.startswith("weekly") else "52-week"

    # Pre-filter by market cap; also strip any whitespace from symbols here
    candidates = [
        row for row in equity_rows
        if _parse_market_cap(row.get("marketCap", "")) >= min_market_cap
        and row.get("symbol", "").strip()   # skip blank/space-only tickers
    ]
    # Clean symbols — NASDAQ API occasionally returns ' XXXX' with a leading space
    symbols = [r["symbol"].strip() for r in candidates]
    if not symbols:
        return [], universe_size, 0

    import pandas as _pd

    # Download in chunks of 100 with threads=False to avoid exhausting the OS
    # thread pool (curl error 6: getaddrinfo() thread failed to start).
    CHUNK = 100
    n_chunks   = (len(symbols) + CHUNK - 1) // CHUNK
    all_series: dict = {}

    print(f"  Fetching {period_label} returns for {len(symbols):,} candidates "
          f"({n_chunks} chunks of ≤{CHUNK})…")

    for idx in range(0, len(symbols), CHUNK):
        chunk    = symbols[idx : idx + CHUNK]
        c_num    = idx // CHUNK + 1
        print(f"    chunk {c_num}/{n_chunks} ({len(chunk)} tickers)…", end="", flush=True)
        try:
            h = yf.download(
                chunk,
                period=yf_period,
                auto_adjust=True,
                progress=False,
                threads=False,      # sequential — no OS thread exhaustion
            )
            if h.empty:
                print(" empty")
                continue

            if len(chunk) == 1:
                # Single ticker → plain OHLCV DataFrame
                if "Close" in h.columns:
                    all_series[chunk[0]] = h["Close"]
            elif isinstance(h.columns, _pd.MultiIndex):
                # Multi-ticker → MultiIndex columns (field, ticker)
                if "Close" in h.columns.get_level_values(0):
                    for sym in h["Close"].columns:
                        all_series[sym] = h["Close"][sym]
            else:
                # Unexpected shape — skip
                pass
            print(f" ok ({len(chunk)} done)")
        except Exception as e:
            print(f" skipped ({e})")
            continue

    if not all_series:
        print(f"  No {period_label} data retrieved — check network connection.")
        return [], universe_size, 0

    close = _pd.DataFrame(all_series).dropna(axis=1, how="all")

    # Period return: (last_valid − first_valid) / first_valid × 100
    def _col_return(col):
        s = col.dropna()
        return (s.iloc[-1] - s.iloc[0]) / s.iloc[0] * 100 if len(s) >= 2 else float("nan")

    returns = close.apply(_col_return).dropna().sort_values()   # worst-first

    sym_to_row = {r["symbol"].strip(): r for r in candidates}
    losers = []
    for sym, pct in returns.items():
        if pct < 0 and sym in sym_to_row:
            row = sym_to_row[sym]
            try:
                last_price = float(close[sym].dropna().iloc[-1])
            except Exception:
                last_price = 0.0
            losers.append({
                "symbol":           sym,
                "name":             row.get("name", ""),
                "changePercentage": round(float(pct), 4),
                "price":            round(last_price, 4),
                "marketCap":        _parse_market_cap(row.get("marketCap", "")),
                "exchange":         row.get("_exchange", ""),
            })

    # Sort by market cap descending — Step 2 analyses the largest companies first
    losers.sort(key=lambda x: x["marketCap"], reverse=True)
    print(f"  {period_label.capitalize()} losers (mktCap > $300M): {len(losers):,}")
    return losers, universe_size, len(losers)


# ── FALLBACK: S&P 1500 via Wikipedia + yfinance bulk download ────────────────────
_SP_WIKI_URLS = [
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
]
_WIKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _fetch_sp1500_yf_losers() -> Tuple[List[Dict], int]:
    """Fallback: S&P 1500 symbols from Wikipedia + yfinance 2-day price download."""
    symbols: List[str] = []
    for url in _SP_WIKI_URLS:
        try:
            r = requests.get(url, headers=_WIKI_HEADERS, timeout=15)
            r.raise_for_status()
            table = pd.read_html(StringIO(r.text))[0]
            col = next((c for c in table.columns if str(c).lower() in ("symbol", "ticker")), None)
            if col:
                symbols += table[col].astype(str).str.replace(".", "-", regex=False).tolist()
        except Exception as e:
            print(f"  [Fallback] Warning: {e}")
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return [], 0

    print(f"  [Fallback] Downloading prices for {len(symbols)} S&P 1500 symbols…")
    try:
        data = yf.download(symbols, period="2d", progress=False,
                           group_by="ticker", auto_adjust=True, threads=True)
    except Exception as e:
        print(f"  [Fallback] yfinance download failed: {e}")
        return [], 0

    losers, total_valid = [], 0
    for sym in symbols:
        try:
            close = data[sym]["Close"].dropna()
            if len(close) < 2:
                continue
            prev, today = float(close.iloc[-2]), float(close.iloc[-1])
            if prev <= 0 or today <= 0 or math.isnan(prev) or math.isnan(today):
                continue
            total_valid += 1
            pct = (today - prev) / prev * 100
            if pct < 0:
                losers.append({"symbol": sym, "changePercentage": round(pct, 4),
                               "price": round(today, 4), "name": sym})
        except Exception:
            continue
    losers.sort(key=lambda x: x["changePercentage"])
    return losers, total_valid


# ── FMP CLIENT ─────────────────────────────────────────────────────────────────
class FMPClient:
    BASE_URL = "https://financialmodelingprep.com/stable"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _make_request(self, endpoint: str, params: Dict = None) -> Any:
        if params is None:
            params = {}
        params['apikey'] = self.api_key
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            r = self.session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"  API error ({endpoint}): {e}")
            return None

    def get_stock_losers(
        self,
        enabled_exchanges: List[str] = None,
        period: str = "daily",
    ) -> Tuple[List[Dict], int, int]:
        """
        Screen the configured exchanges for worst performers over the given period.

        Tier 1 — NASDAQ official API (preferred):
          Fetches ALL equity stocks listed on enabled exchanges.
          period="daily"  → uses NASDAQ API pctchange (fast, 2 HTTP requests)
          period="weekly" → batch yfinance 5-day returns for filtered candidates
          period="yearly" → batch yfinance 52-week returns for filtered candidates

        Tier 2 — S&P 1500 via Wikipedia + yfinance bulk download (fallback).
        Tier 3 — FMP biggest-losers endpoint (last resort, daily only).

        Returns (losers, universe_size, losers_count).
        """
        if not enabled_exchanges:
            enabled_exchanges = ["NYSE", "NASDAQ"]
        # ── Tier 1: NASDAQ official API ──────────────────────────────────────
        try:
            losers, universe_size, losers_count = fetch_nasdaq_nyse_losers(
                min_market_cap=300_000_000,
                enabled_exchanges=enabled_exchanges,
                period=period,
            )
            if losers:
                return losers, universe_size, losers_count
            print("  [Tier 1] NASDAQ API returned no losers — trying fallback…")
        except Exception as e:
            print(f"  [Tier 1] NASDAQ API failed: {e} — trying fallback…")

        # ── Tier 2: S&P 1500 via Wikipedia + yfinance ────────────────────────
        try:
            losers, total_valid = _fetch_sp1500_yf_losers()
            if losers:
                return losers, total_valid, len(losers)
            print("  [Tier 2] S&P 1500 yfinance returned no losers — trying fallback…")
        except Exception as e:
            print(f"  [Tier 2] S&P 1500 fallback failed: {e}")

        # ── Tier 3: FMP biggest-losers (50 stocks, always works) ─────────────
        print("  [Tier 3] Using FMP biggest-losers endpoint…")
        raw = self._make_request("biggest-losers") or []
        losers = []
        for r in raw:
            pct = r.get("changesPercentage") or r.get("changePercentage") or 0
            if pct < 0:
                losers.append({
                    "symbol":           r.get("symbol", ""),
                    "changePercentage": pct,
                    "price":            r.get("price", 0),
                    "name":             r.get("name", ""),
                })
        losers.sort(key=lambda x: x["changePercentage"])
        return losers, len(raw), len(losers)

    def _symbol_get(self, endpoint: str, symbol: str, extra: Dict = None) -> Dict:
        """Fetch a per-symbol endpoint and return first result dict (or {})."""
        params = {"symbol": symbol}
        if extra:
            params.update(extra)
        data = self._make_request(endpoint, params=params)
        if isinstance(data, list) and data:
            return data[0]
        elif isinstance(data, dict) and "Error Message" not in data:
            return data
        return {}

    def get_profile(self, symbol: str) -> Dict:
        """Company profile — beta, sector, industry, price, marketCap."""
        return self._symbol_get("profile", symbol)

    def get_ratios(self, symbol: str) -> Dict:
        """Financial ratios — PE, PB, CR, D/E, net margin, div yield, BVPS."""
        return self._symbol_get("ratios", symbol, {"limit": 1})

    def get_key_metrics(self, symbol: str) -> Dict:
        """Key metrics — ROE, market cap, Graham number."""
        return self._symbol_get("key-metrics", symbol, {"limit": 1})

    def get_income_statement(self, symbol: str) -> Dict:
        """Annual income statement — revenue, net income, EPS (most recent year)."""
        return self._symbol_get("income-statement", symbol, {"limit": 1})

    def get_income_statements(self, symbol: str, limit: int = 10) -> List[Dict]:
        """Annual income statements for multiple years, newest first."""
        data = self._make_request("income-statement", {"symbol": symbol, "limit": limit})
        return data if isinstance(data, list) else []

    def get_cash_flow_statement(self, symbol: str) -> Dict:
        """Annual cash flow statement — FCF, operating CF, capex."""
        return self._symbol_get("cash-flow-statement", symbol, {"limit": 1})


# ── ANALYZER ───────────────────────────────────────────────────────────────────
class ValueInvestingAnalyzer:
    def __init__(self, fmp_client: FMPClient):
        self.client = fmp_client

    def fetch_stock_metrics(self, symbol: str) -> Optional[StockMetrics]:
        print(f"  Fetching {symbol}...")
        try:
            # ── Helper ───────────────────────────────────────────────────────────
            def sf(val, divisor=1):
                if val is None:
                    return None
                try:
                    r = float(val)
                    return None if (math.isnan(r) or math.isinf(r)) else r / divisor
                except (ValueError, TypeError):
                    return None

            def g(d, key, divisor=1):
                """Safe-get from a dict with nan/inf protection."""
                return sf(d.get(key), divisor)

            # ── Layer 1: FMP API — comprehensive free data ────────────────────────
            profile = self.client.get_profile(symbol)
            time.sleep(0.3)

            # Skip ETFs, ETNs, leveraged products — no company financials exist
            if profile.get('isEtf') or profile.get('isFund') or profile.get('isEtn'):
                print(f"  Skipping {symbol} — ETF/ETN/Fund (not eligible for Graham analysis)")
                return None

            ratios  = self.client.get_ratios(symbol)
            time.sleep(0.3)
            km      = self.client.get_key_metrics(symbol)
            time.sleep(0.3)
            inc_history = self.client.get_income_statements(symbol, limit=10)  # 10yr history
            inc = inc_history[0] if inc_history else {}
            time.sleep(0.3)
            cf      = self.client.get_cash_flow_statement(symbol)
            time.sleep(0.3)

            # ── Price & price change (profile has real-time price) ─────────────────
            price_val  = g(profile, 'price') or 0
            changes_abs = g(profile, 'changes')    # absolute $ change vs prev close
            if changes_abs is not None and price_val and (price_val - changes_abs) != 0:
                price_change_pct = changes_abs / (price_val - changes_abs) * 100
            else:
                price_change_pct = 0.0

            # ── Identity ──────────────────────────────────────────────────────────
            company_name = profile.get('companyName') or symbol
            sector       = profile.get('sector')
            industry     = profile.get('industry')
            beta         = g(profile, 'beta')
            market_cap   = g(profile, 'marketCap') or g(km, 'marketCap')

            # ── Valuation ratios (from ratios — most recent FY) ───────────────────
            # FMP ratios use year-end price; recompute against today's price for accuracy
            eps_val  = g(inc, 'epsDiluted') or g(inc, 'eps')
            bvps     = g(ratios, 'bookValuePerShare')

            # P/E: prefer FMP ratios, then compute current (today price / FY EPS)
            pe = g(ratios, 'priceToEarningsRatio')
            if pe is None and price_val and eps_val and eps_val > 0:
                pe = price_val / eps_val

            # P/B: prefer FMP ratios, then compute current (today price / FY BVPS)
            pb = g(ratios, 'priceToBookRatio')
            if pb is None and price_val and bvps and bvps > 0:
                pb = price_val / bvps

            cr  = g(ratios, 'currentRatio')
            de  = g(ratios, 'debtToEquityRatio')   # already as ratio (NOT ×100)
            nm  = g(ratios, 'netProfitMargin')
            # dividendYieldPercentage is already in % (0.40 = 0.40%)
            div_pct = g(ratios, 'dividendYieldPercentage')

            # ── Profitability (key-metrics) ────────────────────────────────────────
            roe = g(km, 'returnOnEquity')

            # ── Fundamentals (income-statement) ───────────────────────────────────
            revenue    = g(inc, 'revenue')
            net_income = g(inc, 'netIncome')

            # ── Cash flow (cash-flow-statement) ───────────────────────────────────
            free_cash_flow = g(cf, 'freeCashFlow')
            if free_cash_flow is None:
                ocf = g(cf, 'operatingCashFlow')
                cap = g(cf, 'capitalExpenditure')   # negative value
                if ocf is not None:
                    free_cash_flow = ocf + (cap or 0)

            # ── Layer 2: yfinance — for forward P/E + fill any remaining gaps ──────
            forward_pe = None
            yf_ticker  = None
            country    = ""     # populated from yfinance; used for geography filtering
            try:
                yf_ticker = yf.Ticker(symbol)
                yf_info = yf_ticker.info or {}

                def yf_f(k, divisor=1):
                    return sf(yf_info.get(k), divisor)

                forward_pe = yf_f('forwardPE')

                # Fill gaps only if FMP returned nothing
                if pe          is None: pe         = yf_f('trailingPE')
                if pb          is None: pb         = yf_f('priceToBook')
                if cr          is None: cr         = yf_f('currentRatio')
                if de          is None: de         = yf_f('debtToEquity', 100)  # yfinance is ×100
                if roe         is None: roe        = yf_f('returnOnEquity')
                if nm          is None: nm         = yf_f('profitMargins')
                if beta        is None: beta       = yf_f('beta')
                if revenue     is None: revenue    = yf_f('totalRevenue')
                if net_income  is None: net_income = yf_f('netIncomeToCommon')
                if eps_val     is None: eps_val    = yf_f('trailingEps')
                if bvps        is None: bvps       = yf_f('bookValue')
                if div_pct     is None: div_pct    = yf_f('dividendYield')  # yf: already %
                if market_cap  is None: market_cap = yf_f('marketCap')
                if not sector:  sector   = yf_info.get('sector')
                if not industry: industry = yf_info.get('industry')
                country = yf_info.get('country', '') or ''
                if not company_name or company_name == symbol:
                    company_name = yf_info.get('shortName') or symbol
                if price_val == 0:
                    price_val = yf_f('currentPrice') or yf_f('regularMarketPrice') or 0
                if price_change_pct == 0:
                    price_change_pct = yf_f('regularMarketChangePercent') or 0

                # Derived: net margin if still missing
                if nm is None and net_income is not None and revenue and revenue != 0:
                    nm = net_income / revenue

                # Derived: P/E if still missing
                if pe is None and price_val and eps_val and eps_val > 0:
                    pe = price_val / eps_val

                # Derived: P/B if still missing
                if pb is None and price_val and bvps and bvps > 0:
                    pb = price_val / bvps

            except Exception as e:
                print(f"    yfinance fallback note for {symbol}: {e}")

            # ── Historical verification (10yr earnings, dividend history) ───────
            hist_profitable_years = None
            hist_total_years      = None
            hist_earnings_source  = None
            hist_div_years        = None
            hist_eps_growth_pct   = None

            # Earnings history from FMP (10yr income statements)
            if inc_history:
                ni_vals = [r.get('netIncome') for r in inc_history
                           if r.get('netIncome') is not None]
                if ni_vals:
                    hist_profitable_years = sum(1 for v in ni_vals if v > 0)
                    hist_total_years      = len(ni_vals)
                    hist_earnings_source  = 'FMP'
                    # EPS growth: newest (index 0) vs oldest (last index)
                    if len(inc_history) >= 2:
                        eps_new = sf(inc_history[0].get('epsDiluted') or inc_history[0].get('eps'))
                        eps_old = sf(inc_history[-1].get('epsDiluted') or inc_history[-1].get('eps'))
                        if eps_new is not None and eps_old and eps_old != 0:
                            hist_eps_growth_pct = (eps_new - eps_old) / abs(eps_old) * 100

            # Dividend history from yfinance
            if yf_ticker is not None:
                try:
                    div_series = yf_ticker.dividends
                    if div_series is not None and len(div_series) > 0:
                        hist_div_years = int(len(div_series.index.year.unique()))
                except Exception:
                    pass

            return StockMetrics(
                symbol=symbol,
                company_name=company_name,
                price=price_val,
                pe_ratio=pe,
                forward_pe=forward_pe,
                pb_ratio=pb,
                debt_to_equity=de,
                current_ratio=cr,
                roe=roe,
                dividend_yield=div_pct,
                market_cap=market_cap,
                revenue=revenue,
                net_income=net_income,
                free_cash_flow=free_cash_flow,
                eps=eps_val,
                book_value_per_share=bvps,
                net_margin=nm,
                beta=beta,
                price_change_percent=price_change_pct,
                sector=sector,
                industry=industry,
                country=country,
                hist_profitable_years=hist_profitable_years,
                hist_total_years=hist_total_years,
                hist_earnings_source=hist_earnings_source,
                hist_div_years=hist_div_years,
                hist_eps_growth_pct=hist_eps_growth_pct,
            )
        except Exception as e:
            print(f"  Error fetching {symbol}: {e}")
            return None

    def score_stock(self, m: StockMetrics) -> Dict:
        graham_checklist  = self._build_checklist(m)
        buffett_checklist = self._build_buffett_checklist(m)
        graham_score  = (sum(1.0 for c in graham_checklist  if c['status'] == 'PASS') +
                         sum(0.5 for c in graham_checklist  if c['status'] == 'COND'))
        buffett_score = (sum(1.0 for c in buffett_checklist if c['status'] == 'PASS') +
                         sum(0.5 for c in buffett_checklist if c['status'] == 'COND'))
        total_score = graham_score + buffett_score
        return {
            'score':           total_score,
            'graham_score':    graham_score,
            'buffett_score':   buffett_score,
            'max_score':       13,
            'grade':           self._grade(total_score),
            'checklist':       graham_checklist,
            'buffett_checklist': buffett_checklist,
            'passes':          total_score >= 6.5,
        }

    def _build_checklist(self, m: StockMetrics) -> List[Dict]:
        cl = []

        # 1. Adequate Size
        if m.revenue is not None:
            ok = m.revenue > 1e9
            cl.append({'criterion': 'Adequate Size (Rev > $1B)',
                       'detail': f"${m.revenue/1e9:.1f}B revenue",
                       'status': 'PASS' if ok else 'FAIL'})
        else:
            cl.append({'criterion': 'Adequate Size (Rev > $1B)',
                       'detail': 'Revenue data unavailable', 'status': 'COND'})

        # 2. Current Ratio >= 2.0
        if m.current_ratio is not None:
            ok = m.current_ratio >= 2.0
            cl.append({'criterion': 'Current Ratio >= 2.0',
                       'detail': f"{m.current_ratio:.2f} — {'solid liquidity' if ok else 'below threshold'}",
                       'status': 'PASS' if ok else 'FAIL'})
        else:
            cl.append({'criterion': 'Current Ratio >= 2.0',
                       'detail': 'Data unavailable', 'status': 'COND'})

        # 3. D/E <= 1.0
        if m.debt_to_equity is not None:
            ok = m.debt_to_equity <= 1.0
            cl.append({'criterion': 'Debt / Equity <= 1.0',
                       'detail': f"{m.debt_to_equity:.2f} — {'conservative leverage' if ok else 'above threshold'}",
                       'status': 'PASS' if ok else 'FAIL'})
        else:
            cl.append({'criterion': 'Debt / Equity <= 1.0',
                       'detail': 'Data unavailable', 'status': 'COND'})

        # 4. 10 Yrs Positive Earnings — verified via FMP income-statement history
        if m.hist_profitable_years is not None and m.hist_total_years:
            yrs   = m.hist_profitable_years
            total = m.hist_total_years
            src   = m.hist_earnings_source or 'data'
            if yrs == total:
                status = 'PASS'
                detail = f"Profitable all {total} of last {total} yrs — verified via {src}"
            elif yrs >= max(total - 2, int(total * 0.8)):
                status = 'COND'
                detail = f"Profitable {yrs}/{total} yrs — verified via {src}"
            else:
                status = 'FAIL'
                detail = f"Profitable only {yrs}/{total} yrs — verified via {src}"
        elif m.net_income is not None:
            ok = m.net_income > 0
            ni = (f"${m.net_income/1e9:.1f}B" if abs(m.net_income) >= 1e9
                  else f"${m.net_income/1e6:.0f}M")
            status = 'COND' if ok else 'FAIL'
            detail = f"{'Profitable' if ok else 'Loss'}: {ni} net income (1 yr only)"
        else:
            status = 'COND'
            detail = 'Earnings data unavailable'
        cl.append({'criterion': '10 Yrs Positive Earnings', 'detail': detail, 'status': status})

        # 5. Dividend Record 20+ yrs — verified via yfinance dividend history
        if m.hist_div_years is not None and m.hist_div_years > 0:
            dy     = m.hist_div_years
            dy_str = f"{dy}+ years" if dy >= 20 else f"{dy} years"
            yld    = f" ({m.dividend_yield:.2f}% yield)" if m.dividend_yield and m.dividend_yield > 0 else ""
            status = 'PASS' if dy >= 20 else 'COND'
            detail = f"Dividends paid {dy_str}{yld} — verified via yfinance"
        elif m.dividend_yield and m.dividend_yield > 0:
            status = 'COND'
            detail = f"{m.dividend_yield:.2f}% yield — history length could not be verified"
        else:
            status = 'FAIL'
            detail = 'No dividend currently paid'
        cl.append({'criterion': 'Dividend Record (20+ yrs)', 'detail': detail, 'status': status})

        # 6. EPS Growth >= 1/3 over 10yr — verified via FMP income-statement history
        if m.hist_eps_growth_pct is not None:
            g_pct = m.hist_eps_growth_pct
            yrs   = m.hist_total_years or 10
            src   = m.hist_earnings_source or 'data'
            if g_pct >= 33:
                status = 'PASS'
                detail = f"EPS grew +{g_pct:.1f}% over {yrs} yrs — verified via {src}"
            elif g_pct > 0:
                status = 'COND'
                detail = f"EPS +{g_pct:.1f}% over {yrs} yrs (< 33% threshold) — via {src}"
            else:
                status = 'FAIL'
                detail = f"EPS declined {g_pct:.1f}% over {yrs} yrs — verified via {src}"
        elif m.eps is not None:
            ok     = m.eps > 0
            status = 'COND' if ok else 'FAIL'
            detail = f"Current EPS ${m.eps:.2f} — multi-year growth not available"
        else:
            status = 'COND'
            detail = 'EPS data unavailable'
        cl.append({'criterion': 'EPS Growth (>= 1/3 / 10yr)', 'detail': detail, 'status': status})

        # 7. P/E <= 15
        if m.pe_ratio and m.pe_ratio > 0:
            ok = m.pe_ratio <= 15
            cl.append({'criterion': 'P/E <= 15',
                       'detail': f"P/E {m.pe_ratio:.2f} — {'passes Graham' if ok else 'above Graham 15x'}",
                       'status': 'PASS' if ok else 'FAIL'})
        else:
            cl.append({'criterion': 'P/E <= 15',
                       'detail': 'P/E unavailable (negative earnings?)', 'status': 'FAIL'})

        # 8. Graham PE x PB <= 22.5
        if m.pe_ratio and m.pb_ratio and m.pe_ratio > 0 and m.pb_ratio > 0:
            pepb = m.pe_ratio * m.pb_ratio
            ok = pepb <= 22.5
            cl.append({'criterion': 'Graham (PExPB <= 22.5)',
                       'detail': f"PExPB = {pepb:.1f} — {'passes formula' if ok else 'exceeds 22.5'}",
                       'status': 'PASS' if ok else 'FAIL'})
        else:
            cl.append({'criterion': 'Graham (PExPB <= 22.5)',
                       'detail': 'Cannot calculate — missing P/E or P/B', 'status': 'COND'})

        return cl

    def _build_buffett_checklist(self, m: StockMetrics) -> List[Dict]:
        cl = []

        # B1. ROE >= 15% — durable competitive advantage
        if m.roe is not None:
            if m.roe >= 0.15:
                status = 'PASS'; detail = f"ROE {m.roe*100:.1f}% — exceeds Buffett's 15% moat threshold"
            elif m.roe >= 0.10:
                status = 'COND'; detail = f"ROE {m.roe*100:.1f}% — below 15%, partial moat signal"
            else:
                status = 'FAIL'; detail = f"ROE {m.roe*100:.1f}% — weak return on equity"
        else:
            status = 'COND'; detail = 'ROE data unavailable'
        cl.append({'criterion': 'ROE >= 15% (Competitive Moat)', 'detail': detail, 'status': status})

        # B2. Net Margin >= 10% — pricing power
        if m.net_margin is not None:
            if m.net_margin >= 0.10:
                status = 'PASS'; detail = f"Net margin {m.net_margin*100:.1f}% — strong pricing power"
            elif m.net_margin >= 0.05:
                status = 'COND'; detail = f"Net margin {m.net_margin*100:.1f}% — moderate margin"
            else:
                status = 'FAIL'; detail = f"Net margin {m.net_margin*100:.1f}% — below Buffett threshold"
        else:
            status = 'COND'; detail = 'Net margin data unavailable'
        cl.append({'criterion': 'Net Margin >= 10% (Pricing Power)', 'detail': detail, 'status': status})

        # B3. D/E <= 0.5 — financial fortress (stricter than Graham)
        if m.debt_to_equity is not None:
            if m.debt_to_equity <= 0.5:
                status = 'PASS'; detail = f"D/E {m.debt_to_equity:.2f} — financial fortress"
            elif m.debt_to_equity <= 1.0:
                status = 'COND'; detail = f"D/E {m.debt_to_equity:.2f} — manageable, above 0.5 threshold"
            else:
                status = 'FAIL'; detail = f"D/E {m.debt_to_equity:.2f} — too leveraged for Buffett"
        else:
            status = 'COND'; detail = 'D/E data unavailable'
        cl.append({'criterion': 'D/E <= 0.5 (Financial Fortress)', 'detail': detail, 'status': status})

        # B4. Positive Free Cash Flow — earnings quality
        if m.free_cash_flow is not None:
            fcf_str = (f"${m.free_cash_flow/1e9:.1f}B" if abs(m.free_cash_flow) >= 1e9
                       else f"${m.free_cash_flow/1e6:.0f}M")
            if m.free_cash_flow > 0:
                status = 'PASS'; detail = f"FCF {fcf_str} — business generates real cash"
            else:
                status = 'FAIL'; detail = f"FCF {fcf_str} — negative free cash flow"
        else:
            status = 'COND'; detail = 'FCF data unavailable'
        cl.append({'criterion': 'Free Cash Flow > 0 (Earnings Quality)', 'detail': detail, 'status': status})

        # B5. FCF Yield >= 3% (FCF / Market Cap) — shareholder value
        if m.free_cash_flow is not None and m.market_cap and m.market_cap > 0:
            fcf_yield = (m.free_cash_flow / m.market_cap) * 100
            if fcf_yield >= 3.0:
                status = 'PASS'; detail = f"FCF yield {fcf_yield:.1f}% — strong shareholder returns"
            elif fcf_yield >= 1.0:
                status = 'COND'; detail = f"FCF yield {fcf_yield:.1f}% — moderate yield"
            else:
                status = 'FAIL'; detail = f"FCF yield {fcf_yield:.1f}% — low or negative yield"
        else:
            status = 'COND'; detail = 'Cannot calculate — missing FCF or market cap'
        cl.append({'criterion': 'FCF Yield >= 3% (Shareholder Value)', 'detail': detail, 'status': status})

        return cl

    def _grade(self, score: float) -> str:
        # Thresholds scaled proportionally from /8 → /13
        if score >= 12.0:  return 'A'
        elif score >= 10.5: return 'B+'
        elif score >= 8.0:  return 'B'
        elif score >= 6.5:  return 'C+'
        elif score >= 5.0:  return 'C'
        elif score >= 2.5:  return 'D'
        else: return 'F'


# ── PDF GENERATOR ──────────────────────────────────────────────────────────────
class IntelligentInvestorPDFGenerator:

    # ── Palette ──
    BG         = colors.HexColor('#0d1117')
    BG_CARD    = colors.HexColor('#161b22')
    BG_METRIC  = colors.HexColor('#1a2033')
    TXT_WHITE  = colors.HexColor('#e6edf3')
    TXT_GRAY   = colors.HexColor('#8b949e')
    TXT_ORANGE = colors.HexColor('#f5a623')
    TXT_GREEN  = colors.HexColor('#3fb950')
    TXT_RED    = colors.HexColor('#f85149')
    TXT_YELLOW = colors.HexColor('#d29922')
    TXT_CYAN   = colors.HexColor('#58a6ff')
    BDR_SUBTLE = colors.HexColor('#30363d')
    BDR_CYAN   = colors.HexColor('#00b4d8')
    BDR_GREEN  = colors.HexColor('#238636')
    BDR_RED    = colors.HexColor('#da3633')
    BDR_ORANGE = colors.HexColor('#e67e22')

    TICKER_COLORS = [
        colors.HexColor('#f5a623'),
        colors.HexColor('#58a6ff'),
        colors.HexColor('#bc8cff'),
        colors.HexColor('#ff7b72'),
        colors.HexColor('#7ee787'),
    ]

    W, H = letter   # 612 x 792
    LM = 36
    RM = 36
    CW = 540        # content width

    # ── Public entry point ──────────────────────────────────────────────────
    def generate_report(self, stocks: List[Tuple], filename: str,
                        run_date: datetime = None) -> str:
        if run_date is None:
            run_date = datetime.now(_TZ_EST)
        date_str  = run_date.strftime('%B %-d, %Y')
        time_str  = run_date.strftime('%H:%M:%S')
        batch_lbl = f"Daily Screen  —  {run_date.strftime('%b %-d, %Y')} {time_str}"

        # Always resolve to an absolute path so the PDF is written to the
        # correct output folder regardless of the process CWD.
        # AGENT_REPORTS_DIR  → per-user reports/ subdirectory (set by dashboard v2)
        # AGENT_OUTPUT_DIR   → per-user root directory (fallback)
        _out_dir = (os.environ.get("AGENT_REPORTS_DIR")
                    or os.environ.get("AGENT_OUTPUT_DIR")
                    or os.path.dirname(os.path.abspath(__file__)))
        os.makedirs(_out_dir, exist_ok=True)
        filepath = os.path.join(_out_dir, os.path.basename(filename))

        c = rl_canvas.Canvas(filepath, pagesize=letter)

        # ── Page 1: Summary cover (all 5 picks overview) ──────────────────
        self._bg(c)
        self._top_strip(c)
        self._summary_cover(c, stocks, run_date, date_str)
        self._footer(c, 1, batch_lbl)

        # ── Pages 2+: one dedicated detail page per stock ─────────────────
        for i, (metrics, evaluation) in enumerate(stocks):
            tc = self.TICKER_COLORS[i % len(self.TICKER_COLORS)]
            c.showPage()
            self._bg(c)
            self._top_strip(c)
            self._stock_detail_page(c, metrics, evaluation, tc)
            self._footer(c, i + 2, batch_lbl)

        # ── Last page: category leaders / disclaimer ───────────────────────
        c.showPage()
        self._bg(c)
        self._top_strip(c)
        self._category_page(c, stocks)
        self._footer(c, len(stocks) + 2, batch_lbl)

        c.save()
        print(f"\n  PDF saved: {filepath}")
        return filepath

    # ── Grade color helper ───────────────────────────────────────────────
    def _grade_color(self, grade: str):
        if grade == 'A':            return self.TXT_GREEN
        if grade in ('B+', 'B'):   return self.TXT_ORANGE
        if grade in ('C+', 'C'):   return self.TXT_YELLOW
        return self.TXT_RED

    # ── Page 1: Summary cover ────────────────────────────────────────────
    def _summary_cover(self, c, stocks, run_date, date_str):
        n = len(stocks)
        y = self.H - 38

        # Eyebrow
        c.setFont('Helvetica', 8)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, y, f"NYSE + NASDAQ  \u00b7  {date_str.upper()}  \u00b7  LIVE SCREEN")
        y -= 30

        # Title
        c.setFont('Helvetica-Bold', 34)
        c.setFillColor(self.TXT_WHITE)
        c.drawString(self.LM, y, "Intelligent Investor Screen")
        y -= 22

        # Subtitle
        if stocks:
            tickers  = '  \u00b7  '.join(m.symbol for m, _ in stocks)
            subtitle = (f"{tickers}  \u2014  Ben Graham \u00d7 Warren Buffett Framework "
                        f"applied to today's worst performers")
        else:
            subtitle = "Ben Graham \u00d7 Warren Buffett Framework \u2014 today's worst performers"
        y = self._wrap(c, subtitle, self.LM, y, self.CW, 'Helvetica', 10.5, self.TXT_GRAY, 1.45)
        y -= 16

        self._rule(c, y)
        y -= 14

        # Snapshot table
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, y, f"COMPARATIVE SNAPSHOT \u2014 ALL {n} PICKS")
        y -= 8
        if stocks:
            y = self._snapshot_table(c, stocks, self.LM, y, self.CW)
        y -= 20

        self._rule(c, y)
        y -= 14

        # Pick highlight boxes (one per stock)
        if stocks:
            box_w = self.CW / max(n, 1)
            box_h = 84
            for i, (m, ev) in enumerate(stocks):
                tc    = self.TICKER_COLORS[i % len(self.TICKER_COLORS)]
                bx    = self.LM + i * box_w
                grade = ev.get('grade', '?')
                score = ev.get('score', 0)

                c.setFillColor(self.BG_CARD)
                c.rect(bx, y - box_h, box_w - 3, box_h, fill=1, stroke=0)
                c.setStrokeColor(tc)
                c.setLineWidth(0.8)
                c.rect(bx, y - box_h, box_w - 3, box_h, fill=0, stroke=1)

                c.setFont('Helvetica-Bold', 7)
                c.setFillColor(self.TXT_GRAY)
                c.drawString(bx + 6, y - 12, f"#{i + 1}")

                c.setFont('Helvetica-Bold', 19)
                c.setFillColor(tc)
                c.drawString(bx + 6, y - 34, m.symbol)

                gw = c.stringWidth(m.symbol, 'Helvetica-Bold', 19)
                gc = self._grade_color(grade)
                c.setFont('Helvetica-Bold', 11)
                c.setFillColor(gc)
                c.drawString(bx + 6 + gw + 4, y - 30, grade)

                c.setFont('Helvetica', 7)
                c.setFillColor(self.TXT_GRAY)
                c.drawString(bx + 6, y - 47, f"{score:.1f}/13")

                chg_col = self.TXT_RED if m.price_change_percent < 0 else self.TXT_GREEN
                c.setFont('Helvetica-Bold', 8)
                c.setFillColor(chg_col)
                c.drawString(bx + 6, y - 59, f"{m.price_change_percent:+.2f}%")

                c.setFont('Helvetica', 6.5)
                c.setFillColor(self.TXT_GRAY)
                name = m.company_name[:17] if len(m.company_name) > 17 else m.company_name
                c.drawString(bx + 6, y - 72, name)

            y -= box_h + 16

        c.setFont('Helvetica-Oblique', 7.5)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, y,
                     "Full analysis for each pick begins on the following pages.")
        y -= 18

        self._rule(c, y)
        y -= 14

        # Scoring legend
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, y, "SCORING LEGEND:")
        y -= 11

        legend = [
            ("Graham Criteria (max 8 pts):",
             "P/E \u226415, CR \u22652.0, D/E \u22641.0, Rev >$1B, 10yr earnings, dividends, EPS growth, P\u00d7PB \u226422.5",
             self.TXT_CYAN),
            ("Buffett Criteria (max 5 pts):",
             "ROE \u226515%, Net Margin \u226510%, D/E \u22640.5, FCF > 0, FCF Yield \u22653%",
             self.TXT_ORANGE),
            ("Combined Score / 13:",
             "A \u226512  \u00b7  B+ \u226510.5  \u00b7  B \u22658  \u00b7  C+ \u22656.5  \u00b7  C \u22655  \u00b7  D \u22652.5  \u00b7  F < 2.5",
             self.TXT_WHITE),
        ]
        for label, desc, col in legend:
            c.setFont('Helvetica-Bold', 7)
            c.setFillColor(col)
            c.drawString(self.LM + 10, y, label)
            c.setFont('Helvetica', 7)
            c.setFillColor(self.TXT_GRAY)
            c.drawString(self.LM + 155, y, desc)
            y -= 12

    # ── Per-stock detail page ────────────────────────────────────────────
    def _stock_detail_page(self, c, m: StockMetrics, ev: Dict, tc):
        PAGE_TOP = self.H - 30   # 762

        # ── Compact stock header ──────────────────────────────────────────
        y       = PAGE_TOP
        hdr_h   = 54
        hdr_btm = y - hdr_h

        c.setFillColor(self.BG_CARD)
        c.rect(self.LM, hdr_btm, self.CW, hdr_h, fill=1, stroke=0)
        c.setStrokeColor(tc)
        c.setLineWidth(1.0)
        c.line(self.LM, hdr_btm, self.LM + self.CW, hdr_btm)

        # Large ticker
        c.setFont('Helvetica-Bold', 26)
        c.setFillColor(tc)
        c.drawString(self.LM + 8, y - 32, m.symbol)
        tw = c.stringWidth(m.symbol, 'Helvetica-Bold', 26)

        # Company name
        c.setFont('Helvetica', 13)
        c.setFillColor(self.TXT_WHITE)
        max_name_w = self.CW - tw - 80
        name_str = m.company_name
        while (c.stringWidth(name_str, 'Helvetica', 13) > max_name_w
               and len(name_str) > 6):
            name_str = name_str[:-4] + '...'
        c.drawString(self.LM + 8 + tw + 8, y - 26, name_str)

        # Sector (top right)
        c.setFont('Helvetica', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawRightString(self.LM + self.CW - 8, y - 12,
                          m.industry or m.sector or '')

        # Price + change
        grade = ev.get('grade', '?')
        score = ev.get('score', 0)
        cap   = f"~${m.market_cap/1e9:.1f}B" if m.market_cap else "N/A"

        c.setFont('Helvetica', 8.5)
        c.setFillColor(self.TXT_WHITE)
        price_str = f"${m.price:.2f}"
        c.drawString(self.LM + 8, y - 46, price_str)
        px = self.LM + 8 + c.stringWidth(price_str, 'Helvetica', 8.5)

        chg_col = self.TXT_RED if m.price_change_percent < 0 else self.TXT_GREEN
        c.setFont('Helvetica-Bold', 8.5)
        c.setFillColor(chg_col)
        chg_str = f"  {m.price_change_percent:+.2f}%"
        c.drawString(px, y - 46, chg_str)
        px += c.stringWidth(chg_str, 'Helvetica-Bold', 8.5)

        c.setFont('Helvetica', 8)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(px + 4, y - 46, f" \u00b7  Mkt Cap: {cap}")

        # Grade (top right of header)
        gc = self._grade_color(grade)
        c.setFont('Helvetica-Bold', 22)
        c.setFillColor(gc)
        c.drawRightString(self.LM + self.CW - 8, y - 38, grade)
        gw = c.stringWidth(grade, 'Helvetica-Bold', 22)
        g_score_str = f"{score:.1f}/13"
        c.setFont('Helvetica', 7.5)
        c.setFillColor(self.TXT_GRAY)
        c.drawRightString(self.LM + self.CW - 8 - gw - 4, y - 35, g_score_str)

        y = hdr_btm - 10   # start of two-column area

        # ── Two-column layout ─────────────────────────────────────────────
        left_x, left_w  = self.LM, 218
        right_x = self.LM + left_w + 14
        right_w = self.CW - left_w - 14

        self._left_col(c, m, ev, left_x, y, left_w)
        self._right_col(c, m, ev, tc, right_x, y, right_w)

    # ── Page primitives ─────────────────────────────────────────────────────
    def _bg(self, c):
        c.setFillColor(self.BG)
        c.rect(0, 0, self.W, self.H, fill=1, stroke=0)

    def _top_strip(self, c):
        strip_colors = [
            colors.HexColor('#e74c3c'),
            colors.HexColor('#f39c12'),
            colors.HexColor('#f1c40f'),
            colors.HexColor('#2ecc71'),
            colors.HexColor('#3498db'),
        ]
        seg_w = 40
        total_w = seg_w * len(strip_colors)
        sx = (self.W - total_w) / 2
        for i, col in enumerate(strip_colors):
            c.setFillColor(col)
            c.rect(sx + i * seg_w, self.H - 6, seg_w, 6, fill=1, stroke=0)

    def _footer(self, c, page_num: int, batch_lbl: str):
        c.setFont('Helvetica', 7.5)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, 22, batch_lbl)
        c.drawRightString(self.W - self.RM, 22, f"Page {page_num}")
        c.setStrokeColor(self.BDR_SUBTLE)
        c.setLineWidth(0.3)
        c.line(self.LM, 30, self.W - self.RM, 30)

    def _rule(self, c, y, alpha=1.0):
        c.setStrokeColor(self.BDR_SUBTLE)
        c.setLineWidth(0.4)
        c.line(self.LM, y, self.LM + self.CW, y)

    # ── Snapshot table ──────────────────────────────────────────────────────
    def _snapshot_table(self, c, stocks, x, y, w) -> float:
        # Column layout:  [Ticker | Drop | Fwd P/E | P/B | Curr.R. | D/E | ROE | Net Mgn | Div.Yld | Rating]
        TICKER_W  = 58       # fixed width for symbol column
        RATING_W  = 62       # fixed width for grade + score column
        metric_w  = (w - TICKER_W - RATING_W) / 8   # remaining 8 metric columns
        row_h     = 30
        hdr_h     = 22

        def col_x(col_index):
            """Left-edge x of a column by index (0=Ticker, 1-8=metrics, 9=Rating)."""
            if col_index == 0:
                return x
            if col_index == 9:
                return x + TICKER_W + 8 * metric_w
            return x + TICKER_W + (col_index - 1) * metric_w

        # Column headers — col 0=Ticker, cols 1-8=metrics, col 9=Rating
        headers = ['Ticker', 'Drop', 'Fwd P/E', 'P/B', 'Curr.R.',
                   'D/E', 'ROE', 'Net Mgn', 'Div.Yld', 'Rating']

        # ── Header row ──────────────────────────────────────────────────────
        c.setFillColor(self.BG_METRIC)
        c.rect(x, y - hdr_h, w, hdr_h, fill=1, stroke=0)
        for i, h in enumerate(headers):
            c.setFont('Helvetica-Bold', 7.5)
            c.setFillColor(self.TXT_GRAY)
            c.drawString(col_x(i) + 5, y - 14, h)
        y -= hdr_h

        # ── Data rows ───────────────────────────────────────────────────────
        def grade_color(grade: str):
            """Map grade letter to a palette color."""
            if grade == 'A':              return self.TXT_GREEN
            if grade in ('B+', 'B'):      return self.TXT_ORANGE
            if grade in ('C+', 'C'):      return self.TXT_YELLOW
            return self.TXT_RED           # D or F

        for j, (m, ev) in enumerate(stocks):
            bg = self.BG_CARD if j % 2 == 0 else self.BG_METRIC
            c.setFillColor(bg)
            c.rect(x, y - row_h, w, row_h, fill=1, stroke=0)

            tc    = self.TICKER_COLORS[j % len(self.TICKER_COLORS)]
            grade = ev.get('grade', '?')
            score = ev.get('score', 0)

            # ── Col 0: Ticker sticker ────────────────────────────────────
            # Symbol in stock's theme color
            c.setFont('Helvetica-Bold', 9.5)
            c.setFillColor(tc)
            c.drawString(col_x(0) + 5, y - 12, m.symbol)
            # Today's % change as small color-coded sub-label
            chg_col = self.TXT_RED if m.price_change_percent < 0 else self.TXT_GREEN
            c.setFont('Helvetica', 7)
            c.setFillColor(chg_col)
            c.drawString(col_x(0) + 5, y - 22, f"{m.price_change_percent:+.2f}%")

            # ── Cols 1-8: metrics ────────────────────────────────────────
            roe_pct = m.roe * 100        if m.roe        else None
            nm_pct  = m.net_margin * 100 if m.net_margin else None
            metric_vals = [
                f"{m.price_change_percent:+.2f}%",                          # Drop
                f"{m.forward_pe:.1f}"      if m.forward_pe            else 'N/A',  # Fwd P/E
                f"{m.pb_ratio:.2f}x"       if m.pb_ratio              else 'N/A',  # P/B
                f"{m.current_ratio:.2f}"   if m.current_ratio  is not None else 'N/A',  # Curr.R.
                f"{m.debt_to_equity:.2f}"  if m.debt_to_equity is not None else 'N/A',  # D/E
                f"{roe_pct:.1f}%"          if roe_pct                 else 'N/A',  # ROE
                f"{nm_pct:.1f}%"           if nm_pct                  else 'N/A',  # Net Mgn
                f"{m.dividend_yield:.2f}%" if m.dividend_yield        else 'N/A',  # Div.Yld
            ]
            for i, val in enumerate(metric_vals):
                c.setFont('Helvetica-Bold', 8.5)
                c.setFillColor(self.TXT_ORANGE)
                c.drawString(col_x(i + 1) + 5, y - 17, val)

            # ── Col 9: Rating ────────────────────────────────────────────
            gc = grade_color(grade)
            # Grade letter (large)
            c.setFont('Helvetica-Bold', 13)
            c.setFillColor(gc)
            c.drawString(col_x(9) + 6, y - 14, grade)
            gw = c.stringWidth(grade, 'Helvetica-Bold', 13)
            # Score x/8 (small, beside grade)
            c.setFont('Helvetica', 7.5)
            c.setFillColor(self.TXT_GRAY)
            c.drawString(col_x(9) + 6 + gw + 3, y - 13, f"{score:.1f}/13")
            # Mini progress bar at bottom of rating cell
            bar_y  = y - row_h + 3
            fill_w = max(4, (score / 8) * (RATING_W - 12))
            c.setFillColor(gc)
            c.rect(col_x(9) + 6, bar_y, fill_w, 3, fill=1, stroke=0)

            y -= row_h

        # Bottom rule
        c.setStrokeColor(self.BDR_SUBTLE)
        c.setLineWidth(0.3)
        c.line(x, y, x + w, y)
        return y

    # ── Stock header card ───────────────────────────────────────────────────
    def _stock_card(self, c, m: StockMetrics, ev: Dict, tc, verdict: str,
                    x, y, w) -> float:
        card_h = 112

        # Card background
        c.setFillColor(self.BG_CARD)
        c.rect(x, y - card_h, w, card_h, fill=1, stroke=0)
        c.setStrokeColor(self.BDR_SUBTLE)
        c.setLineWidth(0.4)
        c.rect(x, y - card_h, w, card_h, fill=0, stroke=1)

        # Large ticker
        c.setFont('Helvetica-Bold', 30)
        c.setFillColor(tc)
        c.drawString(x + 10, y - 36, m.symbol)
        ticker_w = c.stringWidth(m.symbol, 'Helvetica-Bold', 30)

        # Company name
        c.setFont('Helvetica', 14)
        c.setFillColor(self.TXT_WHITE)
        c.drawString(x + 10 + ticker_w + 6, y - 28, m.company_name)

        # Sector / industry (top right)
        sector_txt = f"{m.industry or m.sector or ''}"
        c.setFont('Helvetica', 7.5)
        c.setFillColor(self.TXT_GRAY)
        c.drawRightString(x + w - 10, y - 10, sector_txt)

        # Rule under header row
        c.setStrokeColor(self.BDR_SUBTLE)
        c.setLineWidth(0.3)
        c.line(x + 10, y - 44, x + w - 10, y - 44)

        # Price line
        cap = f"~${m.market_cap/1e9:.1f}B" if m.market_cap else "N/A"
        price_line = f"Price: ${m.price:.2f}  ·  Market Cap: {cap}  ·  Today: {m.price_change_percent:+.2f}%"
        c.setFont('Helvetica', 8.5)
        c.setFillColor(self.TXT_WHITE)
        c.drawString(x + 10, y - 56, price_line)

        # Graham verdict box
        vb_y = y - 62
        vb_h = 44
        c.setFillColor(colors.HexColor('#1a2033'))
        c.rect(x + 8, vb_y - vb_h, w - 16, vb_h, fill=1, stroke=0)

        # "GRAHAM VERDICT" label in red small caps
        c.setFont('Helvetica-Bold', 6.5)
        c.setFillColor(self.TXT_RED)
        c.drawString(x + 14, vb_y - 11, 'GRAHAM VERDICT')

        # Verdict italic text
        self._wrap(c, verdict, x + 14, vb_y - 22, w - 30,
                   'Helvetica-Oblique', 8, self.TXT_WHITE, 1.3)

        return y - card_h

    # ── Text box helper ─────────────────────────────────────────────────────
    def _text_box(self, c, text: str, x, y, w, font, size, text_color,
                  bg_color=None, lh_mult=1.3) -> float:
        """Draw background box + wrapped text. Returns y below the box."""
        n_lines = self._count_lines(c, text, w - 12, font, size, lh_mult)
        lh      = size * lh_mult
        box_h   = max(n_lines * lh + 14, 18)
        if bg_color:
            c.setFillColor(bg_color)
            c.rect(x, y - box_h, w, box_h, fill=1, stroke=0)
        self._wrap(c, text, x + 6, y - 6, w - 12, font, size, text_color, lh_mult)
        return y - box_h

    # ── Left column: metrics + checklist ───────────────────────────────────
    def _left_col(self, c, m: StockMetrics, ev: Dict, x, y, w) -> float:

        # Section label
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(x, y, 'QUANTITATIVE METRICS')
        y -= 8

        # Metrics grid (6 rows × 2 cells) — compact height to fit full page
        cell_w = (w - 4) / 2
        cell_h = 32
        gap    = 3

        roe_pct  = m.roe * 100 if m.roe else None
        nm_pct   = m.net_margin * 100 if m.net_margin else None
        pepb     = (m.pe_ratio * m.pb_ratio
                    if m.pe_ratio and m.pb_ratio and m.pe_ratio > 0 and m.pb_ratio > 0
                    else None)

        def fcf_str(v):
            if v is None: return 'N/A'
            if abs(v) >= 1e9: return f"${v/1e9:.2f}B"
            return f"${v/1e6:.0f}M"

        # Color helpers
        def pe_col(v):
            if v is None: return self.TXT_GRAY
            return self.TXT_GREEN if v <= 15 else (self.TXT_ORANGE if v <= 25 else self.TXT_RED)

        def cr_col(v):
            if v is None: return self.TXT_GRAY
            return self.TXT_GREEN if v >= 2.0 else (self.TXT_ORANGE if v >= 1.5 else self.TXT_RED)

        def de_col(v):
            if v is None: return self.TXT_GRAY
            return self.TXT_GREEN if v <= 0.5 else (self.TXT_ORANGE if v <= 1.0 else self.TXT_RED)

        def roe_col(v):
            if v is None: return self.TXT_GRAY
            return self.TXT_GREEN if v > 15 else self.TXT_ORANGE

        def pepb_col(v):
            if v is None: return self.TXT_GRAY
            return self.TXT_GREEN if v <= 22.5 else self.TXT_RED

        def fcf_col(v):
            if v is None: return self.TXT_GRAY
            return self.TXT_GREEN if v > 0 else self.TXT_RED

        metric_pairs = [
            ('TTM P/E',
             f"{m.pe_ratio:.2f}" if m.pe_ratio else 'N/A',
             pe_col(m.pe_ratio),
             'Forward P/E',
             f"{m.forward_pe:.2f}" if m.forward_pe else 'N/A',
             pe_col(m.forward_pe)),
            ('P/B Ratio',
             f"{m.pb_ratio:.2f}x" if m.pb_ratio else 'N/A',
             self.TXT_ORANGE,
             'PExPB (TTM)',
             f"{pepb:.1f}" if pepb else 'N/A',
             pepb_col(pepb)),
            ('Current Ratio',
             f"{m.current_ratio:.2f}" if m.current_ratio is not None else 'N/A',
             cr_col(m.current_ratio),
             'Debt / Equity',
             f"{m.debt_to_equity:.2f}" if m.debt_to_equity is not None else 'N/A',
             de_col(m.debt_to_equity)),
            ('ROE',
             f"{roe_pct:.2f}%" if roe_pct else 'N/A',
             roe_col(roe_pct),
             'Net Margin',
             f"{nm_pct:.2f}%" if nm_pct else 'N/A',
             self.TXT_ORANGE),
            ('Div. Yield',
             f"{m.dividend_yield:.2f}%" if m.dividend_yield else 'N/A',
             self.TXT_ORANGE,
             'Beta',
             f"{m.beta:.2f}" if m.beta else 'N/A',
             self.TXT_ORANGE),
            ('EPS (TTM)',
             f"${m.eps:.2f}" if m.eps else 'N/A',
             self.TXT_ORANGE,
             'Free Cash Flow',
             fcf_str(m.free_cash_flow),
             fcf_col(m.free_cash_flow)),
        ]

        for label1, val1, col1, label2, val2, col2 in metric_pairs:
            # Left cell
            cx1 = x
            c.setFillColor(self.BG_METRIC)
            c.rect(cx1, y - cell_h, cell_w, cell_h, fill=1, stroke=0)
            c.setStrokeColor(self.BDR_SUBTLE)
            c.setLineWidth(0.3)
            c.rect(cx1, y - cell_h, cell_w, cell_h, fill=0, stroke=1)
            c.setFont('Helvetica', 6.5)
            c.setFillColor(self.TXT_GRAY)
            c.drawString(cx1 + 5, y - 9, label1.upper())
            c.setFont('Helvetica-Bold', 14)
            c.setFillColor(col1)
            c.drawString(cx1 + 5, y - 26, val1)

            # Right cell
            cx2 = x + cell_w + gap
            c.setFillColor(self.BG_METRIC)
            c.rect(cx2, y - cell_h, cell_w, cell_h, fill=1, stroke=0)
            c.setStrokeColor(self.BDR_SUBTLE)
            c.setLineWidth(0.3)
            c.rect(cx2, y - cell_h, cell_w, cell_h, fill=0, stroke=1)
            c.setFont('Helvetica', 6.5)
            c.setFillColor(self.TXT_GRAY)
            c.drawString(cx2 + 5, y - 9, label2.upper())
            c.setFont('Helvetica-Bold', 14)
            c.setFillColor(col2)
            c.drawString(cx2 + 5, y - 26, val2)

            y -= cell_h + gap

        y -= 6

        # Dividend note — show verified history if available
        if m.hist_div_years and m.hist_div_years > 0:
            yld = f" ({m.dividend_yield:.2f}% yield)" if m.dividend_yield and m.dividend_yield > 0 else ""
            div_note = (f"Dividends paid {m.hist_div_years} year(s){yld} — verified via yfinance history."
                        + (" Meets 20yr Graham criterion." if m.hist_div_years >= 20
                           else " Below Graham's 20-yr dividend record threshold."))
        elif m.dividend_yield and m.dividend_yield > 0:
            div_note = f"Dividend yield: {m.dividend_yield:.2f}% — no multi-year history available from current sources."
        else:
            div_note = "No dividend currently paid — fails Graham's 20-year dividend record criterion."
        y = self._wrap(c, div_note, x, y, w,
                       'Helvetica-Oblique', 7, self.TXT_GRAY, 1.3)
        y -= 8

        # ── Graham Checklist ─────────────────────────────────────────────────
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(x, y, 'GRAHAM CHECKLIST  (8 criteria)')
        y -= 5

        for item in ev['checklist']:
            y = self._checklist_row(c, item, x, y, w)

        y -= 6

        # ── Buffett Checklist ────────────────────────────────────────────────
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_ORANGE)
        c.drawString(x, y, 'BUFFETT CHECKLIST  (5 criteria)')
        y -= 5

        for item in ev.get('buffett_checklist', []):
            y = self._checklist_row(c, item, x, y, w)

        return y

    def _checklist_row(self, c, item: Dict, x, y, w) -> float:
        row_h  = 26   # compact height to fit all 13 rows + metrics on one page
        status = item['status']

        # Background
        c.setFillColor(self.BG_CARD)
        c.rect(x, y - row_h, w, row_h, fill=1, stroke=0)

        # Status badge
        if status == 'PASS':
            badge_col = self.TXT_GREEN
            badge_txt = 'v PASS'
        elif status == 'FAIL':
            badge_col = self.TXT_RED
            badge_txt = 'x FAIL'
        else:
            badge_col = self.TXT_YELLOW
            badge_txt = '~ COND'

        badge_x = x + w - 46
        c.setFont('Helvetica-Bold', 7.5)
        c.setFillColor(badge_col)
        c.drawString(badge_x, y - 10, badge_txt)

        # Criterion text
        text_w = badge_x - x - 8
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_WHITE)
        criterion = item['criterion']
        if c.stringWidth(criterion, 'Helvetica-Bold', 7) > text_w:
            criterion = criterion[:40] + '...'
        c.drawString(x + 5, y - 10, criterion)

        # Detail (italic gray)
        c.setFont('Helvetica-Oblique', 6)
        c.setFillColor(self.TXT_GRAY)
        detail = item['detail']
        if c.stringWidth(detail, 'Helvetica-Oblique', 6) > text_w + 30:
            detail = detail[:60] + '...'
        c.drawString(x + 5, y - 20, detail)

        # Bottom border
        c.setStrokeColor(self.BDR_SUBTLE)
        c.setLineWidth(0.25)
        c.line(x, y - row_h, x + w, y - row_h)

        return y - row_h

    # ── Right column: thesis + why down + MOS + risks + grade ──────────────
    def _right_col(self, c, m: StockMetrics, ev: Dict, tc, x, y, w) -> float:

        def section_header(label, color, border_color):
            nonlocal y
            c.setFont('Helvetica-Bold', 8)
            c.setFillColor(color)
            c.drawString(x, y, label)
            c.setStrokeColor(border_color)
            c.setLineWidth(0.5)
            c.line(x, y - 4, x + w, y - 4)
            y -= 16

        # ── Investment Thesis ──────────────────────────────────────────────
        section_header('INVESTMENT THESIS  \u00b7  GRAHAM & BUFFETT LENS',
                       self.TXT_CYAN, self.BDR_CYAN)
        y = self._text_box(c, self._thesis_text(m, ev), x, y, w,
                           'Helvetica', 8, self.TXT_WHITE, self.BG_CARD, 1.3)
        y -= 11

        # ── Why Is This Stock Down Today? ─────────────────────────────────
        section_header('WHY IS THIS STOCK DOWN TODAY?',
                       self.TXT_YELLOW, self.TXT_YELLOW)
        y = self._text_box(c, self._why_down_text(m, ev), x, y, w,
                           'Helvetica', 8, self.TXT_WHITE, self.BG_CARD, 1.3)
        y -= 11

        # ── Margin of Safety ──────────────────────────────────────────────
        section_header('MARGIN OF SAFETY ANALYSIS',
                       self.TXT_GREEN, self.BDR_GREEN)
        y = self._text_box(c, self._mos_text(m, ev), x, y, w,
                           'Helvetica', 8, self.TXT_WHITE, self.BG_CARD, 1.3)
        y -= 11

        # ── Key Risks ─────────────────────────────────────────────────────
        section_header('KEY RISKS & GRAHAM DISQUALIFIERS',
                       self.TXT_RED, self.BDR_RED)
        y = self._text_box(c, self._risks_text(m, ev), x, y, w,
                           'Helvetica', 8, self.TXT_WHITE, self.BG_CARD, 1.3)
        y -= 13

        # ── Grade box ─────────────────────────────────────────────────────
        grade         = ev.get('grade', 'N/A')
        graham_score  = ev.get('graham_score', 0)
        buffett_score = ev.get('buffett_score', 0)
        total_score   = ev.get('score', 0)
        grade_box_h   = 52
        gc            = self._grade_color(grade)

        c.setFillColor(self.BG_CARD)
        c.rect(x, y - grade_box_h, w, grade_box_h, fill=1, stroke=0)
        c.setStrokeColor(self.BDR_ORANGE)
        c.setLineWidth(0.8)
        c.rect(x, y - grade_box_h, w, grade_box_h, fill=0, stroke=1)

        c.setFont('Helvetica-Bold', 26)
        c.setFillColor(gc)
        c.drawString(x + 10, y - 36, grade)
        gw = c.stringWidth(grade, 'Helvetica-Bold', 26)

        c.setFont('Helvetica', 9)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(x + 10 + gw + 8, y - 24, 'Intelligent Investor Grade')

        c.setFont('Helvetica', 8)
        c.setFillColor(self.TXT_ORANGE)
        score_str = (f"Graham: {graham_score:.1f}/8  \u00b7  "
                     f"Buffett: {buffett_score:.1f}/5  \u00b7  "
                     f"Total: {total_score:.1f}/13")
        c.drawString(x + 10 + gw + 8, y - 36, score_str)

        return y - grade_box_h

    # ── Why down today text ──────────────────────────────────────────────────
    def _why_down_text(self, m: StockMetrics, ev: Dict) -> str:
        pct   = abs(m.price_change_percent)
        parts = []

        # Severity framing
        if pct >= 20:
            parts.append(
                f"Down {pct:.1f}% — an extreme single-day drop suggesting a major "
                f"negative catalyst (earnings miss, guidance cut, or macro shock). ")
        elif pct >= 10:
            parts.append(
                f"Down {pct:.1f}% — significant decline, likely driven by "
                f"company-specific news or broad sector-wide selling pressure. ")
        elif pct >= 5:
            parts.append(
                f"Down {pct:.1f}% — a notable correction; may reflect profit-taking, "
                f"earnings concerns, or market rotation. ")
        else:
            parts.append(f"Down {pct:.1f}% today, part of today's broader market selloff. ")

        # Fundamental signals
        if m.pe_ratio and m.pe_ratio > 40:
            parts.append(
                f"High P/E of {m.pe_ratio:.0f}x leaves valuation "
                f"vulnerable to sentiment shifts and rate pressure. ")
        elif m.pe_ratio is not None and m.pe_ratio <= 0:
            parts.append(
                f"Negative trailing earnings signals operating losses, "
                f"amplifying investor concern. ")

        if m.net_margin is not None and m.net_margin < 0:
            parts.append(
                f"Negative net margin ({m.net_margin * 100:.1f}%) indicates unprofitable "
                f"operations at current scale. ")

        if m.debt_to_equity is not None and m.debt_to_equity > 2.0:
            parts.append(
                f"Heavy leverage (D/E {m.debt_to_equity:.2f}) amplifies rate sensitivity "
                f"and credit concerns. ")

        if m.free_cash_flow is not None and m.free_cash_flow < 0:
            fcf_s = (f"${abs(m.free_cash_flow)/1e9:.1f}B" if abs(m.free_cash_flow) >= 1e9
                     else f"${abs(m.free_cash_flow)/1e6:.0f}M")
            parts.append(
                f"Negative FCF of \u2212{fcf_s} raises concerns about cash burn. ")

        if m.beta is not None and m.beta > 1.5:
            parts.append(
                f"High beta ({m.beta:.2f}) amplifies market moves in risk-off sessions. ")

        if len(parts) == 1:
            parts.append(
                "No specific fundamental disqualifier identified from available data; "
                "the drop may reflect broader sector or macro pressure. ")

        parts.append("Verify against latest news and company filings before acting.")
        return ''.join(parts)

    # ── Category Leaders / Disclaimer page ─────────────────────────────────
    def _category_page(self, c, stocks, batch_label=''):
        y = self.H - 44

        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, y, 'CATEGORY LEADERS')
        y -= 10

        if not stocks:
            c.setFont('Helvetica', 10)
            c.setFillColor(self.TXT_WHITE)
            c.drawString(self.LM, y - 20, 'No stocks qualified today.')
            return

        # 5-column category boxes
        n        = len(stocks)
        box_w    = self.CW / max(n, 1)
        box_h    = 110
        cats     = ['Best Value Score', 'Best Balance Sheet', 'Best ROE',
                    'Best Dividend', 'Best P/E']

        for i, (m, ev) in enumerate(stocks):
            tc  = self.TICKER_COLORS[i % len(self.TICKER_COLORS)]
            bx  = self.LM + i * box_w
            cat = cats[i] if i < len(cats) else f"Pick #{i+1}"

            # Box background + border
            c.setFillColor(self.BG_CARD)
            c.rect(bx, y - box_h, box_w - 2, box_h, fill=1, stroke=0)
            c.setStrokeColor(self.BDR_SUBTLE)
            c.setLineWidth(0.4)
            c.rect(bx, y - box_h, box_w - 2, box_h, fill=0, stroke=1)

            # Category label
            c.setFont('Helvetica', 6.5)
            c.setFillColor(self.TXT_GRAY)
            c.drawString(bx + 6, y - 14, cat)

            # Ticker symbol
            c.setFont('Helvetica-Bold', 24)
            c.setFillColor(tc)
            c.drawString(bx + 6, y - 42, m.symbol)

            # Key facts
            roe_pct = m.roe * 100 if m.roe else None
            facts = []
            if m.current_ratio:
                facts.append(f"CR {m.current_ratio:.2f}")
            if m.debt_to_equity is not None:
                facts.append(f"D/E {m.debt_to_equity:.2f}")
            if m.forward_pe:
                facts.append(f"Fwd P/E {m.forward_pe:.1f}")
            if roe_pct:
                facts.append(f"ROE {roe_pct:.1f}%")
            if m.dividend_yield:
                facts.append(f"Div {m.dividend_yield:.2f}%")

            c.setFont('Helvetica', 7)
            c.setFillColor(self.TXT_WHITE)
            for j, fact in enumerate(facts[:4]):
                c.drawString(bx + 6, y - 58 - j * 12, fact)

        y -= box_h + 20

        # Disclaimer
        self._rule(c, y)
        y -= 12
        disclaimer = (
            "Disclaimer: This report is for educational and informational purposes only "
            "and does not constitute financial or investment advice. Data sourced from "
            "Yahoo Finance (yfinance) and Financial Modeling Prep. Ben Graham's strict "
            "PExPB <= 22.5 formula is rarely met in today's market. Consult a licensed "
            "financial advisor before investing."
        )
        c.setFont('Helvetica-Bold', 7)
        c.setFillColor(self.TXT_GRAY)
        c.drawString(self.LM, y, 'Disclaimer:')
        y -= 11
        self._wrap(c, disclaimer, self.LM, y, self.CW,
                   'Helvetica', 7, self.TXT_GRAY, 1.4)

    # ── Text generation ─────────────────────────────────────────────────────
    def _verdict(self, m: StockMetrics, ev: Dict) -> str:
        cl       = ev.get('checklist', [])
        passes   = sum(1 for c in cl if c['status'] == 'PASS')
        fails    = sum(1 for c in cl if c['status'] == 'FAIL')
        grade    = ev.get('grade', '?')
        roe_pct  = m.roe * 100 if m.roe else None
        pepb     = (m.pe_ratio * m.pb_ratio
                    if m.pe_ratio and m.pb_ratio and m.pe_ratio > 0 and m.pb_ratio > 0
                    else None)

        strengths = []
        if m.current_ratio and m.current_ratio >= 2.0:
            strengths.append(f"current ratio {m.current_ratio:.2f}")
        if m.debt_to_equity is not None and m.debt_to_equity <= 0.5:
            strengths.append(f"low D/E {m.debt_to_equity:.2f}")
        if m.pe_ratio and m.pe_ratio <= 15:
            strengths.append(f"P/E {m.pe_ratio:.1f}x")
        if m.forward_pe and m.forward_pe <= 15:
            strengths.append(f"forward P/E {m.forward_pe:.1f}x")
        if m.dividend_yield and m.dividend_yield > 1:
            strengths.append(f"{m.dividend_yield:.1f}% dividend yield")
        if roe_pct and roe_pct > 15:
            strengths.append(f"ROE {roe_pct:.1f}%")

        bcl    = ev.get('buffett_checklist', [])
        b_pass = sum(1 for c in bcl if c['status'] == 'PASS')
        txt = f"Passes {passes}/8 Graham + {b_pass}/5 Buffett criteria (Grade {grade})."
        if strengths:
            txt += f" Strengths: {'; '.join(strengths[:3])}."
        if pepb and pepb > 22.5:
            txt += f" PExPB of {pepb:.0f} fails Graham's 22.5 formula."
        elif pepb and pepb <= 22.5:
            txt += f" PExPB of {pepb:.1f} passes Graham's formula."
        if fails >= 4:
            txt += " Primarily a quality/momentum thesis, not classic Graham value."
        return txt

    def _thesis_text(self, m: StockMetrics, ev: Dict) -> str:
        sector = m.sector or 'diversified'
        rev    = f"${m.revenue/1e9:.1f}B" if m.revenue else "undisclosed revenue"
        cap    = f"${m.market_cap/1e9:.1f}B" if m.market_cap else "undisclosed"
        roe    = m.roe * 100 if m.roe else None
        score  = ev.get('score', 0)
        mx     = ev.get('max_score', 8)

        parts = [f"{m.company_name} ({m.symbol}) operates in the {sector} sector, "
                 f"generating {rev} in annual revenue with a {cap} market cap. "]

        parts.append(f"Down {abs(m.price_change_percent):.2f}% today at ${m.price:.2f}. ")

        if m.pe_ratio:
            direction = "below" if m.pe_ratio <= 15 else "above"
            parts.append(f"Trailing P/E of {m.pe_ratio:.1f}x sits {direction} Graham's 15x threshold. ")
        if m.forward_pe:
            parts.append(f"Forward P/E of {m.forward_pe:.1f}x. ")
        if roe:
            quality = "exceeds" if roe > 15 else "falls short of"
            parts.append(f"ROE of {roe:.1f}% {quality} Graham's 15% benchmark. ")
        if m.current_ratio:
            liq = "solid" if m.current_ratio >= 2.0 else ("adequate" if m.current_ratio >= 1.5 else "tight")
            parts.append(f"Current ratio of {m.current_ratio:.2f} indicates {liq} liquidity. ")
        if m.free_cash_flow and m.free_cash_flow > 0:
            fcf = f"${m.free_cash_flow/1e9:.2f}B" if m.free_cash_flow >= 1e9 else f"${m.free_cash_flow/1e6:.0f}M"
            parts.append(f"Positive FCF of {fcf} confirms cash-generation capability. ")

        g = ev.get('graham_score', score)
        b = ev.get('buffett_score', 0)
        parts.append(f"Graham-Buffett score: {g:.1f}/8 + {b:.1f}/5 = {score:.1f}/13.")
        return ''.join(parts)

    def _mos_text(self, m: StockMetrics, ev: Dict) -> str:
        parts = [f"At ${m.price:.2f}, {m.symbol} is down {abs(m.price_change_percent):.2f}% today. "]

        if m.pe_ratio:
            if m.pe_ratio < 15:
                parts.append(f"P/E of {m.pe_ratio:.1f}x is below Graham's ceiling — a potential value signal. ")
            else:
                parts.append(f"P/E of {m.pe_ratio:.1f}x exceeds Graham's 15x threshold, limiting margin of safety. ")

        if m.forward_pe and m.forward_pe < m.pe_ratio if m.pe_ratio else False:
            parts.append(f"Forward P/E of {m.forward_pe:.1f}x suggests earnings growth expected. ")

        if m.free_cash_flow and m.market_cap and m.market_cap > 0:
            fcf_yield = (m.free_cash_flow / m.market_cap) * 100
            parts.append(f"FCF yield of {fcf_yield:.1f}% provides a real income floor. ")

        if m.dividend_yield and m.dividend_yield > 0:
            parts.append(f"Dividend yield of {m.dividend_yield:.2f}% provides income while awaiting value realization. ")

        if m.book_value_per_share and m.price > 0:
            parts.append(f"Book value per share: ${m.book_value_per_share:.2f}. ")

        if len(parts) == 1:
            parts.append("Insufficient data for a full margin-of-safety calculation — recommend further due diligence.")

        return ''.join(parts)

    def _risks_text(self, m: StockMetrics, ev: Dict) -> str:
        cl     = ev.get('checklist', [])
        fails  = [item for item in cl if item['status'] == 'FAIL']
        roe    = m.roe * 100 if m.roe else None
        pepb   = (m.pe_ratio * m.pb_ratio
                  if m.pe_ratio and m.pb_ratio and m.pe_ratio > 0 and m.pb_ratio > 0
                  else None)

        parts = []
        if fails:
            fail_names = '; '.join(f['criterion'] for f in fails[:3])
            parts.append(f"Graham fails: {fail_names}. ")
        if pepb and pepb > 22.5:
            parts.append(f"PExPB of {pepb:.0f} significantly exceeds the 22.5 Graham formula ceiling. ")
        if m.current_ratio and m.current_ratio < 1.5:
            parts.append(f"Current ratio of {m.current_ratio:.2f} signals liquidity risk. ")
        if m.debt_to_equity and m.debt_to_equity > 1.0:
            parts.append(f"D/E of {m.debt_to_equity:.2f} exceeds Graham's 1.0 maximum. ")
        if not m.dividend_yield or m.dividend_yield == 0:
            parts.append("No dividend — fails Graham's income criterion. ")
        if m.beta and m.beta > 1.3:
            parts.append(f"High beta ({m.beta:.2f}) indicates above-market volatility risk. ")
        if not parts:
            parts.append("No major Graham disqualifiers beyond valuation. Standard market and sector risks apply. ")

        parts.append("Always conduct additional due diligence before investing.")
        return ''.join(parts)

    # ── Text utilities ──────────────────────────────────────────────────────
    def _wrap(self, c, text: str, x, y, max_w,
              font, size, color, lh_mult=1.35) -> float:
        c.setFont(font, size)
        c.setFillColor(color)
        lh = size * lh_mult
        words = text.replace('\n', ' ').split()
        lines, cur, cur_w = [], [], 0
        for word in words:
            ww = c.stringWidth(word + ' ', font, size)
            if cur_w + ww > max_w and cur:
                lines.append(' '.join(cur))
                cur, cur_w = [word], ww
            else:
                cur.append(word)
                cur_w += ww
        if cur:
            lines.append(' '.join(cur))
        for i, line in enumerate(lines):
            c.drawString(x, y - i * lh, line)
        return y - len(lines) * lh

    def _count_lines(self, c, text: str, max_w, font, size, lh_mult=1.35) -> int:
        c.setFont(font, size)
        words = text.replace('\n', ' ').split()
        lines, cur_w = 1, 0
        for word in words:
            ww = c.stringWidth(word + ' ', font, size)
            if cur_w + ww > max_w and cur_w > 0:
                lines += 1
                cur_w = ww
            else:
                cur_w += ww
        return lines


# ── EMAIL ───────────────────────────────────────────────────────────────────────
def send_email_confirmation(
    stocks: List[Tuple],
    pdf_path: str,
    run_date: datetime = None,
    *,
    email_to: str = "",
    period: str = "daily",
    universe_size: int = 0,
    losers_count: int = 0,
    markets_str: str = "NYSE + NASDAQ",
) -> bool:
    if not EMAIL_APP_PWD or not EMAIL_FROM:
        print("\n  [Email] Skipped — GMAIL_FROM / GMAIL_APP_PASSWORD not set in .env.")
        return False
    if not email_to:
        print("\n  [Email] Skipped — no recipient email configured in settings.")
        return False

    if run_date is None:
        run_date = datetime.now(_TZ_EST)

    date_str = run_date.strftime('%B %-d, %Y')
    time_str = run_date.strftime('%H:%M:%S')
    date_time_str = f"{date_str} {time_str}"
    n = len(stocks)

    # Human-readable period label
    period_labels = {
        "daily100":  "today's worst performers — top 100 by market cap",
        "daily500":  "today's worst performers — top 500 by market cap",
        "weekly100": "5-day worst performers — top 100 by market cap",
        "weekly500": "5-day worst performers — top 500 by market cap",
        "yearly100": "52-week worst performers — top 100 by market cap",
        "yearly500": "52-week worst performers — top 500 by market cap",
        "value100":  "all best-value stocks — top 100 by market cap",
        "value500":  "all best-value stocks — top 500 by market cap",
    }
    period_label = period_labels.get(period, period)

    # Build email body
    lines = [
        f"Intelligent Investor Screen — {date_time_str}",
        f"{'=' * 55}",
        f"",
        f"Agent run completed at {run_date.strftime('%H:%M:%S')}.",
        f"Screening period  : {period_label}",
        f"Markets screened  : {markets_str}",
        f"Universe screened : {universe_size:,} stocks",
        f"Losers identified : {losers_count:,} stocks",
        f"Top picks selected: {n}",
        f"",
    ]
    if stocks:
        lines.append("TOP PICKS:")
        for i, (m, ev) in enumerate(stocks, 1):
            roe = f"{m.roe*100:.1f}%" if m.roe else 'N/A'
            lines.append(
                f"  {i}. {m.symbol} ({m.company_name}) | "
                f"Score {ev['score']:.1f}/13 | Grade {ev['grade']} | "
                f"Price ${m.price:.2f} | Change {m.price_change_percent:+.2f}% | ROE {roe}"
            )
    else:
        lines.append("No stocks met the minimum threshold today.")

    lines += [
        "",
        "The full PDF report is attached.",
        "",
        "-- Intelligent Investor Agent (automated)",
    ]

    body = '\n'.join(lines)

    # Flush before the SMTP call so the log always shows we reached this point
    sys.stdout.flush()

    try:
        msg = MIMEMultipart()
        msg['From']    = EMAIL_FROM
        msg['To']      = email_to
        msg['Subject'] = f"Intelligent Investor Screen — {date_time_str} — {n} picks"
        msg.attach(MIMEText(body, 'plain'))

        # Attach PDF if it exists
        if pdf_path and os.path.exists(pdf_path):
            print(f"  Attaching PDF: {os.path.basename(pdf_path)}")
            sys.stdout.flush()
            with open(pdf_path, 'rb') as fp:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(fp.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition',
                            f'attachment; filename="{os.path.basename(pdf_path)}"')
            msg.attach(part)
        elif pdf_path:
            print(f"  [Email] Warning: PDF not found at {pdf_path} — sending without attachment")
            sys.stdout.flush()

        # Read SMTP settings from env — matches auth.py behaviour so any custom
        # SMTP server (e.g. Namecheap Private Email on port 587) is honoured.
        smtp_server = os.environ.get("GMAIL_SMTP_SERVER", "smtp.gmail.com")
        smtp_port   = int(os.environ.get("GMAIL_SMTP_PORT", "465"))
        print(f"  Connecting to {smtp_server}:{smtp_port} …")
        sys.stdout.flush()
        # timeout=30 prevents indefinite blocking if the network is stale/unavailable
        if smtp_port == 465:
            server_ctx = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            server_ctx = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server_ctx.ehlo()
            server_ctx.starttls()
            server_ctx.ehlo()
        with server_ctx as server:
            server.login(EMAIL_FROM, EMAIL_APP_PWD)
            server.send_message(msg)

        print(f"\n  Email sent to {email_to}")
        sys.stdout.flush()
        return True

    except Exception as e:
        import traceback
        print(f"\n  [Email] Failed: {e}")
        print(f"  [Email] Traceback:\n{traceback.format_exc()}")
        sys.stdout.flush()
        return False


# ── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    run_date = datetime.now(_TZ_EST)

    # ── Load dashboard config ─────────────────────────────────────────────────
    cfg             = _load_agent_config()
    email_enabled   = cfg.get("email_enabled",   True)
    pdf_enabled     = cfg.get("pdf_enabled",     True)
    enabled_markets = cfg.get("markets",         ["NYSE", "NASDAQ"])
    # Handle empty markets list — treat as None and use default
    if not enabled_markets:
        enabled_markets = ["NYSE", "NASDAQ"]
    loser_period    = cfg.get("loser_period",    "daily100")
    stock_geography = cfg.get("stock_geography", "all")   # "all"|"usa"|"international"

    _period_labels  = {
        "daily100":  "today's",   "daily500":  "today's",   "dailyall":  "today's",
        "weekly100": "5-day",     "weekly500": "5-day",     "weeklyall": "5-day",
        "yearly100": "52-week",   "yearly500": "52-week",   "yearlyall": "52-week",
        "value100":  "all-market value (top 100)",
        "value500":  "all-market value (top 500)",
    }
    period_label    = _period_labels.get(loser_period, loser_period)
    markets_str     = " + ".join(enabled_markets)
    _geo_labels     = {"all": "All stocks (USA + International)", "usa": "USA only",
                       "international": "International only (non-USA)"}
    geo_label       = _geo_labels.get(stock_geography, stock_geography)

    _criteria_labels = {
        "daily100":  "Daily Losers — Top 100 by market cap",
        "daily500":  "Daily Losers — Top 500 by market cap",
        "dailyall":  "Daily Losers — All (full universe)",
        "weekly100": "Weekly Losers — Top 100 by market cap",
        "weekly500": "Weekly Losers — Top 500 by market cap",
        "weeklyall": "Weekly Losers — All (full universe)",
        "yearly100": "52-Week Losers — Top 100 by market cap",
        "yearly500": "52-Week Losers — Top 500 by market cap",
        "yearlyall": "52-Week Losers — All (full universe)",
        "value100":  "All Best Value Stocks — Top 100 by market cap",
        "value500":  "All Best Value Stocks — Top 500 by market cap",
    }
    criteria_label = _criteria_labels.get(loser_period, loser_period)

    _is_value_mode  = loser_period in ("value100", "value500")
    _is_loser_mode  = not _is_value_mode

    print("=" * 70)
    print("INTELLIGENT INVESTOR AGENT — DAILY SCREEN")
    print("Graham x Buffett Framework")
    print(f"Run date    : {run_date.strftime('%B %-d, %Y  %H:%M:%S')} EST")
    print(f"Markets     : {markets_str}")
    print(f"Criteria    : {criteria_label}")
    print(f"Geography   : {geo_label}")
    print(f"PDF report  : {'enabled' if pdf_enabled else 'DISABLED'}")
    print(f"Email       : {'enabled' if email_enabled else 'DISABLED'}")
    print("=" * 70)

    API_KEY    = "0WKipAzrA4SUELMLzImM8EkYuyE3jYgB"
    fmp_client = FMPClient(API_KEY)
    analyzer   = ValueInvestingAnalyzer(fmp_client)

    # Step 1: Screen selected exchanges per configured criteria
    if _is_value_mode:
        print(f"\nStep 1: Scanning {markets_str} universe for best-value stocks (Graham/Buffett)…")
    else:
        print(f"\nStep 1: Screening {markets_str} universe for {period_label} worst performers…")

    losers, universe_size, losers_count = fmp_client.get_stock_losers(
        enabled_exchanges=enabled_markets,
        period=loser_period,
    )
    print(f"  Universe screened : {universe_size:,} stocks  ({markets_str} equity)")

    _is_all_mode   = loser_period.endswith("all")
    if _is_all_mode:
        ANALYZE_TOP_N = len(losers)
    elif loser_period.endswith("500"):
        ANALYZE_TOP_N = 500
    else:
        ANALYZE_TOP_N = 100
    if _is_value_mode:
        print(f"  Eligible candidates (mktCap > $300M): {losers_count:,} stocks")
        print(f"  Scoring top        : {ANALYZE_TOP_N} by market cap")
    elif _is_all_mode:
        print(f"  Losers (>$300M mktCap) : {losers_count:,} stocks")
        print(f"  Scoring ALL            : {ANALYZE_TOP_N} stocks")
    else:
        print(f"  Losers (>$300M mktCap) : {losers_count:,} stocks")
        print(f"  Scoring top            : {ANALYZE_TOP_N} by market cap")

    if not losers:
        print("No data available. Exiting.")
        return

    # ── Geography helper (needed inside scoring loop) ─────────────────────────
    def _is_usa(c: str) -> bool:
        return c.strip().lower() in ('united states', 'usa', 'us')

    # ── Step 2: Fetch metrics and score candidates ────────────────────────────
    # When a geography filter is active we keep iterating past ANALYZE_TOP_N
    # candidates until we have N *qualifying* stocks (so "Top 100 USA" really
    # yields 100 US stocks, not 71 after discarding foreign names).
    _geo_label = {"usa": "USA only", "international": "International only"}.get(stock_geography, "")
    step2_label = f"top {min(ANALYZE_TOP_N, len(losers))} by market cap"
    if _geo_label:
        step2_label += f" · geo-filter: {_geo_label}"
    print(f"\nStep 2: Fetching fundamentals + Graham-Buffett scoring ({step2_label})…")
    scored = []
    geo_skipped = 0
    stock_iter = iter(losers)
    while len(scored) < ANALYZE_TOP_N:
        try:
            stock = next(stock_iter)
        except StopIteration:
            break
        symbol = stock.get('symbol')
        if not symbol:
            continue
        m = analyzer.fetch_stock_metrics(symbol)
        if not m:
            continue
        # Apply geography filter immediately — only count qualifying stocks
        # towards ANALYZE_TOP_N so the user always gets the requested depth.
        if stock_geography == "usa" and not _is_usa(m.country):
            geo_skipped += 1
            print(f"    {symbol:<8} [geo-skip: non-US — {m.country or 'unknown'}]")
            continue
        if stock_geography == "international" and (not m.country or _is_usa(m.country)):
            geo_skipped += 1
            print(f"    {symbol:<8} [geo-skip: US-domiciled]")
            continue
        ev = analyzer.score_stock(m)
        scored.append((m, ev))
        grade = ev['grade']
        pe_str = f"{m.pe_ratio:.1f}" if m.pe_ratio else 'N/A'
        print(f"    {symbol:<8} Score {ev['score']:.1f}/13 (G:{ev['graham_score']:.1f} B:{ev['buffett_score']:.1f})  Grade {grade}  "
              f"P/E {pe_str}  Change {m.price_change_percent:+.1f}%")
        time.sleep(0.3)

    if geo_skipped:
        print(f"\n  Geography filter ({_geo_label}): skipped {geo_skipped} non-qualifying stocks "
              f"while building {len(scored)} qualifying results")

    # Step 3: Pick top 5 by score
    scored.sort(key=lambda x: x[1]['score'], reverse=True)

    top5 = scored[:5]

    print(f"\nStep 3: Top {len(top5)} picks selected")
    print("-" * 70)
    for i, (m, ev) in enumerate(top5, 1):
        roe = f"{m.roe*100:.1f}%" if m.roe else 'N/A'
        print(f"  {i}. {m.symbol:<6} {m.company_name[:30]:<30} "
              f"Score {ev['score']:.1f}/13  Grade {ev['grade']}  ROE {roe}")
    # Machine-readable snapshot lines for the dashboard parser
    for i, (m, ev) in enumerate(top5, 1):
        fpe_v  = f"{m.forward_pe:.1f}"     if m.forward_pe      is not None else 'N/A'
        pb_v   = f"{m.pb_ratio:.2f}"       if m.pb_ratio        is not None else 'N/A'
        cr_v   = f"{m.current_ratio:.2f}"  if m.current_ratio   is not None else 'N/A'
        de_v   = f"{m.debt_to_equity:.2f}" if m.debt_to_equity  is not None else 'N/A'
        roe_v  = f"{m.roe*100:.1f}"        if m.roe             is not None else 'N/A'
        nm_v   = f"{m.net_margin*100:.1f}" if m.net_margin      is not None else 'N/A'
        div_v  = f"{m.dividend_yield:.2f}" if m.dividend_yield  is not None else 'N/A'
        safe_nm = m.company_name.replace('|', '-')[:35]
        print(
            f"  SNAP|{i}|{m.symbol}|{safe_nm}|{m.price_change_percent:.2f}"
            f"|{fpe_v}|{pb_v}|{cr_v}|{de_v}|{roe_v}|{nm_v}|{div_v}"
            f"|{ev['grade']}|{ev['score']:.1f}"
        )

    # ── Save rich per-pick detail JSON for the dashboard slide-over panel ────
    # AGENT_OUTPUT_DIR redirects output to a per-user directory when set by the dashboard.
    _out_base = os.environ.get("AGENT_OUTPUT_DIR") or os.path.dirname(os.path.abspath(__file__))
    os.makedirs(_out_base, exist_ok=True)
    _detail_path = os.path.join(_out_base, "picks_detail.json")
    _detail_picks = []
    for _i, (_m, _ev) in enumerate(top5, 1):
        _detail_picks.append({
            "rank":                 _i,
            "symbol":               _m.symbol,
            "name":                 _m.company_name,
            "sector":               _m.sector,
            "industry":             _m.industry,
            "country":              _m.country,
            "price_change_pct":     _m.price_change_percent,
            "pe_ratio":             _m.pe_ratio,
            "forward_pe":           _m.forward_pe if hasattr(_m, 'forward_pe') else None,
            "pb_ratio":             _m.pb_ratio,
            "debt_to_equity":       _m.debt_to_equity,
            "current_ratio":        _m.current_ratio,
            "roe":                  _m.roe,
            "net_margin":           _m.net_margin if hasattr(_m, 'net_margin') else None,
            "dividend_yield":       _m.dividend_yield,
            "market_cap":           _m.market_cap,
            "revenue":              _m.revenue,
            "net_income":           _m.net_income,
            "free_cash_flow":       _m.free_cash_flow,
            "eps":                  _m.eps,
            "book_value_per_share": _m.book_value_per_share,
            "beta":                 _m.beta,
            "hist_profitable_years": _m.hist_profitable_years,
            "hist_total_years":     _m.hist_total_years,
            "hist_div_years":       _m.hist_div_years,
            "hist_eps_growth_pct":  _m.hist_eps_growth_pct if hasattr(_m, 'hist_eps_growth_pct') else None,
            "score":                _ev["score"],
            "graham_score":         _ev["graham_score"],
            "buffett_score":        _ev["buffett_score"],
            "max_score":            _ev["max_score"],
            "grade":                _ev["grade"],
            "checklist":            _ev["checklist"],
            "buffett_checklist":    _ev.get("buffett_checklist", []),
        })
    try:
        with open(_detail_path, "w") as _f:
            json.dump({
                "picks":          _detail_picks,
                "run_date":       run_date.isoformat(),
                "period_label":   criteria_label,
                "markets":        markets_str,
                "universe_size":  universe_size,
            }, _f, indent=2, default=str)
    except Exception as _e:
        print(f"  [detail JSON] write failed: {_e}")

    # ── Step 3b: Deep-dive analysis for each pick (Claude API if available, otherwise enhanced fallback) ──
    _ai_key = CLAUDE_API_KEY or _load_agent_config().get("claude_api_key", "")
    if _ai_key:
        # Ensure _generate_ai_analysis uses the key from config if env var missing
        globals()["CLAUDE_API_KEY"] = _ai_key
        print(f"\nStep 3b: Generating Claude AI deep-dive analysis (API key detected, {len(_ai_key)} chars)...")
    else:
        print("\nStep 3b: Generating enhanced fallback analysis (no Claude API key — set ANTHROPIC_API_KEY in .env for AI-powered analysis)...")
    def _save_detail_json():
        try:
            with open(_detail_path, "w") as _f:
                json.dump({
                    "picks":         _detail_picks,
                    "run_date":      run_date.isoformat(),
                    "period_label":  criteria_label,
                    "markets":       markets_str,
                    "universe_size": universe_size,
                }, _f, indent=2, default=str)
            return True
        except Exception as _e:
            print(f"  [detail JSON] save failed: {_e}")
            return False

    for _pick in _detail_picks:
        print(f"  Analyzing {_pick['symbol']}…")
        _pick["ai_analysis"] = _generate_ai_analysis(_pick)
        # Safety net: if analysis somehow returned empty sections, fill them from enhanced fallback
        _ai = _pick["ai_analysis"] or {}
        if (not _ai.get("business_context")
            or not _ai.get("value_trap_flags")
            or not _ai.get("key_signals")
            or not _ai.get("disqualifiers")
            or not _ai.get("recommendation_narrative")):
            _fb = _generate_enhanced_fallback_analysis(_pick)
            for _k, _v in _fb.items():
                if not _ai.get(_k):
                    _ai[_k] = _v
            _pick["ai_analysis"] = _ai
        # Save incrementally so interrupted runs preserve already-analyzed picks
        _save_detail_json()
        if _ai_key:
            time.sleep(0.3)
    print(f"  [detail JSON] all picks saved with analysis")

    # Step 4: Generate PDF (if enabled)
    _out_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = None
    if pdf_enabled:
        print("\nStep 4: Generating Intelligent Investor PDF report...")
        gen      = IntelligentInvestorPDFGenerator()
        # Capture the timestamp at PDF creation time (not at run start) so the
        # filename reflects when the report was actually written to disk.
        # Format: "intelligent_investor_20260424_155600_123.pdf" — YYYYMMDD_HHMMSS
        # plus 3-digit milliseconds to guarantee uniqueness even when two runs
        # complete within the same second (multi-user, rapid retries, etc.).
        # The PDF body still uses `run_date` for the displayed report date so
        # the user sees the date the screener actually ran against market data.
        now = datetime.now(_TZ_EST)
        timestamp = now.strftime('%Y%m%d_%H%M%S') + f"_{now.microsecond // 1000:03d}"
        filename = f"intelligent_investor_{timestamp}.pdf"
        # Defensive guard: if a file with this name somehow exists already,
        # append an incrementing suffix rather than silently overwriting.
        _out_dir_check = (os.environ.get("AGENT_REPORTS_DIR")
                          or os.environ.get("AGENT_OUTPUT_DIR")
                          or os.path.dirname(os.path.abspath(__file__)))
        _candidate = os.path.join(_out_dir_check, filename)
        _suffix = 1
        while os.path.exists(_candidate):
            filename = f"intelligent_investor_{timestamp}_{_suffix}.pdf"
            _candidate = os.path.join(_out_dir_check, filename)
            _suffix += 1
        filepath = gen.generate_report(top5, filename, run_date)
    else:
        print("\nStep 4: PDF generation skipped (disabled in Agent Configuration)")
        filename = "N/A"

    # Step 5: Send email (if enabled)
    if email_enabled:
        print("\nStep 5: Sending email confirmation...")
        email_to = cfg.get("email_address", "")
        print(f"  Email recipient: {email_to if email_to else '(NOT CONFIGURED)'}")
        print(f"  PDF file: {filepath if filepath and os.path.exists(filepath) else 'Not found'}")
        if email_to:
            result = send_email_confirmation(
                top5, filepath or "", run_date,
                email_to=email_to,
                period=loser_period,
                universe_size=universe_size,
                losers_count=losers_count,
                markets_str=markets_str,
            )
            if result:
                print(f"  Email delivery: SUCCESS")
            else:
                print(f"  Email delivery: FAILED - check logs above")
        else:
            print("  Email skipped - no recipient email configured")
    else:
        print("\nStep 5: Email skipped (disabled in Agent Configuration)")

    _candidates_label = {
        "daily100":  "Down today",   "daily500":  "Down today",   "dailyall":  "Down today",
        "weekly100": "Down 5-day",   "weekly500": "Down 5-day",   "weeklyall": "Down 5-day",
        "yearly100": "Down 52-wk",   "yearly500": "Down 52-wk",   "yearlyall": "Down 52-wk",
        "value100":  "Eligible",     "value500":  "Eligible",
    }.get(loser_period, "Candidates")

    print("\n" + "=" * 70)
    print("COMPLETE")
    print(f"  Universe screened : {universe_size:,} stocks ({markets_str})")
    print(f"  {_candidates_label:<24}: {losers_count:,} stocks")
    print(f"  Criteria          : {criteria_label}")
    print(f"  Stocks analyzed   : {len(scored)}")
    print(f"  Top picks         : {len(top5)}")
    print(f"  Report            : {filepath or 'N/A'}")
    print(f"  Output folder     : {_out_dir}")
    print("=" * 70)


if __name__ == "__main__":
    import os, sys
    # ── Self-contained dated logging (works whether run via launchd or terminal) ──
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(_script_dir)                           # always run from the scripts dir
    _log_dir  = os.path.join(_script_dir, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_path = os.path.join(_log_dir, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
    _log_fh   = open(_log_path, "a", buffering=1)   # line-buffered
    _tee_out  = sys.stdout
    _tee_err  = sys.stderr

    class _Tee:
        def __init__(self, *streams): self._s = streams
        def write(self, d):
            for s in self._s:
                s.write(d)
                s.flush()   # flush immediately so logs appear in real-time
        def flush(self):
            for s in self._s: s.flush()

    sys.stdout = _Tee(_tee_out, _log_fh)
    sys.stderr = _Tee(_tee_err, _log_fh)
    print(f"\n{'='*40}")
    print(f"Run started: {datetime.now(_TZ_EST).strftime('%a %b %-d %H:%M:%S %Y')} EST")
    print(f"{'='*40}")
    try:
        main()
    finally:
        print(f"Run finished: {datetime.now(_TZ_EST).strftime('%a %b %-d %H:%M:%S %Y')} EST")
        _log_fh.close()
        sys.stdout = _tee_out
        sys.stderr = _tee_err
