"""
RealSense camera test profile — raw data collection.

Collects depth frame metadata, resolution, point cloud data, camera properties,
geometries, do_command error handling, discovery validation, concurrent stress
tests, and frame timestamp sanity checks.

Runs get_images and get_point_cloud sampling simultaneously during FPS tests.

All validations assert EXACT expected values — no loose/approximate checks.
"""

import asyncio
import hashlib
import io
import time
from typing import Optional
from datetime import datetime, timezone

from google.protobuf.json_format import MessageToDict
from PIL import Image
from viam.components.camera import Camera
from viam.services.discovery import DiscoveryClient

from profiles.base import BaseProfile
from profiles import register

# ---------------------------------------------------------------------------
# Expected constants for the D435/D435i at default config (1280×720, both sensors)
# These come directly from the C++ source and the hardware spec.
# ---------------------------------------------------------------------------

# get_images
EXPECTED_FRAME_COUNT = 2
EXPECTED_COLOR_SOURCE_NAME = "color"
EXPECTED_DEPTH_SOURCE_NAME = "depth"
EXPECTED_COLOR_MIME_TYPE = "image/jpeg"
EXPECTED_DEPTH_MIME_TYPE = "image/vnd.viam.dep"
# depth frame at 1280×720 Z16 = 1280 * 720 * 2 + header (viam dep format)
# Observed: 1843224 bytes consistently during smoke testing
EXPECTED_DEPTH_DATA_BYTES = 1843224

# get_point_cloud
EXPECTED_PCD_MIME_TYPE = "pointcloud/pcd"
EXPECTED_PCD_VERSION = ".7"
EXPECTED_PCD_FIELDS = "x y z rgb"
EXPECTED_PCD_SIZE = "4 4 4 4"
EXPECTED_PCD_TYPE = "F F F U"
EXPECTED_PCD_COUNT = "1 1 1 1"
EXPECTED_PCD_DATA_FORMAT = "binary"
EXPECTED_PCD_POINTS = 921600  # 1280 * 720
EXPECTED_PCD_BYTES_PER_POINT = 16  # 4+4+4+4
EXPECTED_PCD_BODY_BYTES = EXPECTED_PCD_POINTS * EXPECTED_PCD_BYTES_PER_POINT  # 14745600

# get_properties (resolution is config-dependent but default is 1280×720)
EXPECTED_INTRINSIC_WIDTH = 1280
EXPECTED_INTRINSIC_HEIGHT = 720

# get_geometries (hardcoded in C++ for D435/D435i)
EXPECTED_GEOMETRY_COUNT = 1
EXPECTED_GEOMETRY_LABEL = "box"
EXPECTED_GEOMETRY_TYPE = "box"
EXPECTED_GEOMETRY_CENTER_X = -17.5
EXPECTED_GEOMETRY_CENTER_Y = 0.0
EXPECTED_GEOMETRY_CENTER_Z = -12.5
EXPECTED_BOX_X_MM = 90.0
EXPECTED_BOX_Y_MM = 25.0
EXPECTED_BOX_Z_MM = 25.0

# do_command exact error strings (from realsense.hpp C++ source)
EXPECTED_UNKNOWN_CMD_ERROR = "Unknown command. Supported commands: update_firmware"
EXPECTED_TOO_MANY_PARAMS_ERROR = "Firmware update command must contain exactly one parameter"
EXPECTED_WRONG_TYPE_ERROR = "Firmware update URL must be a string"

# discovery
EXPECTED_DISCOVERY_MODEL = "viam:camera:realsense"
EXPECTED_DISCOVERY_API = "rdk:component:camera"
EXPECTED_DISCOVERY_SENSORS = ["color", "depth"]


class RealSenseProfile(BaseProfile):
    """Intel RealSense (D435i, D455, etc.) raw data collection."""

    name = "realsense"
    ACCEPTED_MODELS = {"viam:camera:realsense"}

    def _check_model(self, robot) -> Optional[str]:
        """Validate camera model matches this profile. Returns error string or None."""
        model = self.config.get("model")
        if model and model not in self.ACCEPTED_MODELS:
            return (
                f"Model mismatch: camera '{self.cam_name}' has model '{model}' "
                f"but {self.name} profile only accepts {self.ACCEPTED_MODELS}"
            )
        return None

    async def run(self, robot) -> dict:
        """Override base to run all collection phases."""
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

        model_mismatch = self._check_model(robot)
        if model_mismatch:
            result["errors"].append(model_mismatch)
            return result

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

        # --- New test surfaces ---
        result["profile_data"]["get_properties"] = await self._collect_get_properties(cam)
        result["profile_data"]["get_geometries"] = await self._collect_get_geometries(cam)
        result["profile_data"]["do_command_errors"] = await self._test_do_command_error_handling(cam)
        result["profile_data"]["discovery"] = await self._test_discovery(robot)
        result["profile_data"]["concurrent_stress"] = await self._test_concurrent_stress(cam)
        result["profile_data"]["timestamp_sanity"] = await self._test_timestamp_sanity(cam)

        return result

    # ------------------------------------------------------------------
    # Existing methods
    # ------------------------------------------------------------------

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

        # Validate unfiltered call returns exactly the expected sources
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

                # Validate mime type for the specific source
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

                # Exact mime type check on every sample
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

            # Exact validations against known PCD format
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
        pil_img = Image.open(io.BytesIO(color.data))
        info["width"] = pil_img.size[0]
        info["height"] = pil_img.size[1]
        info["width_exact"] = pil_img.size[0] == EXPECTED_INTRINSIC_WIDTH
        info["height_exact"] = pil_img.size[1] == EXPECTED_INTRINSIC_HEIGHT
        return info

    # ------------------------------------------------------------------
    # 1. get_properties validation
    # ------------------------------------------------------------------

    async def _collect_get_properties(self, cam) -> dict:
        """Call get_properties and validate with exact expected values."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": None,
            "validations": {},
            "cross_check": {},
            "error": None,
        }

        t0 = time.monotonic()
        try:
            props = await cam.get_properties()
            elapsed_ms = (time.monotonic() - t0) * 1000
        except Exception as e:
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = str(e)
            return result

        result["latency_ms"] = round(elapsed_ms, 1)

        intrinsics = getattr(props, "intrinsic_parameters", None)
        distortion = getattr(props, "distortion_parameters", None)
        supports_pcd = getattr(props, "supports_pcd", None)

        raw = {"supports_pcd": supports_pcd}

        if intrinsics is not None:
            raw["intrinsics"] = {
                "width_px": getattr(intrinsics, "width_px", None),
                "height_px": getattr(intrinsics, "height_px", None),
                "focal_x_px": getattr(intrinsics, "focal_x_px", None),
                "focal_y_px": getattr(intrinsics, "focal_y_px", None),
                "center_x_px": getattr(intrinsics, "center_x_px", None),
                "center_y_px": getattr(intrinsics, "center_y_px", None),
            }
        else:
            raw["intrinsics"] = None

        if distortion is not None:
            raw["distortion"] = {
                "model": getattr(distortion, "model", None),
                "parameters": list(getattr(distortion, "parameters", None) or []),
            }
        else:
            raw["distortion"] = None

        result["raw"] = raw

        # --- Exact validations ---
        v = {}

        v["supports_pcd_is_true"] = supports_pcd is True

        if raw["intrinsics"] is not None:
            intr = raw["intrinsics"]
            w = intr["width_px"]
            h = intr["height_px"]
            fx = intr["focal_x_px"]
            fy = intr["focal_y_px"]
            cx = intr["center_x_px"]
            cy = intr["center_y_px"]

            # Resolution must be exactly 1280×720
            v["width_exact"] = w == EXPECTED_INTRINSIC_WIDTH
            v["height_exact"] = h == EXPECTED_INTRINSIC_HEIGHT

            # Focal lengths must be positive floats (exact values are
            # per-device calibration, so we check type + sign only)
            v["focal_x_is_float"] = isinstance(fx, float)
            v["focal_y_is_float"] = isinstance(fy, float)
            v["focal_x_positive"] = isinstance(fx, (int, float)) and fx > 0
            v["focal_y_positive"] = isinstance(fy, (int, float)) and fy > 0

            # Principal point must be positive floats (per-device calibration)
            v["center_x_is_float"] = isinstance(cx, float)
            v["center_y_is_float"] = isinstance(cy, float)
            v["center_x_positive"] = isinstance(cx, (int, float)) and cx > 0
            v["center_y_positive"] = isinstance(cy, (int, float)) and cy > 0

            # Distortion model should be empty string (disabled in C++ source,
            # see RSDK-12408 comment)
            if raw["distortion"] is not None:
                v["distortion_model_empty"] = raw["distortion"]["model"] == ""
        else:
            v["intrinsics_present"] = False

        result["validations"] = v

        # --- Cross-check with get_images color frame ---
        try:
            resp = await cam.get_images()
            imgs = resp[0] if isinstance(resp, tuple) else resp
            color_img = None
            for img in imgs:
                if getattr(img, "name", None) == EXPECTED_COLOR_SOURCE_NAME:
                    color_img = img
                    break

            if color_img is not None and raw["intrinsics"] is not None:
                pil_img = Image.open(io.BytesIO(color_img.data))
                frame_w, frame_h = pil_img.size

                intr_w = raw["intrinsics"]["width_px"]
                intr_h = raw["intrinsics"]["height_px"]

                result["cross_check"] = {
                    "get_images_color_width": frame_w,
                    "get_images_color_height": frame_h,
                    "get_properties_width": intr_w,
                    "get_properties_height": intr_h,
                    "width_match": frame_w == intr_w,
                    "height_match": frame_h == intr_h,
                    "both_match": frame_w == intr_w and frame_h == intr_h,
                }
            else:
                result["cross_check"] = {"note": "No color image or intrinsics to cross-check"}
        except Exception as e:
            result["cross_check"] = {"error": f"get_images for cross-check failed: {e}"}

        return result

    # ------------------------------------------------------------------
    # 2. get_geometries validation
    # ------------------------------------------------------------------

    async def _collect_get_geometries(self, cam) -> dict:
        """Call get_geometries and validate against exact D435/D435i constants."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": [],
            "validations": {},
            "error": None,
        }

        t0 = time.monotonic()
        try:
            geometries = await cam.get_geometries()
            elapsed_ms = (time.monotonic() - t0) * 1000
        except Exception as e:
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            result["error"] = str(e)
            return result

        result["latency_ms"] = round(elapsed_ms, 1)

        raw_geos = []
        for geo in geometries:
            geo_dict = {}
            geo_dict["label"] = getattr(geo, "label", None) or None

            center = getattr(geo, "center", None)
            if center is not None:
                geo_dict["center"] = {
                    "x": getattr(center, "x", 0.0),
                    "y": getattr(center, "y", 0.0),
                    "z": getattr(center, "z", 0.0),
                }
            else:
                geo_dict["center"] = None

            geo_type = None
            box_dims = None

            try:
                if geo.HasField("geometry_type"):
                    which = geo.WhichOneof("geometry_type")
                    geo_type = which

                    if which == "box":
                        dims_mm = geo.box.dims_mm
                        box_dims = {
                            "x_mm": dims_mm.x,
                            "y_mm": dims_mm.y,
                            "z_mm": dims_mm.z,
                        }
            except Exception as e:
                geo_dict["geometry_type_error"] = str(e)

            geo_dict["geometry_type"] = geo_type
            if box_dims is not None:
                geo_dict["box_dims_mm"] = box_dims

            raw_geos.append(geo_dict)

        result["raw"] = raw_geos

        # --- Exact validations ---
        v = {}

        v["geometry_count"] = len(geometries)
        v["geometry_count_exact"] = len(geometries) == EXPECTED_GEOMETRY_COUNT

        if len(geometries) >= 1:
            g = raw_geos[0]

            v["label_exact"] = g.get("label") == EXPECTED_GEOMETRY_LABEL
            v["geometry_type_exact"] = g.get("geometry_type") == EXPECTED_GEOMETRY_TYPE

            c = g.get("center")
            if c is not None:
                v["center_x_exact"] = c.get("x") == EXPECTED_GEOMETRY_CENTER_X
                v["center_y_exact"] = c.get("y") == EXPECTED_GEOMETRY_CENTER_Y
                v["center_z_exact"] = c.get("z") == EXPECTED_GEOMETRY_CENTER_Z
            else:
                v["center_present"] = False

            box = g.get("box_dims_mm")
            if box is not None:
                v["box_x_exact"] = box.get("x_mm") == EXPECTED_BOX_X_MM
                v["box_y_exact"] = box.get("y_mm") == EXPECTED_BOX_Y_MM
                v["box_z_exact"] = box.get("z_mm") == EXPECTED_BOX_Z_MM
            else:
                v["box_dimensions_present"] = False

        result["validations"] = v
        return result

    # ------------------------------------------------------------------
    # 3. do_command error handling (safe — no firmware update)
    # ------------------------------------------------------------------

    async def _test_do_command_error_handling(self, cam) -> dict:
        """Test do_command error paths with exact expected error strings.

        NEVER sends a valid update_firmware command.
        """
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tests": {},
        }

        # --- Test A: Unknown command ---
        test_a = {"command": {"bogus_command": "test_value"}}
        t0 = time.monotonic()
        try:
            resp = await cam.do_command({"bogus_command": "test_value"})
            elapsed_ms = (time.monotonic() - t0) * 1000
            test_a["latency_ms"] = round(elapsed_ms, 1)
            test_a["response"] = _proto_struct_to_dict(resp)
            test_a["raised_exception"] = False

            resp_dict = test_a["response"]
            test_a["error_field_exact"] = resp_dict.get("error") == EXPECTED_UNKNOWN_CMD_ERROR
            # Should NOT have a "success" key (only the error)
            test_a["no_success_key"] = "success" not in resp_dict
        except Exception as e:
            test_a["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            test_a["raised_exception"] = True
            test_a["exception"] = str(e)

        result["tests"]["unknown_command"] = test_a

        # --- Test B: Too many parameters ---
        test_b = {"command": {"param_one": "val1", "param_two": "val2"}}
        t0 = time.monotonic()
        try:
            resp = await cam.do_command({"param_one": "val1", "param_two": "val2"})
            elapsed_ms = (time.monotonic() - t0) * 1000
            test_b["latency_ms"] = round(elapsed_ms, 1)
            test_b["response"] = _proto_struct_to_dict(resp)
            test_b["raised_exception"] = False

            resp_dict = test_b["response"]
            test_b["error_field_exact"] = resp_dict.get("error") == EXPECTED_TOO_MANY_PARAMS_ERROR
            test_b["success_is_false"] = resp_dict.get("success") is False
        except Exception as e:
            test_b["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            test_b["raised_exception"] = True
            test_b["exception"] = str(e)

        result["tests"]["too_many_params"] = test_b

        # --- Test C: Empty command ---
        # Empty dict has size 0, passes the size > 1 check, falls through
        # to get_do_command → no match → UNKNOWN → same error as unknown command
        test_c = {"command": {}}
        t0 = time.monotonic()
        try:
            resp = await cam.do_command({})
            elapsed_ms = (time.monotonic() - t0) * 1000
            test_c["latency_ms"] = round(elapsed_ms, 1)
            test_c["response"] = _proto_struct_to_dict(resp)
            test_c["raised_exception"] = False

            resp_dict = test_c["response"]
            test_c["error_field_exact"] = resp_dict.get("error") == EXPECTED_UNKNOWN_CMD_ERROR
            test_c["no_success_key"] = "success" not in resp_dict
        except Exception as e:
            test_c["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            test_c["raised_exception"] = True
            test_c["exception"] = str(e)

        result["tests"]["empty_command"] = test_c

        # --- Test D: Known key with wrong type ---
        # update_firmware expects a string. Sending int 12345 triggers type
        # validation BEFORE any firmware operation.
        test_d = {"command": {"update_firmware": 12345}}
        t0 = time.monotonic()
        try:
            resp = await cam.do_command({"update_firmware": 12345})
            elapsed_ms = (time.monotonic() - t0) * 1000
            test_d["latency_ms"] = round(elapsed_ms, 1)
            test_d["response"] = _proto_struct_to_dict(resp)
            test_d["raised_exception"] = False

            resp_dict = test_d["response"]
            test_d["error_field_exact"] = resp_dict.get("error") == EXPECTED_WRONG_TYPE_ERROR
            test_d["success_is_false"] = resp_dict.get("success") is False
        except Exception as e:
            test_d["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            test_d["raised_exception"] = True
            test_d["exception"] = str(e)

        result["tests"]["wrong_type_for_known_key"] = test_d

        return result

    # ------------------------------------------------------------------
    # 4. Discovery service validation
    # ------------------------------------------------------------------

    async def _test_discovery(self, robot) -> dict:
        """Test discovery service with exact expected values."""
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "discovery_services_found": [],
            "results": [],
            "validations": {},
            "error": None,
        }

        resource_names = robot.resource_names
        discovery_names = []
        for rn in resource_names:
            if getattr(rn, "subtype", None) == "discovery":
                name = getattr(rn, "name", None)
                if name:
                    discovery_names.append(name)

        result["discovery_services_found"] = discovery_names

        if not discovery_names:
            result["error"] = "No discovery services found on robot"
            return result

        discovered = []
        errors_per_service = {}
        total_latency_ms = 0

        for disc_name in discovery_names:
            t0 = time.monotonic()
            last_err = None
            resources = None
            # Retry once — gRPC channel can be transiently unhappy after
            # heavy PCD/image calls earlier in the profile run.
            for attempt in range(2):
                try:
                    disc = DiscoveryClient.from_robot(robot, disc_name)
                    resources = await disc.discover_resources()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            elapsed_ms = (time.monotonic() - t0) * 1000
            total_latency_ms += elapsed_ms
            if last_err is not None:
                errors_per_service[disc_name] = str(last_err)
                continue

            for rc in resources:
                d = {}
                d["discovery_service"] = disc_name
                d["name"] = getattr(rc, "name", None)
                d["api"] = getattr(rc, "api", "")
                d["model"] = getattr(rc, "model", "")

                attrs_proto = getattr(rc, "attributes", None)
                if attrs_proto is not None:
                    d["attributes"] = MessageToDict(attrs_proto)
                else:
                    d["attributes"] = None

                discovered.append(d)

        result["latency_ms"] = round(total_latency_ms, 1)
        if errors_per_service:
            result["per_service_errors"] = errors_per_service

        result["results"] = discovered
        result["discovered_count"] = len(discovered)

        # --- Exact validations ---
        v = {}

        # Filter to only realsense discoveries (other modules may also have
        # discovery services on the same robot)
        realsense_devices = [
            d for d in discovered
            if d.get("model") == EXPECTED_DISCOVERY_MODEL
        ]
        v["realsense_device_count"] = len(realsense_devices)
        v["has_realsense_device"] = len(realsense_devices) >= 1

        per_device = []
        for d in realsense_devices:
            dv = {"name": d.get("name")}
            attrs = d.get("attributes") or {}

            # Model must be exactly "viam:camera:realsense"
            dv["model_exact"] = d.get("model") == EXPECTED_DISCOVERY_MODEL

            # API must be exactly "rdk:component:camera"
            dv["api_exact"] = d.get("api") == EXPECTED_DISCOVERY_API

            # serial_number must be present and non-empty string
            serial = attrs.get("serial_number")
            dv["serial_number"] = serial
            dv["has_serial_number"] = isinstance(serial, str) and len(serial) > 0

            # Sensors must be exactly ["color", "depth"]
            sensors = attrs.get("sensors")
            if isinstance(sensors, (list, tuple)):
                sensor_list = list(sensors)
            else:
                sensor_list = None
            dv["sensors"] = sensor_list
            dv["sensors_exact"] = sensor_list == EXPECTED_DISCOVERY_SENSORS

            per_device.append(dv)

        v["per_device"] = per_device

        # Check if our camera's serial appears in discovery
        our_serial = self.config.get("serial_number") or self.profile_config.get("serial_number")
        if our_serial:
            found_serials = [dv["serial_number"] for dv in per_device if dv.get("serial_number")]
            v["our_serial"] = our_serial
            v["our_serial_in_discovery"] = our_serial in found_serials

        result["validations"] = v
        return result

    # ------------------------------------------------------------------
    # 5. Concurrent stress test
    # ------------------------------------------------------------------

    async def _test_concurrent_stress(self, cam, rounds=5) -> dict:
        """Fire get_images + get_point_cloud + get_properties simultaneously.

        Every call must succeed. Every round must return exact expected values.
        """
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rounds": rounds,
            "per_round": [],
            "summary": {},
        }

        total_successes = {"get_images": 0, "get_point_cloud": 0, "get_properties": 0}
        total_failures = {"get_images": 0, "get_point_cloud": 0, "get_properties": 0}

        for r in range(rounds):
            round_result = {"round": r}
            t0 = time.monotonic()

            async def _timed_get_images():
                t = time.monotonic()
                try:
                    resp = await cam.get_images()
                    imgs = resp[0] if isinstance(resp, tuple) else resp
                    return {
                        "success": True,
                        "latency_ms": round((time.monotonic() - t) * 1000, 1),
                        "frame_count": len(imgs),
                        "frame_count_exact": len(imgs) == EXPECTED_FRAME_COUNT,
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "latency_ms": round((time.monotonic() - t) * 1000, 1),
                        "error": str(e),
                    }

            async def _timed_get_point_cloud():
                t = time.monotonic()
                try:
                    pcd_bytes, mime_type = await cam.get_point_cloud()
                    return {
                        "success": True,
                        "latency_ms": round((time.monotonic() - t) * 1000, 1),
                        "data_bytes": len(pcd_bytes),
                        "mime_type": mime_type,
                        "mime_type_exact": mime_type == EXPECTED_PCD_MIME_TYPE,
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "latency_ms": round((time.monotonic() - t) * 1000, 1),
                        "error": str(e),
                    }

            async def _timed_get_properties():
                t = time.monotonic()
                try:
                    props = await cam.get_properties()
                    intrinsics = getattr(props, "intrinsic_parameters", None)
                    w = getattr(intrinsics, "width_px", None) if intrinsics else None
                    h = getattr(intrinsics, "height_px", None) if intrinsics else None
                    return {
                        "success": True,
                        "latency_ms": round((time.monotonic() - t) * 1000, 1),
                        "supports_pcd": getattr(props, "supports_pcd", None),
                        "supports_pcd_is_true": getattr(props, "supports_pcd", None) is True,
                        "width": w,
                        "height": h,
                        "width_exact": w == EXPECTED_INTRINSIC_WIDTH,
                        "height_exact": h == EXPECTED_INTRINSIC_HEIGHT,
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "latency_ms": round((time.monotonic() - t) * 1000, 1),
                        "error": str(e),
                    }

            imgs_r, pcd_r, props_r = await asyncio.gather(
                _timed_get_images(),
                _timed_get_point_cloud(),
                _timed_get_properties(),
            )

            round_elapsed = (time.monotonic() - t0) * 1000
            round_result["total_round_ms"] = round(round_elapsed, 1)
            round_result["get_images"] = imgs_r
            round_result["get_point_cloud"] = pcd_r
            round_result["get_properties"] = props_r

            for method, r_val in [("get_images", imgs_r), ("get_point_cloud", pcd_r), ("get_properties", props_r)]:
                if r_val.get("success"):
                    total_successes[method] += 1
                else:
                    total_failures[method] += 1

            result["per_round"].append(round_result)

        # Summary with exact checks
        result["summary"] = {
            "total_rounds": rounds,
            "successes": total_successes,
            "failures": total_failures,
            "zero_failures": all(v == 0 for v in total_failures.values()),
            "all_get_images_succeeded": total_successes["get_images"] == rounds,
            "all_get_point_cloud_succeeded": total_successes["get_point_cloud"] == rounds,
            "all_get_properties_succeeded": total_successes["get_properties"] == rounds,
            "avg_round_ms": round(
                sum(r["total_round_ms"] for r in result["per_round"]) / rounds, 1
            ) if rounds > 0 else None,
        }

        return result

    # ------------------------------------------------------------------
    # 6. Frame timestamp sanity
    # ------------------------------------------------------------------

    async def _test_timestamp_sanity(self, cam) -> dict:
        """Validate get_images response metadata timestamps.

        Every sample must have a parseable timestamp. Timestamps must be
        monotonically non-decreasing, recent, and not identical.
        """
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "samples": [],
            "validations": {},
            "error": None,
        }

        now_before = datetime.now(timezone.utc)
        samples = []

        for i in range(5):
            sample = {"index": i}
            t0 = time.monotonic()
            try:
                resp = await cam.get_images()
                elapsed_ms = (time.monotonic() - t0) * 1000

                if isinstance(resp, tuple) and len(resp) >= 2:
                    metadata = resp[1]
                    captured_at = getattr(metadata, "captured_at", None)

                    sample["latency_ms"] = round(elapsed_ms, 1)

                    if captured_at is not None:
                        if hasattr(captured_at, "isoformat"):
                            ts_dt = captured_at
                        elif hasattr(captured_at, "seconds"):
                            ts_dt = datetime.fromtimestamp(
                                captured_at.seconds + captured_at.nanos / 1e9,
                                tz=timezone.utc,
                            )
                        else:
                            ts_dt = captured_at

                        sample["captured_at_raw"] = str(captured_at)

                        if hasattr(ts_dt, "year"):
                            if ts_dt.tzinfo is None:
                                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                            sample["captured_at_iso"] = ts_dt.isoformat()
                            sample["captured_at_epoch_s"] = ts_dt.timestamp()
                        else:
                            sample["captured_at_iso"] = None
                            sample["captured_at_type"] = type(captured_at).__name__
                    else:
                        sample["captured_at_raw"] = None
                else:
                    sample["response_not_tuple"] = True

            except Exception as e:
                sample["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                sample["error"] = str(e)

            samples.append(sample)

        now_after = datetime.now(timezone.utc)
        result["samples"] = samples

        # --- Exact validations ---
        v = {}

        epoch_samples = [s for s in samples if "captured_at_epoch_s" in s]
        v["samples_with_timestamp"] = len(epoch_samples)
        v["total_samples"] = len(samples)
        # Every sample must have a parseable timestamp
        v["all_samples_have_timestamp"] = len(epoch_samples) == len(samples)

        if epoch_samples:
            epochs = [s["captured_at_epoch_s"] for s in epoch_samples]

            # None at epoch zero: must be after 2020-01-01 (1577836800)
            v["none_at_epoch_zero"] = all(e > 1577836800 for e in epochs)

            # None in the future: must be before now + 5s (tight — no 30s slop)
            future_limit = now_after.timestamp() + 5
            v["none_in_future"] = all(e <= future_limit for e in epochs)

            # All recent: must be within 10 seconds of the test window
            # (not 60s — frames should be fresh, not a minute old)
            staleness_limit = now_before.timestamp() - 10
            v["all_within_10s"] = all(e > staleness_limit for e in epochs)

            oldest_epoch = min(epochs)
            v["max_age_seconds"] = round(now_after.timestamp() - oldest_epoch, 2)

            # Strictly non-decreasing (no tolerance — timestamps must not go backwards)
            if len(epochs) >= 2:
                v["timestamps_non_decreasing"] = all(
                    epochs[i] >= epochs[i - 1]
                    for i in range(1, len(epochs))
                )

                deltas = [
                    round(epochs[i] - epochs[i - 1], 4)
                    for i in range(1, len(epochs))
                ]
                v["inter_sample_deltas_s"] = deltas

            # Not all identical (would mean stale/cached data)
            v["not_all_identical"] = len(set(round(e, 3) for e in epochs)) > 1

        result["validations"] = v
        return result


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _extract_proto_value(value):
    """Extract a native Python value from a protobuf Value message."""
    kind = value.WhichOneof("kind")
    if kind == "string_value":
        return value.string_value
    elif kind == "number_value":
        return value.number_value
    elif kind == "bool_value":
        return value.bool_value
    elif kind == "null_value":
        return None
    elif kind == "struct_value":
        return {k: _extract_proto_value(v) for k, v in value.struct_value.fields.items()}
    elif kind == "list_value":
        return [_extract_proto_value(v) for v in value.list_value.values]
    return str(value)


def _proto_struct_to_dict(proto_struct) -> dict:
    """Best-effort conversion of a ProtoStruct / dict-like to a plain dict."""
    if isinstance(proto_struct, dict):
        return {k: _proto_value_to_python(v) for k, v in proto_struct.items()}
    try:
        return {k: _proto_value_to_python(v) for k, v in dict(proto_struct).items()}
    except Exception:
        return {"_raw": str(proto_struct)}


def _proto_value_to_python(val):
    """Recursively convert ProtoValue / ProtoStruct / ProtoList to native Python."""
    if val is None:
        return None

    if hasattr(val, "get"):
        for type_name in (str, float, int, bool):
            extracted = val.get(type_name)
            if extracted is not None:
                return extracted

    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, dict):
        return {k: _proto_value_to_python(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_proto_value_to_python(v) for v in val]
    try:
        d = dict(val)
        return {k: _proto_value_to_python(v) for k, v in d.items()}
    except Exception:
        pass
    try:
        return list(val)
    except Exception:
        pass
    return str(val)


register(RealSenseProfile)
