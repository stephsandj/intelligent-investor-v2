#!/usr/bin/env python3
"""
Intelligent Investor Agent — Web Dashboard
Run:    python3 dashboard.py
Access: http://localhost:5050
"""

import os
import sys
import glob
import re
import json
import signal
import subprocess
import threading
from datetime import datetime, timedelta

# ── Auto-install Flask if missing ──────────────────────────────────────────────
try:
    from flask import Flask, jsonify, render_template_string, send_file, request
except ImportError:
    print("Installing Flask…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask", "--user", "-q"])
    from flask import Flask, jsonify, render_template_string, send_file, request  # type: ignore

# ── Configuration ──────────────────────────────────────────────────────────────
AGENT_DIR   = os.path.dirname(os.path.abspath(__file__))
LOG_DIR     = os.path.join(AGENT_DIR, "logs")
CONFIG_FILE = os.path.join(AGENT_DIR, "config.json")
PYTHON_BIN  = sys.executable
PORT        = 5050

_DEFAULT_CONFIG = {
    "email_enabled":   True,
    "pdf_enabled":     True,
    "markets":         ["NYSE", "NASDAQ"],
    "loser_period":    "daily100",
    "stock_geography": "all",   # "all" | "usa" | "international"
}

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# ── Run state ─────────────────────────────────────────────────────────────────
_run_lock    = threading.Lock()
_run_process = None
_run_state   = {
    "running":     False,
    "started_at":  None,
    "finished_at": None,
    "exit_code":   None,
}

# ── Agent management (agent removed) ─────────────────────────────────────────
def _start_agent():
    return False, "Stock screening agent has been removed."


def _UNUSED_start_agent_original():
    global _run_process, _run_state
    with _run_lock:
        if _run_state["running"]:
            return False, "Agent is already running"
        os.makedirs(LOG_DIR, exist_ok=True)
        _run_state.update({
            "running":     True,
            "started_at":  datetime.now().isoformat(),
            "finished_at": None,
            "exit_code":   None,
        })
    def _monitor():
        proc.wait()
        with _run_lock:
            _run_state.update({
                "running":     False,
                "finished_at": datetime.now().isoformat(),
                "exit_code":   proc.returncode,
            })
    threading.Thread(target=_monitor, daemon=True).start()
    return True, "Agent started"


def _stop_agent():
    global _run_process
    with _run_lock:
        if not _run_state["running"] or _run_process is None:
            return False, "No agent running"
        _run_process.terminate()
        _run_state.update({
            "running":     False,
            "finished_at": datetime.now().isoformat(),
            "exit_code":   -1,
        })
    return True, "Agent stopped"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _next_run_time():
    now       = datetime.now()
    candidate = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:          # skip Sat=5, Sun=6
        candidate += timedelta(days=1)
    return candidate


def _latest_log_lines(n: int = 300):
    logs = sorted(glob.glob(os.path.join(LOG_DIR, "agent_*.log")), reverse=True)
    if not logs:
        return []
    try:
        with open(logs[0], errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def _list_reports():
    pdfs = sorted(
        glob.glob(os.path.join(AGENT_DIR, "intelligent_investor_*.pdf")),
        reverse=True,
    )
    # Delete PDFs beyond the 10 most recent
    for old_pdf in pdfs[10:]:
        try:
            os.remove(old_pdf)
        except Exception:
            pass
    pdfs = pdfs[:10]
    out = []
    for p in pdfs:
        fname = os.path.basename(p)
        dp    = fname.replace("intelligent_investor_", "").replace(".pdf", "")
        try:
            d = datetime.strptime(dp, "%Y%m%d").strftime("%b %d, %Y")
        except Exception:
            d = dp
        out.append({
            "filename": fname,
            "date":     d,
            "size_kb":  round(os.path.getsize(p) / 1024, 1),
        })
    return out


def _parse_last_picks():
    """Read top-5 picks from picks_detail.json (single source of truth)."""
    try:
        if os.path.exists(_PICKS_DETAIL_FILE):
            with open(_PICKS_DETAIL_FILE) as f:
                d = json.load(f)
            picks = []
            for p in d.get("picks", []):
                roe = p.get("roe")
                roe_str = f"{roe*100:.1f}%" if roe is not None else "N/A"
                picks.append({
                    "rank":   p["rank"],
                    "symbol": p["symbol"],
                    "name":   p["name"],
                    "score":  p["score"],
                    "grade":  p["grade"],
                    "roe":    roe_str,
                })
            return picks
    except Exception:
        pass
    return []


def _parse_snap_picks():
    """Read full metric snapshot from picks_detail.json (single source of truth)."""
    try:
        if os.path.exists(_PICKS_DETAIL_FILE):
            with open(_PICKS_DETAIL_FILE) as f:
                d = json.load(f)
            picks = []
            for p in d.get("picks", []):
                div = p.get("dividend_yield")
                roe = p.get("roe")
                nm  = p.get("net_margin")
                picks.append({
                    "rank":   p["rank"],
                    "symbol": p["symbol"],
                    "name":   p["name"],
                    "change": p.get("price_change_pct"),
                    "fwd_pe": p.get("forward_pe"),
                    "pb":     p.get("pb_ratio"),
                    "cr":     p.get("current_ratio"),
                    "de":     p.get("debt_to_equity"),
                    "roe":    roe * 100 if roe is not None else None,
                    "nm":     nm  * 100 if nm  is not None else None,
                    "div":    div if div is not None else None,
                    "grade":  p["grade"],
                    "score":  p["score"],
                })
            return picks
    except Exception:
        pass
    return []


def _load_config() -> dict:
    """Load agent config from disk, filling defaults for missing keys."""
    cfg = dict(_DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def _save_config(updates: dict) -> dict:
    """Merge updates into existing config and persist to disk."""
    cfg = _load_config()
    cfg.update(updates)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def _parse_run_summary():
    """Extract universe / analyzed / picks counts from the last COMPLETE block."""
    lines = _latest_log_lines(500)
    # Find the index of the LAST "COMPLETE" line
    trigger_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "COMPLETE":
            trigger_idx = i
    if trigger_idx is None:
        return {}
    summary = {}
    for line in lines[trigger_idx + 1:]:
        m = re.match(r"\s+Universe screened\s*:\s*([\d,]+)", line)
        if m:
            summary["universe"] = m.group(1)
        m = re.match(r"\s+Down today\s*:\s*([\d,]+)", line)
        if m:
            summary["down_today"] = m.group(1)
        m = re.match(r"\s+Stocks analyzed\s*:\s*(\d+)", line)
        if m:
            summary["analyzed"] = m.group(1)
        m = re.match(r"\s+Top picks\s*:\s*(\d+)", line)
        if m:
            summary["top_picks"] = m.group(1)
        if re.search(r"={10}", line) and summary:
            break
    return summary


# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    nxt   = _next_run_time()
    delta = nxt - datetime.now()
    h, r  = divmod(int(delta.total_seconds()), 3600)
    m     = r // 60
    return jsonify({
        "run_state":    _run_state,
        "next_run_fmt": nxt.strftime("%a %b %-d at %-I:%M %p"),
        "countdown":    f"{h}h {m}m",
        "last_picks":   _parse_last_picks(),
        "snap_picks":   _parse_snap_picks(),
        "run_summary":  _parse_run_summary(),
        "report_count": len(_list_reports()),
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    ok, msg = _start_agent()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.route("/api/run", methods=["DELETE"])
def api_stop():
    ok, msg = _stop_agent()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/logs")
def api_logs():
    n = min(int(request.args.get("n", 300)), 2000)
    return jsonify({"lines": _latest_log_lines(n)})


@app.route("/api/reports")
def api_reports():
    return jsonify({"reports": _list_reports()})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(_load_config())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True, silent=True) or {}
    cfg  = _save_config(data)
    return jsonify({"ok": True, "config": cfg})


@app.route("/reports/<path:fname>")
def serve_report(fname):
    safe = os.path.realpath(os.path.join(AGENT_DIR, os.path.basename(fname)))
    if not safe.startswith(os.path.realpath(AGENT_DIR)) or not safe.endswith(".pdf"):
        return "Not found", 404
    if not os.path.exists(safe):
        return "Not found", 404
    return send_file(safe, mimetype="application/pdf")


# ── ETF & Bond screener state ──────────────────────────────────────────────────
_ETF_RESULTS_FILE   = os.path.join(AGENT_DIR, "etf_results.json")
_BOND_RESULTS_FILE  = os.path.join(AGENT_DIR, "bond_results.json")
_PICKS_DETAIL_FILE  = os.path.join(AGENT_DIR, "picks_detail.json")

_etf_state  = {"running": False, "error": None, "started_at": None}
_bond_state = {"running": False, "error": None, "started_at": None}
_etf_lock   = threading.Lock()
_bond_lock  = threading.Lock()


def _load_screener_results(path: str):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _run_etf_screen():
    with _etf_lock:
        _etf_state["started_at"] = datetime.utcnow().isoformat() + "Z"
    sys.path.insert(0, AGENT_DIR)
    try:
        import importlib, growth_etf_screener as m
        importlib.reload(m)
        data = m.run_screen()
        # Only overwrite saved results if we actually got data back
        if data.get("results") and len(data["results"]) > 0:
            with open(_ETF_RESULTS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        else:
            with _etf_lock:
                _etf_state["error"] = "Run returned 0 results — possible network issue. Previous results preserved."
    except Exception as e:
        with _etf_lock:
            _etf_state["error"] = str(e)
    finally:
        with _etf_lock:
            _etf_state["running"] = False


def _run_bond_screen():
    with _bond_lock:
        _bond_state["started_at"] = datetime.utcnow().isoformat() + "Z"
    sys.path.insert(0, AGENT_DIR)
    try:
        import importlib, bond_etf_screener as m
        importlib.reload(m)
        data = m.run_screen()
        # Only overwrite saved results if we actually got data back
        if data.get("results") and len(data["results"]) > 0:
            with open(_BOND_RESULTS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        else:
            with _bond_lock:
                _bond_state["error"] = "Run returned 0 results — possible network issue. Previous results preserved."
    except Exception as e:
        with _bond_lock:
            _bond_state["error"] = str(e)
    finally:
        with _bond_lock:
            _bond_state["running"] = False


@app.route("/api/etfs", methods=["GET"])
def api_etfs_get():
    with _etf_lock:
        running    = _etf_state["running"]
        error      = _etf_state["error"]
        started_at = _etf_state["started_at"]
    return jsonify({"running": running, "error": error, "started_at": started_at,
                    "results": _load_screener_results(_ETF_RESULTS_FILE)})


@app.route("/api/etfs", methods=["POST"])
def api_etfs_run():
    with _etf_lock:
        if _etf_state["running"]:
            return jsonify({"ok": False, "message": "ETF screen already running"})
        _etf_state["running"] = True
        _etf_state["error"]   = None
    threading.Thread(target=_run_etf_screen, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/bonds", methods=["GET"])
def api_bonds_get():
    with _bond_lock:
        running    = _bond_state["running"]
        error      = _bond_state["error"]
        started_at = _bond_state["started_at"]
    return jsonify({"running": running, "error": error, "started_at": started_at,
                    "results": _load_screener_results(_BOND_RESULTS_FILE)})


@app.route("/api/bonds", methods=["POST"])
def api_bonds_run():
    with _bond_lock:
        if _bond_state["running"]:
            return jsonify({"ok": False, "message": "Bond screen already running"})
        _bond_state["running"] = True
        _bond_state["error"]   = None
    threading.Thread(target=_run_bond_screen, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/picks/detail")
def api_picks_detail():
    """Return the rich per-pick detail JSON written by the agent after each run."""
    try:
        if os.path.exists(_PICKS_DETAIL_FILE):
            with open(_PICKS_DETAIL_FILE) as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify({"picks": [], "run_date": None})


# ── HTML / CSS / JS ────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Intelligent Investor Agent</title>
  <style>
    /* ── Reset & Variables ──────────────────────────────────── */
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{
      --bg:        #0d1117;
      --bg-card:   #161b22;
      --bg-metric: #1a2033;
      --bg-input:  #21262d;
      --text:      #e6edf3;
      --muted:     #8b949e;
      --orange:    #f5a623;
      --green:     #3fb950;
      --red:       #f85149;
      --blue:      #58a6ff;
      --purple:    #bc8cff;
      --yellow:    #d29922;
      --cyan:      #39d0d8;
      --border:    #30363d;
      --border2:   #21262d;
      --radius:    10px;
      --radius-sm: 6px;
    }
    html{font-size:14px;scroll-behavior:smooth}
    body{
      background:var(--bg);
      color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',system-ui,sans-serif;
      line-height:1.5;
      min-height:100vh;
    }

    /* ── Accent strip ───────────────────────────────────────── */
    .accent-strip{
      height:4px;
      background:linear-gradient(90deg,#e74c3c 0%,#f39c12 20%,#f1c40f 40%,#2ecc71 60%,#3498db 80%,#9b59b6 100%);
      position:sticky;top:0;z-index:200;
    }

    /* ── Header ─────────────────────────────────────────────── */
    .header{
      background:var(--bg-card);
      border-bottom:1px solid var(--border);
      padding:14px 28px;
      display:flex;align-items:center;gap:14px;
      position:sticky;top:4px;z-index:199;
    }
    .logo{
      font-size:1.3rem;font-weight:800;
      color:var(--orange);letter-spacing:-.02em;
    }
    .logo-sub{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
    .hdr-spacer{flex:1}

    /* ── Status pill ────────────────────────────────────────── */
    .status-pill{
      display:flex;align-items:center;gap:7px;
      padding:5px 13px;border-radius:20px;
      border:1px solid var(--border);
      font-size:.8rem;color:var(--muted);
      transition:all .2s;
    }
    .status-dot{width:8px;height:8px;border-radius:50%;background:var(--border);transition:background .2s}
    .status-pill.running{color:var(--green);border-color:rgba(63,185,80,.4)}
    .status-pill.running .status-dot{background:var(--green);animation:pulse-dot 1s infinite}
    @keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.3}}

    /* ── Buttons ────────────────────────────────────────────── */
    .btn{
      padding:7px 16px;border-radius:var(--radius-sm);
      border:1px solid var(--orange);background:transparent;
      color:var(--orange);font-size:.83rem;font-weight:600;
      cursor:pointer;transition:all .15s;white-space:nowrap;
    }
    .btn:hover{background:var(--orange);color:#0d1117}
    .btn:disabled{opacity:.4;cursor:not-allowed}
    .btn:disabled:hover{background:transparent;color:var(--orange)}
    .btn-primary{background:var(--orange);color:#0d1117}
    .btn-primary:hover{background:#e8951f;border-color:#e8951f}
    .btn-danger{border-color:var(--red);color:var(--red)}
    .btn-danger:hover{background:var(--red);color:#fff}
    .btn-sm{padding:4px 10px;font-size:.75rem}

    /* ── Spinner ────────────────────────────────────────────── */
    @keyframes spin{to{transform:rotate(360deg)}}
    .spinner{
      display:inline-block;width:13px;height:13px;
      border:2px solid currentColor;border-top-color:transparent;
      border-radius:50%;animation:spin .7s linear infinite;
      vertical-align:middle;margin-right:5px;
    }

    /* ── Layout ─────────────────────────────────────────────── */
    .container{max-width:1400px;margin:0 auto;padding:24px 28px}

    /* ── Section title ──────────────────────────────────────── */
    .section-title{
      font-size:.72rem;font-weight:700;letter-spacing:.1em;
      text-transform:uppercase;color:var(--muted);
      margin-bottom:14px;display:flex;align-items:center;gap:10px;
    }
    .section-title::after{content:'';flex:1;height:1px;background:var(--border)}

    /* ── Card ───────────────────────────────────────────────── */
    .card{
      background:var(--bg-card);border:1px solid var(--border);
      border-radius:var(--radius);padding:20px;
    }
    .card-label{font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
    .card-value{font-size:1.3rem;font-weight:700}
    .card-sub{font-size:.8rem;color:var(--muted);margin-top:5px}

    /* ── Status row (3 KPI cards) ───────────────────────────── */
    .kpi-row{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px}

    /* ── Main 2-col grid ────────────────────────────────────── */
    .main-grid{display:grid;grid-template-columns:1fr 360px;gap:20px;margin-bottom:24px}

    /* ── Picks table ────────────────────────────────────────── */
    .picks-wrap{overflow:hidden;border-radius:var(--radius);border:1px solid var(--border)}
    .picks-table{width:100%;border-collapse:collapse}
    .picks-table th{
      text-align:left;padding:9px 14px;
      font-size:.7rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
      color:var(--muted);background:var(--bg-metric);border-bottom:1px solid var(--border);
    }
    .picks-table td{padding:12px 14px;border-bottom:1px solid var(--border2);font-size:.85rem;vertical-align:middle}
    .picks-table tbody tr:last-child td{border-bottom:none}
    .picks-table tbody tr:hover td{background:rgba(255,255,255,.025)}
    .ticker-sym{font-weight:800;font-size:.92rem;letter-spacing:.02em}
    .company-name{color:var(--text);font-size:.82rem}
    .sector-tag{font-size:.7rem;color:var(--muted);margin-top:2px}
    .grade-badge{
      display:inline-block;padding:2px 8px;border-radius:4px;
      font-size:.78rem;font-weight:700;
    }
    .score-wrap{display:flex;align-items:center;gap:8px}
    .score-track{flex:1;height:4px;border-radius:2px;background:var(--border);max-width:56px}
    .score-fill{height:100%;border-radius:2px}

    /* ── Snapshot table ─────────────────────────────────────── */
    .snap-section-label{
      font-size:.68rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
      color:var(--muted);margin-bottom:8px;
    }
    .snap-table-wrap{overflow-x:auto;border-radius:var(--radius);border:1px solid var(--border)}
    .snap-table{width:100%;border-collapse:collapse;font-size:.78rem}
    .snap-table th{
      text-align:right;padding:8px 10px;
      font-size:.67rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
      color:var(--muted);background:var(--bg-metric);border-bottom:1px solid var(--border);
      white-space:nowrap;
    }
    .snap-table th:first-child,.snap-table th:nth-child(2){text-align:left}
    .snap-table td{
      padding:10px 10px;border-bottom:1px solid var(--border2);
      text-align:right;vertical-align:middle;
    }
    .snap-table td:first-child,.snap-table td:nth-child(2){text-align:left}
    .snap-table tbody tr:last-child td{border-bottom:none}
    .snap-table tbody tr:hover td{background:rgba(255,255,255,.025)}
    .snap-rating{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.75rem;font-weight:700}
    .snap-drop{font-weight:700}

    /* ── Pick cards ─────────────────────────────────────────── */
    .pick-cards{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:12px}
    @media(max-width:900px){.pick-cards{grid-template-columns:repeat(3,1fr)}}
    @media(max-width:600px){.pick-cards{grid-template-columns:repeat(2,1fr)}}
    .pick-card{
      background:var(--bg-metric);border:1px solid var(--border);
      border-top-width:3px;border-radius:var(--radius-sm);
      padding:11px 10px 10px;display:flex;flex-direction:column;gap:3px;
    }
    .pick-card-top{display:flex;align-items:center;justify-content:space-between}
    .pick-card-rank{font-size:.65rem;color:var(--muted);font-weight:700;letter-spacing:.04em}
    .pick-card-grade{font-size:.7rem;font-weight:700;padding:1px 6px;border-radius:3px}
    .pick-card-ticker{font-size:1.05rem;font-weight:800;letter-spacing:.02em;line-height:1.1}
    .pick-card-score{font-size:.72rem;color:var(--muted)}
    .pick-card-change{font-size:.88rem;font-weight:700}
    .pick-card-name{font-size:.68rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .score-lbl{font-size:.78rem;font-weight:700;min-width:32px}

    /* ── Settings panel ──────────────────────────────────── */
    .cfg-section-lbl{
      font-size:.67rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
      color:var(--muted);margin-bottom:10px;
    }
    .cfg-divider{height:1px;background:var(--border2);margin:14px 0 12px}
    /* Toggle rows */
    .toggle-row{
      display:flex;justify-content:space-between;align-items:center;
      padding:9px 0;border-bottom:1px solid var(--border2);
    }
    .toggle-row:last-of-type{border-bottom:none}
    .toggle-label{font-size:.82rem;color:var(--text);font-weight:500}
    .toggle-sub{font-size:.7rem;color:var(--muted);margin-top:2px}
    /* Toggle switch */
    .tgl-switch{position:relative;width:38px;height:21px;flex-shrink:0}
    .tgl-switch input{opacity:0;width:0;height:0;position:absolute}
    .tgl-track{
      position:absolute;inset:0;background:var(--border);
      border-radius:11px;cursor:pointer;transition:background .2s;
    }
    .tgl-track::before{
      content:'';position:absolute;width:15px;height:15px;
      left:3px;top:3px;background:#fff;border-radius:50%;
      transition:transform .2s;
    }
    .tgl-switch input:checked+.tgl-track{background:var(--green)}
    .tgl-switch input:checked+.tgl-track::before{transform:translateX(17px)}
    /* Market chips */
    .market-chips{display:flex;flex-wrap:wrap;gap:7px;padding:2px 0 4px}
    .mkt-chip{
      padding:4px 13px;border-radius:20px;font-size:.73rem;font-weight:700;
      border:1px solid var(--border);cursor:pointer;
      color:var(--muted);background:transparent;transition:all .15s;
    }
    .mkt-chip.active{border-color:var(--orange);color:var(--orange);background:rgba(245,166,35,.1)}
    .mkt-chip:hover:not(.active){border-color:var(--muted)}
    /* Period radio options */
    .period-opts{display:flex;flex-direction:column;gap:6px;padding:2px 0 2px}
    .period-opt{
      display:flex;align-items:center;gap:10px;
      padding:8px 10px;border-radius:var(--radius-sm);cursor:pointer;
      border:1px solid var(--border);transition:border-color .15s,background .15s;
    }
    .period-opt:hover{background:rgba(255,255,255,.025)}
    .period-opt.sel{border-color:var(--orange);background:rgba(245,166,35,.07)}
    .period-opt input[type=radio]{accent-color:var(--orange);width:14px;height:14px;flex-shrink:0;cursor:pointer}
    .period-opt-lbl{font-size:.81rem;color:var(--text);font-weight:500;line-height:1.2}
    .period-opt-sub{font-size:.68rem;color:var(--muted);margin-top:1px}
    /* Save indicator */
    .cfg-save-ok{font-size:.7rem;color:var(--green);text-align:right;margin-top:6px;min-height:16px}

    /* ── Info panel (right column) ──────────────────────────── */
    .info-row{
      display:flex;justify-content:space-between;align-items:flex-start;
      padding:10px 0;border-bottom:1px solid var(--border2);
    }
    .info-row:last-child{border-bottom:none}
    .info-lbl{font-size:.8rem;color:var(--muted)}
    .info-val{font-size:.83rem;font-weight:600;text-align:right;max-width:180px}

    /* ── Summary stats bar ──────────────────────────────────── */
    .stats-bar{
      display:grid;grid-template-columns:repeat(4,1fr);
      gap:1px;background:var(--border);
      border:1px solid var(--border);border-radius:var(--radius);
      overflow:hidden;margin-bottom:24px;
    }
    .stat-cell{
      background:var(--bg-card);padding:14px 18px;
      text-align:center;
    }
    .stat-num{font-size:1.5rem;font-weight:800;color:var(--orange)}
    .stat-lbl{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-top:2px}

    /* ── Reports grid ───────────────────────────────────────── */
    .reports-grid{
      display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));
      gap:14px;margin-bottom:24px;
    }
    .report-card{
      background:var(--bg-metric);border:1px solid var(--border);
      border-radius:var(--radius);padding:18px;
      text-decoration:none;color:inherit;display:block;
      transition:all .2s;
    }
    .report-card:hover{border-color:var(--orange);transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.4)}
    .report-icon{font-size:2rem;margin-bottom:10px}
    .report-date{font-size:.9rem;font-weight:700}
    .report-meta{font-size:.73rem;color:var(--muted);margin-top:3px}
    .report-cta{
      display:block;margin-top:12px;padding:5px 0;
      text-align:center;font-size:.75rem;color:var(--blue);
      border:1px solid var(--border);border-radius:4px;transition:all .15s;
    }
    .report-card:hover .report-cta{border-color:var(--orange);color:var(--orange)}
    .empty-card{
      grid-column:1/-1;padding:40px;text-align:center;
      background:var(--bg-card);border:1px dashed var(--border);
      border-radius:var(--radius);color:var(--muted);
    }
    .empty-icon{font-size:2.5rem;margin-bottom:12px}
    .empty-card h3{font-size:1rem;color:var(--text);margin-bottom:6px}
    .empty-card p{font-size:.83rem}

    /* ── Log viewer ─────────────────────────────────────────── */
    .log-wrap{background:var(--bg-metric);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:24px}
    .log-bar{
      display:flex;align-items:center;justify-content:space-between;
      padding:10px 16px;background:var(--bg-card);border-bottom:1px solid var(--border);
    }
    .log-title{font-size:.72rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
    .live-badge{
      font-size:.68rem;padding:2px 8px;border-radius:10px;
      background:rgba(63,185,80,.1);color:var(--green);
      border:1px solid rgba(63,185,80,.3);
      display:none;
    }
    .live-badge.active{display:inline}
    #log-output{
      height:380px;overflow-y:auto;padding:14px 16px;
      font-family:'SF Mono','Fira Code','Monaco',monospace;
      font-size:.75rem;line-height:1.7;
      white-space:pre-wrap;word-break:break-word;
      color:var(--muted);
    }
    /* Log line colors */
    .ll-step  {color:var(--orange);font-weight:600}
    .ll-ok    {color:var(--green)}
    .ll-err   {color:var(--red)}
    .ll-warn  {color:var(--yellow)}
    .ll-info  {color:var(--blue)}
    .ll-ticker{color:var(--purple)}
    .ll-rule  {color:var(--border2)}
    .ll-hdr   {color:var(--cyan);font-weight:700}

    /* ── Toast ──────────────────────────────────────────────── */
    #toast{
      position:fixed;bottom:24px;right:24px;z-index:9999;
      background:var(--bg-card);border:1px solid var(--border);
      border-radius:var(--radius);padding:11px 18px;
      font-size:.84rem;box-shadow:0 8px 24px rgba(0,0,0,.5);
      opacity:0;transform:translateY(8px);transition:all .22s;
      pointer-events:none;
    }
    #toast.show{opacity:1;transform:translateY(0)}
    #toast.success{border-color:var(--green);color:var(--green)}
    #toast.error  {border-color:var(--red);color:var(--red)}
    #toast.info   {border-color:var(--blue);color:var(--blue)}
    #toast.warn   {border-color:var(--yellow);color:var(--yellow)}

    /* ── Responsive ─────────────────────────────────────────── */
    @media(max-width:960px){
      .main-grid{grid-template-columns:1fr}
      .kpi-row{grid-template-columns:repeat(2,1fr)}
      .stats-bar{grid-template-columns:repeat(2,1fr)}
      .header{padding:12px 16px}
      .container{padding:16px}
    }
    @media(max-width:500px){
      .kpi-row{grid-template-columns:1fr}
      .logo{font-size:1rem}
    }

    /* ── Tab Navigation ─────────────────────────────────────── */
    .tab-nav{display:flex;gap:4px;margin-bottom:22px;border-bottom:1px solid var(--border);padding-bottom:0}
    .tab-btn{background:transparent;border:none;border-bottom:2px solid transparent;
             color:var(--muted);font-size:.88rem;font-weight:600;padding:10px 20px 10px;
             cursor:pointer;transition:color .15s,border-color .15s;margin-bottom:-1px;
             border-radius:var(--radius-sm) var(--radius-sm) 0 0}
    .tab-btn:hover{color:var(--text)}
    .tab-btn.active{color:var(--orange);border-bottom-color:var(--orange)}
    .tab-panel{display:none}
    .tab-panel.active{display:block}

    /* ── Screener action bar ─────────────────────────────────── */
    .screener-hdr{display:flex;align-items:center;justify-content:space-between;
                  flex-wrap:wrap;gap:12px;margin-bottom:18px}
    .screener-title{font-size:1.05rem;font-weight:700;color:var(--text)}
    .screener-sub{font-size:.76rem;color:var(--muted);margin-top:3px}
    .screener-meta{font-size:.72rem;color:var(--muted);text-align:right}

    /* ── Per-tab control bar ─────────────────────────────────── */
    .tab-ctrl-bar{display:flex;align-items:center;justify-content:space-between;
                  flex-wrap:wrap;gap:12px;margin-bottom:20px;
                  padding:14px 18px;background:var(--bg-card);
                  border:1px solid var(--border);border-radius:var(--radius)}
    .tab-ctrl-left{}
    .tab-ctrl-title{font-size:1.05rem;font-weight:700;color:var(--text)}
    .tab-ctrl-sub{font-size:.76rem;color:var(--muted);margin-top:3px}
    .tab-ctrl-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}

    /* ── Countdown badge ─────────────────────────────────────── */
    .countdown-badge{
      display:inline-flex;align-items:center;gap:5px;
      padding:4px 11px;border-radius:20px;
      background:rgba(245,166,35,.12);border:1px solid rgba(245,166,35,.35);
      color:var(--orange);font-size:.78rem;font-weight:600;
      font-variant-numeric:tabular-nums;letter-spacing:.01em;
      transition:all .3s;
    }
    .countdown-badge.done{background:rgba(63,185,80,.12);border-color:rgba(63,185,80,.35);color:var(--green)}
    .countdown-badge.hidden{display:none}

    /* ── ETF / Bond results table ────────────────────────────── */
    .etf-table-wrap{overflow-x:auto;border-radius:var(--radius);
                    border:1px solid var(--border);margin-top:16px}
    .etf-table{width:100%;border-collapse:collapse;font-size:.81rem}
    .etf-table th{background:var(--bg-metric);color:var(--muted);font-size:.68rem;
                  font-weight:700;text-transform:uppercase;letter-spacing:.04em;
                  padding:9px 12px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}
    .etf-table th:nth-child(1),.etf-table th:nth-child(2),.etf-table th:nth-child(3){text-align:left}
    .etf-table td{padding:10px 12px;border-bottom:1px solid var(--border2);
                  text-align:right;vertical-align:middle;color:var(--text)}
    .etf-table td:nth-child(1),.etf-table td:nth-child(2),.etf-table td:nth-child(3){text-align:left}
    .etf-table tbody tr:last-child td{border-bottom:none}
    .etf-table tbody tr:hover td{background:rgba(255,255,255,.025)}
    .etf-rank{width:28px;height:28px;border-radius:50%;display:inline-flex;
              align-items:center;justify-content:center;font-weight:800;font-size:.78rem}
    .etf-grade{display:inline-block;padding:2px 8px;border-radius:4px;
               font-weight:700;font-size:.78rem}

    /* ── Checklist popover ───────────────────────────────────── */
    .cl-wrap{position:relative;display:inline-block;cursor:pointer}
    .cl-btn{background:var(--bg-metric);border:1px solid var(--border);color:var(--muted);
            font-size:.68rem;padding:2px 7px;border-radius:4px;cursor:pointer}
    .cl-btn:hover{color:var(--text);border-color:var(--muted)}
    .cl-pop{display:none;position:absolute;right:0;top:calc(100% + 4px);
            background:var(--bg-card);border:1px solid var(--border);
            border-radius:var(--radius);padding:10px 14px;width:360px;z-index:100;
            box-shadow:0 8px 32px rgba(0,0,0,.5)}
    .cl-wrap:hover .cl-pop{display:block}
    .cl-row{display:flex;align-items:flex-start;gap:8px;padding:4px 0;
            border-bottom:1px solid var(--border2);font-size:.76rem}
    .cl-row:last-child{border-bottom:none}
    .cl-icon{flex-shrink:0;font-size:.7rem;font-weight:700;
             width:38px;text-align:center;padding:2px 4px;border-radius:3px}
    .cl-lbl{font-weight:600;color:var(--text);min-width:130px}
    .cl-val{color:var(--orange);margin-left:4px}
    .cl-desc{color:var(--muted);font-size:.68rem;margin-top:1px}

    /* ── Pick cards (ETF / Bond) ─────────────────────────────── */
    .etf-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
               gap:12px;margin-bottom:20px}
    .etf-card{background:var(--bg-card);border:1px solid var(--border);
              border-radius:var(--radius);padding:14px 16px;
              border-top:3px solid var(--border)}
    .etf-card-rank{font-size:.68rem;font-weight:700;color:var(--muted);margin-bottom:4px}
    .etf-card-sym{font-size:1.35rem;font-weight:800;color:var(--text)}
    .etf-card-name{font-size:.72rem;color:var(--muted);margin-top:2px;
                   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .etf-card-grade{font-size:1.1rem;font-weight:800;margin-top:8px}
    .etf-card-score{font-size:.76rem;color:var(--muted);margin-top:1px}
    .etf-card-metrics{display:grid;grid-template-columns:1fr 1fr;gap:3px 10px;
                      margin-top:10px;font-size:.7rem;color:var(--muted)}
    .etf-card-metrics span{color:var(--text)}

    /* ── Pick Detail Slide-over ──────────────────────────────── */
    .pdo{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:300;
         opacity:0;pointer-events:none;transition:opacity .22s}
    .pdo.open{opacity:1;pointer-events:all}
    .pdp{position:fixed;top:0;right:0;width:min(820px,96vw);height:100vh;
         background:var(--bg-card);border-left:1px solid var(--border);
         z-index:301;overflow-y:auto;transform:translateX(100%);
         transition:transform .26s cubic-bezier(.4,0,.2,1)}
    .pdo.open .pdp{transform:translateX(0)}
    .pdp-hero{padding:26px 28px 20px;border-bottom:1px solid var(--border);
              position:sticky;top:0;background:var(--bg-card);z-index:10}
    .pdp-ticker{font-family:'Syne',sans-serif;font-weight:800;font-size:2.6rem;line-height:1}
    .pdp-sec{padding:20px 28px;border-bottom:1px solid var(--border)}
    .pdp-sec-title{font-size:.68rem;font-weight:700;color:var(--muted);
                   text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px}
    .pdp-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
    @media(max-width:600px){.pdp-metrics{grid-template-columns:repeat(2,1fr)}}
    .pdp-metric{background:var(--bg-metric);border:1px solid var(--border);
                border-radius:8px;padding:12px 14px;position:relative}
    .pdp-metric-lbl{font-size:.6rem;font-family:'JetBrains Mono',monospace;
                    color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
    .pdp-metric-val{font-size:1.45rem;font-family:'JetBrains Mono',monospace;
                    font-weight:600;margin-top:5px}
    .pdp-dot{position:absolute;top:10px;right:10px;width:7px;height:7px;
             border-radius:50%}
    .pdp-score-tbl{width:100%;border-collapse:collapse;font-size:.8rem}
    .pdp-score-tbl th{background:var(--bg-metric);color:var(--muted);font-size:.63rem;
                      font-weight:700;text-transform:uppercase;letter-spacing:.06em;
                      padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
    .pdp-score-tbl td{padding:9px 12px;border-bottom:1px solid var(--border2);
                      vertical-align:top}
    .pdp-score-tbl tbody tr:last-child td{border-bottom:none}
    .pdp-score-tbl tbody tr:hover td{filter:brightness(1.08)}
    .pdp-verdict{margin:20px 28px 28px;padding:22px 26px;border-radius:10px}
    .pdp-bar-track{height:8px;border-radius:4px;background:var(--border);margin:8px 0 4px}
    .pdp-bar-fill{height:100%;border-radius:4px;transition:width .5s ease}
    .pdp-close{position:absolute;top:20px;right:24px;background:var(--bg-metric);
               border:1px solid var(--border);color:var(--muted);padding:6px 14px;
               border-radius:6px;cursor:pointer;font-size:.78rem;
               transition:color .15s,border-color .15s}
    .pdp-close:hover{color:var(--text);border-color:var(--muted)}
    .pdp-divider{font-size:.66rem;font-weight:700;color:var(--muted);
                 text-transform:uppercase;letter-spacing:.1em;
                 padding:6px 12px;background:var(--bg-metric);
                 border-bottom:1px solid var(--border)}
    .pdp-pill{display:inline-block;padding:2px 9px;border-radius:4px;
              font-size:.7rem;font-weight:700;font-family:'JetBrains Mono',monospace}
    /* ── AI Analysis sections ─────────────────────────── */
    .ai-ctx{padding:16px 20px;font-size:.83rem;line-height:1.7;color:var(--text-dim,#9aafc2)}
    .ai-ctx strong{color:var(--text)}
    .ai-rec{margin:0 28px 0;padding:18px 22px;border-radius:10px;margin-bottom:0}
    .ai-rec-label{font-family:'Syne',sans-serif;font-size:1.15rem;font-weight:800;margin-bottom:8px}
    .ai-rec-body{font-size:.83rem;line-height:1.65;opacity:.9}
    .ai-rec-price{font-family:'JetBrains Mono',monospace;font-size:.75rem;
                  margin-top:8px;opacity:.75}
    .ai-signals{padding:14px 28px}
    .ai-signal-item{display:flex;gap:10px;align-items:flex-start;
                    padding:7px 0;border-bottom:1px solid var(--border2,rgba(255,255,255,.04))}
    .ai-signal-item:last-child{border-bottom:none}
    .ai-signal-dot{width:6px;height:6px;border-radius:50%;background:var(--blue);
                   flex-shrink:0;margin-top:6px}
    .ai-signal-text{font-size:.8rem;color:var(--text-dim,#9aafc2);line-height:1.5}
    .ai-flags{padding:10px 28px}
    .ai-flag-item{display:flex;gap:10px;align-items:flex-start;padding:6px 0}
    .ai-flag-dot{width:6px;height:6px;border-radius:50%;background:var(--yellow);
                 flex-shrink:0;margin-top:6px}
    .ai-flag-text{font-size:.8rem;color:var(--yellow);line-height:1.5}
    .ai-disq{padding:10px 28px}
    .ai-disq-item{display:flex;gap:10px;align-items:flex-start;padding:6px 0}
    .ai-disq-dot{width:6px;height:6px;border-radius:50%;background:var(--red);
                 flex-shrink:0;margin-top:6px}
    .ai-disq-text{font-size:.8rem;color:var(--red);line-height:1.5}
    .ai-sec-lbl{font-size:.62rem;font-weight:700;color:var(--muted);
                letter-spacing:.1em;text-transform:uppercase;
                padding:12px 28px 4px;display:flex;align-items:center;gap:8px}
    .ai-sec-lbl::after{content:'';flex:1;height:1px;background:var(--border)}
    .ai-no-key{padding:20px 28px;font-size:.8rem;color:var(--muted);
               background:var(--bg-metric);border-radius:8px;margin:16px 28px;
               text-align:center;border:1px dashed var(--border)}
  </style>
</head>
<body>

<div class="accent-strip"></div>

<!-- ──────────────── Header ──────────────────────────── -->
<header class="header">
  <div>
    <div class="logo">💼 Intelligent Investor Agent</div>
    <div class="logo-sub">Graham × Buffett × Howard Marks · NYSE + NASDAQ Daily Screen</div>
  </div>
  <div class="hdr-spacer"></div>
</header>

<!-- ──────────────── Main ─────────────────────────────── -->
<main class="container">

  <!-- ── Tab Navigation ──────────────────────────────── -->
  <div class="tab-nav">
    <button class="tab-btn active" id="tabn-stocks" onclick="switchTab('stocks')">📊 Stocks</button>
    <button class="tab-btn"        id="tabn-etfs"   onclick="switchTab('etfs')">🚀 Growth ETFs</button>
    <button class="tab-btn"        id="tabn-bonds"  onclick="switchTab('bonds')">💼 Bond ETFs</button>
  </div>

  <!-- ── Stocks Tab ───────────────────────────────────── -->
  <div id="tab-stocks" class="tab-panel active">

  <!-- Stocks control bar -->
  <div class="tab-ctrl-bar">
    <div class="tab-ctrl-left">
      <div class="tab-ctrl-title">📊 Stock Screener</div>
      <div class="tab-ctrl-sub">Graham × Buffett × Howard Marks scoring — NYSE · NASDAQ · AMEX</div>
    </div>
    <div class="tab-ctrl-right">
      <span id="stocks-countdown" class="countdown-badge hidden"></span>
      <div id="status-pill" class="status-pill">
        <div class="status-dot" id="status-dot"></div>
        <span id="status-text">Idle</span>
      </div>
      <button id="run-btn"  class="btn btn-primary" onclick="runAgent()">▶ Run Now</button>
      <button id="stop-btn" class="btn btn-danger"  onclick="stopAgent()" style="display:none">■ Stop</button>
    </div>
  </div>

  <!-- KPI cards -->
  <div class="kpi-row">
    <div class="card">
      <div class="card-label">Last Run</div>
      <div class="card-value" id="kpi-last-run">—</div>
      <div class="card-sub"  id="kpi-last-sub"></div>
    </div>
    <div class="card">
      <div class="card-label">Next Scheduled Run</div>
      <div class="card-value" id="kpi-next-run">—</div>
      <div class="card-sub"  id="kpi-next-sub"></div>
    </div>
    <div class="card">
      <div class="card-label">Reports Generated</div>
      <div class="card-value" id="kpi-reports">—</div>
      <div class="card-sub">Daily PDF reports archived</div>
    </div>
  </div>

  <!-- Summary stats bar -->
  <div class="stats-bar" id="stats-bar">
    <div class="stat-cell"><div class="stat-num" id="stat-universe">—</div><div class="stat-lbl">Stocks Screened</div></div>
    <div class="stat-cell"><div class="stat-num" id="stat-down">—</div><div class="stat-lbl">Down Today</div></div>
    <div class="stat-cell"><div class="stat-num" id="stat-analyzed">—</div><div class="stat-lbl">Analyzed</div></div>
    <div class="stat-cell"><div class="stat-num" id="stat-picks">—</div><div class="stat-lbl">Top Picks</div></div>
  </div>

  <!-- Main 2-col grid -->
  <div class="main-grid">

    <!-- Left: picks table -->
    <div>
      <div class="section-title">Latest Top Picks</div>
      <div id="picks-wrap"></div>
    </div>

    <!-- Right: agent configuration (interactive) -->
    <div>
      <div class="section-title">Agent Configuration</div>
      <div class="card">

        <!-- ── Features ─────────────────────────────── -->
        <div class="cfg-section-lbl">Features</div>

        <div class="toggle-row">
          <div>
            <div class="toggle-label">Email Report</div>
            <div class="toggle-sub">Send PDF report to your email address</div>
          </div>
          <label class="tgl-switch">
            <input type="checkbox" id="cfg-email">
            <span class="tgl-track"></span>
          </label>
        </div>

        <div class="toggle-row">
          <div>
            <div class="toggle-label">PDF Generation</div>
            <div class="toggle-sub">Generate Intelligent Investor PDF report</div>
          </div>
          <label class="tgl-switch">
            <input type="checkbox" id="cfg-pdf">
            <span class="tgl-track"></span>
          </label>
        </div>

        <!-- ── Markets ───────────────────────────────── -->
        <div class="cfg-divider"></div>
        <div class="cfg-section-lbl">Markets to Screen</div>
        <div class="market-chips">
          <button class="mkt-chip" id="mkt-NYSE"   onclick="toggleMarket('NYSE')">NYSE</button>
          <button class="mkt-chip" id="mkt-NASDAQ" onclick="toggleMarket('NASDAQ')">NASDAQ</button>
          <button class="mkt-chip" id="mkt-AMEX"   onclick="toggleMarket('AMEX')">AMEX</button>
        </div>

        <!-- ── Screening Criteria ────────────────────── -->
        <div class="cfg-divider"></div>
        <div class="cfg-section-lbl">Screening Criteria</div>
        <div class="period-opts">
          <label class="period-opt" id="opt-daily100">
            <input type="radio" name="cfg-period" value="daily100">
            <div>
              <div class="period-opt-lbl">Daily Losers — Top 100</div>
              <div class="period-opt-sub">Stocks down most today · 100 highest market cap</div>
            </div>
          </label>
          <label class="period-opt" id="opt-daily500">
            <input type="radio" name="cfg-period" value="daily500">
            <div>
              <div class="period-opt-lbl">Daily Losers — Top 500</div>
              <div class="period-opt-sub">Stocks down most today · 500 highest market cap</div>
            </div>
          </label>
          <label class="period-opt" id="opt-weekly100">
            <input type="radio" name="cfg-period" value="weekly100">
            <div>
              <div class="period-opt-lbl">Weekly Losers — Top 100</div>
              <div class="period-opt-sub">Worst 5-trading-day performers · 100 highest market cap</div>
            </div>
          </label>
          <label class="period-opt" id="opt-weekly500">
            <input type="radio" name="cfg-period" value="weekly500">
            <div>
              <div class="period-opt-lbl">Weekly Losers — Top 500</div>
              <div class="period-opt-sub">Worst 5-trading-day performers · 500 highest market cap</div>
            </div>
          </label>
          <label class="period-opt" id="opt-yearly100">
            <input type="radio" name="cfg-period" value="yearly100">
            <div>
              <div class="period-opt-lbl">52-Week Losers — Top 100</div>
              <div class="period-opt-sub">Worst performers over the past year · 100 highest market cap</div>
            </div>
          </label>
          <label class="period-opt" id="opt-yearly500">
            <input type="radio" name="cfg-period" value="yearly500">
            <div>
              <div class="period-opt-lbl">52-Week Losers — Top 500</div>
              <div class="period-opt-sub">Worst performers over the past year · 500 highest market cap</div>
            </div>
          </label>
          <label class="period-opt" id="opt-dailyall">
            <input type="radio" name="cfg-period" value="dailyall">
            <div>
              <div class="period-opt-lbl">Daily Losers — All</div>
              <div class="period-opt-sub">Stocks down most today · ALL qualifying stocks scored · 2–3 hrs</div>
            </div>
          </label>
          <label class="period-opt" id="opt-weeklyall">
            <input type="radio" name="cfg-period" value="weeklyall">
            <div>
              <div class="period-opt-lbl">Weekly Losers — All</div>
              <div class="period-opt-sub">Worst 5-day performers · ALL qualifying stocks scored · 3–5 hrs</div>
            </div>
          </label>
          <label class="period-opt" id="opt-yearlyall">
            <input type="radio" name="cfg-period" value="yearlyall">
            <div>
              <div class="period-opt-lbl">52-Week Losers — All</div>
              <div class="period-opt-sub">Worst yearly performers · ALL qualifying stocks scored · 3–5 hrs</div>
            </div>
          </label>
          <label class="period-opt" id="opt-value100">
            <input type="radio" name="cfg-period" value="value100">
            <div>
              <div class="period-opt-lbl">All Best Value Stocks — Top 100</div>
              <div class="period-opt-sub">Graham/Buffett score across 100 largest-cap stocks</div>
            </div>
          </label>
          <label class="period-opt" id="opt-value500">
            <input type="radio" name="cfg-period" value="value500">
            <div>
              <div class="period-opt-lbl">All Best Value Stocks — Top 500</div>
              <div class="period-opt-sub">Graham/Buffett score across 500 largest-cap stocks (slower)</div>
            </div>
          </label>
        </div>

        <!-- ── Stock Geography ──────────────────────────── -->
        <div class="cfg-divider"></div>
        <div class="cfg-section-lbl">Stock Geography</div>
        <div class="period-opts">
          <label class="period-opt" id="opt-geo-all">
            <input type="radio" name="cfg-geo" value="all">
            <div>
              <div class="period-opt-lbl">All Stocks</div>
              <div class="period-opt-sub">USA + International (ADRs, foreign listings)</div>
            </div>
          </label>
          <label class="period-opt" id="opt-geo-usa">
            <input type="radio" name="cfg-geo" value="usa">
            <div>
              <div class="period-opt-lbl">🇺🇸 USA Only</div>
              <div class="period-opt-sub">US-domiciled companies only</div>
            </div>
          </label>
          <label class="period-opt" id="opt-geo-international">
            <input type="radio" name="cfg-geo" value="international">
            <div>
              <div class="period-opt-lbl">🌍 International Only</div>
              <div class="period-opt-sub">Non-US companies only (excludes domestic stocks)</div>
            </div>
          </label>
        </div>

        <!-- AI API Key -->
        <div class="cfg-divider"></div>
        <div style="font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px">Claude AI Analysis</div>
        <div style="font-size:.73rem;color:var(--muted);margin-bottom:8px;line-height:1.5">
          API key for deep-dive AI analysis on each pick (business context, value trap detection, recommendation).
          Get a key at <span style="color:var(--blue);font-family:'JetBrains Mono',monospace">console.anthropic.com</span>
        </div>
        <input type="password" id="cfg-claude-key" placeholder="sk-ant-..." autocomplete="off"
          style="width:100%;background:var(--bg-metric);border:1px solid var(--border);
                 border-radius:6px;padding:8px 12px;color:var(--text);font-family:'JetBrains Mono',monospace;
                 font-size:.78rem;outline:none;transition:border-color .15s"
          oninput="saveConfig()">

        <!-- Save indicator -->
        <div class="cfg-save-ok" id="cfg-save-ok"></div>

        <!-- ── Static info ───────────────────────────── -->
        <div class="cfg-divider"></div>
        <div class="info-row">
          <div class="info-lbl">Schedule</div>
          <div class="info-val" style="color:var(--orange)">Mon – Fri, 6:00 PM</div>
        </div>
        <div class="info-row">
          <div class="info-lbl">Framework</div>
          <div class="info-val">Graham × Buffett<br><span style="font-size:.72rem;color:var(--muted);font-weight:400">8-criterion checklist</span></div>
        </div>
        <div class="info-row">
          <div class="info-lbl">Data Sources</div>
          <div class="info-val">NASDAQ API · FMP · yfinance</div>
        </div>
        <div class="info-row" id="duration-row" style="display:none">
          <div class="info-lbl">Last Run Duration</div>
          <div class="info-val" id="duration-val">—</div>
        </div>
      </div>

      <!-- Graham checklist legend -->
      <div style="margin-top:16px">
        <div class="section-title" style="margin-bottom:10px">Graham 8-Criteria  <span style="color:var(--muted);font-weight:400;font-size:.68rem">(max 8 pts)</span></div>
        <div class="card" style="padding:14px 16px">
          <div style="font-size:.75rem;color:var(--muted);line-height:1.9">
            <div>① Revenue &gt; $1B &nbsp;&nbsp; ② Current Ratio ≥ 2.0</div>
            <div>③ D/E ≤ 1.0 &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; ④ Positive Net Income</div>
            <div>⑤ Pays Dividend &nbsp;&nbsp; ⑥ Positive EPS</div>
            <div>⑦ P/E ≤ 15 &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; ⑧ P/E × P/B ≤ 22.5</div>
          </div>
          <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;font-size:.72rem">
            <span style="color:var(--green)">● PASS = 1.0 pt</span>
            <span style="color:var(--yellow)">● COND = 0.5 pt</span>
            <span style="color:var(--red)">● FAIL = 0 pt</span>
          </div>
        </div>
      </div>

      <!-- Buffett checklist legend -->
      <div style="margin-top:14px">
        <div class="section-title" style="margin-bottom:10px">Buffett 5-Criteria  <span style="color:var(--muted);font-weight:400;font-size:.68rem">(max 5 pts)</span></div>
        <div class="card" style="padding:14px 16px;border-color:rgba(245,166,35,.25)">
          <div style="font-size:.75rem;color:var(--muted);line-height:1.9">
            <div>① ROE ≥ 15% &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <span style="color:var(--orange);font-size:.7rem">Competitive Moat</span></div>
            <div>② Net Margin ≥ 10% &nbsp; <span style="color:var(--orange);font-size:.7rem">Pricing Power</span></div>
            <div>③ D/E ≤ 0.5 &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <span style="color:var(--orange);font-size:.7rem">Financial Fortress</span></div>
            <div>④ FCF &gt; 0 &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <span style="color:var(--orange);font-size:.7rem">Earnings Quality</span></div>
            <div>⑤ FCF Yield ≥ 3% &nbsp;&nbsp;&nbsp; <span style="color:var(--orange);font-size:.7rem">Shareholder Value</span></div>
          </div>
          <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;font-size:.72rem">
            <span style="color:var(--green)">● PASS = 1.0 pt</span>
            <span style="color:var(--yellow)">● COND = 0.5 pt</span>
            <span style="color:var(--red)">● FAIL = 0 pt</span>
          </div>
          <div style="margin-top:8px;font-size:.72rem;color:var(--muted)">
            Combined score: Graham + Buffett = <span style="color:var(--orange);font-weight:700">X / 13</span>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- Reports -->
  <div class="section-title">Reports Archive</div>
  <div id="reports-grid" class="reports-grid"></div>

  <!-- Log viewer -->
  <div class="section-title">Agent Log</div>
  <div class="log-wrap">
    <div class="log-bar">
      <span class="log-title">Latest Agent Output</span>
      <div style="display:flex;align-items:center;gap:10px">
        <span id="live-badge" class="live-badge">● LIVE</span>
        <span id="log-meta" style="font-size:.72rem;color:var(--muted)"></span>
        <button class="btn btn-sm" onclick="refreshLogs()">↺ Refresh</button>
      </div>
    </div>
    <div id="log-output">Loading…</div>
  </div>

  </div><!-- /tab-stocks -->

  <!-- ── Growth ETFs Tab ──────────────────────────────── -->
  <div id="tab-etfs" class="tab-panel">

    <div class="tab-ctrl-bar">
      <div class="tab-ctrl-left">
        <div class="tab-ctrl-title">🚀 Growth &amp; Quality ETF Screener</div>
        <div class="tab-ctrl-sub">Graham × Buffett 10-criteria scoring — ~50 curated growth &amp; quality ETFs</div>
      </div>
      <div class="tab-ctrl-right">
        <div class="screener-meta" id="etf-last-run-lbl"></div>
        <span id="etfs-countdown" class="countdown-badge hidden"></span>
        <div id="etf-status-pill" class="status-pill">
          <div class="status-dot" id="etf-status-dot"></div>
          <span id="etf-status-text">Idle</span>
        </div>
        <button class="btn btn-primary" id="etf-run-btn" onclick="runEtfScreen()">▶ Run Now</button>
        <button class="btn btn-danger"  id="etf-stop-btn" style="display:none" disabled>■ Stop</button>
      </div>
    </div>

    <!-- ETF stats bar -->
    <div class="stats-bar" id="etf-stats-bar" style="display:none">
      <div class="stat-cell"><div class="stat-num" id="etf-stat-screened">—</div><div class="stat-lbl">ETFs Screened</div></div>
      <div class="stat-cell"><div class="stat-num" id="etf-stat-eligible">—</div><div class="stat-lbl">Eligible</div></div>
      <div class="stat-cell"><div class="stat-num" id="etf-stat-rf">—</div><div class="stat-lbl">Risk-Free Rate</div></div>
      <div class="stat-cell"><div class="stat-num" id="etf-stat-dur">—</div><div class="stat-lbl">Run Time</div></div>
    </div>

    <!-- ETF top 5 cards + full table -->
    <div id="etf-results-wrap">
      <div class="card" style="text-align:center;padding:40px;color:var(--muted)">
        <div style="font-size:2rem;margin-bottom:10px">🚀</div>
        <div style="font-weight:600;margin-bottom:6px">Click <strong style="color:var(--text)">Run ETF Screen</strong> to find top-rated growth ETFs</div>
        <div style="font-size:.78rem">Graham × Buffett 10-criteria analysis — est. ~3 min</div>
      </div>
    </div>

  </div><!-- /tab-etfs -->

  <!-- ── Bond ETFs Tab ────────────────────────────────── -->
  <div id="tab-bonds" class="tab-panel">

    <div class="tab-ctrl-bar">
      <div class="tab-ctrl-left">
        <div class="tab-ctrl-title">💼 Investment-Grade Bond ETF Screener</div>
        <div class="tab-ctrl-sub">Graham safety-first + Buffett capital-preservation scoring — ~35 curated IG bond ETFs</div>
      </div>
      <div class="tab-ctrl-right">
        <div class="screener-meta" id="bond-last-run-lbl"></div>
        <span id="bonds-countdown" class="countdown-badge hidden"></span>
        <div id="bond-status-pill" class="status-pill">
          <div class="status-dot" id="bond-status-dot"></div>
          <span id="bond-status-text">Idle</span>
        </div>
        <button class="btn btn-primary" id="bond-run-btn" onclick="runBondScreen()">▶ Run Now</button>
        <button class="btn btn-danger"  id="bond-stop-btn" style="display:none" disabled>■ Stop</button>
      </div>
    </div>

    <!-- Bond stats bar -->
    <div class="stats-bar" id="bond-stats-bar" style="display:none">
      <div class="stat-cell"><div class="stat-num" id="bond-stat-screened">—</div><div class="stat-lbl">Bond ETFs Screened</div></div>
      <div class="stat-cell"><div class="stat-num" id="bond-stat-eligible">—</div><div class="stat-lbl">Eligible</div></div>
      <div class="stat-cell"><div class="stat-num" id="bond-stat-rf">—</div><div class="stat-lbl">Risk-Free Rate</div></div>
      <div class="stat-cell"><div class="stat-num" id="bond-stat-cpi">—</div><div class="stat-lbl">CPI Inflation</div></div>
    </div>

    <!-- Bond top 5 cards + full table -->
    <div id="bond-results-wrap">
      <div class="card" style="text-align:center;padding:40px;color:var(--muted)">
        <div style="font-size:2rem;margin-bottom:10px">💼</div>
        <div style="font-weight:600;margin-bottom:6px">Click <strong style="color:var(--text)">Run Bond Screen</strong> to find top-rated investment-grade bond ETFs</div>
        <div style="font-size:.78rem">Graham safety-first analysis — excludes all HY/junk bonds — est. ~2 min</div>
      </div>
    </div>

  </div><!-- /tab-bonds -->

</main>

<!-- ── Pick Detail Slide-over ───────────────────────────────────────── -->
<div class="pdo" id="pdo" onclick="if(event.target===this)closePick()">
  <div class="pdp" id="pdp">
    <!-- Hero (sticky) -->
    <div class="pdp-hero" id="pdp-hero">
      <button class="pdp-close" onclick="closePick()">✕ Close</button>
    </div>
    <!-- Claude AI Analysis (recommendation, context, signals, disqualifiers) -->
    <div id="pdp-ai"></div>
    <!-- Metrics -->
    <div class="pdp-sec" id="pdp-metrics">
      <div class="pdp-sec-title">Key Metrics</div>
      <div class="pdp-metrics" id="pdp-metrics-grid"></div>
    </div>
    <!-- Graham Scorecard -->
    <div class="pdp-sec" id="pdp-graham-sec">
      <div class="pdp-sec-title">Graham Checklist</div>
      <div style="overflow-x:auto">
        <table class="pdp-score-tbl" id="pdp-graham-tbl">
          <thead><tr>
            <th style="width:42%">Criterion</th>
            <th>Detail</th>
            <th style="width:110px;text-align:center">Result</th>
          </tr></thead>
          <tbody id="pdp-graham-body"></tbody>
        </table>
      </div>
    </div>
    <!-- Buffett Scorecard -->
    <div class="pdp-sec" id="pdp-buffett-sec">
      <div class="pdp-sec-title">Buffett Checklist</div>
      <div style="overflow-x:auto">
        <table class="pdp-score-tbl" id="pdp-buffett-tbl">
          <thead><tr>
            <th style="width:42%">Criterion</th>
            <th>Detail</th>
            <th style="width:110px;text-align:center">Result</th>
          </tr></thead>
          <tbody id="pdp-buffett-body"></tbody>
        </table>
      </div>
    </div>
    <!-- Verdict -->
    <div id="pdp-verdict"></div>
  </div>
</div>

<!-- Toast -->
<div id="toast"></div>

<script>
// ── Constants ──────────────────────────────────────────────
const TC = ['#f5a623','#58a6ff','#bc8cff','#ff7b72','#7ee787'];
const GRADE_CFG = {
  'A' :{ bg:'rgba(63,185,80,.15)',  fg:'#3fb950' },
  'B+':{ bg:'rgba(245,166,35,.15)', fg:'#f5a623' },
  'B' :{ bg:'rgba(245,166,35,.12)', fg:'#f5a623' },
  'C+':{ bg:'rgba(210,153,34,.15)', fg:'#d29922' },
  'C' :{ bg:'rgba(210,153,34,.12)', fg:'#d29922' },
  'D' :{ bg:'rgba(248,81,73,.1)',   fg:'#f85149' },
  'F' :{ bg:'rgba(248,81,73,.15)',  fg:'#f85149' },
};

// ── State ──────────────────────────────────────────────────
let isRunning = false;
let fastPoll  = null;
let slowPoll  = null;

// ── Utils ──────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const gs = g  => GRADE_CFG[g] || { bg:'rgba(139,148,158,.1)', fg:'#8b949e' };

function scoreColor(s){
  return s>=10.5?'#3fb950':s>=8?'#f5a623':s>=5.5?'#d29922':'#f85149';
}

function fmtDt(iso){
  if(!iso) return '—';
  try{
    const d=new Date(iso);
    return d.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'})+
      ' at '+d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});
  }catch{return iso}
}

function toast(msg,type='info',ms=3500){
  const el=$('toast');
  el.textContent=msg;
  el.className='show '+type;
  clearTimeout(toast._t);
  toast._t=setTimeout(()=>el.className='',ms);
}

// ── Status pill ────────────────────────────────────────────
function setRunning(running){
  const pill=$('status-pill'), txt=$('status-text');
  const runBtn=$('run-btn'), stopBtn=$('stop-btn');
  if(running){
    pill.className='status-pill running';
    txt.innerHTML='<span class="spinner"></span>Running…';
    runBtn.style.display='none';
    stopBtn.style.display='';
    $('live-badge').classList.add('active');
  } else {
    pill.className='status-pill';
    txt.textContent='Idle';
    runBtn.style.display='';
    runBtn.disabled=false;
    stopBtn.style.display='none';
    $('live-badge').classList.remove('active');
  }
}

// ── Snap metric helpers ─────────────────────────────────────
function fmtNum(v,dec,suffix){
  if(v===null||v===undefined) return '<span style="color:var(--muted)">—</span>';
  return parseFloat(v).toFixed(dec)+(suffix||'');
}
function snapValColor(v,goodHigh){
  // goodHigh=true: higher is better (ROE, Net Mgn, Curr.R.)
  // goodHigh=false: lower is better (P/E, P/B, D/E)
  if(v===null||v===undefined) return '';
  if(goodHigh) return v>0?'color:var(--green)':'color:var(--red)';
  return v<15&&v>0?'color:var(--green)':v>25?'color:var(--red)':'';
}
function dropColor(v){
  if(v===null||v===undefined) return '';
  return v<=-10?'color:var(--red)':v<=-5?'color:var(--yellow)':'color:var(--muted)';
}

// ── Snapshot table ──────────────────────────────────────────
function renderSnapTable(snapPicks){
  const rows=snapPicks.map((p,i)=>{
    const tc=TC[i%TC.length];
    const gc=gs(p.grade);
    const chg=p.change!==null?p.change.toFixed(2)+'%':'—';
    const chgStyle=dropColor(p.change);
    return `<tr>
      <td style="width:30px;color:var(--muted);font-size:.75rem">${p.rank}</td>
      <td style="width:68px"><span class="ticker-sym" style="color:${tc}">${p.symbol}</span></td>
      <td class="snap-drop" style="${chgStyle}">${chg}</td>
      <td>${fmtNum(p.fwd_pe,1,'x')}</td>
      <td>${fmtNum(p.pb,2,'x')}</td>
      <td style="${snapValColor(p.cr,true)}">${fmtNum(p.cr,2)}</td>
      <td style="${snapValColor(p.de,false)}">${fmtNum(p.de,2)}</td>
      <td style="${snapValColor(p.roe,true)}">${fmtNum(p.roe,1,'%')}</td>
      <td style="${snapValColor(p.nm,true)}">${fmtNum(p.nm,1,'%')}</td>
      <td>${p.div!==null&&p.div!==undefined?p.div.toFixed(2)+'%':'<span style="color:var(--muted)">—</span>'}</td>
      <td style="width:62px"><span class="snap-rating" style="background:${gc.bg};color:${gc.fg}">${p.grade}</span></td>
    </tr>`;
  }).join('');
  return `<div class="snap-section-label">Comparative Snapshot — All ${snapPicks.length} Picks</div>
  <div class="snap-table-wrap">
    <table class="snap-table">
      <thead><tr>
        <th>#</th><th>Ticker</th><th>Drop</th>
        <th>Fwd P/E</th><th>P/B</th><th>Curr.R.</th><th>D/E</th>
        <th>ROE</th><th>Net Mgn</th><th>Div.Yld</th><th>Rating</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

// ── Pick highlight cards ────────────────────────────────────
function renderPickCards(snapPicks){
  const cards=snapPicks.map((p,i)=>{
    const tc=TC[i%TC.length];
    const gc=gs(p.grade);
    const sc=scoreColor(p.score);
    const chg=p.change!==null?p.change.toFixed(2)+'%':'—';
    const chgStyle=dropColor(p.change);
    return `<div class="pick-card" style="border-top-color:${tc};cursor:pointer" onclick="openPick('${p.symbol}')">
      <div class="pick-card-top">
        <span class="pick-card-rank">PICK ${p.rank}</span>
        <span class="pick-card-grade" style="background:${gc.bg};color:${gc.fg}">${p.grade}</span>
      </div>
      <div class="pick-card-ticker" style="color:${tc}">${p.symbol}</div>
      <div class="pick-card-score" style="color:${sc}">${p.score}/13 pts</div>
      <div class="pick-card-change" style="${chgStyle}">${chg}</div>
      <div class="pick-card-name" title="${p.name}">${p.name}</div>
    </div>`;
  }).join('');
  return `<div class="pick-cards">${cards}</div>`;
}

// ── Picks section ───────────────────────────────────────────
// ── Simple pick cards (from last_picks — no full metrics needed) ────────────
function renderSimpleCards(picks){
  const cards=picks.map((p,i)=>{
    const tc=TC[i%TC.length];
    const gc=gs(p.grade);
    const sc=scoreColor(p.score);
    const roeColor=p.roe&&(p.roe.startsWith('-')||p.roe==='N/A')?'var(--red)':'var(--green)';
    return `<div class="pick-card" style="border-top-color:${tc};cursor:pointer" onclick="openPick('${p.symbol}')">
      <div class="pick-card-top">
        <span class="pick-card-rank">PICK ${p.rank}</span>
        <span class="pick-card-grade" style="background:${gc.bg};color:${gc.fg}">${p.grade}</span>
      </div>
      <div class="pick-card-ticker" style="color:${tc}">${p.symbol}</div>
      <div class="pick-card-score" style="color:${sc}">${p.score}/13 pts</div>
      <div class="pick-card-change" style="color:${roeColor};font-size:.78rem">ROE ${p.roe}</div>
      <div class="pick-card-name" title="${p.name}">${p.name}</div>
    </div>`;
  }).join('');
  return `<div class="snap-section-label">Top ${picks.length} Picks</div>
    <div class="pick-cards">${cards}</div>
    <div style="font-size:.68rem;color:var(--muted);margin-top:10px;text-align:right">
      Full metrics snapshot available after next run
    </div>`;
}

// ── Picks section ───────────────────────────────────────────
function renderPicks(picks, snapPicks){
  const wrap=$('picks-wrap');
  const hasSnap=snapPicks&&snapPicks.length>0;
  const hasPicks=picks&&picks.length>0;
  if(!hasSnap&&!hasPicks){
    wrap.innerHTML=`<div class="empty-card">
      <div class="empty-icon">🔍</div>
      <h3>No picks yet</h3>
      <p>Click <strong>Run Now</strong> to screen the market and see today's top picks.</p>
    </div>`;
    return;
  }
  if(hasSnap){
    // Rich view: full snapshot table + detailed pick cards (from SNAP log lines)
    wrap.innerHTML=renderSnapTable(snapPicks)+renderPickCards(snapPicks);
  } else {
    // Partial view: pick cards from available data (no metrics table until next run)
    wrap.innerHTML=renderSimpleCards(picks);
  }
}

// ── Config (settings panel) ────────────────────────────────
let _cfgMarkets    = ['NYSE','NASDAQ'];
let _cfgSaveTimer  = null;

async function loadConfig(){
  try{
    const r=await fetch('/api/config');
    const c=await r.json();
    // Feature toggles
    $('cfg-email').checked = !!c.email_enabled;
    $('cfg-pdf').checked   = !!c.pdf_enabled;
    // Market chips
    _cfgMarkets = Array.isArray(c.markets) ? c.markets : ['NYSE','NASDAQ'];
    ['NYSE','NASDAQ','AMEX'].forEach(m=>{
      const el=$('mkt-'+m);
      if(el) el.classList.toggle('active', _cfgMarkets.includes(m));
    });
    // Period radio — migrate legacy bare values (daily→daily100, etc.)
    const _periodMigrate = {daily:'daily100',weekly:'weekly100',yearly:'yearly100',value:'value100'};
    const period = _periodMigrate[c.loser_period] || c.loser_period || 'daily100';
    const radio  = document.querySelector(`input[name="cfg-period"][value="${period}"]`);
    if(radio){ radio.checked=true; }
    _updatePeriodSel(period);
    // Geography radio
    const geo     = c.stock_geography || 'all';
    const geoRdo  = document.querySelector(`input[name="cfg-geo"][value="${geo}"]`);
    if(geoRdo){ geoRdo.checked=true; }
    _updateGeoSel(geo);
    // Attach change listeners now (after DOM populated)
    $('cfg-email').onchange = saveConfig;
    $('cfg-pdf').onchange   = saveConfig;
    document.querySelectorAll('input[name="cfg-period"]').forEach(el=>{
      el.onchange = ()=>{ _updatePeriodSel(el.value); saveConfig(); };
    });
    document.querySelectorAll('input[name="cfg-geo"]').forEach(el=>{
      el.onchange = ()=>{ _updateGeoSel(el.value); saveConfig(); };
    });
    // Claude API key (show masked placeholder if saved)
    if(c.claude_api_key){
      $('cfg-claude-key').value = c.claude_api_key;
    }
  }catch(e){ console.error('loadConfig error',e); }
}

function _updatePeriodSel(val){
  ['daily100','daily500','dailyall','weekly100','weekly500','weeklyall','yearly100','yearly500','yearlyall','value100','value500'].forEach(v=>{
    const el=$('opt-'+v);
    if(el) el.classList.toggle('sel', v===val);
  });
}

function _updateGeoSel(val){
  ['all','usa','international'].forEach(v=>{
    const el=$('opt-geo-'+v);
    if(el) el.classList.toggle('sel', v===val);
  });
}

function toggleMarket(m){
  const idx = _cfgMarkets.indexOf(m);
  if(idx >= 0){
    if(_cfgMarkets.length <= 1) return;   // keep at least one market active
    _cfgMarkets.splice(idx, 1);
  } else {
    _cfgMarkets.push(m);
  }
  const el = $('mkt-'+m);
  if(el) el.classList.toggle('active', _cfgMarkets.includes(m));
  saveConfig();
}

async function saveConfig(){
  clearTimeout(_cfgSaveTimer);
  _cfgSaveTimer = setTimeout(async()=>{
    try{
      const period = document.querySelector('input[name="cfg-period"]:checked')?.value || 'daily';
      const geo    = document.querySelector('input[name="cfg-geo"]:checked')?.value    || 'all';
      const claudeKey = $('cfg-claude-key')?.value?.trim()||'';
      await fetch('/api/config',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          email_enabled:   $('cfg-email').checked,
          pdf_enabled:     $('cfg-pdf').checked,
          markets:         [..._cfgMarkets],
          loser_period:    period,
          stock_geography: geo,
          ...(claudeKey ? {claude_api_key: claudeKey} : {}),
        }),
      });
      const ok=$('cfg-save-ok');
      ok.textContent='✓ Settings saved';
      setTimeout(()=>{ ok.textContent=''; }, 2000);
    }catch(e){ console.error('saveConfig error',e); }
  }, 400);
}

// ── Summary stats ─────────────────────────────────────────
function renderSummary(s){
  if(!s||!Object.keys(s).length) return;
  if(s.universe)  $('stat-universe').textContent=s.universe;
  if(s.down_today)$('stat-down').textContent=s.down_today;
  if(s.analyzed)  $('stat-analyzed').textContent=s.analyzed;
  if(s.top_picks) $('stat-picks').textContent=s.top_picks;
}

// ── Reports ───────────────────────────────────────────────
function renderReports(reports){
  const grid=$('reports-grid');
  $('kpi-reports').textContent=reports.length;
  if(!reports.length){
    grid.innerHTML=`<div class="empty-card">
      <div class="empty-icon">📄</div>
      <h3>No reports yet</h3>
      <p>PDF reports will appear here after the first successful run.</p>
    </div>`;
    return;
  }
  grid.innerHTML=reports.map(r=>`
    <a class="report-card" href="/reports/${r.filename}" target="_blank">
      <div class="report-icon">📊</div>
      <div class="report-date">${r.date}</div>
      <div class="report-meta">${r.size_kb} KB &nbsp;·&nbsp; PDF</div>
      <div class="report-cta">Open Report →</div>
    </a>
  `).join('');
}

// ── Log colorizer ─────────────────────────────────────────
function colorize(line){
  const e=line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  if(/^={5,}/.test(e)||/^-{5,}/.test(e)) return `<span class="ll-rule">${e}</span>`;
  if(/INTELLIGENT INVESTOR AGENT|COMPLETE/.test(e)) return `<span class="ll-hdr">${e}</span>`;
  if(/Step \d+:/i.test(e))   return `<span class="ll-step">${e}</span>`;
  if(/Email sent|PDF saved|✓/.test(e)) return `<span class="ll-ok">${e}</span>`;
  if(/[Ee]rror|FAIL|[Ff]ailed|Errno/.test(e)) return `<span class="ll-err">${e}</span>`;
  if(/[Ww]arn/.test(e))      return `<span class="ll-warn">${e}</span>`;
  if(/Score\s+[\d.]+\/8\s+Grade/.test(e)) return `<span class="ll-ticker">${e}</span>`;
  if(/Fetching\s+[A-Z]{1,5}[^\.]/.test(e)) return `<span class="ll-info">${e}</span>`;
  if(/Universe screened|Down today|Analyzing top/.test(e)) return `<span class="ll-step">${e}</span>`;
  if(/Run (started|finished)/.test(e)) return `<span class="ll-hdr">${e}</span>`;
  return e;
}

async function refreshLogs(){
  try{
    const r=await fetch('/api/logs?n=400');
    const d=await r.json();
    const out=$('log-output');
    const lines=d.lines||[];
    out.innerHTML=lines.length
      ? lines.map(colorize).join('\\n')
      : '<span style="color:var(--muted)">No logs yet. Click Run Now to start.</span>';
    // auto-scroll to bottom
    out.scrollTop=out.scrollHeight;
    // log meta label
    const lastTs=lines.slice().reverse().find(l=>/Run (started|finished)/i.test(l));
    if(lastTs) $('log-meta').textContent=lastTs.replace(/^.*?(Run .*)/, '$1').substring(0,60);
  }catch(err){ console.error('Log fetch error',err) }
}

// ── Status refresh ────────────────────────────────────────
async function refreshStatus(){
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    const st=d.run_state;
    const wasRunning=isRunning;
    isRunning=st.running;

    // transition: running → done
    if(wasRunning && !isRunning){
      setRunning(false);
      _stopCountdown('stocks', '✓ Done');
      _picksDetail = null;  // invalidate cache so next click fetches fresh data
      if(st.exit_code===0){
        toast('✓ Run complete — picks updated!','success',5000);
      } else {
        toast('⚠ Run ended (exit '+st.exit_code+')','warn',5000);
      }
      refreshLogs();
      // Re-fetch status immediately to load fresh picks into the cards
      setTimeout(async()=>{
        try{
          const r2=await fetch('/api/status');
          const d2=await r2.json();
          renderSummary(d2.run_summary);
          renderPicks(d2.last_picks, d2.snap_picks);
        }catch(e){}
      }, 800);
    } else if(isRunning && !wasRunning){
      // Page loaded / reconnected while a run is already in progress —
      // restore the running UI and start a countdown from the remaining time.
      setRunning(true);
      const estimatedSecs = _estimateRunSecs();
      const elapsedSecs   = st.started_at
        ? Math.round((Date.now() - new Date(st.started_at)) / 1000)
        : 0;
      const remainingSecs = Math.max(30, estimatedSecs - elapsedSecs);
      _startCountdown('stocks', remainingSecs);
    }

    // KPI: last run
    if(st.finished_at){
      $('kpi-last-run').textContent=fmtDt(st.finished_at);
      const ok=st.exit_code===0;
      let sub=ok?'✓ Completed successfully':'✗ Exited with code '+st.exit_code;
      if(st.started_at&&st.finished_at){
        const secs=(new Date(st.finished_at)-new Date(st.started_at))/1000;
        const mins=Math.floor(secs/60), s=Math.floor(secs%60);
        sub+=' · '+mins+'m '+s+'s';
        $('duration-row').style.display='';
        $('duration-val').textContent=mins+'m '+s+'s';
      }
      $('kpi-last-sub').textContent=sub;
      $('kpi-last-sub').style.color=ok?'var(--green)':'var(--red)';
    } else if(isRunning){
      $('kpi-last-run').textContent='Running…';
      $('kpi-last-sub').textContent='Started '+fmtDt(st.started_at);
      $('kpi-last-sub').style.color='var(--orange)';
    }

    // KPI: next run
    $('kpi-next-run').textContent=d.next_run_fmt||'—';
    $('kpi-next-sub').textContent=d.countdown?'in '+d.countdown:'';

    // summary stats + picks
    renderSummary(d.run_summary);
    renderPicks(d.last_picks, d.snap_picks);

    // reports
    const rr=await fetch('/api/reports');
    const rd=await rr.json();
    renderReports(rd.reports||[]);

  }catch(err){ console.error('Status fetch error',err) }
}

// ── Run / Stop ────────────────────────────────────────────
// ── Run-time estimator ────────────────────────────────────
// Calibrated from real log data:
//   ~3.5s per stock scored  (FMP profile + 4×paid-attempt sleeps + yfinance)
//   ~4 min batch yfinance download for weekly/yearly with 2-3 exchanges
//   ~2 min batch yfinance download for weekly/yearly with 1 exchange
//
// Geography-filter overhead (geo-filter now runs inside scoring loop):
//   "all"           → 1.0× (no extra fetches)
//   "usa"  top100   → ~29% of large-caps are non-US  → need to scan ~141 to get 100 → 1.41×
//   "usa"  top500   → ~20% of mid-caps are non-US    → need to scan ~625 to get 500 → 1.25×
//   "intl" top100   → ~71% of large-caps are US      → need to scan ~345 to get 100 → 3.45×
//   "intl" top500   → ~80% of mid-caps are US        → need to scan ~2500 to get 500 → 5.0×
//   (fractions based on NYSE+NASDAQ top-market-cap composition observed in logs)
function _estimateRunSecs(){
  const period = document.querySelector('input[name="cfg-period"]:checked')?.value || 'daily100';
  const geo    = document.querySelector('input[name="cfg-geo"]:checked')?.value    || 'all';
  const nMkts  = _cfgMarkets.length;
  const isAll  = period.endsWith('all');
  const is500  = period.endsWith('500');

  // Estimated qualifying losers for "all" modes (typical market day, USA+NASDAQ+AMEX)
  // daily: ~1,500–1,900 losers; weekly: ~1,200–1,600; yearly: ~900–1,300
  const allN = period.startsWith('daily') ? 1700
             : period.startsWith('weekly') ? 1400
             : 1100;  // yearly

  const n = isAll ? allN : (is500 ? 500 : 100);

  // Geo-filter scan multiplier
  let geoMult = 1.0;
  if(geo === 'usa'){
    geoMult = (is500 || isAll) ? 1.25 : 1.41;
  } else if(geo === 'international'){
    geoMult = (is500 || isAll) ? 5.0  : 3.45;
  }

  const scoreSecs  = Math.round(n * geoMult * 3.5);
  const needsBatch = period.startsWith('weekly') || period.startsWith('yearly');
  // Batch download scales with universe size: all-mode needs more chunks
  const dlSecs = needsBatch ? (isAll ? (nMkts >= 2 ? 900 : 600) : (nMkts >= 2 ? 240 : 120)) : 0;
  return scoreSecs + dlSecs;
}

function _estimateRunTime(){
  const secs   = _estimateRunSecs();
  const mins   = Math.round(secs / 60);
  const period = document.querySelector('input[name="cfg-period"]:checked')?.value || 'daily100';
  const geo    = document.querySelector('input[name="cfg-geo"]:checked')?.value    || 'all';
  const periodLabels = {
    daily100:'Daily Losers Top 100',   daily500:'Daily Losers Top 500',   dailyall:'Daily Losers — All',
    weekly100:'Weekly Losers Top 100', weekly500:'Weekly Losers Top 500', weeklyall:'Weekly Losers — All',
    yearly100:'52-Wk Losers Top 100',  yearly500:'52-Wk Losers Top 500', yearlyall:'52-Wk Losers — All',
    value100:'Best Value Top 100',     value500:'Best Value Top 500',
  };
  const geoSuffix = geo === 'usa' ? ' · 🇺🇸 USA only' : geo === 'international' ? ' · 🌍 Intl only' : '';
  const label   = (periodLabels[period] || period) + geoSuffix;
  const timeStr = mins >= 60
    ? `~${Math.floor(mins/60)}h ${mins%60}m`
    : `~${mins} min`;
  return `▶ ${label} — est. ${timeStr}`;
}

// ── Per-tab countdown timers ─────────────────────────────
const _cdIntervals = {};
const _cdEndAt     = {};

function _startCountdown(tab, totalSecs){
  _cdEndAt[tab] = Date.now() + totalSecs * 1000;
  if(_cdIntervals[tab]) clearInterval(_cdIntervals[tab]);
  _tickCountdown(tab);
  _cdIntervals[tab] = setInterval(() => _tickCountdown(tab), 1000);
}

function _tickCountdown(tab){
  const el = $(tab+'-countdown');
  if(!el) return;
  const rem = Math.max(0, Math.round((_cdEndAt[tab] - Date.now()) / 1000));
  if(rem <= 0){
    el.textContent = '✓ Done';
    el.className = 'countdown-badge done';
    clearInterval(_cdIntervals[tab]);
    _cdIntervals[tab] = null;
    setTimeout(() => { if(el) el.className = 'countdown-badge hidden'; }, 4000);
    return;
  }
  const m = Math.floor(rem / 60);
  const s = rem % 60;
  el.textContent = `⏱ ${m}:${s.toString().padStart(2,'0')} left`;
  el.className = 'countdown-badge';
}

function _stopCountdown(tab, label){
  if(_cdIntervals[tab]){ clearInterval(_cdIntervals[tab]); _cdIntervals[tab] = null; }
  const el = $(tab+'-countdown');
  if(!el || el.classList.contains('hidden')) return;
  if(label){
    el.textContent = label;
    el.className = 'countdown-badge done';
    setTimeout(() => { if(el) el.className = 'countdown-badge hidden'; }, 3500);
  } else {
    el.className = 'countdown-badge hidden';
  }
}

async function runAgent(){
  try{
    const r=await fetch('/api/run',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      toast(_estimateRunTime(),'info',8000);
      isRunning=true;
      setRunning(true);
      _startCountdown('stocks', _estimateRunSecs());
      startFastPoll();
    } else {
      toast('⚠ '+d.message,'error');
    }
  }catch(err){ toast('⚠ Could not contact server','error') }
}

async function stopAgent(){
  try{
    const r=await fetch('/api/run',{method:'DELETE'});
    const d=await r.json();
    if(d.ok){
      toast('■ Agent stopped','warn');
      isRunning=false;
      setRunning(false);
      _stopCountdown('stocks', '■ Stopped');
    }
  }catch(err){ toast('⚠ Could not stop agent','error') }
}

// ── Polling ───────────────────────────────────────────────
function startFastPoll(){
  if(fastPoll) clearInterval(fastPoll);
  if(slowPoll) clearInterval(slowPoll);
  fastPoll=setInterval(async()=>{
    await refreshStatus();
    await refreshLogs();
    if(!isRunning){
      clearInterval(fastPoll);
      startSlowPoll();
    }
  },3500);
}

function startSlowPoll(){
  if(slowPoll) clearInterval(slowPoll);
  slowPoll=setInterval(()=>{
    refreshStatus();
    refreshLogs();
  },30000);
}

// ── Init ──────────────────────────────────────────────────
(async function(){
  await loadConfig();
  await refreshStatus();
  await refreshLogs();
  if(isRunning){ startFastPoll() } else { startSlowPoll() }
  // Load cached ETF/bond results if available
  await refreshEtfResults(true);
  await refreshBondResults(true);
})();

// ── Tab switching ─────────────────────────────────────────
function switchTab(name){
  ['stocks','etfs','bonds'].forEach(t=>{
    $('tab-'+t).classList.toggle('active', t===name);
    $('tabn-'+t).classList.toggle('active', t===name);
  });
}

// ── Grade colour helper ───────────────────────────────────
function gradeColor(g){
  if(!g) return 'var(--muted)';
  if(g==='A+') return '#3fb950';
  if(g==='A')  return '#58d68d';
  if(g==='B+') return '#58a6ff';
  if(g==='B')  return '#85c1e9';
  if(g==='C+') return '#f5a623';
  if(g==='C')  return '#d4ac0d';
  if(g==='D')  return '#f0b27a';
  return '#f85149';
}
function rankColor(i){
  const c=['#f5a623','#8b949e','#cd7f32','#58a6ff','#bc8cff'];
  return c[i]||'var(--muted)';
}

// ── Checklist popover HTML ────────────────────────────────
function clPopHTML(checklist){
  if(!checklist||!checklist.length) return '';
  const rows = checklist.map(c=>{
    const icon = c.status==='PASS'?'✓ PASS':c.status==='COND'?'~ COND':'✗ FAIL';
    const bg   = c.status==='PASS'?'rgba(63,185,80,.2)':c.status==='COND'?'rgba(245,166,35,.2)':'rgba(248,81,73,.2)';
    const col  = c.status==='PASS'?'#3fb950':c.status==='COND'?'#f5a623':'#f85149';
    return `<div class="cl-row">
      <span class="cl-icon" style="background:${bg};color:${col}">${icon}</span>
      <div>
        <div><span class="cl-lbl">${c.label}</span><span class="cl-val">${c.value}</span></div>
        <div class="cl-desc">${c.desc}</div>
      </div>
    </div>`;
  }).join('');
  return `<div class="cl-wrap"><button class="cl-btn">Criteria ▾</button>
    <div class="cl-pop">${rows}</div></div>`;
}

// ── ETF top-5 cards ───────────────────────────────────────
function renderEtfCards(top5, isEtf){
  if(!top5||!top5.length) return '';
  return '<div class="etf-cards">'+top5.map((r,i)=>{
    const m = r.metrics||{};
    const gc = gradeColor(r.grade);
    const rc = rankColor(i);
    const metaRows = isEtf
      ? `<div>1yr</div><span>${m.ret_1y!=null?m.ret_1y+'%':'—'}</span>
         <div>3yr ann</div><span>${m.ret_3y!=null?m.ret_3y+'%':'—'}</span>
         <div>Sharpe</div><span>${m.sharpe??'—'}</span>
         <div>Expense</div><span>${m.expense??'—'}%</span>
         <div>Beta</div><span>${m.beta??'—'}</span>
         <div>AUM</div><span>$${m.aum_b??'—'}B</span>`
      : `<div>Yield</div><span>${m.yield_pct!=null?m.yield_pct+'%':'—'}</span>
         <div>Real Yield</div><span>${m.real_yield!=null?m.real_yield+'%':'—'}</span>
         <div>1yr Ret</div><span>${m.ret_1y!=null?m.ret_1y+'%':'—'}</span>
         <div>Sharpe</div><span>${m.sharpe??'—'}</span>
         <div>Duration</div><span>${m.duration??'—'}</span>
         <div>Expense</div><span>${m.expense??'—'}%</span>`;
    return `<div class="etf-card" style="border-top-color:${rc}">
      <div class="etf-card-rank">#${i+1}</div>
      <div class="etf-card-sym">${r.symbol}</div>
      <div class="etf-card-name" title="${r.name}">${r.name}</div>
      <div class="etf-card-grade" style="color:${gc}">${r.grade}</div>
      <div class="etf-card-score">${r.score}/10 &nbsp;·&nbsp; G:${r.graham_score} B:${r.buffett_score}</div>
      <div class="etf-card-metrics">${metaRows}</div>
    </div>`;
  }).join('')+'</div>';
}

// ── ETF full table ────────────────────────────────────────
function renderEtfTable(results){
  if(!results||!results.length) return '';
  const hdr = `<thead><tr>
    <th>#</th><th>Ticker</th><th>Name</th>
    <th>Score</th><th>Grade</th>
    <th>1yr Ret</th><th>3yr Ann</th><th>5yr Ann</th>
    <th>Sharpe</th><th>Beta</th><th>AUM $B</th><th>Expense</th>
    <th>G Score</th><th>B Score</th><th>Criteria</th>
  </tr></thead>`;
  const rows = results.map((r,i)=>{
    const m  = r.metrics||{};
    const gc = gradeColor(r.grade);
    const rc = rankColor(i);
    const fmt = (v,sfx='') => v!=null?v+sfx:'<span style="color:var(--muted)">—</span>';
    const retColor = v => v==null?'':v>=0?'color:var(--green)':'color:var(--red)';
    return `<tr>
      <td><span class="etf-rank" style="background:${rc}22;color:${rc}">${i+1}</span></td>
      <td><strong>${r.symbol}</strong></td>
      <td style="font-size:.76rem;max-width:160px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${r.name}</td>
      <td><strong>${r.score}</strong>/10</td>
      <td><span class="etf-grade" style="background:${gc}22;color:${gc}">${r.grade}</span></td>
      <td style="${retColor(m.ret_1y)}">${fmt(m.ret_1y,'%')}</td>
      <td style="${retColor(m.ret_3y)}">${fmt(m.ret_3y,'%')}</td>
      <td style="${retColor(m.ret_5y)}">${fmt(m.ret_5y,'%')}</td>
      <td>${fmt(m.sharpe)}</td>
      <td>${fmt(m.beta)}</td>
      <td>${fmt(m.aum_b)}</td>
      <td>${fmt(m.expense,'%')}</td>
      <td>${r.graham_score}/5</td>
      <td>${r.buffett_score}/5</td>
      <td>${clPopHTML(r.checklist)}</td>
    </tr>`;
  }).join('');
  return `<div class="etf-table-wrap"><table class="etf-table">${hdr}<tbody>${rows}</tbody></table></div>`;
}

// ── Bond full table ───────────────────────────────────────
function renderBondTable(results){
  if(!results||!results.length) return '';
  const hdr = `<thead><tr>
    <th>#</th><th>Ticker</th><th>Name</th>
    <th>Score</th><th>Grade</th>
    <th>Credit</th><th>Duration</th>
    <th>Yield</th><th>Real Yield</th><th>1yr Ret</th>
    <th>Sharpe</th><th>Max DD</th><th>AUM $B</th><th>Expense</th>
    <th>G Score</th><th>B Score</th><th>Criteria</th>
  </tr></thead>`;
  const rows = results.map((r,i)=>{
    const m  = r.metrics||{};
    const gc = gradeColor(r.grade);
    const rc = rankColor(i);
    const fmt = (v,sfx='') => v!=null?v+sfx:'<span style="color:var(--muted)">—</span>';
    const retColor = v => v==null?'':v>=0?'color:var(--green)':'color:var(--red)';
    const durBadge = {'ultra-short':'var(--green)','short':'#58d68d','medium':'var(--orange)',
                      'long':'#f0b27a','ultra-long':'var(--red)'};
    const durCol = durBadge[m.duration]||'var(--muted)';
    return `<tr>
      <td><span class="etf-rank" style="background:${rc}22;color:${rc}">${i+1}</span></td>
      <td><strong>${r.symbol}</strong></td>
      <td style="font-size:.76rem;max-width:160px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${r.name}</td>
      <td><strong>${r.score}</strong>/10</td>
      <td><span class="etf-grade" style="background:${gc}22;color:${gc}">${r.grade}</span></td>
      <td style="font-size:.73rem;color:var(--blue)">${m.credit||'—'}</td>
      <td><span style="color:${durCol};font-weight:600;font-size:.73rem">${m.duration||'—'}</span></td>
      <td style="color:var(--orange)">${fmt(m.yield_pct,'%')}</td>
      <td style="${retColor(m.real_yield)}">${fmt(m.real_yield,'%')}</td>
      <td style="${retColor(m.ret_1y)}">${fmt(m.ret_1y,'%')}</td>
      <td>${fmt(m.sharpe)}</td>
      <td style="color:var(--red)">${fmt(m.max_dd,'%')}</td>
      <td>${fmt(m.aum_b)}</td>
      <td>${fmt(m.expense,'%')}</td>
      <td>${r.graham_score}/5</td>
      <td>${r.buffett_score}/5</td>
      <td>${clPopHTML(r.checklist)}</td>
    </tr>`;
  }).join('');
  return `<div class="etf-table-wrap"><table class="etf-table">${hdr}<tbody>${rows}</tbody></table></div>`;
}

// ── ETF screen run/poll ───────────────────────────────────
let _etfPoll = null;

function _setEtfRunning(running){
  const pill=$('etf-status-pill'), dot=$('etf-status-dot'), txt=$('etf-status-text');
  if(running){
    pill.className='status-pill running';
    txt.innerHTML='<span class="spinner"></span>Running…';
    $('etf-run-btn').style.display='none';
    $('etf-stop-btn').style.display='';
    $('etf-stop-btn').disabled=true;
  } else {
    pill.className='status-pill';
    txt.textContent='Idle';
    $('etf-run-btn').style.display='';
    $('etf-stop-btn').style.display='none';
  }
}

const _ETF_TOTAL_SECS  = 210;   // ~3.5 min for ~50 growth ETFs
const _BOND_TOTAL_SECS = 150;   // ~2.5 min for ~35 bond ETFs

async function runEtfScreen(){
  const r = await fetch('/api/etfs',{method:'POST'});
  const d = await r.json();
  if(!d.ok){ toast('⚠ '+d.message,'error'); return; }
  const mins = Math.round(_ETF_TOTAL_SECS / 60);
  toast(`▶ Growth ETF screen started — est. ~${mins} min`,'info',8000);
  _setEtfRunning(true);
  _startCountdown('etfs', _ETF_TOTAL_SECS);
  _startEtfPoll();
}

function _startEtfPoll(){
  if(_etfPoll) clearInterval(_etfPoll);
  _etfPoll = setInterval(()=>refreshEtfResults(false), 4000);
}

async function refreshEtfResults(silent){
  try{
    const r = await fetch('/api/etfs');
    const d = await r.json();
    if(d.running){
      const wasRunning = $('etf-stop-btn').style.display !== 'none';
      _setEtfRunning(true);
      if(!_etfPoll) _startEtfPoll();
      // Page reloaded while ETF screen was running — restore countdown
      if(!wasRunning){
        const elapsedSecs = d.started_at
          ? Math.round((Date.now() - new Date(d.started_at)) / 1000) : 0;
        const remainingSecs = Math.max(15, _ETF_TOTAL_SECS - elapsedSecs);
        _startCountdown('etfs', remainingSecs);
      }
    } else {
      const wasRunning = $('etf-stop-btn').style.display !== 'none';
      _setEtfRunning(false);
      if(_etfPoll){ clearInterval(_etfPoll); _etfPoll=null; }
      if(wasRunning) _stopCountdown('etfs', '✓ Done');
      if(d.error && !silent) toast('⚠ ETF screen error: '+d.error,'error',6000);
    }
    if(d.results) _renderEtfData(d.results);
  }catch(e){ console.error('refreshEtfResults',e); }
}

function _renderEtfData(data){
  // Stats bar
  $('etf-stats-bar').style.display='';
  $('etf-stat-screened').textContent = data.screened||'—';
  $('etf-stat-eligible').textContent = data.eligible||'—';
  $('etf-stat-rf').textContent       = data.risk_free_rate!=null?data.risk_free_rate+'%':'—';
  $('etf-stat-dur').textContent      = data.duration_secs!=null?data.duration_secs+'s':'—';
  // Last run label
  if(data.run_date){
    const dt = new Date(data.run_date);
    $('etf-last-run-lbl').textContent = 'Last run: '+dt.toLocaleString();
  }
  // Cards + table
  const top5  = data.top5||data.results?.slice(0,5)||[];
  const all   = data.results||[];
  $('etf-results-wrap').innerHTML =
    `<div class="section-title" style="margin:0 0 14px">Top 5 Growth ETFs — Graham × Buffett</div>`+
    renderEtfCards(top5, true)+
    `<div class="section-title" style="margin:20px 0 0">Full Rankings (${all.length} ETFs)</div>`+
    renderEtfTable(all);
}

// ── Bond screen run/poll ──────────────────────────────────
let _bondPoll = null;

function _setBondRunning(running){
  const pill=$('bond-status-pill'), dot=$('bond-status-dot'), txt=$('bond-status-text');
  if(running){
    pill.className='status-pill running';
    txt.innerHTML='<span class="spinner"></span>Running…';
    $('bond-run-btn').style.display='none';
    $('bond-stop-btn').style.display='';
    $('bond-stop-btn').disabled=true;
  } else {
    pill.className='status-pill';
    txt.textContent='Idle';
    $('bond-run-btn').style.display='';
    $('bond-stop-btn').style.display='none';
  }
}

async function runBondScreen(){
  const r = await fetch('/api/bonds',{method:'POST'});
  const d = await r.json();
  if(!d.ok){ toast('⚠ '+d.message,'error'); return; }
  const mins = Math.round(_BOND_TOTAL_SECS / 60);
  toast(`▶ Bond ETF screen started — est. ~${mins} min`,'info',8000);
  _setBondRunning(true);
  _startCountdown('bonds', _BOND_TOTAL_SECS);
  _startBondPoll();
}

function _startBondPoll(){
  if(_bondPoll) clearInterval(_bondPoll);
  _bondPoll = setInterval(()=>refreshBondResults(false), 4000);
}

async function refreshBondResults(silent){
  try{
    const r = await fetch('/api/bonds');
    const d = await r.json();
    if(d.running){
      const wasRunning = $('bond-stop-btn').style.display !== 'none';
      _setBondRunning(true);
      if(!_bondPoll) _startBondPoll();
      // Page reloaded while bond screen was running — restore countdown
      if(!wasRunning){
        const elapsedSecs = d.started_at
          ? Math.round((Date.now() - new Date(d.started_at)) / 1000) : 0;
        const remainingSecs = Math.max(15, _BOND_TOTAL_SECS - elapsedSecs);
        _startCountdown('bonds', remainingSecs);
      }
    } else {
      const wasRunning = $('bond-stop-btn').style.display !== 'none';
      _setBondRunning(false);
      if(_bondPoll){ clearInterval(_bondPoll); _bondPoll=null; }
      if(wasRunning) _stopCountdown('bonds', '✓ Done');
      if(d.error && !silent) toast('⚠ Bond screen error: '+d.error,'error',6000);
    }
    if(d.results) _renderBondData(d.results);
  }catch(e){ console.error('refreshBondResults',e); }
}

function _renderBondData(data){
  $('bond-stats-bar').style.display='';
  $('bond-stat-screened').textContent = data.screened||'—';
  $('bond-stat-eligible').textContent = data.eligible||'—';
  $('bond-stat-rf').textContent       = data.risk_free_rate!=null?data.risk_free_rate+'%':'—';
  $('bond-stat-cpi').textContent      = data.inflation_rate!=null?data.inflation_rate+'%':'—';
  if(data.run_date){
    const dt = new Date(data.run_date);
    $('bond-last-run-lbl').textContent = 'Last run: '+dt.toLocaleString();
  }
  const top5 = data.top5||data.results?.slice(0,5)||[];
  const all  = data.results||[];
  $('bond-results-wrap').innerHTML =
    `<div class="section-title" style="margin:0 0 14px">Top 5 Bond ETFs — Graham Safety-First</div>`+
    renderEtfCards(top5, false)+
    `<div class="section-title" style="margin:20px 0 0">Full Rankings (${all.length} Bond ETFs)</div>`+
    renderBondTable(all);
}

// ── Pick detail slide-over ────────────────────────────────
let _picksDetail     = null;
let _picksDetailDate = null;

async function _loadPicksDetail(){
  try{
    const r = await fetch('/api/picks/detail');
    const d = await r.json();
    // Always use fresh data — never serve stale cache across runs
    _picksDetail     = d;
    _picksDetailDate = d.run_date || null;
  }catch(e){ _picksDetail = {picks:[]}; }
  return _picksDetail;
}

async function openPick(symbol){
  const data = await _loadPicksDetail();
  const pick = (data.picks||[]).find(p=>p.symbol===symbol);
  if(!pick){
    toast('Detail data not available — run the agent first','warn',4000);
    return;
  }
  const i = (data.picks||[]).indexOf(pick);
  const accent = TC[i % TC.length];
  renderPickPanel(pick, accent);
  document.body.style.overflow = 'hidden';
  $('pdo').classList.add('open');
}

function closePick(){
  $('pdo').classList.remove('open');
  document.body.style.overflow = '';
}

document.addEventListener('keydown', e=>{ if(e.key==='Escape') closePick(); });

function _pdpFmt(v, decimals, suffix){
  if(v===null||v===undefined) return '<span style="color:var(--muted)">—</span>';
  const n = parseFloat(v);
  if(isNaN(n)) return '<span style="color:var(--muted)">—</span>';
  return n.toFixed(decimals) + (suffix||'');
}

function _pdpMetricColor(key, v){
  if(v===null||v===undefined) return '';
  const n = parseFloat(v);
  if(isNaN(n)) return '';
  const goodHigh  = ['current_ratio','roe','net_margin','free_cash_flow','eps','dividend_yield'];
  const goodLow   = ['pe_ratio','forward_pe','pb_ratio','debt_to_equity'];
  const warn      = ['beta'];
  if(goodHigh.includes(key))  return n>0?'color:var(--green)':'color:var(--red)';
  if(goodLow.includes(key)){
    if(key==='pe_ratio'||key==='forward_pe')  return n>0&&n<15?'color:var(--green)':n>30?'color:var(--red)':'';
    if(key==='pb_ratio')                      return n>0&&n<2?'color:var(--green)':n>4?'color:var(--red)':'';
    if(key==='debt_to_equity')                return n<1?'color:var(--green)':n>2?'color:var(--red)':'color:var(--yellow)';
  }
  if(warn.includes(key)) return n>1.3?'color:var(--red)':n<0.8?'color:var(--green)':'';
  return '';
}

function _pdpMarketCap(v){
  if(v===null||v===undefined) return '—';
  const n = parseFloat(v);
  if(isNaN(n)) return '—';
  if(n>=1e12) return (n/1e12).toFixed(2)+'T';
  if(n>=1e9)  return (n/1e9).toFixed(1)+'B';
  if(n>=1e6)  return (n/1e6).toFixed(0)+'M';
  return n.toFixed(0);
}

function _clStatus(s){
  if(s==='PASS') return {icon:'✓ PASS', bg:'rgba(63,185,80,.18)', col:'#3fb950'};
  if(s==='COND') return {icon:'~ COND', bg:'rgba(245,166,35,.18)', col:'#f5a623'};
  return            {icon:'✗ FAIL', bg:'rgba(248,81,73,.18)',  col:'#f85149'};
}

function _recColors(color){
  if(color==='green') return {bg:'rgba(63,185,80,.12)',border:'rgba(63,185,80,.3)',text:'#3fb950'};
  if(color==='red')   return {bg:'rgba(248,81,73,.12)', border:'rgba(248,81,73,.3)', text:'#f85149'};
  return                     {bg:'rgba(245,166,35,.1)', border:'rgba(245,166,35,.3)',text:'#f5a623'};
}

function renderPickPanel(p, accent){
  const gc  = gs(p.grade||'—');
  const sc  = scoreColor(p.score||0);
  const pct = Math.round(((p.score||0)/(p.max_score||13))*100);
  const ai  = p.ai_analysis || null;

  // ── Hero ──────────────────────────────────────────────
  $('pdp-hero').innerHTML = `
    <button class="pdp-close" onclick="closePick()">✕ Close</button>
    <div style="display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap">
      <div>
        <div class="pdp-ticker" style="color:${accent}">${p.symbol}</div>
        <div style="font-size:.95rem;color:var(--text);margin-top:4px;max-width:440px">${p.name||''}</div>
        <div style="font-size:.72rem;color:var(--muted);margin-top:6px">
          ${p.sector||''}${p.sector&&p.industry?' · ':''}${p.industry||''}
          ${p.country?' &nbsp;·&nbsp; 🌐 '+p.country:''}
        </div>
      </div>
      <div style="margin-left:auto;text-align:right;min-width:110px">
        <div style="font-size:2rem;font-weight:800;color:${sc}">${p.score||0}<span style="font-size:1rem;color:var(--muted)">/${p.max_score||13}</span></div>
        <div style="display:inline-block;padding:3px 12px;border-radius:6px;font-weight:700;font-size:.95rem;background:${gc.bg};color:${gc.fg}">${p.grade||'—'}</div>
        <div class="pdp-bar-track"><div class="pdp-bar-fill" style="width:${pct}%;background:${accent}"></div></div>
        <div style="font-size:.65rem;color:var(--muted)">${pct}% score</div>
      </div>
    </div>
    ${p.price_change_pct!=null?
      `<div style="margin-top:10px;font-size:.82rem;color:${p.price_change_pct<=-10?'var(--red)':p.price_change_pct<=-5?'var(--yellow)':'var(--muted)'}">
        ▼ ${Math.abs(parseFloat(p.price_change_pct)).toFixed(2)}% today
      </div>`:''
    }`;

  // ── AI Analysis section ───────────────────────────────
  const aiEl = $('pdp-ai');
  if(ai && ai.business_context){
    const rc = _recColors(ai.recommendation_color||'amber');
    const hasFlags  = ai.value_trap_flags  && ai.value_trap_flags.length;
    const hasSignals= ai.key_signals       && ai.key_signals.length;
    const hasDisq   = ai.disqualifiers     && ai.disqualifiers.length;

    aiEl.innerHTML = `
      <!-- Recommendation banner -->
      <div class="ai-rec" style="background:${rc.bg};border:1px solid ${rc.border};margin:16px 28px 0">
        <div class="ai-rec-label" style="color:${rc.text}">${ai.recommendation_label||'—'}</div>
        <div class="ai-rec-body">${ai.recommendation_narrative||''}</div>
        ${ai.price_target_note?`<div class="ai-rec-price">🎯 Entry target: ${ai.price_target_note}</div>`:''}
      </div>
      <!-- Business context -->
      <div class="ai-sec-lbl" style="margin-top:16px">Business Overview</div>
      <div class="ai-ctx">${ai.business_context||''}</div>
      ${ai.metrics_assessment?`<div class="ai-ctx" style="padding-top:0;margin-top:-8px;font-style:italic;color:var(--muted);font-size:.78rem">${ai.metrics_assessment}</div>`:''}
      ${hasFlags?`
      <!-- Value trap flags -->
      <div class="ai-sec-lbl" style="color:var(--yellow)">⚠ Value Trap Risks</div>
      <div class="ai-flags">${ai.value_trap_flags.map(f=>`
        <div class="ai-flag-item">
          <div class="ai-flag-dot"></div>
          <div class="ai-flag-text">${f}</div>
        </div>`).join('')}
      </div>`:''}
      ${hasSignals?`
      <!-- Key signals -->
      <div class="ai-sec-lbl">Key Signals</div>
      <div class="ai-signals">${ai.key_signals.map(s=>`
        <div class="ai-signal-item">
          <div class="ai-signal-dot" style="background:${accent}"></div>
          <div class="ai-signal-text">${s}</div>
        </div>`).join('')}
      </div>`:''}
      ${hasDisq?`
      <!-- Hard disqualifiers -->
      <div class="ai-sec-lbl" style="color:var(--red)">✗ Hard Disqualifiers</div>
      <div class="ai-disq">${ai.disqualifiers.map(d=>`
        <div class="ai-disq-item">
          <div class="ai-disq-dot"></div>
          <div class="ai-disq-text">${d}</div>
        </div>`).join('')}
      </div>`:''}
    `;
  } else if(ai && !ai.business_context){
    aiEl.innerHTML = `<div class="ai-no-key">
      🤖 AI analysis requires a Claude API key.<br>
      Add <code>claude_api_key</code> to your config.json or set the <code>ANTHROPIC_API_KEY</code> environment variable, then run the agent again.
    </div>`;
  } else {
    aiEl.innerHTML = '';
  }

  // Metrics grid
  const metrics = [
    {key:'pe_ratio',        lbl:'P/E Ratio',       val:_pdpFmt(p.pe_ratio,1,'x')},
    {key:'forward_pe',      lbl:'Forward P/E',     val:_pdpFmt(p.forward_pe,1,'x')},
    {key:'pb_ratio',        lbl:'Price / Book',    val:_pdpFmt(p.pb_ratio,2,'x')},
    {key:'debt_to_equity',  lbl:'Debt / Equity',   val:_pdpFmt(p.debt_to_equity,2)},
    {key:'current_ratio',   lbl:'Current Ratio',   val:_pdpFmt(p.current_ratio,2)},
    {key:'roe',             lbl:'Return on Equity',val:_pdpFmt(p.roe!=null?p.roe*100:null,1,'%')},
    {key:'net_margin',      lbl:'Net Margin',      val:_pdpFmt(p.net_margin!=null?p.net_margin*100:null,1,'%')},
    {key:'dividend_yield',  lbl:'Div. Yield',      val:_pdpFmt(p.dividend_yield,2,'%')},
    {key:'eps',             lbl:'EPS',             val:_pdpFmt(p.eps,2)},
    {key:'beta',            lbl:'Beta',            val:_pdpFmt(p.beta,2)},
    {key:'book_value',      lbl:'Book Value / Sh', val:_pdpFmt(p.book_value_per_share,2)},
    {key:'market_cap',      lbl:'Market Cap',      val:'$'+_pdpMarketCap(p.market_cap)},
  ];
  $('pdp-metrics-grid').innerHTML = metrics.map(m=>{
    const col = _pdpMetricColor(m.key, m.key==='pe_ratio'?p.pe_ratio:
      m.key==='forward_pe'?p.forward_pe:m.key==='pb_ratio'?p.pb_ratio:
      m.key==='debt_to_equity'?p.debt_to_equity:m.key==='current_ratio'?p.current_ratio:
      m.key==='roe'?p.roe:m.key==='net_margin'?p.net_margin:
      m.key==='dividend_yield'?p.dividend_yield:m.key==='beta'?p.beta:null);
    return `<div class="pdp-metric">
      <div class="pdp-metric-lbl">${m.lbl}</div>
      <div class="pdp-metric-val" style="${col}">${m.val}</div>
    </div>`;
  }).join('');

  // Graham checklist — data keys: criterion, detail, status
  const gCL = p.checklist||[];
  if(gCL.length){
    $('pdp-graham-sec').style.display='';
    $('pdp-graham-body').innerHTML = gCL.map(c=>{
      const s=_clStatus(c.status);
      const criterion = c.criterion||c.label||'';
      const detail    = c.detail||c.value||'';
      return `<tr>
        <td style="font-weight:600;color:var(--text)">${criterion}</td>
        <td style="font-family:'JetBrains Mono',monospace;font-size:.78rem;color:var(--muted)">${detail}</td>
        <td style="text-align:center"><span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:.68rem;font-weight:700;background:${s.bg};color:${s.col};white-space:nowrap">${s.icon}</span></td>
      </tr>`;
    }).join('');
  } else {
    $('pdp-graham-sec').style.display='none';
  }

  // Buffett checklist — data keys: criterion, detail, status
  const bCL = p.buffett_checklist||[];
  if(bCL.length){
    $('pdp-buffett-sec').style.display='';
    $('pdp-buffett-body').innerHTML = bCL.map(c=>{
      const s=_clStatus(c.status);
      const criterion = c.criterion||c.label||'';
      const detail    = c.detail||c.value||'';
      return `<tr>
        <td style="font-weight:600;color:var(--text)">${criterion}</td>
        <td style="font-family:'JetBrains Mono',monospace;font-size:.78rem;color:var(--muted)">${detail}</td>
        <td style="text-align:center"><span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:.68rem;font-weight:700;background:${s.bg};color:${s.col};white-space:nowrap">${s.icon}</span></td>
      </tr>`;
    }).join('');
  } else {
    $('pdp-buffett-sec').style.display='none';
  }

  // Scores summary + verdict
  const gPct = p.graham_score!=null&&p.max_score? Math.round((p.graham_score/(p.max_score/2))*100):null;
  const bPct = p.buffett_score!=null&&p.max_score? Math.round((p.buffett_score/(p.max_score/2))*100):null;
  $('pdp-verdict').innerHTML = `
    <div class="pdp-verdict" style="background:${gc.bg}22;border:1px solid ${accent}44">
      <div style="font-size:.68rem;font-weight:700;color:var(--muted);letter-spacing:.08em;margin-bottom:12px">SCORES BREAKDOWN</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>
          <div style="font-size:.65rem;color:var(--muted);margin-bottom:4px">GRAHAM SCORE</div>
          <div style="font-size:1.5rem;font-weight:800;color:#58a6ff">${p.graham_score??'—'}<span style="font-size:.85rem;color:var(--muted)"> pts</span></div>
          ${gPct!==null?`<div class="pdp-bar-track"><div class="pdp-bar-fill" style="width:${Math.min(gPct,100)}%;background:#58a6ff"></div></div>`:''}
        </div>
        <div>
          <div style="font-size:.65rem;color:var(--muted);margin-bottom:4px">BUFFETT SCORE</div>
          <div style="font-size:1.5rem;font-weight:800;color:#bc8cff">${p.buffett_score??'—'}<span style="font-size:.85rem;color:var(--muted)"> pts</span></div>
          ${bPct!==null?`<div class="pdp-bar-track"><div class="pdp-bar-fill" style="width:${Math.min(bPct,100)}%;background:#bc8cff"></div></div>`:''}
        </div>
      </div>
      <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border2)">
        <span style="font-size:.68rem;font-weight:700;color:var(--muted);letter-spacing:.08em">OVERALL GRADE &nbsp;</span>
        <span style="display:inline-block;padding:3px 14px;border-radius:6px;font-weight:800;font-size:1.1rem;background:${gc.bg};color:${gc.fg}">${p.grade||'—'}</span>
        <span style="font-size:.85rem;color:var(--muted);margin-left:10px">${p.score||0} / ${p.max_score||13} pts</span>
      </div>
    </div>`;
}
</script>
</body>
</html>"""

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser
    import time

    os.makedirs(LOG_DIR, exist_ok=True)

    print("=" * 55)
    print("  Intelligent Investor — Web Dashboard")
    print(f"  URL : http://localhost:{PORT}")
    print(f"  Dir : {AGENT_DIR}")
    print("=" * 55)

    # Open browser unless this looks like a launchd/background invocation
    if sys.stdout.isatty():
        def _open():
            time.sleep(1.2)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=_open, daemon=True).start()

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)
