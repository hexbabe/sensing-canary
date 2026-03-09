#!/usr/bin/env python3
"""
SDK raw data collector for the canary skill.

Collects camera frames, FPS samples, telegraf readings, and machine logs
into a single structured JSON dump. NO pass/fail judgments — just raw data.

Modes:
  Full (default): get_images, FPS samples, point cloud, source filters, telegraf, logs
  Probe (--probe): lightweight get_images (1 call per camera) + telegraf + logs only

Usage:
    python3 profiles/sdk_test.py --config canary.json -o runs/2026-03-05/samples/0130_full.json
    python3 profiles/sdk_test.py --config canary.json --probe -o runs/2026-03-05/samples/0200_probe.json
    python3 profiles/sdk_test.py --config canary.json --machine sean-framework -o sdk.json
    python3 profiles/sdk_test.py --config canary.json --no-logs -o sdk.json
"""

import argparse
import asyncio
import importlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Suppress viam SDK logging so stdout is clean JSON
logging.getLogger("viam").setLevel(logging.WARNING)
logging.getLogger("grpc").setLevel(logging.WARNING)

# Top-level imports required before RobotClient.at_address
from viam.robot.client import RobotClient
from viam.rpc.dial import Credentials, DialOptions
from viam.components.camera import Camera
from viam.components.sensor import Sensor
from viam.app.viam_client import ViamClient

# Auto-discover profiles from this package's directory
# sdk_test.py lives inside the profiles/ package alongside base.py, realsense/, etc.
sys.path.insert(0, str(Path(__file__).parent.parent))  # so "profiles" is importable
from profiles import PROFILES
profiles_dir = Path(__file__).parent
for f in profiles_dir.glob("*.py"):
    if f.stem not in ("__init__", "sdk_test", "config_helper"):
        importlib.import_module(f"profiles.{f.stem}")
for d in profiles_dir.iterdir():
    if d.is_dir() and (d / "__init__.py").exists() and d.name != "__pycache__":
        importlib.import_module(f"profiles.{d.name}")


def get_profile(camera_config):
    profile_name = camera_config.get("profile", "base")
    profile_cls = PROFILES.get(profile_name)
    if profile_cls is None:
        print(f"Warning: unknown profile '{profile_name}', falling back to 'base'", file=sys.stderr)
        profile_cls = PROFILES["base"]
    return profile_cls(camera_config)


def _ensure_utc_iso(dt) -> str | None:
    """Convert a datetime to a UTC ISO-8601 string with timezone suffix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Viam API returns naive datetimes that are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def log_entry_to_dict(entry) -> dict:
    return {
        "time": _ensure_utc_iso(entry.time),
        "level": entry.level,
        "logger": entry.logger_name,
        "message": entry.message,
        "caller": dict(entry.caller) if entry.caller else None,
        "stack": entry.stack if entry.stack else None,
    }


async def collect_logs(machine_config, logs_config) -> dict:
    """Fetch raw machine logs via AppClient."""
    result = {
        "part_id": machine_config.get("part_id"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "num_entries": logs_config.get("num_entries", 100),
            "lookback_minutes": logs_config.get("lookback_minutes", 30),
            "levels": logs_config.get("levels", []),
        },
        "entries": [],
        "fetch_error": None,
    }

    part_id = machine_config.get("part_id")
    if not part_id:
        result["fetch_error"] = "No part_id configured"
        return result

    try:
        dial_opts = DialOptions.with_api_key(
            machine_config["api_key"], machine_config["api_key_id"]
        )
        viam_client = await ViamClient.create_from_dial_options(dial_opts)
        cloud = viam_client.app_client

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=result["config"]["lookback_minutes"])

        entries = await cloud.get_robot_part_logs(
            robot_part_id=part_id,
            num_log_entries=result["config"]["num_entries"],
            log_levels=result["config"]["levels"],
            start=start_time,
            end=end_time,
        )
        viam_client.close()

        result["entries"] = [log_entry_to_dict(e) for e in entries]

    except Exception as e:
        result["fetch_error"] = str(e)

    return result


async def collect_telegraf(robot, telegraf_config) -> dict:
    """Read telegraf sensor — raw readings dump."""
    result = {
        "sensor": telegraf_config["name"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "readings": None,
        "error": None,
    }

    try:
        sensor = Sensor.from_robot(robot, telegraf_config["name"])
    except Exception as e:
        result["error"] = f"Sensor not found: {e}"
        return result

    try:
        readings = await sensor.get_readings()
        result["readings"] = json.loads(json.dumps(readings, default=str))
    except Exception as e:
        result["error"] = f"get_readings failed: {e}"

    return result


async def collect_probe(robot, cam_config) -> dict:
    """Lightweight probe: single get_images call per camera, latency only."""
    cam_name = cam_config["name"]
    result = {
        "camera": cam_name,
        "profile": cam_config.get("profile", "base"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "get_images": None,
        "errors": [],
    }

    try:
        cam = Camera.from_robot(robot, cam_name)
    except Exception as e:
        result["errors"].append(f"Camera not found: {e}")
        return result

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
        result["get_images"] = {
            "latency_ms": round(elapsed_ms, 1),
            "frame_count": len(frames),
            "frames": frames,
        }
    except Exception as e:
        elapsed_ms = (time.monotonic() - t0) * 1000
        result["get_images"] = {
            "latency_ms": round(elapsed_ms, 1),
            "error": str(e),
        }

    return result


async def collect_machine(machine_config, logs_config, probe=False) -> dict:
    """Connect to a machine and collect raw data.

    If probe=True, runs lightweight collection: single get_images per camera
    + telegraf + logs. No FPS samples, no point cloud, no source filters.
    """
    dump = {
        "machine": machine_config["name"],
        "address": machine_config["address"],
        "part_id": machine_config.get("part_id"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "probe" if probe else "full",
        "connected": False,
        "cameras": [],
        "telegraf": None,
        "logs": None,
        "connection_error": None,
    }

    try:
        creds = Credentials(type="api-key", payload=machine_config["api_key"])
        opts = RobotClient.Options(
            dial_options=DialOptions(
                credentials=creds, auth_entity=machine_config["api_key_id"],
            )
        )
        robot = await RobotClient.at_address(machine_config["address"], opts)
        dump["connected"] = True
    except Exception as e:
        dump["connection_error"] = str(e)
        return dump

    try:
        # Camera data
        for cam_config in machine_config.get("cameras", []):
            if probe:
                cam_data = await collect_probe(robot, cam_config)
            else:
                profile = get_profile(cam_config)
                cam_data = await profile.run(robot)
            dump["cameras"].append(cam_data)

        # Telegraf data
        telegraf_config = machine_config.get("telegraf_sensor")
        if telegraf_config:
            dump["telegraf"] = await collect_telegraf(robot, telegraf_config)
    finally:
        await robot.close()

    # Logs (separate AppClient connection)
    if logs_config.get("enabled", True):
        dump["logs"] = await collect_logs(machine_config, logs_config)

    return dump


async def main():
    parser = argparse.ArgumentParser(description="Canary raw data collector")
    parser.add_argument("--config", required=True, help="Path to canary.json")
    parser.add_argument("--machine", help="Collect from this machine only")
    parser.add_argument("--output", "-o", help="Write JSON to file (default: stdout)")
    parser.add_argument("--no-logs", action="store_true", help="Skip log collection")
    parser.add_argument("--probe", action="store_true",
                        help="Lightweight mode: single get_images per camera + telegraf + logs only")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    machines = config["machines"]
    if args.machine:
        machines = [m for m in machines if m["name"] == args.machine]
        if not machines:
            print(json.dumps({"error": f"Machine '{args.machine}' not found"}))
            sys.exit(1)

    logs_config = config.get("logs", {})
    if args.no_logs:
        logs_config["enabled"] = False

    dump = {
        "schema": "canary.dump.v1",
        "mode": "probe" if args.probe else "full",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "machines": [],
    }

    for machine in machines:
        machine_dump = await collect_machine(machine, logs_config, probe=args.probe)
        dump["machines"].append(machine_dump)

    output_str = json.dumps(dump, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            f.write(output_str)
        print(f"Dump written to {args.output} ({len(dump['machines'])} machines)")
    else:
        print(output_str)


if __name__ == "__main__":
    asyncio.run(main())
