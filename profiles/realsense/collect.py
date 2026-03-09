"""
RealSense camera test profile — raw data collection.

Collects depth frame metadata, resolution, and point cloud data.
Runs get_images and get_point_cloud sampling simultaneously during FPS tests.
"""

import asyncio
import hashlib
import time
from datetime import datetime, timezone

from viam.components.camera import Camera

from profiles.base import BaseProfile
from profiles import register


class RealSenseProfile(BaseProfile):
    """Intel RealSense (D435i, D455, etc.) raw data collection."""

    name = "realsense"

    async def run(self, robot) -> dict:
        """Override base to run get_images and get_point_cloud simultaneously."""
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

        # Collect realsense-specific profile data (depth/color frame info)
        result["profile_data"] = await self._collect_profile_data(cam)
        result["profile_data"]["point_cloud"] = pcd_samples
        result["profile_data"]["source_filter"] = await self._test_source_filters(cam)

        return result

    async def _collect_profile_data(self, cam) -> dict:
        """Collect realsense-specific frame metadata."""
        data = {}

        try:
            resp = await cam.get_images()
            imgs = resp[0] if isinstance(resp, tuple) else resp
        except Exception as e:
            return {"error": f"get_images failed for profile data: {e}"}

        data["depth_frame"] = self._collect_depth_info(imgs)
        data["color_frame"] = self._collect_color_info(imgs)
        return data

    async def _test_source_filters(self, cam) -> dict:
        """Test get_images with filter_source_names for each available source.

        1. Call get_images unfiltered to discover available source names
        2. For each source, call get_images(filter_source_names=[source])
        3. Assert only the requested source is returned with expected properties
        """
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "available_sources": [],
            "per_source": {},
            "error": None,
        }

        # Discover available sources from unfiltered call
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

        # Test each source individually
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

                # Validate: should return exactly 1 image matching the requested source
                returned_names = [img.name for img in filtered_imgs]
                test["correct_source_returned"] = returned_names == [source]
                test["extra_sources"] = [n for n in returned_names if n != source]

            except Exception as e:
                test["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                test["error"] = str(e)

            result["per_source"][source] = test

        return result

    async def _collect_pcd_samples(self, cam, n=10) -> dict:
        """Call get_point_cloud N times, record per-call latency, validate format,
        and check for stale data between consecutive calls."""
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

                # Parse and validate PCD header on first and last sample
                if i == 0 or i == n - 1:
                    sample["header"] = self._parse_pcd_header(pcd_bytes)

            except Exception as e:
                sample["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                sample["error"] = str(e)
                hashes.append(None)

            samples.append(sample)

        # Staleness analysis: count how many consecutive pairs are identical
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
            "samples": samples,
            "staleness": {
                "consecutive_identical_pairs": stale_pairs,
                "total_comparable_pairs": total_pairs,
                "all_unique": stale_pairs == 0 and total_pairs > 0,
                "unique_hashes": len(set(h for h in hashes if h is not None)),
            },
        }

    def _parse_pcd_header(self, pcd_bytes: bytes) -> dict:
        """Parse a PCD file header and validate format."""
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

            if "points" in header:
                try:
                    points = int(header["points"])
                    header["points_int"] = points
                    if header.get("data") == "binary" and "size" in header:
                        sizes = [int(s) for s in header["size"].split()]
                        bytes_per_point = sum(sizes)
                        header["bytes_per_point"] = bytes_per_point
                        header["expected_body_bytes"] = points * bytes_per_point
                        header["body_size_match"] = header["body_bytes"] == points * bytes_per_point
                except ValueError:
                    pass

        except Exception as e:
            header["parse_error"] = str(e)

        return header

    def _collect_depth_info(self, imgs) -> dict:
        depth = None
        for img in imgs:
            if "depth" in getattr(img, "name", "").lower():
                depth = img
                break
        if depth is None and len(imgs) >= 2:
            depth = imgs[1]
        if depth is None:
            return {"found": False, "frame_count": len(imgs)}
        return {
            "found": True,
            "name": getattr(depth, "name", None),
            "data_bytes": len(depth.data) if hasattr(depth, "data") else None,
            "mime_type": getattr(depth, "mime_type", None),
        }

    def _collect_color_info(self, imgs) -> dict:
        color = None
        for img in imgs:
            if "color" in getattr(img, "name", "").lower():
                color = img
                break
        if color is None and len(imgs) >= 1:
            color = imgs[0]
        if color is None:
            return {"found": False, "frame_count": len(imgs)}

        info = {
            "found": True,
            "name": getattr(color, "name", None),
            "data_bytes": len(color.data) if hasattr(color, "data") else None,
            "mime_type": getattr(color, "mime_type", None),
        }
        try:
            from PIL import Image
            import io
            pil_img = Image.open(io.BytesIO(color.data))
            info["width"] = pil_img.size[0]
            info["height"] = pil_img.size[1]
        except ImportError:
            info["resolution_note"] = "PIL not available"
        except Exception as e:
            info["resolution_error"] = str(e)
        return info


register(RealSenseProfile)
