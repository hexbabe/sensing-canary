# Canary — Agentic QA Health Monitor

An OpenClaw skill that acts as an automated QA engineer for Viam camera modules and platform health.

## What it does

Each run executes sequentially:

1. **General setup** — clear machine config to blank state (preserving telegraf), start browser, log in
2. **Profile setup** — per-profile via browser: add modules, discovery, add cameras from discovery results
3. **WebRTC testing** — browser-based live stream monitoring (TTFF, frame counts, drops)
4. **SDK testing** — headless Python SDK: get_images, FPS samples, telegraf readings, machine logs
5. **Analysis** — on demand: reads run data, computes trends, generates report

All raw data for a run lives in one folder: `runs/YYYY-MM-DD_HHMM/`

```
runs/2026-03-05_1430/
  setup.json        ← config trace + dev UX observations
  webrtc.json       ← raw getVideoPlaybackQuality() samples
  sdk.json          ← camera frames, FPS, telegraf, logs
  report.md         ← analysis (generated on demand)
```

No interpretation during collection. Dumps are structured JSON scriptable by humans.

## Setup

Edit `canary.json`:

### Machine credentials
- `api_key_id` / `api_key` — machine API key
- `part_id` — for config updates and log retrieval
- `machine_id` — for browser URL: `https://app.viam.com/machine/<machine_id>`

### Test profiles
- `test_profiles`: profiles to test (e.g. `["realsense"]`)
- Each profile: `profiles/<name>/setup.md` + `collect.py`
- `persistent_resources`: resource names to keep across config clears

### Viam app account
- `email` / `password` for browser testing (must be email/password, not Google SSO)

### Where to find values
- **API key**: Machine → Connect → API keys
- **Part ID**: Machine → Setup → Part ID
- **Machine ID**: UUID in URL: `https://app.viam.com/machine/<machine_id>`

### Adding new test profiles

Drop a directory in `profiles/<name>/` with:
- `setup.md` — setup steps for the agent
- `collect.py` — data collection class (subclass `BaseProfile`, call `register()`)
- `__init__.py` — import the collect class

No changes to other files needed.

## Requirements

- OpenClaw with browser tool (`noSandbox: true`)
- Python 3.10+ with `viam-sdk`
- Network access to Viam machines and `app.viam.com`

See `SKILL.md` for the full playbook.
