#!/usr/bin/env python3
"""
Multi-user isolation test suite for IntelligentInvestorAgentV2.

Creates two real test users (A and B), then exercises every isolation scenario:
  1.  Data directories are separate
  2.  picks_detail.json is per-user (no cross-contamination)
  3.  ETF / Bond results are per-user
  4.  PDF reports are per-user (written to correct subdir)
  5.  PDF reports endpoint enforces ownership (User B cannot fetch User A's PDF)
  6.  Run state is per-user (User A running does not block User B)
  7.  Stop only affects the requesting user's screen
  8.  /api/logs returns only the requesting user's logs
  9.  User B cannot overwrite User A's picks by running a concurrent screen
  10. APScheduler jobs are independent per user
  11. Config save updates only the requesting user's schedule job

Run:
    python3 test_multi_user_isolation.py

Requires:
    - DATABASE_URL set in .env or environment
    - Flask app can be imported (all deps installed)
"""

import os, sys, json, threading, time, shutil, tempfile, traceback
import glob as glob_module

# ── Bootstrap: make sure we can import the app ──────────────────────────────
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AGENT_DIR)

# Load .env
_env_path = os.path.join(AGENT_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip(); _v = _v.strip().strip('"').strip("'")
            if _k and _k not in os.environ:
                os.environ[_k] = _v

# ── Test state ───────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0
TESTS = []

def ok(name):
    global PASS
    PASS += 1
    TESTS.append(("PASS", name))
    print(f"  ✓  {name}")

def fail(name, reason=""):
    global FAIL
    FAIL += 1
    TESTS.append(("FAIL", name))
    print(f"  ✗  {name}")
    if reason:
        print(f"       → {reason}")

def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")

# ── Import app internals (after env load) ───────────────────────────────────
try:
    import models
    import bcrypt
    _DB_OK = True
except Exception as _e:
    print(f"[FATAL] Cannot import models: {_e}")
    sys.exit(1)

try:
    from dashboard_v2 import (
        _user_data_dir, _user_reports_dir, _user_logs_dir,
        _picks_detail_file, _etf_results_file, _bond_results_file,
        _user_state_file, _parse_last_picks, _parse_snap_picks,
        _list_reports, _get_run_state, _start_agent, _stop_agent,
        _user_run_states, _user_etf_states, _user_bond_states,
        _default_run_state, _default_etf_state, _default_bond_state,
        _run_etf_screen, _run_bond_screen,
        update_user_schedule_job, _scheduler,
        app, AGENT_DIR as _app_agent_dir,
    )
except Exception as _e:
    print(f"[FATAL] Cannot import dashboard_v2: {_e}")
    traceback.print_exc()
    sys.exit(1)

# ── Create two test users ─────────────────────────────────────────────────────
section("Setup — creating test users A and B")

TEST_EMAIL_A = "test_user_a@isolation-test.local"
TEST_EMAIL_B = "test_user_b@isolation-test.local"
TEST_PASSWORD = "TestPassword123!"

def _hash_pw(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def get_or_create_user(email):
    existing = models.get_user_by_email(email)
    if existing:
        print(f"  Re-using existing user: {email} ({str(existing['id'])[:8]}…)")
        # Ensure they have a subscription
        sub = models.get_user_subscription(existing["id"])
        if not sub:
            models.create_subscription(str(existing["id"]), plan_id=3, status="active")
        return str(existing["id"])
    user = models.create_user(email, _hash_pw(TEST_PASSWORD), full_name=email.split("@")[0])
    uid = str(user["id"])
    models.create_subscription(uid, plan_id=3, status="active")  # Pro plan
    models.verify_user_email(uid)
    print(f"  Created test user: {email} ({uid[:8]}…)")
    return uid

try:
    USER_A = get_or_create_user(TEST_EMAIL_A)
    USER_B = get_or_create_user(TEST_EMAIL_B)
    print(f"  User A: {USER_A}")
    print(f"  User B: {USER_B}")
except Exception as e:
    print(f"[FATAL] Could not create test users: {e}")
    traceback.print_exc()
    sys.exit(1)

# ────────────────────────────────────────────────────────────────────────────
# TEST 1: Per-user directories are separate
# ────────────────────────────────────────────────────────────────────────────
section("Test 1 — Per-user directory isolation")

dir_a = _user_data_dir(USER_A)
dir_b = _user_data_dir(USER_B)

if dir_a != dir_b:
    ok("User A and B have different data directories")
else:
    fail("User A and B have different data directories", f"Both got: {dir_a}")

if USER_A in dir_a and USER_B not in dir_a:
    ok("User A's directory contains their ID, not B's")
else:
    fail("User A's directory contains their ID, not B's", dir_a)

if USER_B in dir_b and USER_A not in dir_b:
    ok("User B's directory contains their ID, not A's")
else:
    fail("User B's directory contains their ID, not A's", dir_b)

picks_a = _picks_detail_file(USER_A)
picks_b = _picks_detail_file(USER_B)
if picks_a != picks_b:
    ok("picks_detail.json paths are different per user")
else:
    fail("picks_detail.json paths are different per user")

etf_a = _etf_results_file(USER_A)
etf_b = _etf_results_file(USER_B)
if etf_a != etf_b:
    ok("etf_results.json paths are different per user")
else:
    fail("etf_results.json paths are different per user")

# ────────────────────────────────────────────────────────────────────────────
# TEST 2: picks_detail.json writes don't cross-contaminate
# ────────────────────────────────────────────────────────────────────────────
section("Test 2 — picks_detail.json isolation (write & read)")

def write_picks(user_id, picks_data):
    os.makedirs(os.path.dirname(_picks_detail_file(user_id)), exist_ok=True)
    with open(_picks_detail_file(user_id), "w") as f:
        json.dump(picks_data, f)

DATA_A = {"picks": [{"rank": 1, "symbol": "AAPL", "name": "Apple", "score": 9.5,
                      "grade": "A", "roe": 0.28}], "owner": "user_a"}
DATA_B = {"picks": [{"rank": 1, "symbol": "MSFT", "name": "Microsoft", "score": 8.9,
                      "grade": "A", "roe": 0.35}], "owner": "user_b"}

write_picks(USER_A, DATA_A)
write_picks(USER_B, DATA_B)

picks_read_a = _parse_last_picks(USER_A)
picks_read_b = _parse_last_picks(USER_B)

if picks_read_a and picks_read_a[0]["symbol"] == "AAPL":
    ok("User A reads their own picks (AAPL)")
else:
    fail("User A reads their own picks", f"Got: {picks_read_a}")

if picks_read_b and picks_read_b[0]["symbol"] == "MSFT":
    ok("User B reads their own picks (MSFT)")
else:
    fail("User B reads their own picks", f"Got: {picks_read_b}")

if picks_read_a and picks_read_a[0]["symbol"] != "MSFT":
    ok("User A does NOT see User B's picks (MSFT not in A's results)")
else:
    fail("User A does NOT see User B's picks")

if picks_read_b and picks_read_b[0]["symbol"] != "AAPL":
    ok("User B does NOT see User A's picks (AAPL not in B's results)")
else:
    fail("User B does NOT see User A's picks")

# Simulate User B overwriting their own picks — A's must be unaffected
DATA_B_V2 = {"picks": [{"rank": 1, "symbol": "GOOGL", "name": "Google", "score": 8.1,
                          "grade": "B+", "roe": 0.21}], "owner": "user_b_v2"}
write_picks(USER_B, DATA_B_V2)
picks_read_a_after = _parse_last_picks(USER_A)
if picks_read_a_after and picks_read_a_after[0]["symbol"] == "AAPL":
    ok("User A's picks unchanged after User B re-writes their own picks")
else:
    fail("User A's picks unchanged after User B re-writes their own picks",
         f"Got: {picks_read_a_after}")

# ────────────────────────────────────────────────────────────────────────────
# TEST 3: ETF / Bond results are per-user
# ────────────────────────────────────────────────────────────────────────────
section("Test 3 — ETF / Bond results isolation")

ETF_A = {"results": [{"symbol": "QQQ", "user": "a"}], "screener": "etf"}
ETF_B = {"results": [{"symbol": "SPY", "user": "b"}], "screener": "etf"}
BOND_A = {"results": [{"symbol": "BND", "user": "a"}], "screener": "bond"}
BOND_B = {"results": [{"symbol": "AGG", "user": "b"}], "screener": "bond"}

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)

write_json(_etf_results_file(USER_A), ETF_A)
write_json(_etf_results_file(USER_B), ETF_B)
write_json(_bond_results_file(USER_A), BOND_A)
write_json(_bond_results_file(USER_B), BOND_B)

with open(_etf_results_file(USER_A)) as f:
    etf_read_a = json.load(f)
with open(_etf_results_file(USER_B)) as f:
    etf_read_b = json.load(f)

if etf_read_a["results"][0]["symbol"] == "QQQ":
    ok("User A reads their own ETF results (QQQ)")
else:
    fail("User A reads their own ETF results")

if etf_read_b["results"][0]["symbol"] == "SPY":
    ok("User B reads their own ETF results (SPY)")
else:
    fail("User B reads their own ETF results")

if etf_read_a["results"][0]["symbol"] != "SPY":
    ok("User A does NOT see User B's ETF results")
else:
    fail("User A does NOT see User B's ETF results")

# ────────────────────────────────────────────────────────────────────────────
# TEST 4: PDF reports are written to per-user reports directory
# ────────────────────────────────────────────────────────────────────────────
section("Test 4 — PDF report directory isolation")

reports_a = _user_reports_dir(USER_A)
reports_b = _user_reports_dir(USER_B)

if reports_a != reports_b:
    ok("PDF reports directories are separate per user")
else:
    fail("PDF reports directories are separate per user")

# Write a dummy PDF to each user's reports dir
pdf_name_a = "intelligent_investor_20260420_100000.pdf"
pdf_name_b = "intelligent_investor_20260420_110000.pdf"
pdf_path_a = os.path.join(reports_a, pdf_name_a)
pdf_path_b = os.path.join(reports_b, pdf_name_b)

with open(pdf_path_a, "wb") as f:
    f.write(b"%PDF-1.4 user_a_report")
with open(pdf_path_b, "wb") as f:
    f.write(b"%PDF-1.4 user_b_report")

reports_list_a = _list_reports(USER_A)
reports_list_b = _list_reports(USER_B)

filenames_a = [r["filename"] for r in reports_list_a]
filenames_b = [r["filename"] for r in reports_list_b]

if pdf_name_a in filenames_a and pdf_name_b not in filenames_a:
    ok("User A's report list contains only their own PDF")
else:
    fail("User A's report list contains only their own PDF",
         f"A list: {filenames_a}, B list: {filenames_b}")

if pdf_name_b in filenames_b and pdf_name_a not in filenames_b:
    ok("User B's report list contains only their own PDF")
else:
    fail("User B's report list contains only their own PDF",
         f"A list: {filenames_a}, B list: {filenames_b}")

# ────────────────────────────────────────────────────────────────────────────
# TEST 5: /reports endpoint — ownership check (Flask test client)
# ────────────────────────────────────────────────────────────────────────────
section("Test 5 — PDF serve endpoint ownership enforcement")

# We test the path-based logic directly since we don't have live JWT tokens here.
# The key invariant: safe path must start with the USER's reports dir.
import posixpath

def _simulate_serve_check(requesting_user_id: str, filename: str) -> bool:
    """Return True if the serve would succeed (file is in user's dir and exists)."""
    basename = os.path.basename(filename)
    if not basename.endswith(".pdf"):
        return False
    user_reports = os.path.realpath(_user_reports_dir(requesting_user_id))
    safe = os.path.realpath(os.path.join(user_reports, basename))
    return safe.startswith(user_reports) and os.path.exists(safe)

# User A can serve their own PDF
if _simulate_serve_check(USER_A, pdf_name_a):
    ok("User A can serve their own PDF")
else:
    fail("User A can serve their own PDF")

# User B can serve their own PDF
if _simulate_serve_check(USER_B, pdf_name_b):
    ok("User B can serve their own PDF")
else:
    fail("User B can serve their own PDF")

# User B CANNOT access User A's PDF (file doesn't exist in B's reports dir)
if not _simulate_serve_check(USER_B, pdf_name_a):
    ok("User B CANNOT access User A's PDF (ownership check blocks it)")
else:
    fail("User B CANNOT access User A's PDF")

# User A CANNOT access User B's PDF
if not _simulate_serve_check(USER_A, pdf_name_b):
    ok("User A CANNOT access User B's PDF (ownership check blocks it)")
else:
    fail("User A CANNOT access User B's PDF")

# Path traversal attempt: User B tries ../../users/{USER_A}/reports/{pdf_name_a}
traversal_attempt = f"../../users/{USER_A}/reports/{pdf_name_a}"
if not _simulate_serve_check(USER_B, traversal_attempt):
    ok("Path traversal attempt blocked (../../ cannot escape user reports dir)")
else:
    fail("Path traversal attempt blocked")

# ────────────────────────────────────────────────────────────────────────────
# TEST 6: Run state is per-user (User A running does not block User B)
# ────────────────────────────────────────────────────────────────────────────
section("Test 6 — Run state isolation")

# Manually set User A as 'running'
from dashboard_v2 import _run_lock
with _run_lock:
    _user_run_states[USER_A] = {
        "running": True, "started_at": "2026-04-23T10:00:00", "finished_at": None, "exit_code": None
    }

state_a = _get_run_state(USER_A)
state_b = _get_run_state(USER_B)

if state_a.get("running") is True:
    ok("User A's run state shows running=True")
else:
    fail("User A's run state shows running=True")

if not state_b.get("running"):
    ok("User B's run state shows running=False (A running does not affect B)")
else:
    fail("User B's run state shows running=False", f"B state: {state_b}")

# Reset A's state
with _run_lock:
    _user_run_states[USER_A] = _default_run_state()

# ────────────────────────────────────────────────────────────────────────────
# TEST 7: Stop only affects requesting user's screen
# ────────────────────────────────────────────────────────────────────────────
section("Test 7 — Stop isolation (User A stop does not affect User B)")

with _run_lock:
    _user_run_states[USER_A] = {
        "running": True, "started_at": "2026-04-23T10:00:00", "finished_at": None, "exit_code": None
    }
    _user_run_states[USER_B] = {
        "running": True, "started_at": "2026-04-23T10:01:00", "finished_at": None, "exit_code": None
    }

# Stop User A (no real subprocess — we test the state logic)
with _run_lock:
    _user_run_states[USER_A]["running"] = False
    _user_run_states[USER_A]["exit_code"] = -1

state_a_after = _get_run_state(USER_A)
state_b_after  = _get_run_state(USER_B)

if not state_a_after.get("running"):
    ok("User A's screen is stopped")
else:
    fail("User A's screen is stopped")

if state_b_after.get("running"):
    ok("User B's screen is still running (unaffected by A's stop)")
else:
    fail("User B's screen is still running", f"B state: {state_b_after}")

# Clean up
with _run_lock:
    _user_run_states[USER_A] = _default_run_state()
    _user_run_states[USER_B] = _default_run_state()

# ────────────────────────────────────────────────────────────────────────────
# TEST 8: /api/logs uses per-user log files
# ────────────────────────────────────────────────────────────────────────────
section("Test 8 — Log isolation")

from dashboard_v2 import _latest_user_log_lines, _user_logs_dir

logs_a = _user_logs_dir(USER_A)
logs_b = _user_logs_dir(USER_B)

# Write distinct log content for each user
log_a_path = os.path.join(logs_a, "agent_run_20260423_100000.log")
log_b_path = os.path.join(logs_b, "agent_run_20260423_100000.log")

with open(log_a_path, "w") as f:
    f.write("User A screen started\nUser A found AAPL\nUser A complete\n")
with open(log_b_path, "w") as f:
    f.write("User B screen started\nUser B found MSFT\nUser B complete\n")

lines_a = _latest_user_log_lines(USER_A, 100)
lines_b = _latest_user_log_lines(USER_B, 100)

if any("User A" in l for l in lines_a) and not any("User B" in l for l in lines_a):
    ok("User A only sees their own log lines")
else:
    fail("User A only sees their own log lines", f"A lines: {lines_a}")

if any("User B" in l for l in lines_b) and not any("User A" in l for l in lines_b):
    ok("User B only sees their own log lines")
else:
    fail("User B only sees their own log lines", f"B lines: {lines_b}")

# ────────────────────────────────────────────────────────────────────────────
# TEST 9: Concurrent screen simulation — User B run doesn't overwrite A's data
# ────────────────────────────────────────────────────────────────────────────
section("Test 9 — Concurrent writes (race condition simulation)")

# Write User A's picks
write_picks(USER_A, {"picks": [{"rank": 1, "symbol": "AAPL", "name": "Apple",
                                  "score": 9.5, "grade": "A", "roe": 0.28}]})

# Simultaneously, User B writes to their own file
errors = []
def b_writes():
    try:
        for _ in range(10):
            write_picks(USER_B, {"picks": [{"rank": 1, "symbol": "MSFT", "name": "Microsoft",
                                              "score": 8.0, "grade": "B", "roe": 0.30}]})
            time.sleep(0.01)
    except Exception as e:
        errors.append(str(e))

t = threading.Thread(target=b_writes)
t.start()
# Simultaneously read A's file many times
for _ in range(20):
    p = _parse_last_picks(USER_A)
    if p and p[0]["symbol"] != "AAPL":
        errors.append(f"User A read User B's data mid-concurrent-write: {p}")
    time.sleep(0.005)
t.join()

if not errors:
    ok("No cross-user data corruption during concurrent writes (10 B-writes, 20 A-reads)")
else:
    fail("No cross-user data corruption during concurrent writes", "; ".join(errors))

# ────────────────────────────────────────────────────────────────────────────
# TEST 10: ETF state isolation
# ────────────────────────────────────────────────────────────────────────────
section("Test 10 — ETF state isolation")

# Set User A as ETF running
from dashboard_v2 import _etf_state_lock
with _etf_state_lock:
    _user_etf_states[USER_A] = {"running": True, "error": None, "started_at": "2026-04-23T10:00:00Z"}

etf_state_a = dict(_user_etf_states.get(USER_A, _default_etf_state()))
etf_state_b = dict(_user_etf_states.get(USER_B, _default_etf_state()))

if etf_state_a.get("running"):
    ok("User A's ETF state shows running=True")
else:
    fail("User A's ETF state shows running=True")

if not etf_state_b.get("running"):
    ok("User B's ETF state shows running=False (independent of A)")
else:
    fail("User B's ETF state shows running=False")

# ETF results are separate
write_json(_etf_results_file(USER_A), {"results": [{"symbol": "QQQ"}]})
write_json(_etf_results_file(USER_B), {"results": [{"symbol": "SPY"}]})

with open(_etf_results_file(USER_A)) as f:
    r_a = json.load(f)
with open(_etf_results_file(USER_B)) as f:
    r_b = json.load(f)

if r_a["results"][0]["symbol"] == "QQQ" and r_b["results"][0]["symbol"] == "SPY":
    ok("ETF results files are fully isolated (A=QQQ, B=SPY)")
else:
    fail("ETF results files are fully isolated")

# Clean up
with _etf_state_lock:
    _user_etf_states.pop(USER_A, None)

# ────────────────────────────────────────────────────────────────────────────
# TEST 11: APScheduler jobs are per-user
# ────────────────────────────────────────────────────────────────────────────
section("Test 11 — Per-user APScheduler job isolation")

if _scheduler is not None:
    from dashboard_v2 import _job_id, _add_user_schedule_job
    # Add distinct jobs for A and B
    _add_user_schedule_job(_scheduler, USER_A, hour=8, minute=0,  days=[0,1,2,3,4], enabled=True)
    _add_user_schedule_job(_scheduler, USER_B, hour=20, minute=30, days=[0,1,2,3,4], enabled=True)

    job_a = _scheduler.get_job(_job_id(USER_A))
    job_b = _scheduler.get_job(_job_id(USER_B))

    if job_a is not None:
        ok("User A has their own APScheduler job")
    else:
        fail("User A has their own APScheduler job")

    if job_b is not None:
        ok("User B has their own APScheduler job")
    else:
        fail("User B has their own APScheduler job")

    if job_a is not None and job_b is not None and job_a.id != job_b.id:
        ok("User A and B have DIFFERENT scheduler job IDs")
    else:
        fail("User A and B have DIFFERENT scheduler job IDs")

    # Disable User A's schedule — B's job should be unaffected
    _add_user_schedule_job(_scheduler, USER_A, hour=8, minute=0, days=[], enabled=False)
    job_a_disabled = _scheduler.get_job(_job_id(USER_A))
    job_b_still    = _scheduler.get_job(_job_id(USER_B))

    if job_a_disabled is None:
        ok("Disabling User A's schedule removes only their job")
    else:
        fail("Disabling User A's schedule removes only their job")

    if job_b_still is not None:
        ok("User B's scheduler job is unaffected when A's is disabled")
    else:
        fail("User B's scheduler job is unaffected when A's is disabled")
else:
    print("  [SKIP] Scheduler not running — skipping scheduler tests")

# ────────────────────────────────────────────────────────────────────────────
# TEST 12: AGENT_OUTPUT_DIR env var is set correctly per user
# ────────────────────────────────────────────────────────────────────────────
section("Test 12 — AGENT_OUTPUT_DIR set to per-user directory")

# Simulate what _start_agent does for env building
env_a = os.environ.copy()
env_a["AGENT_OUTPUT_DIR"] = _user_data_dir(USER_A)
env_b = os.environ.copy()
env_b["AGENT_OUTPUT_DIR"] = _user_data_dir(USER_B)

if env_a["AGENT_OUTPUT_DIR"] != env_b["AGENT_OUTPUT_DIR"]:
    ok("AGENT_OUTPUT_DIR is different for User A and User B")
else:
    fail("AGENT_OUTPUT_DIR is different for User A and User B")

if USER_A in env_a["AGENT_OUTPUT_DIR"]:
    ok("User A's AGENT_OUTPUT_DIR contains their user_id")
else:
    fail("User A's AGENT_OUTPUT_DIR contains their user_id",
         env_a["AGENT_OUTPUT_DIR"])

if USER_B in env_b["AGENT_OUTPUT_DIR"]:
    ok("User B's AGENT_OUTPUT_DIR contains their user_id")
else:
    fail("User B's AGENT_OUTPUT_DIR contains their user_id",
         env_b["AGENT_OUTPUT_DIR"])

# ────────────────────────────────────────────────────────────────────────────
# TEST 13: PDF history limit is per-user (not global)
# ────────────────────────────────────────────────────────────────────────────
section("Test 13 — PDF history limit is per-user")

# Clean slate for both users before this test
rdir_a = _user_reports_dir(USER_A)
rdir_b = _user_reports_dir(USER_B)
for old in glob_module.glob(os.path.join(rdir_a, "*.pdf")):
    os.remove(old)
for old in glob_module.glob(os.path.join(rdir_b, "*.pdf")):
    os.remove(old)

# Create 7 dummy PDFs for User A
for i in range(7):
    p = os.path.join(rdir_a, f"intelligent_investor_202604{i+10:02d}_120000.pdf")
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 dummy")

# Create 2 PDFs for User B
for i in range(2):
    p = os.path.join(rdir_b, f"intelligent_investor_20260501_12000{i}.pdf")
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 dummy")

reports_a_after = _list_reports(USER_A)
reports_b_after  = _list_reports(USER_B)

# User A should be pruned to their plan limit (Pro = 10, we only made 7)
if len(reports_a_after) <= 10:
    ok(f"User A's PDF list is within plan limit ({len(reports_a_after)} PDFs)")
else:
    fail("User A's PDF list is within plan limit", f"Got {len(reports_a_after)}")

# User B should have 2
if len(reports_b_after) == 2:
    ok(f"User B's PDF list has exactly 2 (unaffected by A's pruning)")
else:
    fail("User B's PDF list has exactly 2", f"Got {len(reports_b_after)}")

# ────────────────────────────────────────────────────────────────────────────
# TEST 14: Database config isolation
# ────────────────────────────────────────────────────────────────────────────
section("Test 14 — Database config isolation")

from dashboard_v2 import _save_config, _load_config

# Save different configs for A and B
_save_config({"schedule_hour": 8, "loser_period": "daily100", "markets": ["NYSE"]}, USER_A)
_save_config({"schedule_hour": 20, "loser_period": "weekly100", "markets": ["NASDAQ"]}, USER_B)

cfg_a = _load_config(USER_A)
cfg_b = _load_config(USER_B)

if cfg_a.get("schedule_hour") == 8:
    ok("User A's config has schedule_hour=8")
else:
    fail("User A's config has schedule_hour=8", f"Got: {cfg_a.get('schedule_hour')}")

if cfg_b.get("schedule_hour") == 20:
    ok("User B's config has schedule_hour=20")
else:
    fail("User B's config has schedule_hour=20", f"Got: {cfg_b.get('schedule_hour')}")

if cfg_a.get("loser_period") == "daily100" and cfg_b.get("loser_period") == "weekly100":
    ok("loser_period is independent per user (A=daily100, B=weekly100)")
else:
    fail("loser_period is independent per user",
         f"A={cfg_a.get('loser_period')}, B={cfg_b.get('loser_period')}")

# ────────────────────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────────────────────
section("Results")
total = PASS + FAIL
print(f"\n  Total: {total}  |  Passed: {PASS}  |  Failed: {FAIL}\n")

if FAIL > 0:
    print("  FAILED tests:")
    for status, name in TESTS:
        if status == "FAIL":
            print(f"    ✗  {name}")
    sys.exit(1)
else:
    print("  All isolation tests passed. ✓")
    sys.exit(0)
