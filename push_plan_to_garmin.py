#!/usr/bin/env python3
"""
push_plan_to_garmin.py
======================

Læser en angiven JSON-plan, RYDDER først ALLE tidligere uploadede pas fra samme plan
i Garmin Connect baseret på dens plan_tag, og uploader+skemalægger derefter den 
nye plan.

Krav: Stien til JSON-filen SKAL altid angives som første argument.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from datetime import date, timedelta

# --------------------------------------------------------------------------- #
# KONFIGURATION
# --------------------------------------------------------------------------- #
TOKENSTORE   = os.path.expanduser("~/.garminconnect")
WEEKDAY_OFFSET = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# --------------------------------------------------------------------------- #
# GARMIN-SKEMA-KONSTANTER
# --------------------------------------------------------------------------- #
RUN_SPORT = {"sportTypeId": 1, "sportTypeKey": "running"}

STEP_TYPE = {
    "warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "repeat": 6,
}
END_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
END_TIME     = {"conditionTypeId": 2, "conditionTypeKey": "time"}

TARGET_NO       = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
TARGET_PACE_ZONE_ID  = 6
TARGET_PACE_ZONE_KEY = "pace.zone"


# --------------------------------------------------------------------------- #
# PACE-HJÆLPERE
# --------------------------------------------------------------------------- #
def pace_to_secs(p: str) -> int:
    m, s = p.split(":")
    return int(m) * 60 + int(s)

def pace_to_mps(p: str) -> float:
    return 1000.0 / pace_to_secs(p)

def pace_target(paces: dict, zone: str) -> dict:
    slow, fast = paces[zone]
    return {
        "targetType": {"workoutTargetTypeId": TARGET_PACE_ZONE_ID,
                       "workoutTargetTypeKey": TARGET_PACE_ZONE_KEY},
        "targetValueOne": round(pace_to_mps(slow), 6),
        "targetValueTwo": round(pace_to_mps(fast), 6),
    }


# --------------------------------------------------------------------------- #
# STEP-BYGGERE
# --------------------------------------------------------------------------- #
def exec_step(step_key: str, km: float, target: dict) -> dict:
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": STEP_TYPE[step_key], "stepTypeKey": step_key},
        "endCondition": END_DISTANCE,
        "endConditionValue": round(km * 1000.0, 1),
    }
    step.update(target)
    return step

def exec_step_time(step_key: str, seconds: int, target: dict) -> dict:
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": STEP_TYPE[step_key], "stepTypeKey": step_key},
        "endCondition": END_TIME,
        "endConditionValue": round(float(seconds), 1),
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
        p1_km = round(km * 0.4, 2)
        p2_km = round(km * 0.4, 2)
        p3_km = round(km - p1_km - p2_km, 2)
        return [exec_step("interval", p1_km, pace_target(paces, "easy")),
                exec_step("interval", p2_km, pace_target(paces, "moderate")),
                exec_step("interval", p3_km, pace_target(paces, "threshold"))]

    if t == "fartlek":
        reps  = sess.get("reps", 6)
        surge = sess["surge_km"]; flt = sess["float_km"]
        rest  = max(km - reps * (surge + flt), 1.0)
        wu = cd = round(rest / 2, 2)
        return [exec_step("interval", wu, pace_target(paces, "easy")),
                repeat_group(reps, [exec_step("interval", surge, pace_target(paces, "threshold")),
                                    exec_step("recovery", flt,   pace_target(paces, "easy"))]),
                exec_step("interval", cd, pace_target(paces, "easy"))]

    if t == "intervals":
        reps = sess.get("reps", 5)
        work = sess["work_km"]; rec = sess["recover_km"]
        rest = max(km - reps * (work + rec), 1.0)
        wu = cd = round(rest / 2, 2)
        return [exec_step("interval", wu, pace_target(paces, "easy")),
                repeat_group(reps, [exec_step("interval", work, pace_target(paces, "interval")),
                                    exec_step("recovery", rec,  pace_target(paces, "recovery"))]),
                exec_step("interval", cd, pace_target(paces, "easy"))]

    if t == "strides":
        reps = sess.get("reps", 5)
        rest = max(km - reps * (0.1 + 0.1), 1.0)
        wu = cd = round(rest / 2, 2)
        return [exec_step("interval", wu, pace_target(paces, "easy")),
                repeat_group(reps, [exec_step_time("interval", 20, pace_target(paces, "stride")),
                                    exec_step_time("recovery", 45, pace_target(paces, "recovery"))]),
                exec_step("interval", cd, pace_target(paces, "easy"))]

    raise ValueError(f"Ukendt session-type: {t!r}")


def assign_step_orders(steps: list, start: int = 1) -> int:
    order = start
    for step in steps:
        step["stepOrder"] = order
        order += 1
        if step.get("type") == "RepeatGroupDTO":
            order = assign_step_orders(step["workoutSteps"], order)
    return order


def estimate_secs(steps: list, paces: dict) -> int:
    total = 0.0
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            total += step["numberOfIterations"] * estimate_secs(step["workoutSteps"], paces)
        else:
            if step["endCondition"]["conditionTypeKey"] == "time":
                total += step["endConditionValue"]
            else:
                meters = step["endConditionValue"]
                mps = step.get("targetValueOne") or 2.5
                total += meters / mps
    return int(total)


def build_payload(sess: dict, paces: dict, tag: str) -> dict:
    steps = steps_for(sess, paces)
    assign_step_orders(steps)
    
    day_str = str(sess["day"]).capitalize()
    week_str = f"{sess['week']:02d}"
    name = f"{tag} W{week_str}-{day_str} {sess['name']}"
    
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
# TRANSPORT
# --------------------------------------------------------------------------- #
def _inner(client):
    return getattr(client, "client", None) or getattr(client, "garth", None)


def _request_json(client, method: str, path: str, **kw):
    inner = _inner(client)
    last = None
    for use_api in (True, False):
        try:
            resp = inner.request(method, "connectapi", path, api=use_api, **kw) if use_api \
                else inner.request(method, "connectapi", path, **kw)
            if resp is None:
                return None
            return resp.json() if hasattr(resp, "json") else resp
        except TypeError as exc:
            last = exc
            continue
    raise RuntimeError(f"Kunne ikke kalde {method} {path}: {last}")


def list_plan_workouts(client, tag: str) -> list:
    try:
        data = client.get_workouts(0, 999)
    except Exception:
        data = _request_json(client, "GET", "/workout-service/workouts",
                             params={"start": 0, "limit": 999, "myWorkoutsOnly": True})
    items = data if isinstance(data, list) else (data or {}).get("workoutList", data)
    
    workouts = []
    for w in (items or []):
        w_name = str(w.get("workoutName", ""))
        if w_name.startswith(tag):
            workouts.append(w)
    return workouts


def create_workout(client, payload: dict) -> int:
    res = _request_json(client, "POST", "/workout-service/workout", json=payload)
    return res["workoutId"] if isinstance(res, dict) else res


# --------------------------------------------------------------------------- #
# LOGIN
# --------------------------------------------------------------------------- #
def authenticate():
    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit("Mangler garminconnect. Kør: pip install --upgrade garminconnect curl_cffi")

    email    = os.getenv("GARMIN_EMAIL") or os.getenv("EMAIL")
    password = os.getenv("GARMIN_PASSWORD") or os.getenv("PASSWORD")
    have_token = os.path.isdir(TOKENSTORE) and any(os.scandir(TOKENSTORE))

    if not have_token and not (email and password):
        sys.exit("Første login kræver GARMIN_EMAIL og GARMIN_PASSWORD som miljøvariable.")

    client = Garmin(email, password, prompt_mfa=lambda: input("MFA-kode: "))
    client.login(TOKENSTORE)
    return client


# --------------------------------------------------------------------------- #
# UNIT TESTS
# --------------------------------------------------------------------------- #
class TestGarminPayload(unittest.TestCase):
    def setUp(self):
        self.paces = {
            "recovery":  ["7:20", "7:05"],
            "easy":      ["7:20", "7:00"],
            "moderate":  ["6:55", "6:35"],
            "threshold": ["6:15", "6:00"],
            "interval":  ["5:50", "5:30"],
            "stride":    ["5:45", "5:10"]
        }

    def test_build_payload_title_formatting(self):
        sample_session = {"week": 3, "day": "wed", "type": "easy", "km": 3, "name": "Roligt 3 km"}
        payload = build_payload(sample_session, self.paces, "HM26")
        self.assertEqual(payload["workoutName"], "HM26 W03-Wed Roligt 3 km")

    def test_progressive_is_all_interval_type(self):
        sample_session = {"week": 9, "day": "wed", "type": "progressive", "km": 10, "name": "Progressivt 10 km"}
        steps = steps_for(sample_session, self.paces)
        
        self.assertEqual(len(steps), 3)
        for s in steps:
            self.assertEqual(s["stepType"]["stepTypeKey"], "interval")

    def test_intervals_has_correct_types_and_recovery(self):
        sample_session = {"week": 11, "day": "wed", "type": "intervals", "km": 6, "reps": 5, "work_km": 0.4, "recover_km": 0.2, "name": "Intervaller"}
        steps = steps_for(sample_session, self.paces)
        
        # Start og slut er nu almindelige interval-løb, som bestilt
        self.assertEqual(steps[0]["stepType"]["stepTypeKey"], "interval")
        self.assertEqual(steps[2]["stepType"]["stepTypeKey"], "interval")
        
        # Pausen indeni SKAL være recovery
        repeat_grp = steps[1]
        self.assertEqual(repeat_grp["workoutSteps"][1]["stepType"]["stepTypeKey"], "recovery")


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Ryd og upload en specifik træningsplan til Garmin Connect.")
    ap.add_argument("plan", help="Stien til JSON-planfilen (f.eks. halvmaraton_plan.json).")
    ap.add_argument("--dry-run", action="store_true", help="Byg og vis pasene/JSON uden upload.")
    ap.add_argument("--week", type=int, action="append", help="Begræns til uge(r). Kan gentages.")
    ap.add_argument("--no-clean", action="store_true", help="Ryd ikke gammel plan først.")
    ap.add_argument("--yes", action="store_true", help="Spring bekræftelse over.")
    ap.add_argument("--skip-past", action="store_true", help="Spring pas over, der ligger i fortiden (før i dag).")
    ap.add_argument("--run-tests", action="store_true", help="Kør interne unit tests og afslut.")
    args = ap.parse_args()

    if args.run_tests:
        suite = unittest.TestSuite()
        suite.addTest(unittest.TestLoader().loadTestsFromTestCase(TestGarminPayload))
        runner = unittest.TextTestRunner()
        result = runner.run(suite)
        return 0 if result.wasSuccessful() else 1

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)

    tag    = plan["plan_tag"]
    paces  = plan["paces"]
    start  = date.fromisoformat(plan["start_monday"])
    today  = date.today()

    sess_list = [s for s in plan["sessions"] if not args.week or s["week"] in args.week]
    if not sess_list:
        print("Ingen sessioner matcher valget."); return 1

    built = []
    for s in sess_list:
        d = session_date(start, s["week"], s["day"])
        
        if args.skip_past and d < today:
            continue
            
        built.append((d, build_payload(s, paces, tag)))
        
    if not built:
        print("Ingen kommende sessioner tilbage efter filtrering."); return 0
        
    built.sort(key=lambda x: x[0])

    print(f"Plan '{tag}' fra filen '{args.plan}' — {len(built)} pas skal uploades (Skipper fortid: {args.skip_past}):\n")
    for d, p in built:
        mins = p["estimatedDurationInSecs"] // 60
        print(f"  {d.isoformat()} ({d.strftime('%a')})  {p['workoutName']:<45} ~{mins} min")

    if args.dry_run:
        print("\n--- JSON for første pas ---")
        print(json.dumps(built[0][1], ensure_ascii=False, indent=2))
        print("\n--dry-run: intet uploadet eller slettet.")
        return 0

    if not args.yes:
        msg = f"Rydde ALLE eksisterende '{tag}'-pas i Garmin og uploade de {len(built)} pas? [j/N] "
        if input(msg).strip().lower() not in ("j", "y"):
            print("Afbrudt."); return 0

    client = authenticate()

    if not args.no_clean:
        old = list_plan_workouts(client, tag)
        print(f"\nRydder ALLEREDE EKSISTERENDE versioner for '{tag}' ({len(old)} pas fundet)...")
        for w in old:
            try:
                client.delete_workout(w["workoutId"])
                print(f"  - slettet: {w.get('workoutName')}")
            except Exception as exc:
                print(f"  x kunne ikke slette {w.get('workoutName')}: {exc}")

    print("\nUploader + skemalægger de opdaterede pas ...")
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

    print(f"\nFærdig. {ok} skemalagt, {fail} fejlede.")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
