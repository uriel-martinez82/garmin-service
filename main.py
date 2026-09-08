"""
Garmin Bridge Service
FastAPI + garth: expone endpoints para que Next.js pueda:
  - Vincular una cuenta Garmin (POST /auth/link)
  - Sincronizar datos de Garmin    (POST /data/sync)

Los tokens de garth se persisten en Supabase (garmin_credentials.garth_tokens)
para sobrevivir reinicios del servicio (Railway, etc.).
"""

import os
import json
import tempfile
from datetime import date, timedelta
from typing import Optional

import garth
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET      = os.environ["GARMIN_SERVICE_SECRET"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Garmin Bridge Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Auth helper ───────────────────────────────────────────────────────────────
def verify_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ── garth helpers ─────────────────────────────────────────────────────────────
def _save_tokens_to_db(user_id: str) -> None:
    """Serializa los tokens de garth y los guarda en Supabase."""
    with tempfile.TemporaryDirectory() as tmp:
        garth.save(tmp)
        tokens = {}
        for fname in ("oauth1_token.json", "oauth2_token.json"):
            fpath = os.path.join(tmp, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    tokens[fname] = json.load(f)

    supabase.table("garmin_credentials").update(
        {"garth_tokens": json.dumps(tokens)}
    ).eq("user_id", user_id).execute()


def _load_tokens_from_db(user_id: str) -> None:
    """Carga los tokens desde Supabase y restaura la sesión de garth."""
    result = (
        supabase.table("garmin_credentials")
        .select("garth_tokens")
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not result.data or not result.data.get("garth_tokens"):
        raise HTTPException(status_code=400, detail="No Garmin session found. Re-link the account.")

    tokens: dict = json.loads(result.data["garth_tokens"])

    with tempfile.TemporaryDirectory() as tmp:
        for fname, data in tokens.items():
            with open(os.path.join(tmp, fname), "w") as f:
                json.dump(data, f)
        garth.resume(tmp)


# ── Schemas ───────────────────────────────────────────────────────────────────
class LinkRequest(BaseModel):
    user_id: str
    email: str
    password: str

class SyncRequest(BaseModel):
    user_id: str


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/link", dependencies=[Depends(verify_key)])
def link_garmin(req: LinkRequest):
    try:
        garth.login(req.email, req.password)
    except Exception as e:
        msg = str(e)
        print(f"[garth] login error: {msg}")
        raise HTTPException(status_code=400, detail=f"Garmin login failed: {msg}")

    # Upsert credentials row (email + tokens)
    supabase.table("garmin_credentials").upsert(
        {
            "user_id":      req.user_id,
            "garmin_email": req.email,
            # garmin_password_enc stays managed by Next.js — we only update tokens
        },
        on_conflict="user_id",
    ).execute()

    _save_tokens_to_db(req.user_id)

    # Try to get garmin user id
    try:
        info = garth.connectapi("/userprofile-service/userprofile/personal-information")
        garmin_user_id = str(info.get("userId", ""))
    except Exception:
        garmin_user_id = None

    return {"success": True, "garmin_user_id": garmin_user_id}


@app.post("/data/sync", dependencies=[Depends(verify_key)])
def sync_data(req: SyncRequest):
    _load_tokens_from_db(req.user_id)

    # ── Actividades (últimas 50) ───────────────────────────────────────────
    try:
        raw_acts = garth.connectapi(
            "/activitylist-service/activities/search/activities?start=0&limit=50"
        )
        if not isinstance(raw_acts, list):
            raw_acts = []
    except Exception as e:
        print(f"[garth] activities error: {e}")
        raw_acts = []

    activities = []
    for a in raw_acts:
        activities.append({
            "activity_type":  (a.get("activityType") or {}).get("typeKey", "unknown"),
            "start_time":     a.get("startTimeLocal") or a.get("startTimeGMT") or "",
            "duration_seconds": round(a.get("duration") or 0),
            "avg_heart_rate": a.get("averageHR") or 0,
            "max_heart_rate": a.get("maxHR") or 0,
            "distance_meters": round(a.get("distance") or 0),
            "avg_cadence":    a.get("averageCadenceValue") or a.get("averageCadence"),
            "hr_zone_1_sec":  round(a.get("hrTimeInZone_1") or a.get("hrTimeZone1") or 0),
            "hr_zone_2_sec":  round(a.get("hrTimeInZone_2") or a.get("hrTimeZone2") or 0),
            "hr_zone_3_sec":  round(a.get("hrTimeInZone_3") or a.get("hrTimeZone3") or 0),
            "hr_zone_4_sec":  round(a.get("hrTimeInZone_4") or a.get("hrTimeZone4") or 0),
            "hr_zone_5_sec":  round(a.get("hrTimeInZone_5") or a.get("hrTimeZone5") or 0),
        })

    # ── Resúmenes diarios (últimos 30 días) ───────────────────────────────
    steps_list, cal_list, hr_list, active_list, bb_list, stress_list = [], [], [], [], [], []
    fulfilled = 0

    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        try:
            summary = garth.connectapi(
                f"/usersummary-service/usersummary/daily?calendarDate={d}"
            )
            if not summary:
                continue
            fulfilled += 1
            if summary.get("totalSteps"):              steps_list.append(summary["totalSteps"])
            if summary.get("totalKilocalories"):       cal_list.append(summary["totalKilocalories"])
            if summary.get("restingHeartRate"):        hr_list.append(summary["restingHeartRate"])
            if summary.get("bodyBatteryMostRecentValue"): bb_list.append(summary["bodyBatteryMostRecentValue"])
            if summary.get("averageStressLevel"):      stress_list.append(summary["averageStressLevel"])
            active = (summary.get("moderateIntensityMinutes") or 0) + (summary.get("vigorousIntensityMinutes") or 0) * 2
            if active > 0: active_list.append(active)
        except Exception as e:
            print(f"[garth] daily summary {d} error: {e}")
            continue

    def avg(lst):
        valid = [v for v in lst if v and v > 0]
        return round(sum(valid) / len(valid)) if valid else 0

    garmin_summary = {
        "avg_steps":              avg(steps_list),
        "avg_calories":           avg(cal_list),
        "avg_resting_heart_rate": avg(hr_list),
        "avg_active_minutes":     avg(active_list),
        "avg_body_battery":       avg(bb_list),
        "avg_stress":             avg(stress_list),
        "total_days":             fulfilled,
    }

    return {"summary": garmin_summary, "activities": activities}
