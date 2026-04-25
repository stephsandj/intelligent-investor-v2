-- ─────────────────────────────────────────────────────────────────
-- PostgreSQL Docker init — runs ONCE on first container start
-- when the data directory is empty (fresh volume).
--
-- This script runs as POSTGRES_USER (ii_user, which is the DB
-- superuser inside this isolated container).
--
-- The application schema is created by models.init_db() on first
-- app startup — no need to run setup.sql here.
-- ─────────────────────────────────────────────────────────────────

-- Required for gen_random_uuid() used throughout the schema
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- pg_stat_statements is useful for query analysis (optional)
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
