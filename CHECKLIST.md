# ✅ Pre-Deployment Checklist

Complete this checklist before pushing to GitHub and deploying to production.

## 📋 Code Quality

- [ ] No hardcoded paths (check: `grep -r "stephanesandjong\|Library/Application" *.py`)
- [ ] No localhost/127.0.0.1 hardcoded (except defaults in code)
- [ ] All config in `.env.example`
- [ ] `.gitignore` includes `.env` files
- [ ] No API keys in code
- [ ] No database credentials in code

## 🐳 Docker & Deployment

- [ ] `Dockerfile` present and builds
- [ ] `docker-compose.yml` complete
- [ ] `.dockerignore` configured
- [ ] `deploy.sh` script present
- [ ] `requirements.txt` updated with all dependencies
- [ ] PostgreSQL init script exists (`docker/postgres/init.sql`)

## 🔐 Security

- [ ] All passwords use environment variables
- [ ] JWT_SECRET is 64-char hex (not hardcoded)
- [ ] API keys in `.env.example` as placeholders
- [ ] SMTP credentials in `.env` not visible
- [ ] Stripe keys use environment variables
- [ ] Database URL uses environment variable

## 🌐 Configuration

- [ ] `.env.example` has all required variables
- [ ] `APP_BASE_URL` uses environment variable
- [ ] `PORT` uses environment variable (default 5050)
- [ ] Database connection pooling configured
- [ ] Nginx config present and valid
- [ ] CORS configured properly

## 📝 Documentation

- [ ] `README.md` present and complete
- [ ] `DEPLOYMENT.md` detailed and accurate
- [ ] `QUICK_START.md` easy to follow
- [ ] Comments in deploy.sh are clear
- [ ] Error messages are user-friendly

## 🧪 Testing

- [ ] Application starts locally: `bash start_v2_dashboard.sh`
- [ ] API endpoints respond: `curl http://localhost:5051/api/health`
- [ ] Docker builds: `docker build -t ii-v2 .`
- [ ] Docker compose up works: `docker compose up -d`
- [ ] Database initialization successful
- [ ] Admin panel accessible

## 🎯 Final Checks

- [ ] Git repository initialized
- [ ] `.gitignore` prevents committing secrets
- [ ] All files added to Git (except ignored)
- [ ] Initial commit created
- [ ] Ready to push to GitHub
- [ ] Hostinger VPS access confirmed
- [ ] Domain points to VPS (or ready to configure)

## 🚀 Ready to Deploy?

If all boxes are checked, you're ready!

```bash
# Push to GitHub
git push -u origin main

# Deploy to VPS
ssh root@your.vps.ip
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2
bash deploy.sh app.yourdomain.com admin@yourdomain.com
```

**Estimated deployment time: 2-3 minutes**

---

## ⚠️ Critical Items (Must Have)

- ✅ `.env` file is in `.gitignore`
- ✅ No API keys visible in code
- ✅ Docker files present
- ✅ `deploy.sh` script present
- ✅ Database credentials use environment variables
- ✅ `APP_BASE_URL` is configurable

**If any of above are missing, deployment will fail!**
