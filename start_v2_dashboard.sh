#!/bin/bash
# Start PostgreSQL via pg_ctl, wait for it, then start V2 dashboard

set -e

# PostgreSQL default locations (PostgresApp)
PG_BIN="/Users/stephanesandjong/Library/Application Support/PostgresApp/pg16/bin"
PG_DATA="/Users/stephanesandjong/Library/Application Support/PostgresApp/data"

# 1. Start PostgreSQL if not already running
if ! pgrep -q "postgres: " 2>/dev/null; then
    if [ -d "$PG_DATA" ] && [ -x "$PG_BIN/pg_ctl" ]; then
        echo "[$(date)] Starting PostgreSQL via pg_ctl..."
        "$PG_BIN/pg_ctl" -D "$PG_DATA" -l /tmp/postgres.log start 2>/dev/null || true
        # Wait up to 30 seconds for Postgres to be ready
        for i in {1..30}; do
            if "$PG_BIN/psql" -U postgres -d postgres -c "SELECT 1" >/dev/null 2>&1; then
                echo "[$(date)] PostgreSQL is ready"
                break
            fi
            echo "[$(date)] Waiting for PostgreSQL... ($i/30)"
            sleep 1
        done
    fi
fi

# 2. Start V2 Flask dashboard
cd "/Users/stephanesandjong/Library/Application Support/IntelligentInvestorAgentV2"
exec /usr/bin/python3 dashboard_v2.py
