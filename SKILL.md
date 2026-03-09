# Canary — Agentic QA Health Monitor

## Purpose

Act as a QA engineer: set up Viam machines from scratch, exercise camera modules via browser and SDK, collect raw health data over ~24 hours, and produce daily analysis reports.

## File Layout

```
canary/
  SKILL.md                          ← you are here
  canary.json                        ← config
  profiles/
    __init__.py                      ← profile registry
    base.py                          ← base data collector
    sdk_test.py                      ← raw SDK data collector (full + probe modes)
    config_helper.py                 ← machine config CLI
    realsense/
      setup.md                       ← profile setup playbook
      collect.py                     ← profile data collection
    orbbec/
      setup.md                       ← profile setup playbook
      collect.py                     ← profile data collection
  runs/
    YYYY-MM-DD/                      ← one folder per day
      .lock                          ← run lock (prevents overlapping ticks)
      setup.json                     ← setup trace (once per day)
      report.md                      ← analysis report (generated at rollover)
      machine/                       ← machine-level telemetry
        HHMM_full_telegraf.json
        HHMM_full_logs.json
      <profile>/                     ← one per active profile (realsense, orbbec, etc.)
        webrtc.json                  ← WebRTC samples for this profile's camera
        samples/
          HHMM_full.json             ← full SDK collection (profile cameras only)
          HHMM_probe.json            ← lightweight probes (profile cameras only)
```

## Python Environment

**Always use the skill's own venv for all Python invocations.** Never use system Python.

### Setup (one-time)

```bash
cd <SKILL_DIR>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Usage

All `python3` commands in this skill must use the venv interpreter:

```bash
cd <SKILL_DIR>
.venv/bin/python3 profiles/sdk_test.py --config canary.json ...
.venv/bin/python3 profiles/config_helper.py --config canary.json ...
```

If the venv doesn't exist or is missing dependencies, create/update it before proceeding:

```bash
cd <SKILL_DIR>
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
```

**Why:** System Python may lack `Pillow` (needed for resolution cross-checks in profiles), have wrong `viam-sdk` version, or conflict with other tools. The venv pins exact versions via `requirements.txt` and keeps canary isolated.

---

## Configuration

`canary.json` fields:

- **machines[]**: `name`, `address`, `api_key_id`, `api_key`, `part_id`, `machine_id`, `test_profiles[]`, `persistent_resources[]`, `telegraf_sensor`
- **viam_app**: `email`, `password` (must be email/password, not Google SSO)
- **logs**: `enabled`, `num_entries`, `lookback_minutes`, `levels`
- **schedule**: `cron_expr`, `timezone`
- **alerts**: `slack_webhook`, `whatsapp`
- **runs_dir**: output directory (default: `runs`)

## Cron Registration

```
cron(action="add", job={
  "name": "canary",
  "schedule": { "kind": "cron", "expr": "<schedule.cron_expr>", "tz": "<schedule.timezone>" },
  "payload": {
    "kind": "agentTurn",
    "message": "Run a canary tick. Read <skill_dir>/SKILL.md and follow the Tick Execution Flow."
  },
  "sessionTarget": "isolated",
  "enabled": true
})
```

---

## Daily Run Model

Instead of independent runs every 30 minutes, the canary accumulates data within a single daily run folder (`runs/YYYY-MM-DD/`):

- **First tick of the day**: Rollover (analyze previous day → WhatsApp report) → full setup (browser) → full SDK collection. Budget ~1 hour for this.
- **Subsequent ticks**: Lightweight probe only (single get_images per camera + telegraf + logs). Takes ~2 minutes.
- **Lock file**: Prevents overlapping ticks. If a previous tick is still running, the new tick skips gracefully.

---

## Tick Execution Flow

Every cron tick follows this flow:

### 0. Determine state

```
SKILL_DIR = directory containing this SKILL.md
PROFILES_DIR = SKILL_DIR/profiles
RUNS_DIR = SKILL_DIR/<runs_dir from canary.json>
TIMEZONE = schedule.timezone from canary.json
TODAY = YYYY-MM-DD in TIMEZONE
NOW = HHMM in TIMEZONE
TODAY_DIR = RUNS_DIR/TODAY
LOCK_FILE = TODAY_DIR/.lock
```

### 1. Check lock

Check if `LOCK_FILE` exists:
- **Lock exists and age < 90 minutes** → Write `TODAY_DIR/machine/NOW_skipped.json` with `{"reason": "locked", "lock_age_minutes": N}`. Exit.
- **Lock exists and age ≥ 90 minutes** → Stale lock. Delete it, log a warning, proceed.
- **No lock** → Proceed.

Create `TODAY_DIR` (and `TODAY_DIR/machine/`) if they don't exist. Write `LOCK_FILE` with current timestamp.

### 2. Determine tick type

Check if `TODAY_DIR/setup.json` exists:
- **No** → This is the **first tick of the day**. Go to step 3 (Rollover + Setup + Full Collection).
- **Yes** → This is a **probe tick**. Go to step 7 (Probe Collection).

### 3. Rollover — analyze previous day

Find the most recent `runs/YYYY-MM-DD/` folder that is NOT today and HAS `setup.json`.

If found:
- Read all files from that folder: `setup.json`, `<profile>/webrtc.json`, `<profile>/samples/*.json`, `machine/*_telegraf.json`, `machine/*_logs.json`
- Run **Step 9: Analysis** on that data
- Write `report.md` into that folder
- Send WhatsApp summary (compact: key metrics + pass/fail per category)

If not found (first ever run), skip rollover.

### 4. General Setup

#### 4.1: Load config

Read `canary.json`. Create `TODAY_DIR` and `TODAY_DIR/samples/` if not already done.

#### 4.2: Clear machine config

Preserve persistent resources, clear everything else:

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 config_helper.py --config ../canary.json --machine <MACHINE> \
  clear-resources --preserve <comma-separated persistent_resources>
```

#### 4.3: Start browser and log in

1. `browser(action="start", profile="openclaw")`
2. Navigate to `https://app.viam.com/machine/<machine_id>`
3. Log in with email/password from `canary.json` viam_app credentials if needed
4. Use JS evaluate (IIFE pattern) for interactions

### 5. Profile Setup (browser)

For each profile in `test_profiles`:

1. Read `profiles/<profile>/setup.md`
2. Follow the steps via the browser on app.viam.com
3. After each step, capture logs via `config_helper.py get-logs`
4. After all profile steps, verify with `config_helper.py get-config`

**`config_helper.py` is ONLY used for:**
- `clear-resources` (step 4.2)
- `get-config` (verification)
- `get-version` (capture viam-server + module semvers)
- `get-logs` (raw log capture)
- `discover` + `add-resource-from-discovery-result` (fallback if discovery UI is broken)

#### 5.1: Capture versions

After all profiles are set up and config is saved, capture exact versions:

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 config_helper.py --config ../canary.json --machine <MACHINE> get-version
```

This returns viam-server version/platform/api_version and all module name/module_id/version entries.

#### Write setup dump

Write `TODAY_DIR/setup.json`. The `versions` field is mandatory — it records the exact semvers of viam-server and all modules used in this run:

```json
{
  "schema": "canary.setup.v1",
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601",
  "machine": "name",
  "versions": {
    "viam_server": {
      "version": "v0.x.y",
      "platform": "linux/amd64",
      "api_version": "v0.x.y"
    },
    "modules": [
      { "name": "viam_realsense", "module_id": "viam:realsense", "version": "0.21.0-rc1" }
    ]
  },
  "cleared_config": { ... },
  "profiles": {
    "<profile>": {
      "setup_steps": [
        {
          "step": "description",
          "result": { ... },
          "logs_after": [ ... ],
          "elapsed_ms": 1234,
          "notes": "..."
        }
      ],
      "components_added": ["name"],
      "dev_ux_observations": [ "..." ],
      "errors": [],
      "total_setup_ms": 15000
    }
  },
  "final_config": { ... }
}
```

### 6. WebRTC Testing (browser)

Browser is already open from setup. Only run if `viam_app.password` is set and not `"REPLACE_ME"`.

**Run WebRTC for each profile's camera sequentially.** For each profile in `test_profiles`:

1. Navigate to machine CONTROL tab
2. Find the camera stream for this profile's camera, switch to **Live** mode (WebRTC)
3. Record `stream_start_ts` via JS: `Date.now()`
4. Poll `getVideoPlaybackQuality()` at configured interval until first frame → record `ttff_ts`
5. Continue sampling for configured duration
6. Write `TODAY_DIR/<profile>/webrtc.json`:

```json
{
  "schema": "canary.webrtc.v1",
  "collected_at": "ISO-8601",
  "machine": "name",
  "profile": "realsense",
  "camera": "name",
  "stream_start_ts": 1709654400000,
  "ttff_ts": 1709654407000,
  "sample_interval_ms": 30000,
  "samples": [
    {
      "ts": 1709654430000,
      "total_frames": 142,
      "dropped_frames": 0,
      "video_width": 1280,
      "video_height": 720,
      "current_time": 4.5,
      "paused": false
    }
  ]
}
```

No FPS calculation, no pass/fail. Raw samples only.

**Note:** Each additional profile adds ~5 minutes to WebRTC collection. Budget accordingly.

### 6.5. Close browser

**MANDATORY.** After WebRTC collection, stop the browser immediately. Do not leave it running.

```
browser(action="stop", profile="openclaw")
```

Chrome renderer processes leak memory aggressively (~1-2 GB per session). If the browser is not stopped, leftover renderers accumulate across daily runs and will consume all available RAM within days, producing false memory-leak signals in canary reports.

This step is not optional. If WebRTC fails or is skipped, still stop the browser. If setup fails partway through, still stop the browser. **Any code path that calls `browser(action="start")` MUST eventually call `browser(action="stop")`.**

### 6.6. Full SDK Collection

After closing the browser, run a full SDK collection as the first sample of the day:

#### Build runtime config

Read machine config via `get-config`. Match camera components to profiles by model:
- `viam:camera:realsense` → realsense
- `viam:orbbec:astra2` → orbbec

Write `/tmp/canary-runtime.json` with machine credentials + all discovered cameras:
```json
{
  "machines": [{
    "name": "...", "address": "...", "api_key_id": "...", "api_key": "...", "part_id": "...",
    "cameras": [
      { "name": "realsense-348522073801", "profile": "realsense", "type": "3d" },
      { "name": "orbbec-astra2", "profile": "orbbec", "type": "3d" }
    ],
    "telegraf_sensor": { "name": "telegraf-sensor" }
  }],
  "logs": { "enabled": true, "num_entries": 100, "lookback_minutes": 30, "levels": [] }
}
```

#### Run full collector

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 sdk_test.py --config /tmp/canary-runtime.json --output-dir <TODAY_DIR> --tag <NOW>_full
```

This writes profile-scoped output:
- `<TODAY_DIR>/<profile>/samples/<NOW>_full.json` — per-profile camera data (schema `canary.dump.v2`)
- `<TODAY_DIR>/machine/<NOW>_full_telegraf.json` — telegraf readings (schema `canary.telegraf.v1`)
- `<TODAY_DIR>/machine/<NOW>_full_logs.json` — machine logs (schema `canary.logs.v1`)

**Backward compat:** The old `-o` flag still works for combined single-file output (schema `canary.dump.v1`). Use `--output-dir`/`--tag` for all new runs.

#### Release lock and exit

Delete `LOCK_FILE`. Verify the browser is not running (it should already be stopped from step 6.5, but double-check). First tick complete.

---

### 7. Probe Collection (subsequent ticks)

Lightweight — no browser, no setup, no point cloud.

#### 7.1: Build runtime config

Same as step 6.6 — read machine config, build `/tmp/canary-runtime.json`.

If the machine config has no cameras (setup failed or config was cleared externally), log an error to `machine/NOW_skipped.json` and release the lock.

#### 7.2: Run probe collector

```bash
cd <PROFILES_DIR>
<SKILL_DIR>/.venv/bin/python3 sdk_test.py --config /tmp/canary-runtime.json --probe --output-dir <TODAY_DIR> --tag <NOW>_probe
```

#### 7.3: Release lock and exit

Delete `LOCK_FILE`. Probe tick complete.

---

## Step 9: Analysis (at rollover or on demand)

### Load data

Read all files from the target run folder (`runs/YYYY-MM-DD/`):
- `setup.json` — setup trace
- `<profile>/webrtc.json` — WebRTC samples (one per profile)
- `<profile>/samples/*_full.json` — full SDK collections (per profile, schema `canary.dump.v2`)
- `<profile>/samples/*_probe.json` — lightweight probes (per profile, schema `canary.dump.v2`)
- `machine/*_telegraf.json` — telegraf readings (schema `canary.telegraf.v1`)
- `machine/*_logs.json` — machine logs (schema `canary.logs.v1`)

**Legacy compat:** If `samples/*_full.json` or `samples/*_probe.json` exist at the run root (v1 layout), read those instead. This handles runs that started before the v2 output migration.

### Analyze

**Setup (from setup.json):**
- Exact viam-server version + all module semvers (from `versions` field)
- Discovery results, setup timing
- Developer UX observations collated across profiles

**WebRTC (from `<profile>/webrtc.json`):**
- Per-profile TTFF (ttff_ts - stream_start_ts)
- FPS time series (frame deltas between samples)
- Dropped frames, resolution consistency
- Compare TTFF across profiles if multiple exist

**SDK — Trend Analysis (from `<profile>/samples/`):**

Analyze each profile separately, then compare:
- **Latency**: p50/p95/p99 of get_images latency across all probes (per profile)
- **FPS stability**: from full collection FPS samples (per profile)
- **Failure rate**: % of probes with SDK call errors (per profile)
- **Frame consistency**: data_bytes variance across probes (sudden drops = concern)
- **Cross-profile comparison**: latency/reliability differences between realsense and orbbec

**Telegraf — Resource Trends (from `machine/*_telegraf.json`):**
- **Memory**: baseline (first sample) vs final, linear regression on RSS. Positive slope = leak candidate. Flag if slope suggests >10% growth per 24h.
- **CPU**: mean, max, sustained high periods
- **Temperature**: max, trend direction, thermal throttle risk
- **Disk**: usage trend
- **Load**: mean load1 vs n_cpus

**Logs — Noise & Severity (from `machine/*_logs.json`):**
- Total error/fatal count across the day
- Top 5 recurring log messages (grouped by message template)
- Error rate per hour (are errors bursty or steady?)
- New error classes (appeared only in later probes, not in first)
- Fatal entries highlighted

**Stability:**
- Any probes that failed to connect (module crash/restart?)
- Lock contention (any skipped ticks?)
- Config state consistency (did components disappear mid-day?)

### Generate report

Write `TODAY_DIR/report.md` with sections for each category above.

### WhatsApp Summary

Compact format for WhatsApp (no markdown tables, no headers):

```
🐤 Canary Report — YYYY-MM-DD

*realsense (D435i)*
get_images p50: XXms  p95: XXms
PCD: XXms avg (N calls)
WebRTC TTFF: XXs
Probes: XX/XX ok

*orbbec (Astra2)*
get_images p50: XXms  p95: XXms
PCD: XXms avg (N calls)
WebRTC TTFF: XXs
Probes: XX/XX ok

*Machine*
Memory: baseline XXM → final XXM (slope: +X.X MB/hr)
Errors: XX total (XX unique)

*Top Issues*
1. [ERROR] repeated message (×42)
2. [WARN] another message (×15)

*UX Notes*
- observation from setup
```

---

## Thresholds (analysis only)

- Memory > 80% or slope suggests >10% growth per 24h → ⚠️
- CPU idle < 20% sustained → ⚠️
- Swap > 0 → ⚠️
- Load1 > 2× n_cpus → ⚠️
- Zombies > 0 → ⚠️
- Temp > 80°C → ⚠️
- Disk > 90% → ⚠️
- Errors in logs → ⚠️
- get_images failures → ⚠️
- >2 skipped ticks (lock contention) → ⚠️

---

## Troubleshooting

- **config_helper.py errors**: Check API key and part_id
- **Discovery empty**: Module may not have started — wait, check logs
- **Browser broken**: Google Chrome (not snap Chromium), `noSandbox: true`
- **Google SSO blocks login**: Must use email/password
- **Telegraf missing after clear**: Check `persistent_resources`
- **Lock stuck**: If `.lock` is older than 90 min, it's stale — delete and proceed
- **First tick too slow**: Setup + browser + WebRTC can take 30-60 min. Lock prevents overlap.
- **Memory climbing across probes**: Check `ps aux --sort=-%mem | head -20` — if Chrome renderers are top consumers, the browser wasn't stopped after setup. Kill them with `browser(action="stop")` or `pkill -f "chrome.*openclaw/browser"`. This is a canary bug, not a viam-server leak.

## Rules

1. **Never add components/modules outside setup.** Setup is intentional and logged.
2. **Always run browser steps in a subagent.**
3. **Setup uses the browser, not the SDK.** `config_helper.py` only for clear/verify/logs/fallback.
4. **Steps 1-6.5 are collection only.** No interpretation, no pass/fail, no comparisons.
5. **Developer UX observations during setup.** Error quality, log noise, health visibility, debuggability.
6. **Self-contained.** All config from `canary.json` and this directory.
7. **Cron for recurring work, not subagents.**
8. **Dumps are source of truth.** Scriptable by humans.
9. **persistent_resources survive clears.**
10. **Lock before work, unlock after.** Never leave a lock behind — always clean up, even on error.
11. **First tick gets a full hour.** Budget for setup + browser + WebRTC + full SDK.
12. **Probe ticks are lightweight.** No browser, no point cloud, no source filters.
13. **ALWAYS stop the browser.** Every code path that starts Chrome must stop it before exiting — including error/failure paths. Chrome renderers leak ~1-2 GB each and accumulate across runs. Failing to stop the browser poisons memory data for all future probes.
