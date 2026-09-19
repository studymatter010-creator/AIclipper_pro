# Pre-Sale Checklist for AIClipper

Complete this checklist before distributing or selling the source code.

---

## ⚠️ CRITICAL - Must Do Before Sale

### 1. Delete User Data

```bash
# Navigate to project directory
cd AIClipper

# Delete local database files (contains user's video data)
rm -f data/*.db
rm -f data/*.db-journal

# Delete all generated content
rm -rf uploads/*
rm -rf outputs/*
rm -rf subtitles/*
rm -rf thumbnails/*
rm -rf logs/*
rm -rf temp/*

# Delete any cached AI models (optional - buyer will download their own)
rm -rf models/*
```

### 2. Verify .env is Not Included

```bash
# Confirm .env is ignored
cat .gitignore | grep "\.env"

# If you accidentally committed .env, remove it
git rm --cached .env
```

### 3. Clear Git History (Optional - for cleaner distribution)

If you want a fresh git history without your development history:

```bash
# WARNING: This deletes all commit history
rm -rf .git
git init
git add .
git commit -m "Initial release"
```

---

## ✅ Verify Before Listing

### Files That Should NOT Be in Distribution

- [ ] `.env` - Contains local settings (already in .gitignore ✅)
- [ ] `data/*.db` - User database files (already in .gitignore ✅)
- [ ] `logs/*.log` - User activity logs (already in .gitignore ✅)
- [ ] `uploads/*` - User's uploaded videos (already in .gitignore ✅)
- [ ] `outputs/*` - Generated clips (already in .gitignore ✅)
- [ ] `.venv/` - Virtual environment (already in .gitignore ✅)

### Files That SHOULD Be in Distribution

- [ ] `README.md` - ✅ Updated
- [ ] `.gitignore` - ✅ Comprehensive
- [ ] `LICENSE` - ✅ Updated to 2026
- [ ] `requirements.txt` - All dependencies
- [ ] `.env.example` - Template for buyers

---

## 📋 Files to Update Before Sale (Optional)

### 1. Update Contact Information

If you want buyers to contact you:

**Files to check:**
- `docs/setup.md` - Contact info
- `docs/deployment.md` - Support email
- `.env.example` - Comments

### 2. Update Repository URL

In `README.md`, update the clone URL:
```bash
# Find and replace:
https://github.com/studymatter010-creator/AIclipper_pro.git
# with your actual repo URL
```

### 3. Update Branding (Optional)

To white-label:
- `frontend/index.html` - App title
- `frontend/css/` - Custom colors/logo
- `backend/api/app.py` - API title

---

## 📦 Recommended Distribution Package

### Option A: GitHub Repository
1. Create a new GitHub repository
2. Push clean code (following this checklist)
3. Set as Private or Public with license
4. Add GitHub release with .zip download

### Option B: Direct File Transfer
1. Create a .zip excluding user data:
   ```bash
   # On Linux/Mac
   zip -r aiclipper-source.zip . -x "*.db" "*.log" "uploads/*" "outputs/*" ".venv/*" ".git/*"
   ```
2. Send to buyer

### Option C: Code Marketplace
1. Prepare .zip file
2. Upload to CodeCanyon, Flippa, etc.
3. Include license key system if selling multiple copies

---

## 💰 Pricing Reference

**Factors affecting price:**
- Number of features (AI, transcription, audio remix)
- Documentation quality (✅ comprehensive)
- Code organization (✅ modular, well-structured)
- Testing coverage (✅ has test suite)
- Deployment ready (✅ has Docker config)

**Typical price range for similar apps:**
- Basic version: $500 - $1,500
- Full-featured (like this): $2,000 - $5,000
- With support packages: $5,000 - $10,000+

---

## 📝 Quick Commands Summary

```bash
# Clean everything before sale
cd AIClipper
rm -f data/*.db data/*.db-journal
rm -rf uploads/* outputs/* subtitles/* thumbnails/* logs/* temp/*
rm -rf .venv  # Optional - buyer creates their own

# Verify .gitignore is working
git check-ignore -v .env data/aiclipper.db

# Create distribution zip
zip -r aiclipper-v1.0.zip . -x "*.db" "*.log" "uploads/*" "outputs/*" ".venv/*" ".git/*" "node_modules/*"

# Verify zip contents
unzip -l aiclipper-v1.0.zip | head -50
```

---

## ✅ Final Verification

Before listing, confirm:
- [ ] No .env file included
- [ ] No database files included  
- [ ] No user videos/clips included
- [ ] README is professional and complete
- [ ] LICENSE has correct year (2026)
- [ ] .gitignore covers all sensitive paths
- [ ] Code compiles without errors
- [ ] You have a way for buyers to contact you

---

**You're ready to sell! 🎉**