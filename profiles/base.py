"""
Base camera test profile — raw data collection, no pass/fail judgments.

Subclass and override `profile_data()` to add hardware-specific measurements.
"""

import time
from datetime import datetime, timezone

from viam.components.camera import Camera

from . import register


class BaseProfile:
    """Common camera data: get_images response + per-frame FPS samples."""

    name = "base"

    def __init__(self, camera_config):
        self.config = camera_config
        self.cam_name = camera_config["name"]
        self.profile_config = camera_config.get("profile_config", {})

    async def run(self, robot) -> dict:
        """Collect all raw data (common + profile-specific)."""
        result = {
            "camera": self.cam_name,
            "profile": self.config.get("profile", self.name),
            "config": self.config,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "get_images": None,
            "fps_samples": None,
            "profile_data": {},
            "errors": [],
        }

        try:
            cam = Camera.from_robot(robot, self.cam_name)
        except Exception as e:
            result["errors"].append(f"Camera not found: {e}")
            return result

        result["get_images"] = await self._collect_get_images(cam)
        result["fps_samples"] = await self._collect_fps_samples(cam)
        result["profile_data"] = await self.profile_data(robot, cam)

        return result

    async def profile_data(self, robot, cam) -> dict:
        """Override in subclasses for hardware-specific raw data."""
        return {}

    async def _collect_get_images(self, cam) -> dict:
        """Call get_images once and return raw frame metadata."""
        t0 = time.monotonic()
        try:
            resp = await cam.get_images()
            elapsed_ms = (time.monotonic() - t0) * 1000
            imgs = resp[0] if isinstance(resp, tuple) else resp
            frames = []
            for img in imgs:
                frames.append({
                    "name": getattr(img, "name", None),
                    "data_bytes": len(img.data) if hasattr(img, "data") else None,
                    "mime_type": getattr(img, "mime_type", None),
                })
            return {
                "latency_ms": round(elapsed_ms, 1),
                "frame_count": len(frames),
                "frames": frames,
            }
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            return {
                "latency_ms": round(elapsed_ms, 1),
                "error": str(e),
            }

    async def _collect_fps_samples(self, cam, n=10) -> dict:
        """Call get_images N times, record per-call latency."""
        samples = []
        for i in range(n):
            t0 = time.monotonic()
            try:
                await cam.get_images()
                elapsed_ms = (time.monotonic() - t0) * 1000
                samples.append({"index": i, "latency_ms": round(elapsed_ms, 1)})
            except Exception as e:
                elapsed_ms = (time.monotonic() - t0) * 1000
                samples.append({"index": i, "latency_ms": round(elapsed_ms, 1), "error": str(e)})

        total_ms = sum(s["latency_ms"] for s in samples)
        successful = [s for s in samples if "error" not in s]
        return {
            "num_calls": n,
            "total_ms": round(total_ms, 1),
            "successful": len(successful),
            "samples": samples,
        }


register(BaseProfile)
