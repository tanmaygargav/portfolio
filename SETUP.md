# Portfolio Setup Guide

## 1. Install Python dependencies

```bash
pip install requests python-dotenv
```

---

## 2. Set up Strava API

1. Go to https://www.strava.com/settings/api
2. Create an app — set "Authorization Callback Domain" to `localhost`
3. Copy your **Client ID** and **Client Secret**
4. Run this one-time OAuth to get your refresh token:

```bash
# Replace YOUR_CLIENT_ID with your actual Client ID
open "https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all"
```

5. After approving, you'll be redirected to a URL like:
   `http://localhost/?code=XXXXXXXXXX&scope=...`
   Copy the `code` value.

6. Exchange the code for a refresh token:

```bash
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=CODE_FROM_STEP_5 \
  -d grant_type=authorization_code
```

7. Copy the `refresh_token` from the response.

---

## 3. Set up Google Sheets

1. Create a new Google Sheet
2. Add three tabs named exactly: **Skills**, **Content**, **Photos**

### Skills tab columns:
| Category | Skill | Level | Icon |
|----------|-------|-------|------|
| Languages | Python | daily | 🐍 |
| Languages | JavaScript | daily | ⚡ |

Level values: `daily` / `proficient` / `familiar`

### Content tab columns:
| Key | Value |
|-----|-------|
| bio | Your bio text here |
| currentRole | Software Engineer |
| company | 47Billion |
| location | India |
| yearsOfExperience | 5 |
| photographyDescription | Moments captured between miles. |

### Photos tab columns:
| File | Title | Location | Date | Tags |
|------|-------|----------|------|------|
| sunset.jpg | Mountain Trail | Himalayas | 2025-11-10 | landscape,hiking |
| https://drive.google.com/file/d/ABC123/view | City Lights | Mumbai | 2026-01-15 | urban,night |

The **File** column accepts:
- **Local filenames** (e.g., `sunset.jpg`) — must exist in the `photos/` folder
- **Full URLs** (e.g., `https://...`) — used as-is; Google Drive share links are auto-converted to direct image URLs
- **Column aliases**: `File`, `Filename`, or `URL` all work

**Hosting photos externally (recommended for large collections):**
1. Upload photos to Google Drive
2. Right-click → Share → "Anyone with the link"
3. Paste the share URL in the File column — `sync.py` converts it automatically

3. Go to **File → Share → Publish to web** → publish the entire document
4. Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/**SHEET_ID**/edit`

---

## 4. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

---

## 5. Test the sync

```bash
# Dry run — fetch data but don't push to GitHub
python sync.py --dry-run --manual

# Full manual sync
python sync.py --manual
```

---

## 6. Set up GitHub Pages

```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git add .
git commit -m "initial portfolio"
git push -u origin main
```

Then on GitHub: **Settings → Pages → Source: main branch / root**

Your portfolio will be live at: `https://YOUR_USERNAME.github.io/YOUR_REPO`

---

## 7. Schedule daily auto-sync (Mac cron)

```bash
# Open crontab
crontab -e

# Add this line — runs every day at 6:00 AM
0 6 * * * cd /path/to/your/Portfolio && /usr/bin/python3 sync.py >> sync.log 2>&1
```

Replace `/path/to/your/Portfolio` with the actual path to this folder.

---

## Manual sync (anytime)

```bash
python sync.py --manual
```

---

## Updating your portfolio content

| What to update | Where |
|----------------|-------|
| Name, title, tagline, colors | `config.json` |
| Skills | Google Sheet → Skills tab |
| Bio, about text | Google Sheet → Content tab |
| Photos | Drop files in `photos/` folder, add rows in Sheet → Photos tab |
| Fitness data | Automatic via Strava |
| Show/hide sections | `config.json` → sections array |
