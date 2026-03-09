# RealSense Setup Playbook

## Rule
Camera components MUST come from the discovery service's test card. Never manually add a realsense camera component.

## Method
Browser on app.viam.com for all setup. Avoid using the raw JSON editor tab on the configure page. Use it only if the builder tab is too hard to use for the canary.
Use `config_helper.py` only for: `clear-resources`, `get-config`, `get-logs`, and as discovery fallback if UI is broken.

## Steps

### 1. Add the realsense module
Navigate to machine config page. Add the `viam:realsense` module (not a camera component). Set version to "latest-with-prerelease" using the builder UI. Save.

### 2. Add the discovery service
Add service: model `viam:realsense:discovery`. Save.

### 3. Wait for startup
Wait for module + discovery service to come online. Check logs: `get-logs --num 50 --lookback 2`.

**Observe:**
- Did any errors fire during startup? Are they real failures or benign noise?
- Can you tell from the logs alone whether startup succeeded or failed?
- Is there a clear "ready" signal, or do you have to guess?
- Are error levels accurate? (e.g. benign issues logged as ERROR = bad signal-to-noise)

### 4. Discover and add cameras
Go to CONTROL tab → discovery service test card → trigger/view discovery results → add discovered cameras to config. Save.

If discovery UI is broken, fall back to CLI and note it:
```bash
python3 config_helper.py --config canary.json --machine <MACHINE> discover --service <name>
python3 config_helper.py --config canary.json --machine <MACHINE> add-resource-from-discovery-result --json '<result>'
```

### 5. Verify cameras running
Check CONTROL tab for live stream. Then toggle the **point cloud viewer** on the camera's test card and verify the 3D point cloud renders.

Check logs: `get-logs --num 50 --lookback 2`.

**Observe:**
- Does the 2D live stream render correctly?
- Does the point cloud viewer load and display a 3D scene? Does it look reasonable (not a flat plane, not all zeros, not garbage)?
- Are there firmware warnings? Are they actionable (do they tell you what to do)?
- Any errors that look scary but are actually harmless? Note the false alarm.
- Could you diagnose a real failure from these logs without source code access?

## After Setup
Run `get-config` to verify: camera has `serial_number` in attributes, name matches discovery output (not generic like `camera-1`), model is `viam:camera:realsense`.

## Developer UX Observations

At every step, evaluate from the perspective of a developer debugging or setting up this module:

- **Error quality** — Do error messages explain what went wrong AND what to do about it? Or are they opaque (e.g. "Unknown sensor type" with no context)?
- **Log noise** — Are logs cluttered with benign errors/warnings that drown out real issues? What's the signal-to-noise ratio?
- **Health visibility** — Can you tell at a glance whether the module/camera/service is healthy? Or do you have to dig through logs and guess?
- **Debuggability** — If something broke, would the logs + UI give you enough to file a bug report without reading source code?
- **Accuracy** — Do error levels match severity? (INFO for info, WARN for recoverable, ERROR for failures — not ERROR for benign quirks)
- **Firmware/version clarity** — Is it obvious what versions are running and whether they're current?
- **Failure modes** — If a camera is disconnected or USB is flaky, do you get a useful error or a cryptic crash?

## Expected Outcome
- 1 module: `viam:realsense` (latest-with-prerelease)
- 1 discovery service
- N cameras from discovery (with `serial_number` in attributes)
- Live video on CONTROL tab
