"""
Orbbec Astra2 camera test profile — raw data collection (MVP).

Collects get_images frame metadata, point cloud data with PCD header validation,
source filter tests, and FPS samples.

All validations assert EXACT expected values measured from real Astra2 hardware
on 2026-03-09. Constants derived from D2C-aligned output (depth reprojected to
color resolution 1280×720).
"""

import asyncio
import hashlib
import io
import time
from datetime import datetime, timezone

from PIL import Image
from viam.components.camera import Camera

from profiles.base import BaseProfile
from profiles import register

# ---------------------------------------------------------------------------
# Expected constants for the Orbbec Astra2 at default config
# Color: 1280×720 MJPG, Depth: 1600×1200 Y16 (aligned to 1280×720 in SDK output)
# Measured on real hardware 2026-03-09, serial AARY14100X5
# ---------------------------------------------------------------------------

# get_images
EXPECTED_FRAME_COUNT = 2
EXPECTED_COLOR_SOURCE_NAME = "color"
EXPECTED_DEPTH_SOURCE_NAME = "depth"
EXPECTED_COLOR_MIME_TYPE = "image/jpeg"
EXPECTED_DEPTH_MIME_TYPE = "image/vnd.viam.dep"
# Depth at 1280×720 (after D2C alignment) in viam dep format
# Measured: 1843224 bytes consistently
EXPECTED_DEPTH_DATA_BYTES = 1843224

# Color resolution (from RGB intrinsics, default config)
EXPECTED_COLOR_WIDTH = 1280
EXPECTED_COLOR_HEIGHT = 720

# get_point_cloud
EXPECTED_PCD_MIME_TYPE = "pointcloud/pcd"
EXPECTED_PCD_VERSION = ".7"
EXPECTED_PCD_FIELDS = "x y z rgb"
EXPECTED_PCD_SIZE = "4 4 4 4"
EXPECTED_PCD_TYPE = "F F F U"
EXPECTED_PCD_COUNT = "1 1 1 1"
EXPECTED_PCD_DATA_FORMAT = "binary"
EXPECTED_PCD_POINTS = 921600  # 1280 × 720 (after D2C alignment)
EXPECTED_PCD_BYTES_PER_POINT = 16  # 4+4+4+4
EXPECTED_PCD_BODY_BYTES = EXPECTED_PCD_POINTS * EXPECTED_PCD_BYTES_PER_POINT  # 14745600

# discovery
EXPECTED_DISCOVERY_MODEL = "viam:orbbec:astra2"
EXPECTED_DISCOVERY_API = "rdk:component:camera"
# Note: orbbec discovery does NOT include a "sensors" attribute (unlike realsense)


class OrbbecProfile(BaseProfile):
    """Orbbec Astra2 raw data collection (MVP: get_images + get_point_cloud)."""

    name = "orbbec"

    async def run(self, robot) -> dict:
        """Collect all raw data for the Astra2."""
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

        # Single get_images call for frame metadata
        result["get_images"] = await self._collect_get_images(cam)

        # Run get_images FPS samples and get_point_cloud samples simultaneously
        imgs_task = asyncio.create_task(self._collect_fps_samples(cam))
        pcd_task = asyncio.create_task(self._collect_pcd_samples(cam))
        result["fps_samples"], pcd_samples = await asyncio.gather(imgs_task, pcd_task)

        # Profile-specific data
        result["profile_data"] = await self._collect_profile_data(cam)
        result["profile_data"]["point_cloud"] = pcd_samples
        result["profile_data"]["source_filter"] = await self._test_source_filters(cam)

        return result

    # ------------------------------------------------------------------
    # Frame metadata
    # ------------------------------------------------------------------

    async def _collect_profile_data(self, cam) -> dict:
        """Collect orbbec-specific frame metadata."""
        data = {}

        try:
            resp = await cam.get_images()
            imgs = resp[0] if isinstance(resp, tuple) else resp
        except Exception as e:
            return {"error": f"get_images failed for profile data: {e}"}

        data["depth_frame"] = self._collect_depth_info(imgs)
        data["color_frame"] = self._collect_color_info(imgs)
        return data

    def _collect_depth_info(self, imgs) -> dict:
        depth = None
        for img in imgs:
            if getattr(img, "name", None) == EXPECTED_DEPTH_SOURCE_NAME:
                depth = img
                break
        if depth is None:
            return {"found": False, "frame_count": len(imgs)}

        data_bytes = len(depth.data) if hasattr(depth, "data") else None
        mime = getattr(depth, "mime_type", None)
        return {
            "found": True,
            "name": depth.name,
            "name_exact": depth.name == EXPECTED_DEPTH_SOURCE_NAME,
            "data_bytes": data_bytes,
            "data_bytes_exact": data_bytes == EXPECTED_DEPTH_DATA_BYTES,
            "mime_type": mime,
            "mime_type_exact": mime == EXPECTED_DEPTH_MIME_TYPE,
        }

    def _collect_color_info(self, imgs) -> dict:
        color = None
        for img in imgs:
            if getattr(img, "name", None) == EXPECTED_COLOR_SOURCE_NAME:
                color = img
                break
        if color is None:
            return {"found": False, "frame_count": len(imgs)}

        data_bytes = len(color.data) if hasattr(color, "data") else None
        mime = getattr(color, "mime_type", None)
        info = {
            "found": True,
            "name": color.name,
            "name_exact": color.name == EXPECTED_COLOR_SOURCE_NAME,
            "data_bytes": data_bytes,
            "mime_type": mime,
            "mime_type_exact": mime == EXPECTED_COLOR_MIME_TYPE,
        }
        try:
            pil_img = Image.open(io.BytesIO(color.data))
            info["width"] = pil_img.size[0]
            info["height"] = pil_img.size[1]
            info["width_exact"] = pil_img.size[0] == EXPECTED_COLOR_WIDTH
            info["height_exact"] = pil_img.size[1] == EXPECTED_COLOR_HEIGHT
        except Exception as e:
            info["pil_error"] = str(e)
        return info

    # ------------------------------------------------------------------
    # Point cloud sampling
    # ------------------------------------------------------------------

    async def _collect_pcd_samples(self, cam, n=10) -> dict:
        """Call get_point_cloud N times with exact format validation."""
        samples = []
        hashes = []

        for i in range(n):
            sample = {"index": i}
            t0 = time.monotonic()
            try:
                pcd_bytes, mime_type = await cam.get_point_cloud()
                elapsed_ms = (time.monotonic() - t0) * 1000

                h = hashlib.sha256(pcd_bytes).hexdigest()
                sample["latency_ms"] = round(elapsed_ms, 1)
                sample["mime_type"] = mime_type
                sample["data_bytes"] = len(pcd_bytes)
                sample["sha256"] = h
                hashes.append(h)

                sample["mime_type_exact"] = mime_type == EXPECTED_PCD_MIME_TYPE

                # Parse and validate PCD header on first and last sample
                if i == 0 or i == n - 1:
                    sample["header"] = self._parse_pcd_header(pcd_bytes)

            except Exception as e:
                sample["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                sample["error"] = str(e)
                hashes.append(None)

            samples.append(sample)

        # Staleness analysis
        stale_pairs = 0
        total_pairs = 0
        for i in range(1, len(hashes)):
            if hashes[i] is not None and hashes[i - 1] is not None:
                total_pairs += 1
                if hashes[i] == hashes[i - 1]:
                    stale_pairs += 1

        successful = [s for s in samples if "error" not in s]
        total_ms = sum(s["latency_ms"] for s in samples)

        return {
            "num_calls": n,
            "total_ms": round(total_ms, 1),
            "successful": len(successful),
            "all_succeeded": len(successful) == n,
            "all_mime_types_correct": all(
                s.get("mime_type_exact") is True for s in samples if "error" not in s
            ),
            "samples": samples,
            "staleness": {
                "consecutive_identical_pairs": stale_pairs,
                "total_comparable_pairs": total_pairs,
                "all_unique": stale_pairs == 0 and total_pairs > 0,
                "zero_stale_pairs": stale_pairs == 0,
                "unique_hashes": len(set(h for h in hashes if h is not None)),
            },
        }

    def _parse_pcd_header(self, pcd_bytes: bytes) -> dict:
        """Parse a PCD file header and validate against exact expected values."""
        header = {}
        try:
            data_idx = pcd_bytes.find(b"\nDATA ")
            if data_idx < 0:
                return {"error": "No DATA field found in PCD header"}

            data_line_end = pcd_bytes.find(b"\n", data_idx + 1)
            if data_line_end < 0:
                data_line_end = data_idx + 30

            header_text = pcd_bytes[:data_line_end].decode("ascii", errors="replace")
            body_offset = data_line_end + 1

            for line in header_text.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    header[parts[0].lower()] = parts[1]

            header["body_offset"] = body_offset
            header["body_bytes"] = len(pcd_bytes) - body_offset
            header["total_bytes"] = len(pcd_bytes)

            v = {}
            v["version_exact"] = header.get("version") == EXPECTED_PCD_VERSION
            v["fields_exact"] = header.get("fields") == EXPECTED_PCD_FIELDS
            v["size_exact"] = header.get("size") == EXPECTED_PCD_SIZE
            v["type_exact"] = header.get("type") == EXPECTED_PCD_TYPE
            v["count_exact"] = header.get("count") == EXPECTED_PCD_COUNT
            v["data_format_exact"] = header.get("data") == EXPECTED_PCD_DATA_FORMAT

            if "points" in header:
                try:
                    points = int(header["points"])
                    header["points_int"] = points
                    v["points_exact"] = points == EXPECTED_PCD_POINTS

                    if header.get("data") == EXPECTED_PCD_DATA_FORMAT and "size" in header:
                        sizes = [int(s) for s in header["size"].split()]
                        bytes_per_point = sum(sizes)
                        header["bytes_per_point"] = bytes_per_point
                        header["expected_body_bytes"] = points * bytes_per_point
                        v["bytes_per_point_exact"] = bytes_per_point == EXPECTED_PCD_BYTES_PER_POINT
                        v["body_bytes_exact"] = header["body_bytes"] == EXPECTED_PCD_BODY_BYTES
                        v["body_size_match"] = header["body_bytes"] == points * bytes_per_point
                except ValueError:
                    v["points_parse_error"] = True

            header["validations"] = v

        except Exception as e:
            header["parse_error"] = str(e)

        return header

    # ------------------------------------------------------------------
    # Source filter tests
    # ------------------------------------------------------------------

    async def _test_source_filters(self, cam) -> dict:
        """Test get_images with filter_source_names for each source."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "available_sources": [],
            "per_source": {},
            "validations": {},
            "error": None,
        }

        try:
            resp = await cam.get_images()
            imgs = resp[0] if isinstance(resp, tuple) else resp
            result["available_sources"] = [
                {"name": img.name, "mime_type": getattr(img, "mime_type", None),
                 "data_bytes": len(img.data) if hasattr(img, "data") else None}
                for img in imgs
            ]
        except Exception as e:
            result["error"] = f"Unfiltered get_images failed: {e}"
            return result

        source_names = [s["name"] for s in result["available_sources"]]

        v = {}
        v["source_count"] = len(source_names)
        v["source_count_is_2"] = len(source_names) == EXPECTED_FRAME_COUNT
        v["source_names"] = source_names
        v["source_names_exact"] = source_names == [EXPECTED_COLOR_SOURCE_NAME, EXPECTED_DEPTH_SOURCE_NAME]

        for source in source_names:
            t0 = time.monotonic()
            test = {"requested": source}
            try:
                resp = await cam.get_images(filter_source_names=[source])
                elapsed_ms = (time.monotonic() - t0) * 1000
                filtered_imgs = resp[0] if isinstance(resp, tuple) else resp

                test["latency_ms"] = round(elapsed_ms, 1)
                test["returned_count"] = len(filtered_imgs)
                test["returned_sources"] = []

                for img in filtered_imgs:
                    test["returned_sources"].append({
                        "name": img.name,
                        "mime_type": getattr(img, "mime_type", None),
                        "data_bytes": len(img.data) if hasattr(img, "data") else None,
                    })

                returned_names = [img.name for img in filtered_imgs]
                test["returned_count_is_1"] = len(filtered_imgs) == 1
                test["returned_name_exact"] = returned_names == [source]
                test["extra_sources"] = [n for n in returned_names if n != source]
                test["no_extra_sources"] = len(test["extra_sources"]) == 0

                if len(filtered_imgs) == 1:
                    img = filtered_imgs[0]
                    if source == EXPECTED_COLOR_SOURCE_NAME:
                        test["mime_type_exact"] = getattr(img, "mime_type", None) == EXPECTED_COLOR_MIME_TYPE
                    elif source == EXPECTED_DEPTH_SOURCE_NAME:
                        test["mime_type_exact"] = getattr(img, "mime_type", None) == EXPECTED_DEPTH_MIME_TYPE
                        test["depth_data_bytes_exact"] = len(img.data) == EXPECTED_DEPTH_DATA_BYTES

            except Exception as e:
                test["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                test["error"] = str(e)

            result["per_source"][source] = test

        result["validations"] = v
        return result


register(OrbbecProfile)
