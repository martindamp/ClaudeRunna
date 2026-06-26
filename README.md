# push_plan_to_garmin

Small CLI tool that reads a training plan from an editable JSON file and
uploads it as structured running workouts to Garmin Connect — scheduled on
the right dates, so they sync to the watch as "today's workout".

The plan is versioned as data (JSON), not as code. When you change the
plan — move a long run, adjust a pace, add a week — you edit the file and
run the script again. It cleans up after itself: everything from a
previous upload of the same plan is removed from Garmin Connect before
the new version is added, so you never end up with duplicates or stale
workouts in the calendar.

## Contents

- [What the program does](#what-the-program-does)
- [Installation](#installation)
- [Authentication](#authentication)
- [Usage](#usage)
- [Flags](#flags)
- [Plan file format](#plan-file-format)
- [Session types](#session-types)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)

## What the program does

For each session in the plan, the script builds a structured Garmin
workout (warmup/work/recovery/cooldown, optionally as a repeat group for
intervals), with distance in kilometers and a pace target pulled from the
plan's `paces` table. The workout is named
`<plan_tag> W<week>-<day> <name>` — e.g. `HM26 W11-Sun Long run 16 km
(peak)` — uploaded to Garmin Connect, and then scheduled on the computed
calendar date (`start_monday` + week/day offset).

By default, an upload removes **all** previous workouts whose name starts
with `plan_tag` before the new version is added — that's how you "update"
a plan: edit the JSON file, run the script again, old version gone, new
version in. The cleanup only targets workout templates with that tag
prefix; it does not touch your logged, completed activities.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade garminconnect curl_cffi
```

## Authentication

Login goes through [`garminconnect`](https://github.com/cyberjunky/python-garminconnect),
which uses the same OAuth flow as the official app, and caches a token
locally in `~/.garminconnect`.

```bash
export GARMIN_EMAIL="you@example.com"
export GARMIN_PASSWORD="your-password"
```

- **First run** requires `GARMIN_EMAIL` and `GARMIN_PASSWORD`. If your
  account has MFA enabled, you'll be prompted for a code in the terminal;
  if MFA is disabled, the script just logs in without asking.
- **Subsequent runs** use the cached token in `~/.garminconnect`. You
  don't need to set the environment variables again as long as the token
  is still valid.
- If both the token and credentials are missing at startup, the script
  exits with a clear error instead of failing deep inside an API call.
- Garmin rate-limits login after many attempts in a short time (HTTP
  429). If that happens, wait a few minutes — the token cache means you
  rarely hit the login limit again once it's set.

## Usage

```bash
python push_plan_to_garmin.py <plan.json> [flags]
```

The path to the JSON plan is a **required** positional argument — there
is no default filename.

## Flags

| Flag | Description |
|---|---|
| `<plan>` | (required) Path to the plan JSON file. |
| `--dry-run` | Builds and shows all workouts (date, name, estimated duration) plus the full JSON for the first workout — uploads or deletes nothing. Always use this first after a change. |
| `--week N` | Restrict the run to week `N`. Can be repeated (`--week 3 --week 4`) for multiple weeks. Without this flag, the whole plan runs. |
| `--no-clean` | Skip the cleanup and just upload the selected workouts on top of whatever already exists. Normally an upload always clears all old workouts for the same `plan_tag` first. |
| `--skip-past` | Skip sessions whose date is before today. Useful when you update a plan mid-way through and don't want to recreate workouts that have already happened. |
| `--delete-plan` | Deletes ALL workouts (and their calendar entries) for the plan's `plan_tag` and exits. Used to clean up completely, e.g. if you switch to a new plan file with a new tag. |
| `--yes` | Skips the interactive yes/no confirmation. Useful in scripts/cron. |
| `--run-tests` | Runs the program's internal unit tests (payload building, naming, deletion logic) and exits — does not touch Garmin Connect or the plan file (though the positional `plan` argument must still be given on the command line). |

`--delete-plan` and `--dry-run`/`--week`/`--no-clean` aren't mutually
exclusive in the code, but don't give a meaningful combination — use
`--delete-plan` on its own (optionally with `--yes`).

## Plan file format

The plan is a single JSON file with four parts: a `_readme` comment list
(ignored by the script, for your own reference only), `plan_tag`,
`start_monday`, a `paces` table, and a list of `sessions`.

```json
{
  "_readme": ["Free-text field — the script does not read it."],

  "plan_tag": "HM26",
  "start_monday": "2026-06-29",

  "paces": {
    "recovery":  ["7:25", "7:05"],
    "easy":      ["7:15", "6:55"],
    "long":      ["7:25", "7:05"],
    "moderate":  ["6:55", "6:35"],
    "threshold": ["6:15", "6:00"],
    "interval":  ["5:50", "5:30"],
    "stride":    ["5:45", "5:10"]
  },

  "sessions": [
    {"week": 3, "day": "wed", "type": "easy", "km": 3, "name": "Easy 3 km"}
  ]
}
```

### Top-level fields

| Field | Type | Description |
|---|---|---|
| `plan_tag` | string | Prefix used in workout names and as the filter when cleaning up/deleting. Pick something short and unique (e.g. `HM26`), and don't use it as a prefix on workouts you create manually in Garmin Connect — they'll be cleared along with the plan. |
| `start_monday` | `"YYYY-MM-DD"` | The Monday of week number 1. Every session's date is computed as `start_monday + (week-1)*7 days + weekday offset`. |
| `paces` | object | Named pace zones, each as `["slow", "fast"]` in `"m:ss"` per km, e.g. `["7:15", "6:55"]`. Session types reference these names (see below) — you can add your own zone names if you extend `steps_for()`. |
| `sessions` | list | The individual workouts, see below. |

### Session fields

| Field | Type | Description |
|---|---|---|
| `week` | int | Week number, 1-indexed relative to `start_monday`. |
| `day` | string | `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, or `sun`. |
| `type` | string | One of: `easy`, `recovery`, `long`, `progressive`, `fartlek`, `intervals`, `strides` — see [Session types](#session-types). |
| `km` | number | Total distance for the session in kilometers. |
| `name` | string | Free text, included in the workout's name in Garmin. |
| extra fields | — | Some types require additional keys (`reps`, `surge_km`, `work_km`, etc.) — see the table below. |

## Session types

| `type` | Structure | Required extra fields | Pace zones used |
|---|---|---|---|
| `easy` | One continuous block of the full `km`. | — | `easy` |
| `recovery` | One continuous block of the full `km`. | — | `recovery` |
| `long` | One continuous block of the full `km`. | — | `long` |
| `progressive` | Three equal thirds of `km`, increasing pace. | — | `easy` → `moderate` → `threshold` |
| `fartlek` | Warmup, then `reps` × (surge + float), then cooldown. Warmup/cooldown fill the rest of `km`. | `reps`, `surge_km`, `float_km` | `easy` (warmup/cooldown + float), `threshold` (surge) |
| `intervals` | Warmup, then `reps` × (work + recovery), then cooldown. | `reps`, `work_km`, `recover_km` | `easy` (warmup/cooldown), `interval` (work), `recovery` (rest) |
| `strides` | Warmup, then `reps` × (20 sec. stride + 45 sec. recovery — time-based), then cooldown. | `reps` | `easy` (warmup/cooldown), `stride`, `recovery` |

For `fartlek` and `intervals`, warmup + reps × (sub-components) +
cooldown don't have to sum exactly to `km` — the remainder is split
between warmup and cooldown, with a 1 km floor in total if the reps block
exceeds the given distance.

## Examples

**Check the plan without touching Garmin Connect:**

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --dry-run
```

**Upload/update the whole plan** (clears all old `HM26` workouts and
uploads the current JSON file again):

```bash
python push_plan_to_garmin.py halvmaraton_plan.json
```

**Test on a single week first**, before running the whole plan:

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --week 3 --dry-run
python push_plan_to_garmin.py halvmaraton_plan.json --week 3
```

**Update the plan mid-way through** without recreating workouts that
have already happened:

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --skip-past
```

**Add new workouts without deleting existing ones** (e.g. if you've only
added a week and don't want to touch the rest):

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --no-clean --week 13
```

**Clear a plan entirely** (e.g. before switching to a new plan file with
a new tag):

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --delete-plan
python push_plan_to_garmin.py halvmaraton_plan.json --delete-plan --yes   # without confirmation
```

**Run in a script/cron without an interactive confirmation:**

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --yes
```

**Run the internal tests** (does not touch Garmin Connect; the plan
argument is still required by argparse, even though it isn't read):

```bash
python push_plan_to_garmin.py halvmaraton_plan.json --run-tests
```

### Example: minimal plan file

```json
{
  "plan_tag": "TEST1",
  "start_monday": "2026-07-06",
  "paces": {
    "easy": ["7:15", "6:55"],
    "long": ["7:25", "7:05"]
  },
  "sessions": [
    {"week": 1, "day": "wed", "type": "easy", "km": 5, "name": "Easy 5 km"},
    {"week": 1, "day": "sun", "type": "long", "km": 10, "name": "Long run 10 km"}
  ]
}
```

### Example: interval session

```json
{
  "week": 11, "day": "wed", "type": "intervals", "km": 6,
  "name": "Intervals 6 km",
  "reps": 5, "work_km": 0.4, "recover_km": 0.2
}
```

Builds: easy warmup → 5×(400 m at `interval` pace / 200 m at `recovery`
pace) → easy cooldown, roughly 6 km total.

## Troubleshooting

- **`API Error 429` / "too many login attempts" on login** — Garmin's
  IP rate limit on the SSO login itself, not on the workout API. Wait a
  few minutes. Once a valid token sits in `~/.garminconnect`, normal use
  of the script rarely hits this again.
- **The pace range shows up reversed on the watch** — Garmin stores pace
  as speed (m/s); `targetValueOne` is set as the slow bound and
  `targetValueTwo` as the fast bound in `pace_target()`. Swap them there
  if your account/device expects it the other way around.
- **A workout looks wrong in Garmin Connect** — run `--dry-run --week N`
  for that week and read through the JSON for the first workout before
  uploading live.
