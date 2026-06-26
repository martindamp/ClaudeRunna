#!/usr/bin/env python3
"""
push_plan_to_garmin.py
======================

Laeser en redigerbar JSON-plan (halvmaraton_plan.json), RYDDER foerst alle tidligere
uploadede pas fra samme plan i Garmin Connect, og uploader+skemalaegger derefter den
nye plan, saa pasene synker til uret som "dagens traening".

Aktiviteter angives i KM, og hvert step faar et pace-interval (m:ss/km) fra planens
"paces"-sektion.

AFHAENGER AF
-----------
    pip install --upgrade garminconnect curl_cffi

VERIFICERET mod garminconnect v0.3.4 (maj 2026):
    Garmin(...).login(tokenstore)        # SSO + token-cache i ~/.garminconnect
    client.schedule_workout(id, "YYYY-MM-DD")
    client.delete_workout(id)            # fjerner workout OG dens kalender-/skemapost

REVERSE-ENGINEERED (kan ikke koeres-testes her — verificer med --dry-run + en enkelt uge):
    * Workout-payloadens felt-id'er (stepType/endCondition/targetType) staar samlet i
      afsnittet "GARMIN-SKEMA-KONSTANTER". Vises pace ikke paa uret, er det oftest
      TARGET_PACE_ZONE_ID der afviger i din version.
    * Transporten til create/list ligger i afsnittet "TRANSPORT" med flere fallbacks
      paa tvaers af biblioteksversioner.

VIGTIGT — SIKKERHED
-------------------
    Oprydningen sletter KUN workout-skabeloner hvis navn starter med plan_tag (fx "HM26").
    Den roerer IKKE dine registrerede/loebne aktiviteter. Lav derfor ikke egne manuelle
    workouts med samme tag-praefiks.

BRUG
----
    export GARMIN_EMAIL="din@mail.dk"      # kun noedvendigt ved foerste login
    export GARMIN_PASSWORD="..."           # derefter bruges token-cachen

    python push_plan_to_garmin.py --dry-run            # byg + vis JSON, upload intet
    python push_plan_to_garmin.py --week 11 --dry-run  # kun en uge
    python push_plan_to_garmin.py                       # ryd gammel plan + upload ny (bekraeftelse)
    python push_plan_to_garmin.py --yes                 # spring bekraeftelse over
    python push_plan_to_garmin.py --no-clean            # upload uden at rydde foerst
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

# --------------------------------------------------------------------------- #
# KONFIGURATION
# --------------------------------------------------------------------------- #
DEFAULT_PLAN = "halvmaraton_plan.json"
TOKENSTORE   = os.path.expanduser("~/.garminconnect")
WEEKDAY_OFFSET = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# --------------------------------------------------------------------------- #
# GARMIN-SKEMA-KONSTANTER  (reverse-engineered — ret her hvis din version afviger)
# --------------------------------------------------------------------------- #
RUN_SPORT = {"sportTypeId": 1, "sportTypeKey": "running"}

STEP_TYPE = {  # stepTypeKey -> stepTypeId
    "warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "repeat": 6,
}
END_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance"}  # endConditionValue i METER

TARGET_NO       = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
TARGET_PACE_ZONE_ID  = 6        # pace.zone — den hyppigste kilde til afvigelse mellem versioner
TARGET_PACE_ZONE_KEY = "pace.zone"


# --------------------------------------------------------------------------- #
# PACE-HJAELPERE
# --------------------------------------------------------------------------- #
def pace_to_secs(p: str) -> int:
    m, s = p.split(":")
    return int(m) * 60 + int(s)

def pace_to_mps(p: str) -> float:
    return 1000.0 / pace_to_secs(p)   # m/s

def pace_target(paces: dict, zone: str) -> dict:
    """Garmin gemmer pace-maal som hastighed i m/s. targetValueOne=langsom, Two=hurtig.
    Bytt om hvis din enhed viser intervallet omvendt."""
    slow, fast = paces[zone]
    return {
        "targetType": {"workoutTargetTypeId": TARGET_PACE_ZONE_ID,
                       "workoutTargetTypeKey": TARGET_PACE_ZONE_KEY},
        "targetValueOne": round(pace_to_mps(slow), 6),
        "targetValueTwo": round(pace_to_mps(fast), 6),
    }


# --------------------------------------------------------------------------- #
# STEP-BYGGERE  (rene dicts — fuld kontrol over distance + pace)
# --------------------------------------------------------------------------- #
def exec_step(step_key: str, km: float, target: dict) -> dict:
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": STEP_TYPE[step_key], "stepTypeKey": step_key},
        "endCondition": END_DISTANCE,
        "endConditionValue": round(km * 1000.0, 1),   # meter
    }
    step.update(target)
    return step

def repeat_group(reps: int, children: list) -> dict:
    return {
        "type": "RepeatGroupDTO",
        "stepType": {"stepTypeId": STEP_TYPE["repeat"], "stepTypeKey": "repeat"},
        "numberOfIterations": reps,
        "smartRepeat": False,
        "workoutSteps": children,
    }


# --------------------------------------------------------------------------- #
# SESSION -> STEPS
# --------------------------------------------------------------------------- #
def steps_for(sess: dict, paces: dict) -> list:
    t, km = sess["type"], sess["km"]

    if t in ("easy", "recovery", "long"):
        return [exec_step("interval", km, pace_target(paces, t))]

    if t == "progressive":
        easy_km = round(km * 0.7, 2)
        mod_km  = round(km - easy_km, 2)
        return [exec_step("warmup",   easy_km, pace_target(paces, "easy")),
                exec_step("interval", mod_km,  pace_target(paces, "moderate"))]

    if t == "fartlek":
        reps  = sess.get("reps", 6)
        surge = sess["surge_km"]; flt = sess["float_km"]
        rest  = max(km - reps * (surge + flt), 1.0)
        wu = cd = round(rest / 2, 2)
        return [exec_step("warmup", wu, pace_target(paces, "easy")),
                repeat_group(reps, [exec_step("interval", surge, pace_target(paces, "threshold")),
                                    exec_step("recovery", flt,   pace_target(paces, "easy"))]),
                exec_step("cooldown", cd, pace_target(paces, "easy"))]

    if t == "intervals":
        reps = sess.get("reps", 5)
        work = sess["work_km"]; rec = sess["recover_km"]
        rest = max(km - reps * (work + rec), 1.0)
        wu = cd = round(rest / 2, 2)
        return [exec_step("warmup", wu, pace_target(paces, "easy")),
                repeat_group(reps, [exec_step("interval", work, pace_target(paces, "interval")),
                                    exec_step("recovery", rec,  pace_target(paces, "recovery"))]),
                exec_step("cooldown", cd, pace_target(paces, "easy"))]

    if t == "strides":
        reps   = sess.get("reps", 5)
        stride = sess["stride_km"]; rec = sess["recover_km"]
        easy_km = max(km - reps * (stride + rec), 1.0)
        return [exec_step("warmup", round(easy_km, 2), pace_target(paces, "easy")),
                repeat_group(reps, [exec_step("interval", stride, pace_target(paces, "stride")),
                                    exec_step("recovery", rec,    pace_target(paces, "recovery"))])]

    raise ValueError(f"Ukendt session-type: {t!r}")


def assign_step_orders(steps: list, start: int = 1) -> int:
    """Garmin kraever fortloebende stepOrder paa tvaers af hele workouten, ogsaa i repeats."""
    order = start
    for step in steps:
        step["stepOrder"] = order
        order += 1
        if step.get("type") == "RepeatGroupDTO":
            order = assign_step_orders(step["workoutSteps"], order)
    return order


def estimate_secs(steps: list, paces: dict) -> int:
    """Groft estimat ud fra langsom pace-graense; Garmin genberegner alligevel."""
    total = 0.0
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            total += step["numberOfIterations"] * estimate_secs(step["workoutSteps"], paces)
        else:
            meters = step["endConditionValue"]
            mps = step.get("targetValueOne") or 2.5   # langsom graense hvis sat
            total += meters / mps
    return int(total)


def build_payload(sess: dict, paces: dict, tag: str) -> dict:
    steps = steps_for(sess, paces)
    assign_step_orders(steps)
    name = sess["name"]
    if not name.startswith(tag):
        name = f"{tag} {name}"
    return {
        "sportType": RUN_SPORT,
        "workoutName": name,
        "estimatedDurationInSecs": estimate_secs(steps, paces),
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": RUN_SPORT, "workoutSteps": steps}
        ],
    }


def session_date(start_monday: date, week: int, day: str) -> date:
    return start_monday + timedelta(days=(week - 1) * 7 + WEEKDAY_OFFSET[day])


# --------------------------------------------------------------------------- #
# TRANSPORT  (flere fallbacks paa tvaers af garminconnect-versioner)
# --------------------------------------------------------------------------- #
def api_call(client, path: str, method: str = "GET", **kw):
    last = None
    for fn in (
        lambda: client.connectapi(path, method=method, **kw),
        lambda: client.garth.connectapi(path, method=method, **kw),
    ):
        try:
            return fn()
        except (AttributeError, TypeError) as exc:
            last = exc
            continue
    raise RuntimeError(f"Kunne ikke kalde {method} {path}: {last}")


def list_plan_workouts(client, tag: str) -> list:
    try:
        data = client.get_workouts(0, 999)
    except Exception:
        data = api_call(client, "/workout-service/workouts",
                        params={"start": 0, "limit": 999, "myWorkoutsOnly": True})
    items = data if isinstance(data, list) else data.get("workoutList", data)
    return [w for w in items if str(w.get("workoutName", "")).startswith(tag)]


def create_workout(client, payload: dict) -> int:
    res = api_call(client, "/workout-service/workout", method="POST", json=payload)
    return res["workoutId"] if isinstance(res, dict) else res


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Ryd og upload halvmaratonplanen til Garmin Connect.")
    ap.add_argument("plan", nargs="?", default=DEFAULT_PLAN, help=f"JSON-plan (default {DEFAULT_PLAN}).")
    ap.add_argument("--dry-run", action="store_true", help="Byg og vis pasene/JSON uden upload.")
    ap.add_argument("--week", type=int, action="append", help="Begraens til uge(r). Kan gentages.")
    ap.add_argument("--no-clean", action="store_true", help="Ryd ikke gammel plan foerst.")
    ap.add_argument("--yes", action="store_true", help="Spring bekraeftelse over.")
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)

    tag    = plan["plan_tag"]
    paces  = plan["paces"]
    start  = date.fromisoformat(plan["start_monday"])
    sess_list = [s for s in plan["sessions"] if not args.week or s["week"] in args.week]
    if not sess_list:
        print("Ingen sessioner matcher valget."); return 1

    built = []
    for s in sess_list:
        d = session_date(start, s["week"], s["day"])
        built.append((d, build_payload(s, paces, tag)))
    built.sort(key=lambda x: x[0])

    print(f"Plan '{tag}' — {len(built)} pas:\n")
    for d, p in built:
        mins = p["estimatedDurationInSecs"] // 60
        print(f"  {d.isoformat()} ({d.strftime('%a')})  {p['workoutName']:<42} ~{mins} min")

    if args.dry_run:
        print("\n--- JSON for foerste pas ---")
        print(json.dumps(built[0][1], ensure_ascii=False, indent=2))
        print("\n--dry-run: intet uploadet.")
        return 0

    if not args.yes:
        msg = "Ryd gammel plan og upload ovenstaaende? [j/N] " if not args.no_clean \
              else "Upload ovenstaaende (uden oprydning)? [j/N] "
        if input(msg).strip().lower() not in ("j", "y"):
            print("Afbrudt."); return 0

    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit("Mangler garminconnect. Koer: pip install --upgrade garminconnect curl_cffi")

    client = Garmin(os.getenv("GARMIN_EMAIL") or os.getenv("EMAIL"),
                    os.getenv("GARMIN_PASSWORD") or os.getenv("PASSWORD"),
                    prompt_mfa=lambda: input("MFA-kode: "))
    client.login(TOKENSTORE)

    if not args.no_clean:
        old = list_plan_workouts(client, tag)
        print(f"\nRydder {len(old)} tidligere '{tag}'-pas ...")
        for w in old:
            try:
                client.delete_workout(w["workoutId"])     # fjerner ogsaa kalenderpost
                print(f"  - slettet: {w.get('workoutName')}")
            except Exception as exc:
                print(f"  x kunne ikke slette {w.get('workoutName')}: {exc}")

    print("\nUploader + skemalaegger ...")
    ok = fail = 0
    for d, payload in built:
        try:
            wid = create_workout(client, payload)
            client.schedule_workout(wid, d.isoformat())
            print(f"  + {d.isoformat()}  {payload['workoutName']}  (id={wid})")
            ok += 1
        except Exception as exc:
            print(f"  x {d.isoformat()}  {payload['workoutName']}  FEJL: {exc}")
            fail += 1

    print(f"\nFaerdig. {ok} skemalagt, {fail} fejlede.")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
