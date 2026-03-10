import { createRobotClient, StreamClient, type RobotClient } from '@viamrobotics/sdk';

// ---- Config from URL params ----
// Usage: ?host=ADDRESS&key=API_KEY&keyId=API_KEY_ID&cameras=cam1,cam2&duration=60&interval=5
const params = new URLSearchParams(window.location.search);
const HOST = params.get('host') ?? '';
const API_KEY = params.get('key') ?? '';
const API_KEY_ID = params.get('keyId') ?? '';
const CAMERA_NAMES = (params.get('cameras') ?? '').split(',').filter(Boolean);
const DURATION_S = parseInt(params.get('duration') ?? '60', 10);
const INTERVAL_S = parseInt(params.get('interval') ?? '5', 10);
const TTFF_TIMEOUT_MS = parseInt(params.get('ttffTimeout') ?? '30000', 10);

// ---- DOM helpers ----
const statusEl = document.getElementById('status')!;
const camerasEl = document.getElementById('cameras')!;
const jsonOutputEl = document.getElementById('json-output')!;

function setStatus(msg: string, cls: 'connecting' | 'connected' | 'error') {
  statusEl.textContent = msg;
  statusEl.className = cls;
}

interface CameraStats {
  name: string;
  streamStartTs: number;
  ttffMs: number | null;
  samples: Sample[];
  finalState: 'ok' | 'error';
  error?: string;
}

interface Sample {
  ts: number;
  elapsedMs: number;
  totalFrames: number;
  droppedFrames: number;
  videoWidth: number;
  videoHeight: number;
  currentTime: number;
  fps: number | null;
}

// ---- Per-camera panel ----
function createCameraPanel(name: string): {
  panel: HTMLElement;
  video: HTMLVideoElement;
} {
  const panel = document.createElement('div');
  panel.className = 'camera-panel';
  panel.id = `panel-${name}`;
  panel.innerHTML = `
    <h2>${name}</h2>
    <video autoplay muted playsinline></video>
    <div class="stats-grid">
      <div class="stat" id="stat-${name}-status"><span class="label">status: </span><span class="value">waiting</span></div>
      <div class="stat" id="stat-${name}-ttff"><span class="label">TTFF: </span><span class="value">—</span></div>
      <div class="stat" id="stat-${name}-resolution"><span class="label">resolution: </span><span class="value">—</span></div>
      <div class="stat" id="stat-${name}-fps"><span class="label">FPS: </span><span class="value">—</span></div>
      <div class="stat" id="stat-${name}-total-frames"><span class="label">total frames: </span><span class="value">0</span></div>
      <div class="stat" id="stat-${name}-dropped"><span class="label">dropped: </span><span class="value">0</span></div>
      <div class="stat" id="stat-${name}-elapsed"><span class="label">elapsed: </span><span class="value">0s</span></div>
    </div>
  `;
  camerasEl.appendChild(panel);
  return {
    panel,
    video: panel.querySelector('video')!,
  };
}

function updateStat(name: string, stat: string, value: string, cls?: string) {
  const el = document.getElementById(`stat-${name}-${stat}`);
  if (!el) return;
  const valEl = el.querySelector('.value')!;
  valEl.textContent = value;
  if (cls) {
    el.className = `stat ${cls}`;
  }
}

/** Returns true once the video element has decoded at least one frame. */
function hasFirstFrame(video: HTMLVideoElement): boolean {
  const quality = video.getVideoPlaybackQuality?.();
  if (quality && quality.totalVideoFrames > 0) return true;
  return video.videoWidth > 0 && video.videoHeight > 0;
}

// ---- Collect stats for one camera ----
async function collectCameraStats(
  streamClient: StreamClient,
  name: string
): Promise<CameraStats> {
  const { video } = createCameraPanel(name);
  const stats: CameraStats = {
    name,
    streamStartTs: Date.now(),
    ttffMs: null,
    samples: [],
    finalState: 'ok',
  };

  updateStat(name, 'status', 'connecting…', 'connecting');

  try {
    const stream = await streamClient.getStream(name);
    video.srcObject = stream;
    video.play().catch(() => {});
    updateStat(name, 'status', 'stream received', '');

    // Wait for first frame
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error(`TTFF timeout (${TTFF_TIMEOUT_MS / 1000}s)`));
      }, TTFF_TIMEOUT_MS);

      const checkFrame = () => {
        if (hasFirstFrame(video)) {
          stats.ttffMs = Date.now() - stats.streamStartTs;
          updateStat(name, 'ttff', `${stats.ttffMs}ms`, 'ok');
          updateStat(name, 'status', 'streaming', 'ok');
          clearTimeout(timeout);
          resolve();
          return;
        }
        requestAnimationFrame(checkFrame);
      };
      requestAnimationFrame(checkFrame);
    });

    // Collect samples at interval
    let prevTotalFrames = 0;
    let prevTs = Date.now();

    for (let elapsed = 0; elapsed < DURATION_S; elapsed += INTERVAL_S) {
      await new Promise((r) => setTimeout(r, INTERVAL_S * 1000));

      const now = Date.now();
      const quality = video.getVideoPlaybackQuality?.();
      const totalFrames = quality?.totalVideoFrames ?? 0;
      const droppedFrames = quality?.droppedVideoFrames ?? 0;
      const dtSec = (now - prevTs) / 1000;
      const fps = dtSec > 0 ? Math.round(((totalFrames - prevTotalFrames) / dtSec) * 10) / 10 : null;

      const sample: Sample = {
        ts: now,
        elapsedMs: now - stats.streamStartTs,
        totalFrames,
        droppedFrames,
        videoWidth: video.videoWidth,
        videoHeight: video.videoHeight,
        currentTime: video.currentTime,
        fps,
      };
      stats.samples.push(sample);
      prevTotalFrames = totalFrames;
      prevTs = now;

      updateStat(name, 'resolution', `${sample.videoWidth}×${sample.videoHeight}`);
      updateStat(name, 'fps', fps !== null ? `${fps}` : '—');
      updateStat(name, 'total-frames', `${sample.totalFrames}`);
      updateStat(name, 'dropped', `${sample.droppedFrames}`, sample.droppedFrames > 0 ? 'error' : 'ok');
      updateStat(name, 'elapsed', `${Math.round(sample.elapsedMs / 1000)}s`);
    }

    try { await streamClient.remove(name); } catch { /* ignore cleanup errors */ }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    stats.finalState = 'error';
    stats.error = msg;
    updateStat(name, 'status', `error: ${msg}`, 'error');
  }

  return stats;
}

// ---- Main ----
async function main() {
  if (!HOST || !API_KEY || !API_KEY_ID || CAMERA_NAMES.length === 0) {
    setStatus('Missing params. Need: ?host=...&key=...&keyId=...&cameras=cam1,cam2', 'error');
    return;
  }

  setStatus(`Connecting to ${HOST}...`, 'connecting');

  let client: RobotClient;
  try {
    client = await createRobotClient({
      host: HOST,
      credentials: {
        type: 'api-key',
        authEntity: API_KEY_ID,
        payload: API_KEY,
      },
      signalingAddress: 'https://app.viam.com:443',
      iceServers: [{ urls: 'stun:global.stun.twilio.com:3478' }],
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    setStatus(`Connection failed: ${msg}`, 'error');
    return;
  }

  setStatus(`Connected. Testing ${CAMERA_NAMES.length} camera(s) for ${DURATION_S}s each...`, 'connected');

  const streamClient = new StreamClient(client);
  const allStats: CameraStats[] = [];

  for (const cam of CAMERA_NAMES) {
    const stats = await collectCameraStats(streamClient, cam);
    allStats.push(stats);
  }

  const result = {
    schema: 'canary.webrtc.v2',
    collectedAt: new Date().toISOString(),
    host: HOST,
    durationS: DURATION_S,
    intervalS: INTERVAL_S,
    cameras: allStats,
  };

  jsonOutputEl.style.display = 'block';
  jsonOutputEl.textContent = JSON.stringify(result, null, 2);
  jsonOutputEl.setAttribute('data-complete', 'true');
  (window as unknown as Record<string, unknown>).__canaryWebrtcResult = result;

  setStatus(`Done. ${allStats.length} camera(s) tested.`, 'connected');

  try { client.disconnect(); } catch { /* ignore */ }
}

main();
