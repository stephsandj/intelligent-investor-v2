#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# start_local.sh — Start Intelligent Investor V2 with local PostgreSQL
#
# Usage:
#   ./start_local.sh          # start both Postgres + Flask server
#   ./start_local.sh stop     # stop both
#   ./start_local.sh psql     # open a psql prompt
#   ./start_local.sh status   # check health
# ─────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PG_BIN="$HOME/Library/Application Support/PostgresApp/pg16/bin"
PG_DATA="$HOME/Library/Application Support/PostgresApp/data"
PG_LOG="$HOME/Library/Application Support/PostgresApp/postgres.log"
export DYLD_LIBRARY_PATH="$HOME/Library/Application Support/PostgresApp/pg16/lib"
DB_URL="postgresql://ii_user:PE702Mdo3Ld4wJjLPI8CPg7JLD54@localhost:5432/intelligentinvestor"

case "${1:-start}" in

  start)
    # ── 1. Start PostgreSQL if not running ──────────────────────
    if "$PG_BIN/pg_isready" -h localhost -p 5432 -q 2>/dev/null; then
      echo "✓  PostgreSQL already running"
    else
      echo "▶  Starting PostgreSQL..."
      "$PG_BIN/pg_ctl" start -D "$PG_DATA" -l "$PG_LOG" -o "-p 5432" -w
      sleep 1
      "$PG_BIN/pg_isready" -h localhost -p 5432 && echo "✓  PostgreSQL started" || {
        echo "✗  PostgreSQL failed to start — check $PG_LOG"
        exit 1
      }
    fi

    # ── 2. Kill any old Flask server ─────────────────────────────
    OLD_PID=$(lsof -ti :5051 2>/dev/null)
    [ -n "$OLD_PID" ] && kill "$OLD_PID" && sleep 1 && echo "✓  Stopped old server (PID $OLD_PID)"

    # ── 3. Start Flask server ────────────────────────────────────
    mkdir -p "$SCRIPT_DIR/logs"
    LOG_FILE="$SCRIPT_DIR/logs/dashboard_v2_5051.log"
    PORT=5051 DYLD_LIBRARY_PATH="$DYLD_LIBRARY_PATH" \
      /usr/bin/python3 "$SCRIPT_DIR/dashboard_v2.py" >> "$LOG_FILE" 2>&1 &
    FLASK_PID=$!
    echo "▶  Flask server started (PID $FLASK_PID)"

    sleep 2
    STATUS=$(curl -s http://localhost:5051/health | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    db = d['checks']['db']['status']
    jwt = d['checks']['jwt_secret']['status']
    print(f\"db={db}  jwt={jwt}  overall={d['status']}\")
except:
    print('health check failed')
" 2>/dev/null)
    echo "✓  Health: $STATUS"
    echo ""
    echo "   Dashboard → http://localhost:5051"
    echo "   Logs      → tail -f '$LOG_FILE'"
    ;;

  stop)
    echo "▶  Stopping Flask server..."
    PID=$(lsof -ti :5051 2>/dev/null)
    [ -n "$PID" ] && kill "$PID" && echo "✓  Flask stopped (PID $PID)" || echo "   Flask not running"

    echo "▶  Stopping PostgreSQL..."
    "$PG_BIN/pg_ctl" stop -D "$PG_DATA" -m fast 2>&1 | tail -1
    ;;

  psql)
    "$PG_BIN/psql" "$DB_URL" "${@:2}"
    ;;

  status)
    echo "=== PostgreSQL ==="
    "$PG_BIN/pg_isready" -h localhost -p 5432 || echo "Not running"
    echo ""
    echo "=== Flask /health ==="
    curl -s http://localhost:5051/health | python3 -m json.tool 2>/dev/null || echo "Not running"
    ;;

  *)
    echo "Usage: $0 [start|stop|psql|status]"
    exit 1
    ;;
esac
