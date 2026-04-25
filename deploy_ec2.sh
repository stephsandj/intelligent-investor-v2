#!/usr/bin/env bash
# =============================================================================
# deploy_ec2.sh — Intelligent Investor Agent
# Deploys the full application stack on a fresh Ubuntu 22.04 EC2 instance.
# Architecture: single EC2 instance with PostgreSQL, Redis, Nginx, Gunicorn.
#
# Usage:
#   chmod +x deploy_ec2.sh
#   sudo ./deploy_ec2.sh
#
# Run as root or with sudo.
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'   # No Colour

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${GREEN}========================================${NC}"; \
             echo -e "${BOLD}${GREEN}  $*${NC}"; \
             echo -e "${BOLD}${GREEN}========================================${NC}"; }

# ---------------------------------------------------------------------------
# Guard: must be running as root
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
fi

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------
APP_NAME="intelligentinvestor"
APP_USER="intelligentinvestor"
APP_DIR="/opt/${APP_NAME}"
APP_PORT=5050
WSGI_WORKERS=4
DOMAIN="REPLACE_WITH_YOUR_DOMAIN"   # <-- set before running

PYTHON_BIN="/usr/bin/python3.11"
PIP_BIN="/usr/bin/pip3.11"

PG_DB="intelligentinvestor"
PG_USER="ii_user"

# ---------------------------------------------------------------------------
# Step 1 — System update
# ---------------------------------------------------------------------------
header "Step 1: System update"
info "Running apt update && apt upgrade ..."
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
success "System updated."

# ---------------------------------------------------------------------------
# Step 2 — Install PostgreSQL 14
# ---------------------------------------------------------------------------
header "Step 2: Install PostgreSQL 14"
apt-get install -y gnupg curl lsb-release
PG_CODENAME=$(lsb_release -cs)
if ! apt-cache show postgresql-14 &>/dev/null; then
    info "Adding PostgreSQL APT repository..."
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/postgresql-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/postgresql-archive-keyring.gpg] \
https://apt.postgresql.org/pub/repos/apt ${PG_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    apt-get update -y
fi
apt-get install -y postgresql-14 postgresql-client-14
systemctl enable postgresql
systemctl start postgresql
success "PostgreSQL 14 installed and started."

# ---------------------------------------------------------------------------
# Step 3 — Install Redis 7
# ---------------------------------------------------------------------------
header "Step 3: Install Redis 7"
if ! apt-cache show redis-server | grep -q "^Version: 7\." 2>/dev/null; then
    info "Adding Redis PPA for version 7..."
    add-apt-repository -y ppa:redislabs/redis 2>/dev/null || true
    apt-get update -y
fi
apt-get install -y redis-server
systemctl enable redis-server
systemctl start redis-server
success "Redis installed and started."

# ---------------------------------------------------------------------------
# Step 4 — Install Nginx
# ---------------------------------------------------------------------------
header "Step 4: Install Nginx"
apt-get install -y nginx
systemctl enable nginx
systemctl start nginx
success "Nginx installed and started."

# ---------------------------------------------------------------------------
# Step 5 — Install Python 3.11
# ---------------------------------------------------------------------------
header "Step 5: Install Python 3.11"
apt-get install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
apt-get update -y
apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 || true
success "Python 3.11 installed: $(python3.11 --version)"

# ---------------------------------------------------------------------------
# Step 6 — Install Certbot (Let's Encrypt)
# ---------------------------------------------------------------------------
header "Step 6: Install Certbot"
apt-get install -y certbot python3-certbot-nginx
success "Certbot installed: $(certbot --version 2>&1 | head -1)"

# ---------------------------------------------------------------------------
# Step 7 — Create system user
# ---------------------------------------------------------------------------
header "Step 7: Create system user '${APP_USER}'"
if id "${APP_USER}" &>/dev/null; then
    warn "User '${APP_USER}' already exists; skipping creation."
else
    useradd --system --shell /bin/bash --home-dir "${APP_DIR}" \
            --create-home "${APP_USER}"
    success "System user '${APP_USER}' created."
fi

# ---------------------------------------------------------------------------
# Step 8 — Create application directory
# ---------------------------------------------------------------------------
header "Step 8: Create app directory"
mkdir -p "${APP_DIR}"/{logs,static,templates}
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
success "App directory created at ${APP_DIR}."

# ---------------------------------------------------------------------------
# Step 9 — Install Python dependencies
# ---------------------------------------------------------------------------
header "Step 9: Install Python dependencies"
# Use a virtualenv inside the app directory for clean isolation
python3.11 -m venv "${APP_DIR}/venv"
VENV_PIP="${APP_DIR}/venv/bin/pip"

"${VENV_PIP}" install --upgrade pip wheel setuptools

PYTHON_PACKAGES=(
    flask
    gunicorn
    "psycopg2-binary"
    PyJWT
    bcrypt
    stripe
    anthropic
    requests
    yfinance
    reportlab
    schedule
)

info "Installing: ${PYTHON_PACKAGES[*]}"
"${VENV_PIP}" install "${PYTHON_PACKAGES[@]}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/venv"
success "Python dependencies installed."

# ---------------------------------------------------------------------------
# Step 10 — Generate secrets and create PostgreSQL database/user
# ---------------------------------------------------------------------------
header "Step 10: PostgreSQL database and user setup"

DB_PASSWORD=$(openssl rand -hex 16)
JWT_SECRET=$(openssl rand -hex 32)
REDIS_PASSWORD=$(openssl rand -hex 16)

PG_HBA="/etc/postgresql/14/main/pg_hba.conf"

# Create DB user and database as the postgres OS user
sudo -u postgres psql <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${PG_USER}') THEN
        CREATE USER ${PG_USER} WITH PASSWORD '${DB_PASSWORD}';
    ELSE
        ALTER USER ${PG_USER} WITH PASSWORD '${DB_PASSWORD}';
    END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${PG_DB} OWNER ${PG_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${PG_DB}')\gexec

GRANT ALL PRIVILEGES ON DATABASE ${PG_DB} TO ${PG_USER};
SQL

success "PostgreSQL user '${PG_USER}' and database '${PG_DB}' ready."

# ---------------------------------------------------------------------------
# Step 11 — Write .env file
# ---------------------------------------------------------------------------
header "Step 11: Write /opt/${APP_NAME}/.env"

cat > "${APP_DIR}/.env" <<ENV
# ============================================================
#  Intelligent Investor Agent — Environment Configuration
#  Generated by deploy_ec2.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
#  KEEP THIS FILE SECRET — chmod 600 and owned by ${APP_USER}
# ============================================================

# --- Database ---
DATABASE_URL=postgresql://${PG_USER}:${DB_PASSWORD}@localhost/${PG_DB}

# --- Redis ---
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:6379/0
REDIS_PASSWORD=${REDIS_PASSWORD}

# --- Application security ---
JWT_SECRET=${JWT_SECRET}

# --- Stripe (replace with real keys from dashboard.stripe.com) ---
STRIPE_SECRET_KEY=sk_live_REPLACE_ME
STRIPE_WEBHOOK_SECRET=whsec_REPLACE_ME
STRIPE_PUBLISHABLE_KEY=pk_live_REPLACE_ME

# Stripe Price IDs (create in Stripe dashboard, then paste here)
STRIPE_PRICE_STARTER_MONTHLY=price_REPLACE_ME
STRIPE_PRICE_STARTER_YEARLY=price_REPLACE_ME
STRIPE_PRICE_PRO_MONTHLY=price_REPLACE_ME
STRIPE_PRICE_PRO_YEARLY=price_REPLACE_ME
STRIPE_PRICE_ANALYST_MONTHLY=price_REPLACE_ME
STRIPE_PRICE_ANALYST_YEARLY=price_REPLACE_ME

# --- Anthropic ---
ANTHROPIC_API_KEY=REPLACE_ME

# --- Financial Modeling Prep ---
FMP_API_KEY=REPLACE_ME

# --- Gmail (App Password — not your main account password) ---
GMAIL_APP_PASSWORD=REPLACE_ME
GMAIL_FROM=REPLACE_ME

# --- App ---
APP_BASE_URL=https://${DOMAIN}
ADMIN_EMAIL=REPLACE_ME
ENV

chmod 600 "${APP_DIR}/.env"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
success ".env file written."

# ---------------------------------------------------------------------------
# Step 12 — Systemd service
# ---------------------------------------------------------------------------
header "Step 12: Create systemd service"

cat > /etc/systemd/system/${APP_NAME}.service <<SERVICE
[Unit]
Description=Intelligent Investor Agent (Gunicorn)
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=notify
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn \\
    --workers ${WSGI_WORKERS} \\
    --worker-class sync \\
    --bind 127.0.0.1:${APP_PORT} \\
    --timeout 120 \\
    --keep-alive 5 \\
    --log-level info \\
    --access-logfile ${APP_DIR}/logs/access.log \\
    --error-logfile  ${APP_DIR}/logs/error.log \\
    --capture-output \\
    app:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=5
StandardOutput=append:${APP_DIR}/logs/gunicorn_stdout.log
StandardError=append:${APP_DIR}/logs/gunicorn_stderr.log

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}
ProtectHome=true

[Install]
WantedBy=multi-user.target
SERVICE

success "Systemd service file created."

# ---------------------------------------------------------------------------
# Step 13 — Enable and start the service
# ---------------------------------------------------------------------------
header "Step 13: Enable and start service"
systemctl daemon-reload
systemctl enable "${APP_NAME}"
# Don't start yet — app code hasn't been deployed. We'll restart at the end.
info "Service enabled. It will be started once application code is in place."

# ---------------------------------------------------------------------------
# Step 14 — Nginx configuration
# ---------------------------------------------------------------------------
header "Step 14: Configure Nginx"

# Remove default site
rm -f /etc/nginx/sites-enabled/default

# Write main nginx config for the app
cat > /etc/nginx/sites-available/${APP_NAME} <<'NGINX'
# Rate limiting zones
limit_req_zone $binary_remote_addr zone=auth_limit:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=api_limit:10m  rate=30r/m;

upstream app_backend {
    server 127.0.0.1:APP_PORT_PLACEHOLDER fail_timeout=0;
    keepalive 32;
}

# -----------------------------------------------------------------------
# HTTP — redirect all traffic to HTTPS
# -----------------------------------------------------------------------
server {
    listen 80;
    listen [::]:80;
    server_name DOMAIN_PLACEHOLDER;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# -----------------------------------------------------------------------
# HTTPS — main application server
# -----------------------------------------------------------------------
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name DOMAIN_PLACEHOLDER;

    # --- SSL (managed by certbot; paths auto-filled after cert issuance) ---
    ssl_certificate     /etc/letsencrypt/live/DOMAIN_PLACEHOLDER/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/DOMAIN_PLACEHOLDER/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    # --- Security headers ---
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header X-Frame-Options           "DENY"                                          always;
    add_header X-Content-Type-Options    "nosniff"                                       always;
    add_header Referrer-Policy           "strict-origin"                                 always;
    add_header X-XSS-Protection          "1; mode=block"                                 always;
    add_header Content-Security-Policy   "default-src 'self'; script-src 'self' https://js.stripe.com; frame-src https://js.stripe.com; connect-src 'self' https://api.stripe.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:;" always;
    add_header Permissions-Policy        "geolocation=(), microphone=(), camera=()"      always;

    # --- General settings ---
    client_max_body_size 10M;
    keepalive_timeout    75s;
    send_timeout         60s;

    # --- Gzip compression ---
    gzip              on;
    gzip_vary         on;
    gzip_proxied      any;
    gzip_comp_level   6;
    gzip_min_length   1024;
    gzip_types
        text/plain
        text/css
        text/javascript
        application/json
        application/javascript
        application/x-javascript
        application/xml
        image/svg+xml;

    # --- Stripe webhook — raw body, no rate limiting ---
    location = /billing/webhook {
        proxy_pass          http://app_backend;
        proxy_set_header    Host              $host;
        proxy_set_header    X-Real-IP         $remote_addr;
        proxy_set_header    X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;
        proxy_read_timeout  30s;
        proxy_send_timeout  30s;
        proxy_request_buffering off;  # pass raw body unchanged
    }

    # --- Auth routes — strict rate limiting ---
    location /auth/ {
        limit_req zone=auth_limit burst=5 nodelay;
        limit_req_status 429;

        proxy_pass          http://app_backend;
        proxy_set_header    Host              $host;
        proxy_set_header    X-Real-IP         $remote_addr;
        proxy_set_header    X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;
        proxy_read_timeout  30s;
        proxy_send_timeout  30s;
    }

    # --- API routes — moderate rate limiting ---
    location /api/ {
        limit_req zone=api_limit burst=10 nodelay;
        limit_req_status 429;

        proxy_pass          http://app_backend;
        proxy_set_header    Host              $host;
        proxy_set_header    X-Real-IP         $remote_addr;
        proxy_set_header    X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;
        proxy_read_timeout  120s;
        proxy_send_timeout  120s;
    }

    # --- Static files — long cache ---
    location /static/ {
        alias          /opt/APP_NAME_PLACEHOLDER/static/;
        expires        1y;
        add_header     Cache-Control "public, immutable";
        access_log     off;
        gzip_static    on;
    }

    # --- All other routes — proxy to Flask ---
    location / {
        proxy_pass          http://app_backend;
        proxy_set_header    Host              $host;
        proxy_set_header    X-Real-IP         $remote_addr;
        proxy_set_header    X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;
        proxy_http_version  1.1;
        proxy_set_header    Connection        "";
        proxy_read_timeout  90s;
        proxy_send_timeout  90s;
        proxy_buffering     on;
        proxy_buffer_size   16k;
        proxy_buffers       8 16k;
    }

    # --- Custom error pages ---
    error_page 429 /errors/429.json;
    error_page 502 503 504 /errors/50x.json;

    location /errors/ {
        internal;
        root /opt/APP_NAME_PLACEHOLDER;
    }
}
NGINX

# Substitute real values into the nginx config
sed -i "s/APP_PORT_PLACEHOLDER/${APP_PORT}/g"    /etc/nginx/sites-available/${APP_NAME}
sed -i "s/DOMAIN_PLACEHOLDER/${DOMAIN}/g"        /etc/nginx/sites-available/${APP_NAME}
sed -i "s/APP_NAME_PLACEHOLDER/${APP_NAME}/g"    /etc/nginx/sites-available/${APP_NAME}

ln -sf /etc/nginx/sites-available/${APP_NAME} /etc/nginx/sites-enabled/${APP_NAME}

nginx -t && systemctl reload nginx
success "Nginx configured."

# ---------------------------------------------------------------------------
# Step 15 — Let's Encrypt SSL certificate
# ---------------------------------------------------------------------------
header "Step 15: Obtain Let's Encrypt SSL certificate"

if [[ "${DOMAIN}" == "REPLACE_WITH_YOUR_DOMAIN" ]]; then
    warn "DOMAIN is still set to placeholder. Skipping certbot."
    warn "After updating DOMAIN, run:"
    warn "  certbot --nginx -d ${DOMAIN} --non-interactive --agree-tos -m ADMIN_EMAIL"
else
    mkdir -p /var/www/certbot

    ADMIN_EMAIL_VALUE=$(grep '^ADMIN_EMAIL=' "${APP_DIR}/.env" | cut -d= -f2)
    if [[ -z "${ADMIN_EMAIL_VALUE}" || "${ADMIN_EMAIL_VALUE}" == "REPLACE_ME" ]]; then
        warn "ADMIN_EMAIL not set in .env — using webmaster@${DOMAIN} for certbot."
        ADMIN_EMAIL_VALUE="webmaster@${DOMAIN}"
    fi

    certbot --nginx \
        -d "${DOMAIN}" \
        --non-interactive \
        --agree-tos \
        --email "${ADMIN_EMAIL_VALUE}" \
        --redirect
    success "SSL certificate issued for ${DOMAIN}."

    # Auto-renewal via systemd timer (installed by certbot package)
    systemctl enable certbot.timer
    systemctl start certbot.timer
    success "Certbot auto-renewal timer enabled."
fi

# ---------------------------------------------------------------------------
# Step 16 — Configure Redis with password
# ---------------------------------------------------------------------------
header "Step 16: Configure Redis with requirepass"

REDIS_CONF="/etc/redis/redis.conf"

# Remove any existing requirepass line and append the new one
sed -i '/^requirepass /d' "${REDIS_CONF}"
echo "requirepass ${REDIS_PASSWORD}" >> "${REDIS_CONF}"

# Bind to loopback only (extra safety even with UFW)
sed -i 's/^bind .*/bind 127.0.0.1 ::1/' "${REDIS_CONF}"

# Disable Redis commands that can be dangerous in production
cat >> "${REDIS_CONF}" <<'REDISCFG'

# Production hardening
rename-command FLUSHDB   ""
rename-command FLUSHALL  ""
rename-command DEBUG     ""
rename-command CONFIG    ""
REDISCFG

systemctl restart redis-server
success "Redis hardened with password and restarted."

# Update REDIS_URL in .env with the password (already set above at generation time)
# Confirm the line is correct:
if grep -q "REDIS_URL=redis://:${REDIS_PASSWORD}@" "${APP_DIR}/.env"; then
    success "REDIS_URL in .env includes password — correct."
else
    # Patch it in case the password was regenerated
    sed -i "s|^REDIS_URL=.*|REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:6379/0|" "${APP_DIR}/.env"
    sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PASSWORD}|" "${APP_DIR}/.env"
    success "REDIS_URL updated in .env."
fi

# ---------------------------------------------------------------------------
# Step 17 — Harden PostgreSQL (local connections only)
# ---------------------------------------------------------------------------
header "Step 17: Harden PostgreSQL (loopback-only connections)"

PG_CONF="/etc/postgresql/14/main/postgresql.conf"

# Ensure PostgreSQL only listens on localhost
sed -i "s/^#*listen_addresses\s*=.*/listen_addresses = 'localhost'/" "${PG_CONF}"

# pg_hba.conf — allow only local/loopback connections
cat > "${PG_HBA}" <<'PGHBA'
# TYPE  DATABASE        USER            ADDRESS                 METHOD

# Local OS-level access (Unix socket)
local   all             postgres                                peer
local   all             all                                     md5

# IPv4 loopback — md5 (password) auth
host    all             all             127.0.0.1/32            md5

# IPv6 loopback
host    all             all             ::1/128                 md5

# Reject everything else
host    all             all             0.0.0.0/0               reject
PGHBA

systemctl restart postgresql
success "PostgreSQL hardened to accept only loopback connections."

# ---------------------------------------------------------------------------
# Step 18 — UFW firewall
# ---------------------------------------------------------------------------
header "Step 18: Configure UFW firewall"

apt-get install -y ufw

# Reset to defaults (non-interactively)
ufw --force reset

ufw default deny incoming
ufw default allow outgoing

# Allow SSH, HTTP, HTTPS
ufw allow 22/tcp   comment 'SSH'
ufw allow 80/tcp   comment 'HTTP'
ufw allow 443/tcp  comment 'HTTPS'

# Enable UFW non-interactively
ufw --force enable

ufw status verbose
success "UFW firewall enabled: ports 22, 80, 443 open."

# ---------------------------------------------------------------------------
# Step 19 — Create error page stubs
# ---------------------------------------------------------------------------
header "Step 19: Create error page stubs"

mkdir -p "${APP_DIR}/errors"
cat > "${APP_DIR}/errors/429.json" <<'JSON'
{"error": "Too Many Requests", "message": "You have exceeded the rate limit. Please wait and try again.", "status": 429}
JSON
cat > "${APP_DIR}/errors/50x.json" <<'JSON'
{"error": "Server Error", "message": "The server encountered an error. Please try again later.", "status": 503}
JSON
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/errors"
success "Error stubs created."

# ---------------------------------------------------------------------------
# Final — Print setup summary
# ---------------------------------------------------------------------------
header "DEPLOYMENT COMPLETE"

echo ""
echo -e "${BOLD}Application directory:${NC}  ${APP_DIR}"
echo -e "${BOLD}Environment file:${NC}       ${APP_DIR}/.env"
echo -e "${BOLD}Systemd service:${NC}        ${APP_NAME}.service"
echo -e "${BOLD}Nginx config:${NC}           /etc/nginx/sites-available/${APP_NAME}"
echo -e "${BOLD}PostgreSQL:${NC}             DB=${PG_DB}  User=${PG_USER}"
echo -e "${BOLD}Redis:${NC}                  127.0.0.1:6379 (password protected)"
echo ""
echo -e "${YELLOW}${BOLD}NEXT STEPS${NC}"
echo ""
echo "  1. Upload your application code to ${APP_DIR}/"
echo "     e.g.:  rsync -avz ./src/ ubuntu@<EC2_IP>:${APP_DIR}/"
echo ""
echo "  2. Open ${APP_DIR}/.env and replace ALL placeholder values:"
echo "     - STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PUBLISHABLE_KEY"
echo "     - STRIPE_PRICE_* (create prices in Stripe dashboard first)"
echo "     - ANTHROPIC_API_KEY"
echo "     - FMP_API_KEY"
echo "     - GMAIL_APP_PASSWORD, GMAIL_FROM"
echo "     - APP_BASE_URL  (set to your actual domain)"
echo "     - ADMIN_EMAIL"
echo ""
echo "  3. Run the database schema:"
echo "     sudo -u postgres psql ${PG_DB} < ${APP_DIR}/setup.sql"
echo ""
echo "  4. If you haven't updated DOMAIN in this script, obtain SSL cert:"
echo "     certbot --nginx -d <your-domain> --non-interactive --agree-tos -m <email>"
echo ""
echo "  5. Start the application service:"
echo "     systemctl start ${APP_NAME}"
echo "     systemctl status ${APP_NAME}"
echo ""
echo "  6. Tail logs:"
echo "     journalctl -u ${APP_NAME} -f"
echo "     tail -f ${APP_DIR}/logs/error.log"
echo ""
echo -e "${GREEN}${BOLD}Deployment script finished successfully.${NC}"
