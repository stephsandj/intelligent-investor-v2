# 📊 Intelligent Investor V2

**AI-Powered Stock Screening & Analysis SaaS**

> Production-ready application with authentication, subscription management, stock analysis, and AI insights.

![Status](https://img.shields.io/badge/status-production--ready-brightgreen)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Docker](https://img.shields.io/badge/docker-ready-blue)
![License](https://img.shields.io/badge/license-proprietary-red)

---

## 🚀 Quick Start

### Local Development

```bash
# Clone repository
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2

# Copy environment template
cp .env.example .env

# Edit .env with your local database URL and API keys
nano .env

# Start local development server
bash start_v2_dashboard.sh

# Access at http://localhost:5051
```

### Docker Deployment (Production)

```bash
# One-command deployment to Hostinger VPS
ssh root@your.vps.ip.address
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2
bash deploy.sh app.yourdomain.com admin@yourdomain.com

# Your app is now live at https://app.yourdomain.com
```

**See [DEPLOYMENT.md](./DEPLOYMENT.md) for detailed deployment instructions.**

---

## 📋 Features

### ✅ Core Features
- **Stock Screener** - Analyze stocks using Graham-Buffett criteria
- **ETF Screener** - Growth & quality ETF analysis
- **Bond ETF Screener** - Bond ETF analysis with safety ratings
- **Single Ticker Research** - Deep-dive analysis on any stock
- **AI Analysis** - Claude AI-powered insights
- **PDF Reports** - Generate downloadable analysis reports

### ✅ User Management
- Email verification
- Password reset flow
- Account lockout (5 failed attempts)
- Password history (prevent reuse of last 3 passwords)
- Subscription status management (Active, Expired, Cancelled, Inactive, Pending Payment)

### ✅ Subscription Management
- **Active** - Full access
- **Expired** - Dashboard access but no screening
- **Cancelled** - Account blocked
- **Inactive** - Account blocked
- **Pending Payment** - Account blocked

### ✅ Security
- bcrypt password hashing
- JWT authentication with HttpOnly cookies
- Rate limiting
- CSRF protection via Nginx
- SSL/TLS encryption
- Environment-based configuration (no hardcoded secrets)

### ✅ Admin Dashboard
- User management
- Subscription administration
- Run count tracking
- System health monitoring

---

## 🛠️ Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend | Flask + Gunicorn |
| Database | PostgreSQL 16 |
| Frontend | HTML5 + Vanilla JavaScript |
| Reverse Proxy | Nginx |
| Containerization | Docker + Docker Compose |
| AI Analysis | Anthropic Claude API |
| Stock Data | yfinance + FMP |
| Payments | Stripe (optional) |

---

## 📦 Project Structure

```
intelligent-investor-v2/
├── DEPLOYMENT.md              # 📘 Deployment guide (START HERE!)
├── README.md                  # This file
├── requirements.txt           # Python dependencies
├── .env.example               # Environment template
├── .gitignore                 # Git configuration
│
├── docker-compose.yml         # Container orchestration
├── Dockerfile                 # App image recipe
├── gunicorn.conf.py           # WSGI server config
├── nginx.conf                 # Reverse proxy config
├── deploy.sh                  # One-command deployment
│
├── dashboard_v2.py            # Main Flask application
├── auth.py                    # Authentication & JWT
├── models.py                  # Database ORM & models
├── admin_routes.py            # Admin API endpoints
│
├── bond_etf_screener.py       # Bond screener logic
├── growth_etf_screener.py     # ETF screener logic
├── billing.py                 # Stripe integration
│
├── preview/                   # Frontend files
│   └── index.html             # Main SPA
├── admin_panel.html           # Admin dashboard
│
├── docker/                    # Docker support files
│   ├── postgres/
│   │   └── init.sql           # Database initialization
│   └── nginx/
│       └── default.conf       # Default nginx config
│
└── logs/                      # Application logs (gitignored)
```

---

## 🔧 Configuration

### Environment Variables

All configuration is environment-based using `.env` file:

```bash
# Database
DATABASE_URL=postgresql://ii_user:password@db:5432/intelligentinvestor
DB_PASSWORD=your_strong_password

# Authentication
JWT_SECRET=your_64_char_hex_secret
JWT_ALGORITHM=HS256

# API Keys
ANTHROPIC_API_KEY=sk-ant-...
FMP_API_KEY=...

# Email
GMAIL_FROM=your-email@gmail.com
GMAIL_APP_PASSWORD=...

# Application
APP_BASE_URL=https://app.yourdomain.com
PORT=5050
ADMIN_EMAIL=admin@yourdomain.com
```

**⚠️ NEVER commit `.env` to version control!** Use `.env.example` template instead.

---

## 🚀 Deployment

### Option 1: Quick Deployment (Recommended)

```bash
# SSH into Hostinger VPS
ssh root@your.vps.ip.address

# Clone and deploy
git clone https://github.com/YOUR_USERNAME/intelligent-investor-v2.git
cd intelligent-investor-v2
bash deploy.sh app.yourdomain.com admin@yourdomain.com

# ✅ Done! App is live at https://app.yourdomain.com
```

### Option 2: Manual Deployment

See [DEPLOYMENT.md - Option B: Manual Deployment](./DEPLOYMENT.md#option-b-manual-deployment-if-you-need-more-control)

### Option 3: Local Docker

```bash
docker compose up -d
# Access at http://localhost:5051
```

---

## 📊 Database Schema

Key tables:
- `users` - User accounts
- `subscriptions` - User subscriptions & billing
- `plans` - Subscription plans
- `password_history` - Password change history
- `login_attempts` - Failed login tracking
- `audit_log` - User actions
- `screening_runs` - Historical runs
- `stripe_events` - Payment events

---

## 🔐 Security Features

✅ **Password Security**
- Minimum 12 characters
- Bcrypt hashing (12-round)
- Password strength meter
- History tracking (prevent reuse of last 3)

✅ **Account Protection**
- Account lockout after 5 failed attempts (5-minute cooldown)
- Email verification required
- Password reset with token expiration
- Rate limiting on auth endpoints

✅ **Data Protection**
- SSL/TLS encryption (Let's Encrypt)
- HttpOnly cookies for JWT tokens
- Environment-based secrets (no hardcoding)
- Secure CORS configuration
- Database backups (automatic daily)

---

## 📈 Performance

- **Load Time**: < 2s (optimized frontend)
- **API Response**: < 200ms (average)
- **Screening Run**: 15-30 seconds (depending on analysis type)
- **Database Queries**: Indexed for performance
- **Concurrent Users**: 100+ (with scaling)

---

## 🧪 Testing

### Automated Tests (GitHub Actions)

```bash
# Tests run automatically on push/PR
# Check .github/workflows/test.yml for details
```

### Manual Testing

```bash
# Test API endpoints
curl -X POST http://localhost:5051/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"TestPassword123!@#"}'

# Check health
curl http://localhost:5051/api/health

# View logs
docker compose logs -f app
```

---

## 🛠️ Maintenance

### Regular Tasks

**Weekly:**
- Monitor logs: `docker compose logs app | grep ERROR`
- Check disk space: `df -h`

**Monthly:**
- Database maintenance
- Backup verification
- SSL certificate status

**Quarterly:**
- Dependency updates
- Security patches
- Performance review

### Backup Database

```bash
# Manual backup
docker compose exec db pg_dump -U ii_user intelligentinvestor > backup.sql

# Restore
docker compose exec -T db psql -U ii_user intelligentinvestor < backup.sql
```

### Update Application

```bash
# Pull latest changes
git pull origin main

# Rebuild containers
docker compose up -d --build

# Verify health
curl https://app.yourdomain.com/api/health
```

---

## 📚 Documentation

- **[DEPLOYMENT.md](./DEPLOYMENT.md)** - Complete deployment guide (START HERE!)
- **[API_DOCS.md](./docs/API.md)** - REST API reference (if available)
- **[TROUBLESHOOTING.md](./DEPLOYMENT.md#-troubleshooting)** - Common issues & fixes

---

## 🐛 Troubleshooting

### Application won't start

```bash
docker compose logs app
# Check DATABASE_URL in .env
# Verify PostgreSQL is healthy: docker compose logs db
```

### SSL certificate issues

```bash
# Renew certificate
certbot renew --force-renewal
docker compose restart
```

### High memory usage

```bash
docker stats
# Check for runaway processes
docker compose restart app
```

See [DEPLOYMENT.md - Troubleshooting](./DEPLOYMENT.md#-troubleshooting) for more.

---

## 📞 Support

- **Documentation**: See [DEPLOYMENT.md](./DEPLOYMENT.md)
- **Issues**: Check existing GitHub issues
- **Email**: Contact admin@yourdomain.com

---

## 📄 License

Proprietary - All rights reserved

---

## ✨ Credits

Built with:
- Flask & PostgreSQL
- Anthropic Claude API
- yfinance & FMP
- Docker & Nginx

---

## 🎯 Version History

| Version | Date | Changes |
|---------|------|---------|
| **1.0.0** | Apr 25, 2026 | Initial production release |
| 0.9.0 | Apr 20, 2026 | Subscription management |
| 0.8.0 | Apr 15, 2026 | Password policy implementation |
| 0.7.0 | Apr 10, 2026 | Authentication system |

---

**Ready to deploy? Start with [DEPLOYMENT.md](./DEPLOYMENT.md)** 🚀
