#!/usr/bin/env python3
"""
Portfolio Sync Script
─────────────────────────────────────────────────────────
Fetches data from Strava CSV export and Google Sheets,
generates JSON data files in /data, then commits
and pushes to GitHub.

Usage:
  python sync.py                        # auto mode (used by cron)
  python sync.py --manual               # manual run, verbose output
  python sync.py --dry-run              # fetch + generate, skip git push
  python sync.py --csv activities.csv   # use a specific CSV file

Strava CSV Export:
  1. Go to Strava → Settings → My Account → Download or Delete
  2. Request your archive — Strava emails a zip file
  3. Extract activities.csv from the zip
  4. Place it at: strava_export/activities.csv  (or pass --csv path)
  5. Run: python sync.py --manual

Requirements:
  pip install requests python-dotenv
"""

import os
import sys
import json
import csv
import io
import subprocess
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    print("Missing dependencies. Run: pip install requests python-dotenv")
    sys.exit(1)

# ─── SETUP ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "")
GOOGLE_SHEETS_ID     = os.getenv("GOOGLE_SHEETS_ID", "")
AUTO_PUSH            = os.getenv("AUTO_PUSH", "true").lower() == "true"

TOKEN_CACHE = ROOT / ".strava_token_cache.json"

# ─── ARGS & LOGGING ──────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Portfolio sync script")
parser.add_argument("--manual",  action="store_true", help="Verbose output for manual runs")
parser.add_argument("--dry-run", action="store_true", help="Fetch data but skip git push")
parser.add_argument("--csv",     metavar="PATH",      help="Path to Strava activities.csv export")
args = parser.parse_args()

# Default CSV path if not specified
STRAVA_CSV_DEFAULT = ROOT / "strava_export" / "activities.csv"
STRAVA_CSV_PATH    = Path(args.csv) if args.csv else STRAVA_CSV_DEFAULT

log_level = logging.DEBUG if args.manual else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("sync")

# ─── STRAVA ──────────────────────────────────────────────────────────────────

def strava_refresh_access_token():
    """Exchange refresh token for a new access token."""
    log.info("Refreshing Strava access token…")
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type":    "refresh_token",
            "refresh_token": STRAVA_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token_data = resp.json()

    # Cache the new refresh token in case Strava rotates it
    TOKEN_CACHE.write_text(json.dumps(token_data, indent=2))

    # Update .env STRAVA_REFRESH_TOKEN if it changed
    new_refresh = token_data.get("refresh_token", STRAVA_REFRESH_TOKEN)
    if new_refresh != STRAVA_REFRESH_TOKEN:
        _update_env_key("STRAVA_REFRESH_TOKEN", new_refresh)
        log.debug("Strava refresh token rotated — .env updated")

    log.debug("Access token expires at %s", token_data.get("expires_at"))
    return token_data["access_token"]


def _update_env_key(key, value):
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    updated = []
    found = False
    for line in lines:
        if line.startswith(f"{key}="):
            updated.append(f"{key}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{key}={value}")
    env_path.write_text("\n".join(updated) + "\n")


def strava_fetch_activities(access_token, per_page=200):
    """Fetch all activities from Strava (handles pagination)."""
    log.info("Fetching Strava activities…")
    activities = []
    page = 1
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"per_page": per_page, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        activities.extend(batch)
        log.debug("  Page %d → %d activities", page, len(batch))
        if len(batch) < per_page:
            break
        page += 1

    log.info("Fetched %d total Strava activities", len(activities))
    return activities


def _fmt_pace(speed_mps):
    """Convert m/s to min/km string."""
    if not speed_mps or speed_mps <= 0:
        return "--:--"
    secs_per_km = 1000 / speed_mps
    mins = int(secs_per_km // 60)
    secs = int(secs_per_km % 60)
    return f"{mins}:{secs:02d}"


def _fmt_duration(seconds):
    """Convert seconds to h:mm:ss or m:ss string."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _activity_type_key(strava_type):
    """Map Strava sport types to portfolio categories."""
    mapping = {
        "Run":              "running",
        "VirtualRun":       "running",
        "TrailRun":         "running",
        "Ride":             "cycling",
        "VirtualRide":      "cycling",
        "MountainBikeRide": "cycling",
        "GravelRide":       "cycling",
        "Swim":             "swimming",
        "Hike":             "hiking",
        "BackcountrySki":   "hiking",
        "Walk":             "walking",
        "Workout":          "workout",
        "WeightTraining":   "workout",
        "Yoga":             "workout",
        "CrossFit":         "workout",
        "Elliptical":       "workout",
    }
    return mapping.get(strava_type, None)


def process_strava_activities(activities):
    """Process raw Strava activities into portfolio-ready JSON structure."""
    log.info("Processing Strava data…")

    by_type = {
        "running":  {"activities": [], "monthlyKm": defaultdict(float)},
        "cycling":  {"activities": [], "monthlyKm": defaultdict(float)},
        "swimming": {"activities": [], "monthlyKm": defaultdict(float)},
        "hiking":   {"activities": [], "monthlyKm": defaultdict(float)},
        "walking":  {"activities": [], "monthlyKm": defaultdict(float)},
        "workout":  {"activities": [], "monthlyKm": defaultdict(float)},
    }
    all_clean = []

    for a in activities:
        key = _activity_type_key(a.get("sport_type") or a.get("type", ""))
        if key is None:
            continue

        dist_km  = round(a.get("distance", 0) / 1000, 2)
        elev_m   = round(a.get("total_elevation_gain", 0), 1)
        duration = a.get("moving_time", 0)
        date_str = (a.get("start_date_local") or a.get("start_date", ""))[:10]
        month_key = date_str[:7]  # YYYY-MM

        clean = {
            "id":         a.get("id"),
            "name":       a.get("name", "Activity"),
            "type":       key,
            "date":       date_str,
            "distanceKm": dist_km,
            "durationFmt": _fmt_duration(duration),
            "elevationM": elev_m,
            "avgSpeedKph": round((a.get("average_speed", 0) or 0) * 3.6, 1),
            "avgPace":    _fmt_pace(a.get("average_speed", 0)) if key == "running" else None,
            "stravaUrl":  f"https://www.strava.com/activities/{a.get('id')}",
        }

        by_type[key]["activities"].append(clean)
        by_type[key]["monthlyKm"][month_key] += dist_km
        all_clean.append(clean)

    # Sort each type by date descending
    for key in by_type:
        by_type[key]["activities"].sort(key=lambda x: x["date"], reverse=True)

    # Build summary stats
    def running_stats(acts):
        pbs = {"5k": None, "10k": None, "half": None, "full": None}
        races = []
        for a in acts:
            d = a["distanceKm"]
            # Label races by distance bracket
            if 4.8 <= d <= 5.2:
                if not pbs["5k"]:
                    pbs["5k"] = {"time": a["durationFmt"], "date": a["date"]}
            elif 9.8 <= d <= 10.2:
                if not pbs["10k"]:
                    pbs["10k"] = {"time": a["durationFmt"], "date": a["date"]}
            elif 20 <= d <= 22.5:
                if not pbs["half"]:
                    pbs["half"] = {"time": a["durationFmt"], "date": a["date"]}
                races.append({**a, "raceLabel": "Half Marathon"})
            elif 41 <= d <= 43:
                if not pbs["full"]:
                    pbs["full"] = {"time": a["durationFmt"], "date": a["date"]}
                races.append({**a, "raceLabel": "Marathon"})

        # Fill missing PBs with placeholder
        for k in pbs:
            if pbs[k] is None:
                pbs[k] = {"time": "--:--", "date": ""}

        total_dist = sum(a["distanceKm"] for a in acts)
        total_secs = sum(
            int(a["durationFmt"].split(":")[-1])
            + int(a["durationFmt"].split(":")[-2]) * 60
            + (int(a["durationFmt"].split(":")[0]) * 3600 if a["durationFmt"].count(":") == 2 else 0)
            for a in acts
        )
        return {
            "totalRuns": len(acts),
            "totalDistanceKm": round(total_dist, 1),
            "totalHours": round(total_secs / 3600, 1),
            "personalBests": pbs,
            "races": races[:20],
        }

    def generic_stats(acts, key_label):
        total_dist = sum(a["distanceKm"] for a in acts)
        total_elev = sum(a["elevationM"] for a in acts)
        longest = max((a["distanceKm"] for a in acts), default=0)
        return {
            f"total{key_label}": len(acts),
            "totalDistanceKm": round(total_dist, 1),
            "totalElevationM": round(total_elev, 1),
            f"longest{key_label}Km": round(longest, 1),
        }

    def monthly_series(monthly_km_dict, last_n=12):
        """Return last N months as [{month, km}] sorted ascending."""
        all_months = sorted(monthly_km_dict.keys())
        last = all_months[-last_n:] if len(all_months) > last_n else all_months
        return [{"month": m, "km": round(monthly_km_dict[m], 1)} for m in last]

    run_acts  = by_type["running"]["activities"]
    cyc_acts  = by_type["cycling"]["activities"]
    swm_acts  = by_type["swimming"]["activities"]
    hik_acts  = by_type["hiking"]["activities"]
    wlk_acts  = by_type["walking"]["activities"]
    wkt_acts  = by_type["workout"]["activities"]

    r_stats = running_stats(run_acts)
    output = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "totalActivities": len(all_clean),
            "totalDistanceKm": round(sum(a["distanceKm"] for a in all_clean), 1),
            "totalElevationM": round(sum(a["elevationM"] for a in all_clean), 1),
            "totalHours":      round(sum(
                int(a["durationFmt"].split(":")[-1])
                + int(a["durationFmt"].split(":")[-2]) * 60
                + (int(a["durationFmt"].split(":")[0]) * 3600 if a["durationFmt"].count(":") == 2 else 0)
                for a in all_clean
            ) / 3600, 1),
        },
        "byType": {
            "running": {
                "stats": {
                    "totalRuns":       r_stats["totalRuns"],
                    "totalDistanceKm": r_stats["totalDistanceKm"],
                    "totalHours":      r_stats["totalHours"],
                    "personalBests":   r_stats["personalBests"],
                },
                "races":             r_stats["races"],
                "recentActivities":  run_acts[:10],
                "monthlyKm":         monthly_series(by_type["running"]["monthlyKm"]),
            },
            "cycling": {
                "stats": generic_stats(cyc_acts, "Rides"),
                "recentActivities": cyc_acts[:10],
                "monthlyKm":        monthly_series(by_type["cycling"]["monthlyKm"]),
            },
            "swimming": {
                "stats": {
                    **generic_stats(swm_acts, "Sessions"),
                    "poolSessions":      len([a for a in swm_acts if a["distanceKm"] < 2]),   # pool swims < 2km
                    "openWaterSessions": len([a for a in swm_acts if a["distanceKm"] >= 2]),  # open water >= 2km
                },
                "recentActivities": swm_acts[:10],
                "monthlyKm":        monthly_series(by_type["swimming"]["monthlyKm"]),
            },
            "hiking": {
                "stats": generic_stats(hik_acts, "Hikes"),
                "recentActivities": hik_acts[:10],
                "monthlyKm":        monthly_series(by_type["hiking"]["monthlyKm"]),
            },
            "walking": {
                "stats": {
                    **generic_stats(wlk_acts, "Walks"),
                    "totalWalks": len(wlk_acts),
                },
                "recentActivities": wlk_acts[:10],
                "monthlyKm":        monthly_series(by_type["walking"]["monthlyKm"]),
            },
            "workout": {
                "stats": {
                    "totalWorkouts": len(wkt_acts),
                    "totalHours": round(sum(
                        int(a["durationFmt"].split(":")[-1])
                        + int(a["durationFmt"].split(":")[-2]) * 60
                        + (int(a["durationFmt"].split(":")[0]) * 3600 if a["durationFmt"].count(":") == 2 else 0)
                        for a in wkt_acts
                    ) / 3600, 1),
                },
                "recentActivities": wkt_acts[:10],
            },
        },
        "recentAll": sorted(all_clean, key=lambda x: x["date"], reverse=True)[:20],
    }
    return output


# ─── STRAVA CSV EXPORT ───────────────────────────────────────────────────────

def _parse_strava_date(raw):
    """Parse Strava's various date formats into YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    formats = [
        "%b %d, %Y, %I:%M:%S %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10]


def _col(row, *keys):
    """Get first matching column from a CSV row (handles column name variations)."""
    for key in keys:
        if key in row and row[key].strip():
            return row[key].strip()
    return ""


def process_strava_csv(csv_path):
    """
    Parse Strava bulk export activities.csv and return
    the same structure as process_strava_activities().
    """
    log.info("Reading Strava CSV: %s", csv_path)

    if not Path(csv_path).exists():
        log.error("CSV file not found: %s", csv_path)
        return None

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    log.info("Found %d rows in CSV", len(rows))

    raw_activities = []

    for row in rows:
        raw_type = _col(row, "Activity Type", "Sport Type", "Type")
        key = _activity_type_key(raw_type)
        if key is None:
            continue

        # Distance
        raw_dist = _col(row, "Distance", "Distance.1")
        try:
            dist_raw = float(raw_dist.replace(",", "") or 0)
            dist_km  = round(dist_raw / 1000, 2)  # Strava CSV always exports distance in meters
        except ValueError:
            dist_km = 0.0

        # Duration
        raw_time = _col(row, "Moving Time", "Elapsed Time")
        try:
            duration_secs = int(float(raw_time.replace(",", "") or 0))
        except ValueError:
            duration_secs = 0

        # Elevation
        raw_elev = _col(row, "Elevation Gain", "Total Elevation Gain")
        try:
            elev_m = round(float(raw_elev.replace(",", "") or 0), 1)
        except ValueError:
            elev_m = 0.0

        # Speed
        raw_speed = _col(row, "Average Speed", "Average Speed.1")
        try:
            speed_mps = float(raw_speed.replace(",", "") or 0)
            if speed_mps > 10 and key != "cycling":
                speed_mps = speed_mps / 3.6
        except ValueError:
            speed_mps = 0.0

        date_str = _parse_strava_date(_col(row, "Activity Date", "Start Date", "Date"))
        act_id   = _col(row, "Activity ID", "ID")
        act_name = _col(row, "Activity Name", "Name") or "Activity"

        raw_activities.append({
            "id":           act_id,
            "name":         act_name,
            "type":         raw_type,
            "sport_type":   raw_type,
            "start_date_local": date_str,
            "distance":     dist_km * 1000,
            "moving_time":  duration_secs,
            "total_elevation_gain": elev_m,
            "average_speed": speed_mps,
        })

    log.info("Parsed %d matching activities from CSV", len(raw_activities))
    return process_strava_activities(raw_activities)


# ─── GOOGLE SHEETS ───────────────────────────────────────────────────────────

def sheets_fetch_tab(sheet_id, tab_name):
    """Fetch a Google Sheets tab published as CSV."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={tab_name}"
    )
    log.debug("Fetching Sheets tab: %s", tab_name)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def process_skills(rows):
    """Convert Skills sheet rows into skills.json structure."""
    categories = defaultdict(list)
    for row in rows:
        name     = (row.get("Skill") or row.get("Name") or "").strip()
        category = (row.get("Category") or "Other").strip()
        level    = (row.get("Level") or "familiar").strip().lower()
        icon     = (row.get("Icon") or "🔧").strip()
        if name:
            categories[category].append({"name": name, "level": level, "icon": icon})

    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "categories": [
            {"name": cat, "skills": skills}
            for cat, skills in categories.items()
        ],
        "levels": {
            "daily":      {"label": "Daily Use",  "color": "#00d4ff"},
            "proficient": {"label": "Proficient", "color": "#00ff87"},
            "familiar":   {"label": "Familiar",   "color": "#888888"},
        },
    }


def process_content(rows):
    """Convert Content sheet rows into content.json structure."""
    data = {r.get("Key", ""): r.get("Value", "") for r in rows if r.get("Key")}
    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "about": {
            "bio":               data.get("bio", ""),
            "currentRole":       data.get("currentRole", ""),
            "company":           data.get("company", ""),
            "location":          data.get("location", ""),
            "yearsOfExperience": data.get("yearsOfExperience", ""),
        },
        "photography": {
            "description": data.get("photographyDescription", ""),
            "photos":      [],
        },
    }


def _is_url(val):
    """Check if a string looks like a full URL."""
    return val.startswith("http://") or val.startswith("https://")


def _gdrive_direct_url(url):
    """Convert Google Drive sharing URL to a direct image URL."""
    # Handle: https://drive.google.com/file/d/FILE_ID/view?...
    import re
    m = re.match(r"https://drive\.google\.com/file/d/([^/]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=view&id={m.group(1)}"
    # Handle: https://drive.google.com/open?id=FILE_ID
    m = re.match(r"https://drive\.google\.com/open\?id=([^&]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=view&id={m.group(1)}"
    return url


def process_photos(rows):
    """Convert Photos sheet rows into photos.json structure.

    The File/URL column supports:
      - Full URLs (https://...) — used as-is (Google Drive links auto-converted)
      - Local filenames (photo.jpg) — prefixed with photos/
    """
    photos = []
    for i, row in enumerate(rows, 1):
        file_val = (row.get("File") or row.get("Filename") or row.get("URL") or "").strip()
        if not file_val:
            continue

        # Resolve file path: full URL or local filename
        if _is_url(file_val):
            src = _gdrive_direct_url(file_val)
        else:
            src = f"photos/{file_val}"

        photos.append({
            "id":       i,
            "file":     src,
            "title":    (row.get("Title") or "").strip(),
            "location": (row.get("Location") or "").strip(),
            "date":     (row.get("Date") or "").strip(),
            "tags":     [t.strip() for t in (row.get("Tags") or "").split(",") if t.strip()],
        })

    log.info("Processed %d photos", len(photos))
    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "photos": photos,
    }


# ─── GIT ─────────────────────────────────────────────────────────────────────

def git_push(dry_run=False):
    """Commit and push all changes in /data to GitHub."""
    log.info("Committing data files to Git…")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    cmds = [
        ["git", "-C", str(ROOT), "add", "data/"],
        ["git", "-C", str(ROOT), "commit", "-m", f"sync: portfolio data update {ts}"],
        ["git", "-C", str(ROOT), "push"],
    ]
    if dry_run:
        log.info("DRY RUN — skipping git push")
        for cmd in cmds:
            log.debug("  would run: %s", " ".join(cmd))
        return

    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
                log.info("Nothing new to commit — data unchanged")
                return
            log.error("Git error: %s", result.stderr.strip())
            return
        log.debug("  %s", result.stdout.strip())
    log.info("✓ Pushed to GitHub")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    log.info("━━━ Portfolio Sync Started (%s) ━━━", "manual" if args.manual else "auto")
    errors = []

    # ── Strava ───────────────────────────────────────────────
    if STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN:
        try:
            token   = strava_refresh_access_token()
            raw     = strava_fetch_activities(token)
            strava  = process_strava_activities(raw)
            (DATA / "strava.json").write_text(json.dumps(strava, indent=2, ensure_ascii=False))
            log.info("✓ strava.json written (%d activities)", len(raw))
        except Exception as e:
            log.error("Strava API sync failed: %s", e)
            errors.append("strava")
    elif STRAVA_CSV_PATH.exists():
        # ── CSV export mode ───────────────────────────────────
        log.info("Using Strava CSV export: %s", STRAVA_CSV_PATH)
        try:
            strava = process_strava_csv(STRAVA_CSV_PATH)
            if strava:
                (DATA / "strava.json").write_text(json.dumps(strava, indent=2, ensure_ascii=False))
                log.info("✓ strava.json written from CSV (%d total activities)",
                         strava["summary"]["totalActivities"])
            else:
                log.error("CSV processing returned no data")
                errors.append("strava-csv")
        except Exception as e:
            log.error("Strava CSV sync failed: %s", e)
            errors.append("strava-csv")
    else:
        log.warning("No Strava source found — place activities.csv in strava_export/ folder")

    # ── Google Sheets ─────────────────────────────────────────
    if GOOGLE_SHEETS_ID:
        sheet_tasks = [
            ("Skills",  process_skills,  "skills.json"),
            ("Content", process_content, "content.json"),
            ("Photos",  process_photos,  "photos.json"),
        ]
        for tab, processor, filename in sheet_tasks:
            try:
                rows = sheets_fetch_tab(GOOGLE_SHEETS_ID, tab)
                data = processor(rows)
                (DATA / filename).write_text(json.dumps(data, indent=2, ensure_ascii=False))
                log.info("✓ %s written (%d rows)", filename, len(rows))
            except Exception as e:
                log.error("Sheets '%s' sync failed: %s", tab, e)
                errors.append(tab.lower())
    else:
        log.warning("GOOGLE_SHEETS_ID not set — skipping Sheets sync")

    # ── Git push ──────────────────────────────────────────────
    if AUTO_PUSH or args.manual:
        git_push(dry_run=args.dry_run)

    # ── Summary ───────────────────────────────────────────────
    if errors:
        log.warning("Sync completed with errors in: %s", ", ".join(errors))
    else:
        log.info("━━━ Sync Complete ✓ ━━━")


if __name__ == "__main__":
    main()
