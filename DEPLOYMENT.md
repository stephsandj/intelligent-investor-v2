# 🚀 Intelligent Investor V2 — Production Deployment Plan

**Status:** ✅ Production-Ready  
**Last Updated:** April 25, 2026  
**Target:** Hostinger VPS + Custom Domain + SSL/TLS

---

## 📋 Table of Contents

1. [Pre-Deployment Checklist](#pre-deployment-checklist)
2. [GitHub Setup](#github-setup)
3. [Hostinger VPS Configuration](#hostinger-vps-configuration)
4. [Deployment Process](#deployment-process)
5. [Post-Deployment Verification](#post-deployment-verification)
6. [Maintenance & Updates](#maintenance--updates)
7. [Troubleshooting](#troubleshooting)

---

## ✅ Pre-Deployment Checklist

### Information You Need to Gather

- [ ] **Domain Name** (e.g., `app.yourdomain.com`)
- [ ] **Hostinger VPS Details:**
  - IP address: `_______________`
  - Root password: `_______________` (save in secure password manager)
  - OS: Ubuntu 22.04 or 24.04
- [ ] **Email Address** (for Let's Encrypt SSL certificates)
  - `_______________`
- [ ] **API Keys:**
  - Anthropic (Claude): `sk-ant-...`
  - FMP (Financial Modeling Prep): `0WKipAzr...`
  - Stripe Keys (if using payments)
  - Gmail SMTP credentials (for email)
- [ ] **Admin Email** (first admin user)
  - `_______________`

### Application Verification

- [ ] No hardcoded paths (✅ verified)
- [ ] `.gitignore` configured (✅ verified)
- [ ] `.env.example` created (✅ verified)
- [ ] Docker files present (✅ verified)
- [ ] `deploy.sh` script present (✅ verified)

---

## 🔧 GitHub Setup

### Step 1: Create GitHub Repository

```bash
# On your local machine
cd "/path/to/IntelligentInvestorAgentV2"
git init
git add .
git commit -m "Initial commit: Intelligent Investor V2 - Production ready"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
git push -u origin main
```

### Step 2: Configure GitHub (IMPORTANT)

**Settings → Secrets and variables → Actions**

Add these secrets (deploy.sh will use them for automated deployments):

```
HOSTINGER_VPS_IP        = your.vps.ip.address
HOSTINGER_VPS_USER      = root
HOSTINGER_VPS_PASSWORD  = your_vps_password
DEPLOYMENT_DOMAIN       = app.yourdomain.com
DEPLOYMENT_EMAIL        = admin@yourdomain.com
```

**⚠️ DO NOT commit .env file — it's ignored by .gitignore**

### Step 3: Create Release Versions

```bash
# Tag your first release
git tag -a v1.0.0 -m "First production release"
git push origin v1.0.0

# For future updates:
git tag -a v1.0.1 -m "Bugfix: subscription status"
git push origin v1.0.1
```

---

## 🖥️ Hostinger VPS Configuration

### Prerequisites (Do Once)

**1. Update System**
```bash
ssh root@your.vps.ip.address
apt update && apt upgrade -y
```

**2. Install Git**
```bash
apt install -y git
```

**3. Configure DNS**
- Go to Hostinger Domain Management
- Point your domain to the VPS IP:
  ```
  A Record: app.yourdomain.com → your.vps.ip.address
  ```
- Wait 5-10 minutes for DNS propagation (test with `nslookup app.yourdomain.com`)

**4. Configure Firewall (if enabled)**
```bash
# Allow SSH, HTTP, HTTPS
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (Certbot)
ufw allow 443/tcp   # HTTPS
ufw enable
```

---

## 📦 Deployment Process

### Option A: One-Command Deployment (Recommended)

**Run this ONCE on your VPS to fully deploy:**

```bash
# SSH into VPS
ssh root@your.vps.ip.address

# Clone repository
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2

# Run deployment script (generates .env with random secrets)
# First time ONLY — this takes 2-3 minutes
bash deploy.sh app.yourdomain.com admin@yourdomain.com

# That's it! Your app is live at https://app.yourdomain.com
```

**What deploy.sh does automatically:**
1. ✅ Installs Docker + Docker Compose
2. ✅ Generates `.env` with random secrets
3. ✅ Builds application image
4. ✅ Starts PostgreSQL + Flask containers
5. ✅ Obtains Let's Encrypt SSL certificate
6. ✅ Configures Nginx reverse proxy
7. ✅ Sets up certificate auto-renewal

### Option B: Manual Deployment (If you need more control)

If deploy.sh fails or you prefer manual steps:

```bash
# 1. SSH into VPS and clone repo
ssh root@your.vps.ip.address
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2

# 2. Create .env file (copy from example and fill in)
cp .env.example .env
# Edit with your credentials:
nano .env

# 3. Install Docker (if not already done)
curl -fsSL https://get.docker.com -o get-docker.sh
bash get-docker.sh

# 4. Start containers
docker compose up -d

# 5. Wait for PostgreSQL to be ready
sleep 10

# 6. Setup SSL (install Certbot first)
apt install -y certbot python3-certbot-nginx
certbot certonly --standalone -d app.yourdomain.com -m admin@yourdomain.com

# 7. Configure Nginx with certificate
# The deploy.sh does this automatically, but if manual, see nginx/nginx.conf

# 8. Reload Nginx
docker compose exec nginx nginx -s reload
```

---

## 🔄 Updating Your Application

### Quick Updates (for bug fixes, feature updates)

After pushing code to GitHub:

```bash
# SSH into VPS
ssh root@your.vps.ip.address
cd intelligent-investor-v2

# Pull latest code
git pull origin main

# Rebuild and restart app container
docker compose up -d --build app

# View logs
docker compose logs -f app
```

### Full Rebuild (if dependencies change)

```bash
# Stop all containers
docker compose down

# Rebuild everything
docker compose up -d --build

# Verify health
curl https://app.yourdomain.com/api/health
```

---

## ✔️ Post-Deployment Verification

### 1. Check Application Status

```bash
# SSH into VPS
ssh root@your.vps.ip.address
cd intelligent-investor-v2

# View running containers
docker compose ps

# Check logs
docker compose logs -f app

# Test health endpoint
curl https://app.yourdomain.com/api/health
```

**Expected output:**
```json
{
  "status": "ok",
  "version": "2.0.0",
  "db": {"status": "ok"}
}
```

### 2. Test Application in Browser

- **URL:** `https://app.yourdomain.com`
- **Expected:** Login page appears
- **Check:**
  - [ ] HTTPS (lock icon visible)
  - [ ] SSL certificate valid
  - [ ] Page loads without errors
  - [ ] No console errors (F12)

### 3. Test Core Functionality

**Registration:**
```bash
# Should create user, send verification email
curl -X POST https://app.yourdomain.com/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"TestPassword123!@#"}'
```

**Login:**
```bash
# Should return tokens and subscription info
curl -X POST https://app.yourdomain.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"TestPassword123!@#"}'
```

### 4. Database Connection

```bash
# Connect to PostgreSQL inside container
docker compose exec db psql -U ii_user -d intelligentinvestor

# Inside psql:
\dt                    # List tables
SELECT COUNT(*) FROM users;
\q                     # Quit
```

### 5. Check SSL Certificate

```bash
# Verify certificate is valid
curl -vI https://app.yourdomain.com 2>&1 | grep -i "certificate\|subject"

# Check expiration date
docker compose exec app curl -vI https://app.yourdomain.com 2>&1 | grep -i "expire"
```

---

## 🛠️ Maintenance & Updates

### Regular Tasks

**Weekly:**
- [ ] Check logs for errors: `docker compose logs app | grep ERROR`
- [ ] Monitor disk space: `df -h`

**Monthly:**
- [ ] Review database size: `docker compose exec db psql -U ii_user -d intelligentinvestor -c "SELECT pg_size_pretty(pg_database_size('intelligentinvestor'));"`
- [ ] Test database backups (see below)

**Quarterly:**
- [ ] Update base images: `docker pull postgres:16-alpine && docker pull python:3.11-slim`
- [ ] Review logs for security issues

### Backup Database

```bash
# Manual backup
docker compose exec db pg_dump -U ii_user intelligentinvestor > backup_$(date +%Y%m%d).sql

# Restore from backup
docker compose exec -T db psql -U ii_user intelligentinvestor < backup_20260425.sql
```

### Update Application (Zero-Downtime)

```bash
# On your local machine
git add .
git commit -m "Feature: add new screener"
git push origin main

# On VPS
cd intelligent-investor-v2
git pull origin main
docker compose up -d --build app

# Old container stops, new one starts (within seconds)
```

### Emergency Rollback

```bash
# If new version breaks something
git log --oneline
git checkout v1.0.0    # Go back to known-good release tag
docker compose up -d --build app
```

---

## 🐛 Troubleshooting

### Application Container Won't Start

```bash
# Check logs
docker compose logs app

# Common issues:
# 1. Database not ready: wait 30 more seconds
# 2. Bad .env: check DATABASE_URL format
# 3. Port in use: change PORT in .env

# Force restart
docker compose down
docker compose up -d
```

### SSL Certificate Errors

```bash
# Renew certificate manually
docker compose down
apt install -y certbot
certbot renew --force-renewal
docker compose up -d
```

### Database Connection Issues

```bash
# Test PostgreSQL
docker compose logs db

# Verify credentials in .env
docker compose exec db psql -U ii_user -c "\l"

# If corrupted, nuke and rebuild (⚠️ DELETES ALL DATA):
docker compose down -v    # -v removes volumes
docker compose up -d
# Database will reinitialize
```

### High Memory/CPU Usage

```bash
# Monitor resources
docker stats

# Check for runaway processes
docker compose logs app | tail -100

# Restart app to free memory
docker compose restart app
```

### Domain/DNS Issues

```bash
# Verify DNS is pointing to your VPS
nslookup app.yourdomain.com

# Should return your VPS IP, not an error

# If still wrong, allow 24 hours for DNS propagation
# Check Hostinger DNS records
```

---

## 📁 Directory Structure (On VPS)

```
/root/intelligent-investor-v2/
├── docker-compose.yml        # Container orchestration
├── Dockerfile                # App image recipe
├── deploy.sh                 # One-command deployment
├── .env                      # Secrets (NEVER commit)
├── .env.example              # Template (commit to GitHub)
├── requirements.txt          # Python dependencies
├── dashboard_v2.py           # Flask app
├── auth.py                   # Authentication
├── models.py                 # Database models
├── admin_routes.py           # Admin API
├── preview/index.html        # Frontend
├── nginx.conf                # Reverse proxy config
├── nginx/                    # Nginx configs (volumes)
└── logs/                     # Application logs
```

---

## 🎯 Quick Reference Commands

```bash
# View logs
docker compose logs -f app

# Access app bash
docker compose exec app bash

# Access database
docker compose exec db psql -U ii_user intelligentinvestor

# Stop/start containers
docker compose stop / start

# Restart one container
docker compose restart app

# Full shutdown
docker compose down

# Remove all volumes (⚠️ DELETES DATA)
docker compose down -v

# Check resource usage
docker stats

# View deployed version
curl https://app.yourdomain.com/api/health

# SSH into VPS
ssh root@your.vps.ip.address
```

---

## 📞 Getting Help

**If deployment fails:**

1. Check deploy.sh output carefully — it tells you what went wrong
2. Run `docker compose logs` to see detailed error messages
3. Verify all secrets in .env are correct
4. Ensure DNS is pointing to correct IP
5. Check Hostinger firewall allows ports 80, 443

**For application errors:**
1. Check app logs: `docker compose logs app`
2. Check database: `docker compose exec db psql -U ii_user -d intelligentinvestor`
3. Try restarting containers: `docker compose restart`

---

## ✨ You're Done!

Your application is now live, secure, and maintainable.

**What's Next:**
- Monitor logs daily for first week
- Set up email alerts for SSL certificate renewal
- Create database backups regularly
- Plan for scaling (load balancing, CDN, etc.)

**Enjoy your production deployment! 🎉**

