"""
garmin_sync.py — read-only sync of your own Garmin Connect data to local files.

Two commands:

    python garmin_sync.py login          # one-time: log in, handle 2FA, save token
    python garmin_sync.py sync --days 3  # pull recent data into ./garmin/

It NEVER writes anything back to Garmin. It only reads.

Your login token is stored locally in ./token so you don't have to log in again.
Your password is never saved to disk and never printed.
"""

import argparse
import getpass
import json
import math
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

try:
    from garminconnect import Garmin
except ImportError:
    print("The 'garminconnect' library isn't installed yet.")
    print("Run:  python -m pip install -r requirements.txt")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_DIR = os.path.join(HERE, "token")
OUT_DIR = os.path.join(HERE, "garmin")
WELLNESS_DIR = os.path.join(OUT_DIR, "wellness")
WORKOUTS_DIR = os.path.join(OUT_DIR, "workouts")

# All daily wellness is bucketed by Eric's local calendar date. Setting this
# explicitly (instead of relying on the machine clock) keeps dates correct even
# if the sync runs on a UTC server.
LOCAL_TZ = ZoneInfo("Europe/Helsinki")


def local_today():
    return datetime.now(LOCAL_TZ).date()


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def _prompt_mfa():
    return input("Enter the 6-digit code Garmin just sent you (email/text): ").strip()


def login_interactive():
    """First-time login. Prompts for email + password locally (hidden), handles
    2FA, then saves a reusable token to ./token."""
    print("=" * 60)
    print(" Garmin one-time login")
    print("=" * 60)
    print("Your password is typed here, not into the chat. It is used once to")
    print("get a login token, then discarded. It is never saved or printed.\n")

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden as you type): ")

    if not email or not password:
        print("\nEmail and password are both required. Nothing was changed.")
        sys.exit(1)

    os.makedirs(TOKEN_DIR, exist_ok=True)

    try:
        try:
            # Modern garminconnect API (return_on_mfa + resume_login)
            garmin = Garmin(email=email, password=password, return_on_mfa=True)
            result = garmin.login()
            if isinstance(result, tuple) and result[0] == "needs_mfa":
                code = _prompt_mfa()
                garmin.resume_login(result[1], code)
        except TypeError:
            # Older API that takes a prompt_mfa callback instead
            garmin = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
            garmin.login()

        # Persist the token so future syncs don't need the password.
        # (garminconnect exposes the underlying garth session as .client; older
        # versions used .garth — support both so this survives upgrades.)
        _dumper = getattr(garmin, "garth", None) or getattr(garmin, "client", None)
        _dumper.dump(TOKEN_DIR)
    except Exception as e:  # noqa: BLE001
        # Write a diagnostic (no password — getpass never echoes it) so the
        # problem can be read back without another blind attempt.
        import traceback

        log_path = os.path.join(HERE, "login_error.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        print("\n" + "!" * 60)
        print("Login did NOT complete. Reason:")
        print(f"  {type(e).__name__}: {e}")
        print(f"\nDetails were written to: {log_path}")
        print("!" * 60)
        password = None
        del password
        sys.exit(1)

    # Wipe password reference.
    password = None
    del password

    # Clear any stale error log from a previous failed attempt.
    old_log = os.path.join(HERE, "login_error.log")
    if os.path.exists(old_log):
        try:
            os.remove(old_log)
        except OSError:
            pass

    print("\nLogin successful. Token saved to the 'token' folder.")
    print("You won't need to log in again unless Garmin expires the token.")


def load_client():
    """Resume a session from the saved token. In the cloud (GitHub Actions) the
    token comes from the GARMIN_TOKEN_B64 secret; locally it's the ./token dir."""
    tok_b64 = os.environ.get("GARMIN_TOKEN_B64")
    if tok_b64:
        import base64
        os.makedirs(TOKEN_DIR, exist_ok=True)
        with open(os.path.join(TOKEN_DIR, "garmin_tokens.json"), "wb") as f:
            f.write(base64.b64decode(tok_b64))
    if not os.path.isdir(TOKEN_DIR) or not os.listdir(TOKEN_DIR):
        print("No saved login found. Double-click 'Login to Garmin.bat' first")
        print("(or run: python garmin_sync.py login).")
        sys.exit(1)
    garmin = Garmin()
    garmin.login(TOKEN_DIR)
    return garmin


def export_token():
    """Print the local Garmin token as base64 for use as a GitHub secret."""
    path = os.path.join(TOKEN_DIR, "garmin_tokens.json")
    if not os.path.exists(path):
        print("Ingen token hittad. Logga in forst (Login to Garmin.bat).")
        sys.exit(1)
    import base64
    with open(path, "rb") as f:
        print(base64.b64encode(f.read()).decode())


# --------------------------------------------------------------------------- #
# Supabase (writes to the MinLoparApp project, in addition to the markdown)
# --------------------------------------------------------------------------- #
SUPABASE_CONFIG = os.path.join(TOKEN_DIR, "supabase.json")
SUPABASE_URL = "https://lqfrdgwosiuxennsedxn.supabase.co"
TREADMILL_TYPES = {"treadmill_running", "indoor_running", "virtual_run"}


def set_supabase_key():
    """Store the Supabase service_role key locally (kept out of the chat)."""
    print("=" * 60)
    print(" Supabase service_role key")
    print("=" * 60)
    print("Paste your service_role key (Supabase > Project Settings > API).")
    print("It is stored locally in token/supabase.json and never printed.\n")
    key = getpass.getpass("service_role key (hidden as you paste): ").strip()
    if not key:
        print("Nothing entered. Aborted, nothing changed.")
        sys.exit(1)
    os.makedirs(TOKEN_DIR, exist_ok=True)
    with open(SUPABASE_CONFIG, "w", encoding="utf-8") as f:
        json.dump({"url": SUPABASE_URL, "service_key": key}, f)
    key = None
    del key
    print("\nSaved. The sync will now also write to Supabase.")


def load_supabase():
    """Return (url, service_key) if configured, else (None, None). In the cloud
    the key comes from the SUPABASE_SERVICE_KEY secret."""
    env_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if env_key:
        return os.environ.get("SUPABASE_URL", SUPABASE_URL), env_key
    if not os.path.exists(SUPABASE_CONFIG):
        return None, None
    try:
        with open(SUPABASE_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("url") or SUPABASE_URL, cfg.get("service_key")
    except (OSError, ValueError):
        return None, None


def _sb_headers(key, prefer=None):
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def sb_upsert(url, key, table, row, on_conflict, return_rep=False):
    prefer = "resolution=merge-duplicates"
    if return_rep:
        prefer += ",return=representation"
    r = requests.post(
        f"{url}/rest/v1/{table}",
        headers=_sb_headers(key, prefer),
        params={"on_conflict": on_conflict},
        data=json.dumps([row], default=str),
        timeout=30,
    )
    r.raise_for_status()
    if return_rep and r.text:
        data = r.json()
        return data[0] if data else None
    return None


def push_wellness_supabase(url, key, w):
    d = w["date"]

    def mins(sec):
        return int(round(sec / 60)) if sec else None

    sb_upsert(url, key, "sleep_hrv_metrics", {
        "date": d,
        "hrv_rmssd": w.get("hrv_last_night_avg"),
        "hrv_status": w.get("hrv_status"),
        "hrv_weekly_avg": w.get("hrv_weekly_avg"),
        "sleep_score": w.get("sleep_score"),
        "sleep_total_min": mins(w.get("sleep_seconds")),
        "sleep_deep_min": mins(w.get("sleep_deep_seconds")),
        "sleep_light_min": mins(w.get("sleep_light_seconds")),
        "sleep_rem_min": mins(w.get("sleep_rem_seconds")),
        "sleep_awake_min": mins(w.get("sleep_awake_seconds")),
        "resting_hr": w.get("resting_hr"),
    }, on_conflict="date")

    sb_upsert(url, key, "daily_recovery_metrics", {
        "date": d,
        "body_battery_high": w.get("body_battery_high"),
        "body_battery_low": w.get("body_battery_low"),
        "body_battery_latest": w.get("body_battery_recent"),
        "readiness_score": w.get("training_readiness_score"),
        "readiness_level": w.get("training_readiness_level"),
        "readiness_feedback": w.get("training_readiness_feedback"),
        "avg_stress": w.get("stress_avg"),
        "max_stress": w.get("stress_max"),
        "steps": w.get("steps"),
    }, on_conflict="date")


# --- Derived metrics stored as a historical snapshot in Supabase (the app
# recomputes them live from settings; these give an AI ready-made history). ---
SENB_REF = 315  # sec/km (5:15) reference anchor for senbelastning (LPK 1.0)
SENB_MAX = 180  # sec/km (3:00) max anchor (LPK 7.0)
# Representative pace (sec/km) per pace zone Z0..Z6 — mirrors the app.
SENB_PACE_ZONE_REP = [360, 307, 272, 245, 222, 202, 180]


def load_index(avg_hr, duration_s):
    """Simple training load: minutes x relative intensity (avg HR / max HR)."""
    if not avg_hr or not duration_s:
        return None
    return round(duration_s / 60 * (avg_hr / MAX_HR), 1)


def tendon_load_index(pace_zones):
    """Senbelastning from time-in-pace-zones: per zone distance x LPK(zone pace)."""
    k = math.log(7.0) / (SENB_REF / 60 - SENB_MAX / 60)
    total = 0.0
    for z in range(0, 7):
        sec = pace_zones.get(z, 0) or 0
        if sec <= 0:
            continue
        rep = SENB_PACE_ZONE_REP[z]
        km = sec / rep
        total += km * math.exp(k * (SENB_REF / 60 - rep / 60))
    return round(total, 2) if total > 0 else None


def push_workout_supabase(url, key, a, intensity):
    """Upsert one run + replace its blocks. Returns number of blocks written."""
    type_key = (a.get("activityType") or {}).get("typeKey")
    hz = intensity.get("hr_zones", {})
    pz = intensity.get("pace_zones", {})
    date_local = (a.get("startTimeLocal") or "")[:10] or None
    dist_m = a.get("distance")

    def i(v):
        return int(round(v)) if isinstance(v, (int, float)) else None

    row = {
        "garmin_activity_id": a.get("activityId"),
        "date": date_local,
        "distance_km": round(dist_m / 1000.0, 2) if dist_m else None,
        "duration_seconds": i(a.get("duration")),
        "avg_hr": i(a.get("averageHR")),
        "max_hr": i(a.get("maxHR")),
        "zone_1_sec": hz.get(1), "zone_2_sec": hz.get(2), "zone_3_sec": hz.get(3),
        "zone_4_sec": hz.get(4), "zone_5_sec": hz.get(5),
        "zone_0_sec": hz.get(0),
        "pace_zone_0_sec": pz.get(0), "pace_zone_1_sec": pz.get(1),
        "pace_zone_2_sec": pz.get(2), "pace_zone_3_sec": pz.get(3),
        "pace_zone_4_sec": pz.get(4), "pace_zone_5_sec": pz.get(5),
        "pace_zone_6_sec": pz.get(6),
        "training_effect_aerobic": a.get("aerobicTrainingEffect"),
        "training_effect_anaerobic": a.get("anaerobicTrainingEffect"),
        "sport_type": type_key,
        "is_treadmill": type_key in TREADMILL_TYPES,
        # Derived snapshots for historical AI analysis.
        "load_index": load_index(a.get("averageHR"), a.get("duration")),
        "tendon_load_index": (
            tendon_load_index(pz) if is_run(type_key) else None
        ),
        "pace_10s": (
            round((a.get("duration") / (dist_m / 1000.0)) / 10) * 10
            if dist_m and a.get("duration")
            else None
        ),
    }
    saved = sb_upsert(url, key, "running_activities", row,
                      on_conflict="garmin_activity_id", return_rep=True)
    if not saved:
        return 0
    activity_row_id = saved.get("id")

    # Replace this activity's blocks so re-syncs don't duplicate.
    requests.delete(
        f"{url}/rest/v1/activity_blocks",
        headers=_sb_headers(key),
        params={"activity_id": f"eq.{activity_row_id}"},
        timeout=30,
    ).raise_for_status()

    blocks = [
        {
            "activity_id": activity_row_id,
            "date": date_local,
            "pace_10s": blk.get("band_low"),
            "avg_hr": blk.get("avg_hr"),
            "duration_sec": blk.get("duration_sec"),
            "distance_km": blk.get("distance_km"),
            "block_order": blk.get("block_order"),
            "seconds_from_start": blk.get("seconds_from_start"),
            # Running Dynamics not recorded by current device -> NULL
            "cadence_spm": None,
            "vertical_osc_mm": None,
            "ground_contact_ms": None,
            "vertical_ratio": None,
        }
        for blk in intensity.get("fartblock", {}).get("blocks", [])
    ]
    if blocks:
        requests.post(
            f"{url}/rest/v1/activity_blocks",
            headers=_sb_headers(key),
            data=json.dumps(blocks, default=str),
            timeout=30,
        ).raise_for_status()
    return len(blocks)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
SAFE_TIMEOUT = 30  # seconds; a single Garmin call may not hang the whole sync


def safe(fn, *args, **kwargs):
    """Call a Garmin endpoint with a hard timeout so one stalled request can't
    freeze the sync. Returns an error marker instead of crashing/hanging. Runs
    in a daemon thread (Windows-safe) that is abandoned on timeout."""
    box = {}

    def run():
        try:
            box["v"] = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            box["v"] = {"_error": str(e)}

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(SAFE_TIMEOUT)
    if t.is_alive():
        return {"_error": "timeout"}
    return box.get("v")


def fmt_hm(seconds):
    if not seconds:
        return None
    seconds = int(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


def slugify(text):
    keep = "-_ "
    cleaned = "".join(c if c.isalnum() or c in keep else "-" for c in str(text))
    return "-".join(cleaned.split())[:60] or "activity"


def meters_to_km(m):
    if m in (None, 0):
        return None
    return round(m / 1000.0, 2)


def pace_per_km(distance_m, duration_s):
    if not distance_m or not duration_s:
        return None
    km = distance_m / 1000.0
    if km <= 0:
        return None
    sec_per_km = duration_s / km
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d} /km"


# Pace-zone upper bounds in seconds per km (faster pace = smaller number = higher
# zone). A lap is placed in the fastest zone whose upper bound it does not beat;
# anything slower than Z1's bound (330 s/km, i.e. slower than 5:30/km) is Z0.
#   Z0 Återhämtning >5:30 | Z1 Lugn distansträning 4:45-5:30 | Z2 Aerob 4:20-4:45
#   Z3 Marathontempo 3:50-4:20 | Z4 Tröskel 3:35-3:50 | Z5 VO2max 3:10-3:35
#   Z6 Anaerob <3:10
PACE_ZONE_BOUNDS = [(6, 190), (5, 215), (4, 230), (3, 260), (2, 285), (1, 330)]
PACE_ZONE_NAMES = {
    0: "Återhämtning",
    1: "Lugn distansträning",
    2: "Aerob",
    3: "Marathontempo",
    4: "Tröskel",
    5: "VO2max",
    6: "Anaerob",
}


def pace_zone(sec_per_km):
    if sec_per_km is None:
        return None
    for zone, upper in PACE_ZONE_BOUNDS:
        if sec_per_km <= upper:
            return zone
    return 0  # slower than every bound (>5:30/km) -> recovery


# Heart-rate zones — Olympiatoppen intensity scale, expressed as % of max HR.
# A beat falls in the highest zone whose lower % bound it reaches. Z0 is
# everything below 60% of max HR.
MAX_HR = 200
HR_ZONE_PCT = [(0, 0.00), (1, 0.60), (2, 0.72), (3, 0.82), (4, 0.87), (5, 0.92)]


def hr_zone(bpm):
    if bpm is None:
        return None
    frac = bpm / MAX_HR
    zone = 0
    for z, lower in HR_ZONE_PCT:
        if frac >= lower:
            zone = z
    return zone


def hr_zone_label(z):
    """e.g. '(120–144)' — the bpm span for Olympiatoppen zone z at current MAX_HR."""
    bounds = [round(MAX_HR * p) for _, p in HR_ZONE_PCT]  # [0,120,144,164,174,184]
    if z == 0:
        return f"(<{bounds[1]})"
    if z >= 5:
        return f"({bounds[5]}+)"
    return f"({bounds[z]}–{bounds[z + 1]})"


def fmt_min_sec(seconds):
    """Zone time, e.g. 772 -> '12m 52s'."""
    if seconds is None:
        return "0m 0s"
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60}s"


def fmt_mmss(seconds):
    """Clock/pace time, e.g. 324 -> '5:24'."""
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- Fartblock (speed blocks) — one block per lap (Eric always presses the lap
# button when changing pace). Only for outdoor/trail running. A lap qualifies as
# a block only if it is sustained long enough, then blocks are summed per
# 10 s/km band with an average heart rate.
OUTDOOR_RUN_TYPES = {"running", "trail_running", "street_running", "track_running"}
# Running subtypes where GPS pace is unreliable (excluded from pace-based analysis).
INDOOR_RUN_TYPES = {"treadmill_running", "indoor_running", "virtual_run"}


def is_outdoor_run(type_key):
    """Any running subtype with reliable outdoor pace (running, trail, track,
    street, obstacle, ...) but not treadmill / indoor / virtual. Used for
    Fartblock (excludes treadmill)."""
    return bool(type_key) and "running" in type_key and type_key not in INDOOR_RUN_TYPES


def is_run(type_key):
    """Any running activity INCLUDING treadmill/indoor. Used for senbelastning,
    which should count treadmill sessions (pace comes from the belt/footpod)."""
    return bool(type_key) and ("running" in type_key or type_key == "virtual_run")


def speed_band(pace):
    """10 s/km band lower bound for a pace (sec/km). None if slower than 5:00/km."""
    if pace is None or pace >= 300:
        return None
    return int(pace // 10 * 10)


def block_min_seconds(pace):
    """Minimum sustained duration for a lap to count: 10 min if slower than
    4:20/km, otherwise 150 s."""
    return 600 if pace > 260 else 150


def compute_speed_blocks(laps, type_key):
    result = {"applicable": is_outdoor_run(type_key), "blocks": [], "bands": []}
    if not result["applicable"]:
        return result

    per_band = {}  # band_low -> [total_sec, hr*sec, hr_sec]
    elapsed = 0.0  # seconds from activity start to the current lap's start
    order = 0
    for lap in laps or []:
        dist_m = lap.get("distance")
        dur_s = lap.get("duration")
        hr = lap.get("averageHR")
        lap_start = elapsed
        if dur_s:
            elapsed += dur_s
        if not dist_m or not dur_s or dist_m <= 0:
            continue
        pace = dur_s / (dist_m / 1000.0)
        band = speed_band(pace)
        if band is None or dur_s < block_min_seconds(pace):
            continue
        order += 1
        result["blocks"].append(
            {
                "band": f"{fmt_mmss(band)}–{fmt_mmss(band + 10)}",
                "band_low": band,
                "pace_sec_per_km": int(round(pace)),
                "duration_sec": int(round(dur_s)),
                "distance_km": round(dist_m / 1000.0, 2),
                "avg_hr": int(round(hr)) if hr is not None else None,
                "block_order": order,
                "seconds_from_start": int(round(lap_start)),
            }
        )
        acc = per_band.setdefault(band, [0.0, 0.0, 0.0])
        acc[0] += dur_s
        if hr is not None:
            acc[1] += hr * dur_s
            acc[2] += dur_s

    for band in sorted(per_band):
        tot, hrsum, hrsec = per_band[band]
        result["bands"].append(
            {
                "band": f"{fmt_mmss(band)}–{fmt_mmss(band + 10)}",
                "band_low": band,
                "total_sec": int(round(tot)),
                "avg_hr": int(round(hrsum / hrsec)) if hrsec else None,
            }
        )
    return result


def collect_weather(garmin, activity_id):
    """Temperature and conditions for an activity. Garmin returns Fahrenheit;
    converted to Celsius."""
    w = safe(garmin.get_activity_weather, activity_id)
    if not isinstance(w, dict):
        return {}

    def f_to_c(f):
        return round((f - 32) * 5 / 9) if isinstance(f, (int, float)) else None

    return {
        "temp_c": f_to_c(w.get("temp")),
        "feels_like_c": f_to_c(w.get("apparentTemp")),
        "humidity": w.get("relativeHumidity"),
        "conditions": (w.get("weatherTypeDTO") or {}).get("desc"),
    }


def collect_stream_zones(garmin, activity_id):
    """Time (seconds) in each HR zone (Olympiatoppen, 0-5) and pace zone (0-6),
    integrated second-by-second from the activity's raw HR + speed streams.
    Both are computed the same way so pace-zone time no longer depends on how
    laps were recorded on the watch."""
    hr_zones = {z: 0.0 for z in range(0, 6)}
    pace_zones = {z: 0.0 for z in range(0, 7)}
    empty = {
        "hr_zones": {z: 0 for z in hr_zones},
        "pace_zones": {z: 0 for z in pace_zones},
    }

    det = safe(garmin.get_activity_details, activity_id)
    if not isinstance(det, dict):
        return empty

    descs = det.get("metricDescriptors") or []
    idx = {d.get("key"): d.get("metricsIndex") for d in descs}
    ts_i = idx.get("directTimestamp")
    hr_i = idx.get("directHeartRate")
    sp_i = idx.get("directSpeed")  # m/s
    rows = det.get("activityDetailMetrics") or []
    if ts_i is None or not rows:
        return empty

    def cell(m, i):
        return m[i] if (i is not None and i < len(m)) else None

    prev_ts = prev_hr = prev_sp = None
    for row in rows:
        m = row.get("metrics") or []
        ts = cell(m, ts_i)
        hr = cell(m, hr_i)
        sp = cell(m, sp_i)
        if prev_ts is not None and ts is not None:
            dt = (ts - prev_ts) / 1000.0
            if 0 < dt <= 60:  # cap guards against paused/GPS gaps
                if prev_hr:
                    z = hr_zone(prev_hr)
                    if z is not None:
                        hr_zones[z] += dt
                if prev_sp is not None:
                    pace = 1000.0 / prev_sp if prev_sp > 0 else None
                    pz = pace_zone(pace) if pace is not None else 0
                    pace_zones[pz] += dt
        prev_ts, prev_hr, prev_sp = ts, hr, sp

    return {
        "hr_zones": {z: int(round(v)) for z, v in hr_zones.items()},
        "pace_zones": {z: int(round(v)) for z, v in pace_zones.items()},
    }


def collect_intensity(garmin, activity_id, type_key=None):
    """Fetch HR/pace zones (second-by-second), raw laps, and the lap-based
    Fartblock summary. Every field is defensive — a missing endpoint yields
    empty data rather than crashing the whole sync."""
    result = {
        "hr_zones": {z: 0 for z in range(0, 6)},
        "pace_zones": {z: 0 for z in range(0, 7)},
        "laps": [],
        "fartblock": {"applicable": is_outdoor_run(type_key), "blocks": [], "bands": []},
    }
    if not activity_id:
        return result

    # --- HR + pace zones, both integrated second-by-second from the raw
    # HR/speed streams (Olympiatoppen HR bands, our pace bands). Pace-zone time
    # no longer depends on how laps were pressed on the watch. ---
    stream = collect_stream_zones(garmin, activity_id)
    result["hr_zones"] = stream["hr_zones"]
    result["pace_zones"] = stream["pace_zones"]

    # --- Laps (raw, kept for reference) + Fartblock summary (per 10 s/km band) ---
    splits = safe(garmin.get_activity_splits, activity_id)
    laps = splits.get("lapDTOs") if isinstance(splits, dict) else None
    laps = laps or []
    for lap in laps:
        dist_m = lap.get("distance")
        dur_s = lap.get("duration")
        if not dist_m or not dur_s or dist_m <= 0:
            continue
        avg_hr = lap.get("averageHR")
        result["laps"].append(
            {
                "pace_sec_per_km": int(round(dur_s / (dist_m / 1000.0))),
                "avg_hr": int(round(avg_hr)) if avg_hr is not None else None,
                "duration_sec": int(round(dur_s)),
                "distance_km": round(dist_m / 1000.0, 2),
            }
        )

    result["fartblock"] = compute_speed_blocks(laps, type_key)
    return result


# --------------------------------------------------------------------------- #
# Data extraction (defensive — every field via .get)
# --------------------------------------------------------------------------- #
def collect_wellness(garmin, cdate):
    d = cdate.isoformat()
    stats = safe(garmin.get_stats, d) or {}
    sleep = safe(garmin.get_sleep_data, d) or {}
    hrv = safe(garmin.get_hrv_data, d) or {}
    readiness = safe(garmin.get_training_readiness, d)

    daily_sleep = (sleep or {}).get("dailySleepDTO", {}) or {}
    sleep_scores = daily_sleep.get("sleepScores", {}) or {}
    hrv_summary = (hrv or {}).get("hrvSummary", {}) or {}

    # get_training_readiness returns several intraday snapshots (latest first).
    # Readiness is a morning metric — after a hard session it drops sharply — and
    # the Garmin app shows the morning value for the day. So pick the earliest
    # reading of the day, not the most recent one.
    if isinstance(readiness, list) and readiness:
        tr = min(
            readiness,
            key=lambda r: r.get("timestampLocal") or r.get("timestamp") or "9999",
        )
    elif isinstance(readiness, dict):
        tr = readiness
    else:
        tr = {}

    return {
        "date": d,
        "steps": stats.get("totalSteps"),
        "resting_hr": stats.get("restingHeartRate"),
        "stress_avg": stats.get("averageStressLevel"),
        "stress_max": stats.get("maxStressLevel"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "body_battery_recent": stats.get("bodyBatteryMostRecentValue"),
        "sleep_seconds": daily_sleep.get("sleepTimeSeconds"),
        "sleep_deep_seconds": daily_sleep.get("deepSleepSeconds"),
        "sleep_light_seconds": daily_sleep.get("lightSleepSeconds"),
        "sleep_rem_seconds": daily_sleep.get("remSleepSeconds"),
        "sleep_awake_seconds": daily_sleep.get("awakeSleepSeconds"),
        "sleep_score": (sleep_scores.get("overall", {}) or {}).get("value"),
        "hrv_last_night_avg": hrv_summary.get("lastNightAvg"),
        "hrv_status": hrv_summary.get("status"),
        "hrv_weekly_avg": hrv_summary.get("weeklyAvg"),
        "training_readiness_score": tr.get("score"),
        "training_readiness_level": tr.get("level"),
        "training_readiness_feedback": tr.get("feedbackShort") or tr.get("feedbackLong"),
    }


def wellness_markdown(w):
    def line(label, value, suffix=""):
        return f"- **{label}:** {value}{suffix}" if value not in (None, "") else None

    parts = [
        f"# Recovery — {w['date']}",
        "",
        "## Sleep",
        line("Total sleep", fmt_hm(w["sleep_seconds"])),
        line("Sleep score", w["sleep_score"], " / 100"),
        line("Deep", fmt_hm(w["sleep_deep_seconds"])),
        line("Light", fmt_hm(w["sleep_light_seconds"])),
        line("REM", fmt_hm(w["sleep_rem_seconds"])),
        line("Awake", fmt_hm(w["sleep_awake_seconds"])),
        "",
        "## Heart & HRV",
        line("Resting heart rate", w["resting_hr"], " bpm"),
        line("HRV (last night avg)", w["hrv_last_night_avg"], " ms"),
        line("HRV status", w["hrv_status"]),
        line("HRV weekly avg", w["hrv_weekly_avg"], " ms"),
        "",
        "## Body Battery & Stress",
        line("Body Battery high", w["body_battery_high"]),
        line("Body Battery low", w["body_battery_low"]),
        line("Body Battery most recent", w["body_battery_recent"]),
        line("Average stress", w["stress_avg"]),
        line("Max stress", w["stress_max"]),
        "",
        "## Training Readiness",
        line("Readiness score", w["training_readiness_score"], " / 100"),
        line("Readiness level", w["training_readiness_level"]),
        line("Feedback", w["training_readiness_feedback"]),
        "",
        "## Activity",
        line("Steps", w["steps"]),
        "",
    ]
    return "\n".join(p for p in parts if p is not None) + "\n"


def workout_markdown(a, intensity=None):
    name = a.get("activityName") or "Activity"
    atype = (a.get("activityType") or {}).get("typeKey", "unknown")
    start = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
    dist_m = a.get("distance")
    dur_s = a.get("duration")

    def line(label, value, suffix=""):
        return f"- **{label}:** {value}{suffix}" if value not in (None, "", 0) else None

    def as_int(value):
        return int(round(value)) if isinstance(value, (int, float)) else value

    def one_dp(value):
        return round(value, 1) if isinstance(value, (int, float)) else value

    # Some watches report a broken/miscalibrated cadence for a given activity
    # (e.g. 2.5 spm). Only show it when it's a plausible running cadence.
    cadence = a.get("averageRunningCadenceInStepsPerMinute")
    if not (isinstance(cadence, (int, float)) and cadence >= 100):
        cadence = None

    parts = [
        f"# {name}",
        "",
        line("Date/time", start),
        line("Type", atype),
        line("Distance", meters_to_km(dist_m), " km"),
        line("Duration", fmt_hm(dur_s)),
        line("Avg pace", pace_per_km(dist_m, dur_s)),
        line("Avg heart rate", as_int(a.get("averageHR")), " bpm"),
        line("Max heart rate", as_int(a.get("maxHR")), " bpm"),
        line("Calories", as_int(a.get("calories")), " kcal"),
        line("Elevation gain", as_int(a.get("elevationGain")), " m"),
        line("Avg cadence", as_int(cadence), " spm"),
        line("Training effect (aerobic)", one_dp(a.get("aerobicTrainingEffect"))),
        line("Training effect (anaerobic)", one_dp(a.get("anaerobicTrainingEffect"))),
        line("Temperatur", (a.get("weather") or {}).get("temp_c"), " °C"),
        line("Väder", (a.get("weather") or {}).get("conditions")),
        "",
    ]
    body = "\n".join(p for p in parts if p is not None) + "\n"

    if intensity:
        body += intensity_markdown(intensity)
    return body


def intensity_markdown(intensity):
    hr_zones = intensity.get("hr_zones", {})
    pace_zones = intensity.get("pace_zones", {})
    laps = intensity.get("laps", [])

    lines = [f"## Pulszoner (Olympiatoppen, maxpuls {MAX_HR})"]
    for z in range(0, 6):
        lines.append(
            f"Z{z} {hr_zone_label(z)}: {fmt_min_sec(hr_zones.get(z, 0))}"
        )

    lines += ["", "## Fartzoner"]
    for z in range(0, 7):
        lines.append(
            f"Z{z} {PACE_ZONE_NAMES[z]}: {fmt_min_sec(pace_zones.get(z, 0))}"
        )

    fb = intensity.get("fartblock", {})
    lines += ["", "## Fartblock (per 10 s/km-band, utomhuslöpning)"]
    if not fb.get("applicable"):
        lines.append("Ej tillämpligt (inte utomhus- eller traillöpning).")
    elif not fb.get("bands"):
        lines.append("Inga godkända fartblock (inget varv snabbare än 5:00/km "
                     "som hölls tillräckligt länge).")
    else:
        lines += [
            "| Band (min/km) | Tid | Snittpuls |",
            "|---------------|-----|-----------|",
        ]
        for b in fb["bands"]:
            puls = b.get("avg_hr")
            puls = f"{puls} bpm" if puls is not None else "-"
            lines.append(f"| {b['band']} | {fmt_mmss(b['total_sec'])} | {puls} |")
        # Individual qualifying blocks, in the order they were run.
        lines += ["", "**Godkända block:**"]
        for blk in fb["blocks"]:
            puls = blk.get("avg_hr")
            puls = f"{puls} bpm" if puls is not None else "-"
            lines.append(
                f"- {blk['band']} · {fmt_mmss(blk['duration_sec'])} · {puls} "
                f"(snittfart {fmt_mmss(blk['pace_sec_per_km'])}/km)"
            )

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def run_sync(days, no_wellness=False, only_new=False):
    garmin = load_client()
    sb_url, sb_key = load_supabase()
    sb_on = bool(sb_key)
    sb_counts = {"sleep": 0, "recovery": 0, "runs": 0, "blocks": 0, "skipped": 0, "errors": 0}

    os.makedirs(WELLNESS_DIR, exist_ok=True)
    os.makedirs(WORKOUTS_DIR, exist_ok=True)

    today = local_today()
    start = today - timedelta(days=days - 1)

    print(f"Pulling data from {start.isoformat()} to {today.isoformat()} ...")
    print(f"Supabase: {'ON' if sb_on else 'off (run: garmin_sync.py setkey)'}\n")

    data = {
        "synced_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "range": {"start": start.isoformat(), "end": today.isoformat()},
        "wellness": [],
        "workouts": [],
    }

    # --- Daily wellness, one note per day ---
    for i in ([] if no_wellness else range(days)):
        cdate = start + timedelta(days=i)
        w = collect_wellness(garmin, cdate)
        data["wellness"].append(w)
        path = os.path.join(WELLNESS_DIR, f"{w['date']}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(wellness_markdown(w))
        srhr = w["resting_hr"] if w["resting_hr"] is not None else "-"
        ssleep = fmt_hm(w["sleep_seconds"]) or "-"
        print(f"  wellness {w['date']}  sleep={ssleep}  RHR={srhr}")
        if sb_on:
            try:
                push_wellness_supabase(sb_url, sb_key, w)
                sb_counts["sleep"] += 1
                sb_counts["recovery"] += 1
            except Exception as e:  # noqa: BLE001
                sb_counts["errors"] += 1
                print(f"    ! Supabase wellness {w['date']} failed: {e}")

    # --- Workouts, one note per activity ---
    activities = safe(
        garmin.get_activities_by_date, start.isoformat(), today.isoformat()
    )
    if isinstance(activities, dict) and "_error" in activities:
        print(f"\n  (could not load activities: {activities['_error']})")
        activities = []
    activities = activities or []

    # For big backfills: skip activities already stored in Supabase (saves calls).
    existing_ids = set()
    if only_new and sb_on:
        try:
            r = requests.get(
                f"{sb_url}/rest/v1/running_activities?select=garmin_activity_id",
                headers=_sb_headers(sb_key), timeout=30,
            )
            r.raise_for_status()
            existing_ids = {row.get("garmin_activity_id") for row in r.json()}
            print(f"only-new: {len(existing_ids)} pass finns redan, hoppar over dem.\n")
        except Exception as e:  # noqa: BLE001
            print(f"  (could not load existing ids: {e})")

    print()
    for a in activities:
        if only_new and a.get("activityId") in existing_ids:
            sb_counts["skipped"] += 1
            continue
        time.sleep(0.3)  # be gentle on Garmin during large pulls
        # Enrich with per-activity HR/pace zones, Fartblock, and weather.
        type_key = (a.get("activityType") or {}).get("typeKey")
        intensity = collect_intensity(garmin, a.get("activityId"), type_key)
        a["intensity"] = intensity
        a["weather"] = collect_weather(garmin, a.get("activityId"))
        data["workouts"].append(a)
        start_local = (a.get("startTimeLocal") or "")[:10] or today.isoformat()
        name = slugify(a.get("activityName") or "activity")
        fname = f"{start_local}-{name}.md"
        path = os.path.join(WORKOUTS_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(workout_markdown(a, intensity))
        blocks_msg = ""
        if sb_on:
            try:
                nb = push_workout_supabase(sb_url, sb_key, a, intensity)
                sb_counts["runs"] += 1
                sb_counts["blocks"] += nb
                blocks_msg = f"  -> Supabase ({nb} block)"
            except Exception as e:  # noqa: BLE001
                sb_counts["errors"] += 1
                blocks_msg = f"  ! Supabase failed: {e}"
        print(
            f"  workout  {start_local}  {a.get('activityName') or 'Activity'}"
            f"  ({len(intensity['laps'])} laps){blocks_msg}"
        )

    with open(os.path.join(OUT_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    print(
        f"\nDone. {len(data['wellness'])} wellness notes, "
        f"{len(data['workouts'])} workout notes."
    )
    if sb_on:
        print(
            f"Supabase: {sb_counts['recovery']} recovery/sleep days, "
            f"{sb_counts['runs']} runs, {sb_counts['blocks']} blocks, "
            f"{sb_counts['skipped']} skipped, {sb_counts['errors']} errors."
        )
    print(f"Saved under: {OUT_DIR}")


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Read-only Garmin Connect sync.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login", help="one-time login (handles 2FA, saves token)")
    sub.add_parser("setkey", help="store the Supabase service_role key locally")
    sub.add_parser("exporttoken", help="print token as base64 for a GitHub secret")

    p_sync = sub.add_parser("sync", help="pull recent data into ./garmin/")
    p_sync.add_argument("--days", type=int, default=3, help="how many days back")
    p_sync.add_argument("--no-wellness", action="store_true",
                        help="skip daily wellness (activities only)")
    p_sync.add_argument("--only-new", action="store_true",
                        help="skip activities already stored in Supabase (backfill)")

    args = parser.parse_args()

    if args.command == "login":
        login_interactive()
    elif args.command == "setkey":
        set_supabase_key()
    elif args.command == "exporttoken":
        export_token()
    elif args.command == "sync":
        # When launched by the Windows scheduler via pythonw.exe there is no
        # console, so any error would be invisible. Mirror all output to
        # sync.log and record the outcome so scheduled runs are debuggable.
        log_path = os.path.join(HERE, "sync.log")
        no_console = sys.stdout is None
        logf = open(log_path, "a", encoding="utf-8")
        if no_console:
            sys.stdout = logf
            sys.stderr = logf
        stamp = datetime.now().isoformat(timespec="seconds")
        print(f"\n===== sync run {stamp} =====")
        try:
            run_sync(max(1, args.days), no_wellness=args.no_wellness,
                     only_new=args.only_new)
            print(f"===== sync OK {datetime.now().isoformat(timespec='seconds')} =====")
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            print(f"===== sync FAILED {datetime.now().isoformat(timespec='seconds')} =====")
            logf.flush()
            sys.exit(1)
        finally:
            logf.flush()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
