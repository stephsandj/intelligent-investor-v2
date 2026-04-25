# ⚡ Quick Start — 5 Minutes to Production

## Step 1: Create GitHub Repository (2 min)

```bash
# On your local machine
cd "/Users/stephanesandjong/Library/Application Support/IntelligentInvestorAgentV2"

git init
git add .
git commit -m "Initial commit: Intelligent Investor V2 - Production ready"
git branch -M main

# Create repo on GitHub.com, then:
git remote add origin https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
git push -u origin main

echo "✅ Repository created and code pushed to GitHub"
```

## Step 2: Prepare Your Information (1 min)

Gather these details (have them ready):
- **Hostinger VPS IP**: `_______________`
- **Domain name**: `app.yourdomain.com`
- **Email**: `admin@yourdomain.com`
- **API Keys**:
  - Anthropic: `sk-ant-...`
  - FMP: `0WKipAzr...`

## Step 3: Deploy to Production (2 min)

```bash
# SSH into your Hostinger VPS
ssh root@YOUR_VPS_IP

# Clone repository
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2

# ONE-COMMAND DEPLOYMENT
bash deploy.sh app.yourdomain.com admin@yourdomain.com

# ✅ DONE! Your app is live at https://app.yourdomain.com
```

**That's it!** The `deploy.sh` script automatically:
- ✅ Installs Docker
- ✅ Generates secrets
- ✅ Sets up PostgreSQL
- ✅ Configures SSL/TLS
- ✅ Sets up Nginx
- ✅ Starts your app

---

## 🔍 Verify Deployment

```bash
# Check if app is running
curl https://app.yourdomain.com/api/health

# View logs
docker compose logs -f app

# Test login (should return tokens)
curl -X POST https://app.yourdomain.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"Test123!@#"}'
```

---

## 🔄 Update Your App Later

```bash
# On your local machine
git add .
git commit -m "Feature: add new screener"
git push origin main

# On VPS (SSH)
cd intelligent-investor-v2
git pull origin main
docker compose up -d --build app

# ✅ Updated! Zero downtime
```

---

## 📚 Need More Details?

- **Full Deployment Guide**: See `DEPLOYMENT.md`
- **Project Info**: See `README.md`
- **Troubleshooting**: See `DEPLOYMENT.md` → Troubleshooting section

---

## ✨ Common Commands (On VPS)

```bash
# View logs
docker compose logs -f app

# SSH into container
docker compose exec app bash

# Access database
docker compose exec db psql -U ii_user intelligentinvestor

# Stop/start
docker compose stop / docker compose start

# Check status
docker compose ps
```

---

**You're ready to go! 🚀**
