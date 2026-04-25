-- =============================================================================
-- setup.sql — Intelligent Investor Agent V2
-- Complete PostgreSQL schema setup (UUID primary keys throughout)
--
-- Usage (run as superuser or the postgres OS user):
--   psql -U postgres -f setup.sql
--
-- Re-running on an existing database:
--   All CREATE TABLE statements use IF NOT EXISTS — safe to re-run.
--   Column additions require ALTER TABLE migrations run separately.
--   For fresh installs: all tables are created with correct types on first run.
--
-- Replace REPLACE_ME_DB_PASSWORD with the actual password from .env
-- =============================================================================

-- =============================================================================
-- SECTION 1: Database and user creation (run as postgres superuser)
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ii_user') THEN
        CREATE USER ii_user WITH PASSWORD 'REPLACE_ME_DB_PASSWORD';
    END IF;
END
$$;

SELECT 'CREATE DATABASE intelligentinvestor OWNER ii_user'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'intelligentinvestor')\gexec

GRANT ALL PRIVILEGES ON DATABASE intelligentinvestor TO ii_user;

\connect intelligentinvestor

GRANT ALL ON SCHEMA public TO ii_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ii_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ii_user;


-- =============================================================================
-- SECTION 2: Extensions
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), crypt()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- trigram index for email search


-- =============================================================================
-- SECTION 3: PLANS table (SERIAL integer PK — referenced by integer plan_id everywhere)
-- =============================================================================

CREATE TABLE IF NOT EXISTS plans (
    id                  SERIAL          PRIMARY KEY,
    name                VARCHAR(64)     NOT NULL UNIQUE,
    display_name        VARCHAR(128)    NOT NULL,
    description         TEXT,

    -- Run limits (column names match models.py queries)
    runs_per_day        INTEGER,                            -- NULL = unlimited
    max_ai_picks        INTEGER,                            -- NULL = unlimited
    max_pdf_history     INTEGER,                            -- NULL = unlimited
    trial_days          INTEGER         NOT NULL DEFAULT 0,

    -- Feature flags (flexible JSONB blob, read by frontend)
    features            JSONB           NOT NULL DEFAULT '{}',

    -- Pricing (in USD cents to avoid float rounding; displayed as dollars)
    price_monthly       NUMERIC(10, 2),
    price_yearly        NUMERIC(10, 2),

    -- Stripe Price IDs (populated from .env / Stripe dashboard)
    stripe_price_monthly VARCHAR(128),
    stripe_price_yearly  VARCHAR(128),

    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    sort_order          INTEGER         NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 4: USERS table (UUID primary key)
-- =============================================================================

CREATE TABLE IF NOT EXISTS users (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    email               VARCHAR(255)    NOT NULL UNIQUE,
    password_hash       VARCHAR(255)    NOT NULL,

    -- Profile
    full_name           VARCHAR(255),
    timezone            VARCHAR(64)     NOT NULL DEFAULT 'UTC',
    email_verified      BOOLEAN         NOT NULL DEFAULT FALSE,
    email_verify_token  VARCHAR(128),
    email_verify_sent_at TIMESTAMPTZ,

    -- Password reset
    reset_token         VARCHAR(128),
    reset_token_expires TIMESTAMPTZ,

    -- Admin flag (separate admin_users table is canonical; this is a cache)
    is_admin            BOOLEAN         NOT NULL DEFAULT FALSE,

    -- Stripe customer (stored on user for quick lookup)
    stripe_customer_id  VARCHAR(128)    UNIQUE,

    -- Metadata
    last_login_at       TIMESTAMPTZ,
    last_login_ip       INET,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 5: ADMIN_USERS table (UUID FK to users)
-- =============================================================================

CREATE TABLE IF NOT EXISTS admin_users (
    user_id     UUID    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    role        VARCHAR(20)     NOT NULL DEFAULT 'admin'
);


-- =============================================================================
-- SECTION 6: USER_CONFIGS table (UUID PK — one row per user)
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_configs (
    user_id             UUID            PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    loser_period        VARCHAR(20)     NOT NULL DEFAULT 'daily100',
    markets             TEXT[]          NOT NULL DEFAULT ARRAY['NYSE','NASDAQ'],
    stock_geography     VARCHAR(20)     NOT NULL DEFAULT 'usa',
    email_enabled       BOOLEAN         NOT NULL DEFAULT FALSE,
    pdf_enabled         BOOLEAN         NOT NULL DEFAULT TRUE,
    email_address       VARCHAR(255),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 7: SUBSCRIPTIONS table (UUID PK, UUID FK to users)
-- =============================================================================

CREATE TABLE IF NOT EXISTS subscriptions (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id                 INTEGER         NOT NULL REFERENCES plans(id),

    -- Status: 'trial' | 'active' | 'inactive' | 'canceled' | 'past_due'
    --          'pending_payment' | 'paused'
    status                  VARCHAR(32)     NOT NULL DEFAULT 'trial',
    billing_cycle           VARCHAR(16),    -- 'monthly' | 'yearly' | NULL for trial

    -- Timeline
    started_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    expires_at              TIMESTAMPTZ,
    trial_ends_at           TIMESTAMPTZ,

    -- Payment method (for display)
    payment_method          VARCHAR(50),

    -- Stripe references (used by billing.py webhook handlers)
    stripe_customer_id      VARCHAR(255),
    stripe_sub_id           VARCHAR(255),
    stripe_price_id         VARCHAR(128),
    cancel_at_period_end    BOOLEAN         NOT NULL DEFAULT FALSE,
    canceled_at             TIMESTAMPTZ,

    -- Activation tracking (set when admin or Stripe activates)
    activated_by            UUID            REFERENCES users(id),
    activated_at            TIMESTAMPTZ,

    -- Notes (admin use)
    notes                   TEXT,

    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT subscriptions_user_unique UNIQUE (user_id)
    -- One active subscription per user. Historical records go to subscription_history.
);


-- =============================================================================
-- SECTION 8: SUBSCRIPTION_HISTORY table (UUID PK, UUID FK)
-- =============================================================================

CREATE TABLE IF NOT EXISTS subscription_history (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id         INTEGER         NOT NULL REFERENCES plans(id),
    status          VARCHAR(32)     NOT NULL,
    billing_cycle   VARCHAR(16),
    started_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    stripe_sub_id   VARCHAR(128),
    notes           TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 9: DAILY_RUN_COUNTS table (composite UUID+date PK, matches models.py)
-- =============================================================================

CREATE TABLE IF NOT EXISTS daily_run_counts (
    user_id     UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_date    DATE        NOT NULL DEFAULT CURRENT_DATE,
    count       INTEGER     NOT NULL DEFAULT 0,   -- column name 'count' matches models.py

    PRIMARY KEY (user_id, run_date)
);


-- =============================================================================
-- SECTION 10: SCREENING_RUNS table (UUID PK, UUID FK)
-- =============================================================================

CREATE TABLE IF NOT EXISTS screening_runs (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Input parameters
    run_date        DATE            NOT NULL DEFAULT CURRENT_DATE,
    loser_period    VARCHAR(20),                        -- 'daily100' | 'daily500' | etc.
    markets         TEXT[],                             -- e.g. ['NYSE','NASDAQ']
    symbols         TEXT[],                             -- ticker symbols actually screened
    criteria        JSONB,                              -- screening filter criteria used

    -- Results
    completed       BOOLEAN         NOT NULL DEFAULT FALSE,
    picks_count     INTEGER         NOT NULL DEFAULT 0,
    status          VARCHAR(32)     NOT NULL DEFAULT 'pending',
    -- Valid: 'pending' | 'running' | 'completed' | 'failed'

    results         JSONB,                              -- full structured results
    summary         TEXT,                               -- AI-generated narrative summary
    pdf_path        VARCHAR(512),                       -- path to generated PDF report
    error_message   TEXT,

    -- Timeline
    started_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    stopped_at      TIMESTAMPTZ,                        -- set on finish or stop
    completed_at    TIMESTAMPTZ,
    duration_ms     INTEGER,

    ip_address      INET,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 11: WATCHLISTS table (UUID PK, UUID FK)
-- =============================================================================

CREATE TABLE IF NOT EXISTS watchlists (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(128)    NOT NULL DEFAULT 'My Watchlist',
    symbols     TEXT[]          NOT NULL DEFAULT '{}',
    is_default  BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 12: EMAIL_ALERTS table (UUID PK, UUID FK)
-- =============================================================================

CREATE TABLE IF NOT EXISTS email_alerts (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    alert_type      VARCHAR(64)     NOT NULL,   -- 'run_complete' | 'weekly_digest' | 'trial_expiring'
    subject         VARCHAR(512),
    body_preview    TEXT,
    status          VARCHAR(32)     NOT NULL DEFAULT 'queued',
    -- Valid: 'queued' | 'sent' | 'failed'
    sent_at         TIMESTAMPTZ,
    error_message   TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 13: STRIPE_EVENTS table (idempotency store for Stripe webhooks)
-- =============================================================================

CREATE TABLE IF NOT EXISTS stripe_events (
    id              BIGSERIAL       PRIMARY KEY,
    stripe_event_id VARCHAR(128)    NOT NULL UNIQUE,
    event_type      VARCHAR(128)    NOT NULL,
    payload         JSONB           NOT NULL,
    processed       BOOLEAN         NOT NULL DEFAULT FALSE,
    processed_at    TIMESTAMPTZ,
    error_message   TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 14: AUDIT_LOG table (BIGSERIAL PK, UUID FKs — merged from both schemas)
-- =============================================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL       PRIMARY KEY,

    -- Who did it (NULL = system-generated event)
    actor_id        UUID            REFERENCES users(id) ON DELETE SET NULL,

    -- The authenticated user the action was performed ON (from models.py)
    user_id         UUID            REFERENCES users(id) ON DELETE SET NULL,

    -- For admin actions: the target user being modified
    target_user_id  UUID            REFERENCES users(id) ON DELETE SET NULL,

    action          VARCHAR(128)    NOT NULL,
    -- Examples: 'user_registered' | 'user_login' | 'activate_user'
    --           'update_plan' | 'reset_daily_runs' | 'extend_trial'
    --           'checkout_completed' | 'invoice_paid' | 'subscription_cancelled'

    details         JSONB,          -- structured data (from models.py)
    notes           TEXT,           -- human-readable admin notes

    ip_address      INET,
    user_agent      VARCHAR(512),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 15: API_KEYS table (UUID PK, UUID FK — for Analyst plan api_access)
-- =============================================================================

CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash        VARCHAR(128)    NOT NULL UNIQUE,    -- bcrypt hash of the raw key
    key_prefix      VARCHAR(16)     NOT NULL,           -- first 8 chars shown to user
    name            VARCHAR(128)    NOT NULL DEFAULT 'Default Key',
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- SECTION 16: Automatic updated_at trigger
-- =============================================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Attach the trigger to every table with an updated_at column
-- (daily_run_counts has no updated_at — it uses a composite PK pattern)
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'plans',
        'users',
        'subscriptions',
        'screening_runs',
        'watchlists',
        'user_configs'
    ]
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON %1$s;
             CREATE TRIGGER trg_%1$s_updated_at
             BEFORE UPDATE ON %1$s
             FOR EACH ROW EXECUTE FUNCTION set_updated_at();',
            tbl
        );
    END LOOP;
END;
$$;


-- =============================================================================
-- SECTION 17: Indexes for performance
-- =============================================================================

-- users
CREATE INDEX IF NOT EXISTS idx_users_email
    ON users (email);

CREATE INDEX IF NOT EXISTS idx_users_stripe_customer
    ON users (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_reset_token
    ON users (reset_token)
    WHERE reset_token IS NOT NULL;

-- subscriptions
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
    ON subscriptions (user_id);

CREATE INDEX IF NOT EXISTS idx_subscriptions_status
    ON subscriptions (status);

CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_customer
    ON subscriptions (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_sub
    ON subscriptions (stripe_sub_id)
    WHERE stripe_sub_id IS NOT NULL;

-- screening_runs
CREATE INDEX IF NOT EXISTS idx_screening_runs_user_id_run_date
    ON screening_runs (user_id, run_date DESC);

CREATE INDEX IF NOT EXISTS idx_screening_runs_status
    ON screening_runs (status)
    WHERE status IN ('pending', 'running');

-- daily_run_counts
CREATE INDEX IF NOT EXISTS idx_daily_run_counts_user_date
    ON daily_run_counts (user_id, run_date);

-- audit_log
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id_created_at
    ON audit_log (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_actor_id
    ON audit_log (actor_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_target_user_id
    ON audit_log (target_user_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log (action);

-- stripe_events
CREATE INDEX IF NOT EXISTS idx_stripe_events_event_id
    ON stripe_events (stripe_event_id);

CREATE INDEX IF NOT EXISTS idx_stripe_events_unprocessed
    ON stripe_events (processed, created_at)
    WHERE processed = FALSE;

-- email_alerts
CREATE INDEX IF NOT EXISTS idx_email_alerts_user_id
    ON email_alerts (user_id);

CREATE INDEX IF NOT EXISTS idx_email_alerts_status
    ON email_alerts (status)
    WHERE status = 'queued';

-- watchlists
CREATE INDEX IF NOT EXISTS idx_watchlists_user_id
    ON watchlists (user_id);

-- subscription_history
CREATE INDEX IF NOT EXISTS idx_subscription_history_user_id
    ON subscription_history (user_id, started_at DESC);

-- api_keys
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id_active
    ON api_keys (user_id)
    WHERE is_active = TRUE;


-- =============================================================================
-- SECTION 18: Seed data — Plans
-- Plan IDs must match models.py _SEED_PLANS (1=trial, 2=starter, 3=pro, 4=analyst, 5=enterprise)
-- =============================================================================

INSERT INTO plans (
    id, name, display_name, description,
    runs_per_day, max_ai_picks, max_pdf_history, trial_days,
    features, price_monthly, price_yearly, sort_order
)
VALUES
    -- 1: Free Trial (7-day gated onboarding)
    (1, 'trial', 'Free Trial',
     'Get started with basic stock screening. No credit card required.',
     1, 3, 0, 7,
     '{"etf":false,"bond":false,"value":false,"amex":false,"all_modes":false,"email":false,"export":false,"api":false,"markets":["NYSE","NASDAQ"],"geography":["usa"]}',
     NULL, NULL, 0),

    -- 2: Starter
    (2, 'starter', 'Starter',
     'Ideal for individual investors. AI analysis and PDF reports.',
     2, 3, 5, 0,
     '{"etf":false,"bond":false,"value":false,"amex":false,"all_modes":false,"email":false,"export":true,"api":false,"markets":["NYSE","NASDAQ"],"geography":["usa"]}',
     29.00, 249.00, 1),

    -- 3: Pro
    (3, 'pro', 'Pro',
     'For serious investors. Full screeners, email alerts, and priority support.',
     5, 5, 10, 0,
     '{"etf":true,"bond":false,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":false,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all"]}',
     79.00, 699.00, 2),

    -- 4: Analyst
    (4, 'analyst', 'Analyst',
     'Full-featured for professionals. API access and maximum limits.',
     NULL, NULL, 30, 0,
     '{"etf":true,"bond":true,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":true,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all","international"]}',
     179.00, 1599.00, 3),

    -- 5: Enterprise
    (5, 'enterprise', 'Enterprise',
     'Custom plans for teams and institutions. Contact us.',
     NULL, NULL, NULL, 0,
     '{"etf":true,"bond":true,"value":true,"amex":true,"all_modes":true,"email":true,"export":true,"api":true,"markets":["NYSE","NASDAQ","AMEX"],"geography":["usa","all","international"]}',
     499.00, 4999.00, 4)

ON CONFLICT (id) DO UPDATE SET
    name                = EXCLUDED.name,
    display_name        = EXCLUDED.display_name,
    description         = EXCLUDED.description,
    runs_per_day        = EXCLUDED.runs_per_day,
    max_ai_picks        = EXCLUDED.max_ai_picks,
    max_pdf_history     = EXCLUDED.max_pdf_history,
    trial_days          = EXCLUDED.trial_days,
    features            = EXCLUDED.features,
    price_monthly       = EXCLUDED.price_monthly,
    price_yearly        = EXCLUDED.price_yearly,
    sort_order          = EXCLUDED.sort_order,
    updated_at          = NOW();

-- Reset plans sequence so future manual inserts don't collide
SELECT setval('plans_id_seq', (SELECT MAX(id) FROM plans));


-- =============================================================================
-- SECTION 19: Seed data — Admin user (uncomment and update before running)
-- =============================================================================

-- Steps to create the first admin user:
--   1. Generate a bcrypt hash in Python:
--        import bcrypt; print(bcrypt.hashpw(b"YourPassword", bcrypt.gensalt()).decode())
--   2. Uncomment the block below, replace the placeholders, and run.

-- INSERT INTO users (email, password_hash, full_name, is_admin, email_verified)
-- VALUES (
--     'admin@REPLACE_WITH_YOUR_DOMAIN',
--     '$2b$12$REPLACE_WITH_BCRYPT_HASH',
--     'Admin',
--     TRUE,
--     TRUE
-- )
-- ON CONFLICT (email) DO UPDATE SET is_admin = TRUE;
--
-- -- Grant admin role in admin_users table
-- INSERT INTO admin_users (user_id)
-- SELECT id FROM users WHERE email = 'admin@REPLACE_WITH_YOUR_DOMAIN'
-- ON CONFLICT (user_id) DO NOTHING;
--
-- -- Assign analyst (plan_id=4) subscription to admin
-- INSERT INTO subscriptions (user_id, plan_id, status, billing_cycle)
-- SELECT u.id, 4, 'active', 'monthly'
-- FROM users u
-- WHERE u.email = 'admin@REPLACE_WITH_YOUR_DOMAIN'
-- ON CONFLICT (user_id) DO NOTHING;


-- =============================================================================
-- SECTION 20: Reporting views
-- =============================================================================

-- Active subscriptions with plan and user info
CREATE OR REPLACE VIEW v_active_subscriptions AS
SELECT
    u.id            AS user_id,
    u.email,
    u.full_name,
    p.name          AS plan_name,
    p.display_name  AS plan_display_name,
    s.status,
    s.billing_cycle,
    s.trial_ends_at,
    s.expires_at,
    s.stripe_sub_id,
    p.price_monthly,
    p.price_yearly,
    CASE s.billing_cycle
        WHEN 'yearly'  THEN ROUND(p.price_yearly  / 12.0, 2)
        ELSE                ROUND(p.price_monthly, 2)
    END             AS mrr_contribution,
    s.started_at    AS subscribed_at
FROM subscriptions s
JOIN users  u ON u.id = s.user_id
JOIN plans  p ON p.id = s.plan_id
WHERE s.status = 'active'
  AND (s.expires_at IS NULL OR s.expires_at > NOW());

-- MRR summary by plan
CREATE OR REPLACE VIEW v_mrr_by_plan AS
SELECT
    p.name          AS plan_name,
    p.display_name,
    COUNT(*)        AS subscriber_count,
    SUM(
        CASE s.billing_cycle
            WHEN 'yearly' THEN ROUND(p.price_yearly / 12.0, 2)
            ELSE               ROUND(p.price_monthly, 2)
        END
    )               AS mrr_usd
FROM subscriptions s
JOIN plans p ON p.id = s.plan_id
WHERE s.status = 'active'
  AND (s.expires_at IS NULL OR s.expires_at > NOW())
GROUP BY p.id, p.name, p.display_name
ORDER BY mrr_usd DESC;

-- Daily run totals across all users
CREATE OR REPLACE VIEW v_daily_runs_summary AS
SELECT
    run_date,
    COUNT(DISTINCT user_id) AS unique_users,
    SUM(count)              AS total_runs
FROM daily_run_counts
GROUP BY run_date
ORDER BY run_date DESC;


-- =============================================================================
-- Done
-- =============================================================================
-- Verify:
--   \dt           -- list all tables
--   \dv           -- list views
--   SELECT * FROM plans ORDER BY id;
--   \d users      -- check UUID types
-- =============================================================================
