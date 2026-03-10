# Canary — Agentic QA Health Monitor

An OpenClaw skill that acts as an automated QA engineer for Viam camera modules and platform health.

## What it does

Each run executes sequentially:

1. **General setup** — clear machine config to blank state (preserving telegraf)
2. **Profile setup** — via `config_helper.py` CLI: add modules, discovery, add cameras from discovery results
3. **WebRTC testing** — local `webrtc-stats` app: TTFF, frame counts, drops, FPS (no external browser login)
4. **SDK testing** — headless Python SDK: get_images, FPS samples, telegraf readings, machine logs
5. **Analysis** — on demand: reads run data, computes trends, generates report

All raw data for a run lives in one folder: `runs/YYYY-MM-DD/`

```
runs/2026-03-05/
  setup.json                  ← config trace + dev UX observations
  <profile>/webrtc.json       ← WebRTC stats per camera profile
  <profile>/samples/*.json    ← SDK collections per camera profile
  machine/*_telegraf.json     ← telegraf readings
  machine/*_logs.json         ← machine logs
  report.md                   ← analysis (generated on demand)
```

No interpretation during collection. Dumps are structured JSON scriptable by humans.

## Setup

Edit `canary.json`:

### Machine credentials
- `api_key_id` / `api_key` — machine API key
- `part_id` — for config updates and log retrieval

### Test profiles
- `test_profiles`: profiles to test (e.g. `["realsense", "orbbec"]`)
- Each profile: `profiles/<name>/setup.md` + `collect.py`
- `persistent_resources`: resource names to keep across config clears

### Where to find values
- **API key**: Machine → Connect → API keys
- **Part ID**: Machine → Setup → Part ID

### Adding new test profiles

Drop a directory in `profiles/<name>/` with:
- `setup.md` — setup steps for the agent
- `collect.py` — data collection class (subclass `BaseProfile`, call `register()`)
- `__init__.py` — import the collect class

No changes to other files needed.

## Requirements

- OpenClaw with browser tool (for local webrtc-stats app)
- Node.js (for webrtc-stats dev server)
- Python 3.10+ with `viam-sdk`
- Network access to Viam machines

See `SKILL.md` for the full playbook.
