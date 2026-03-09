#!/usr/bin/env python3
"""
Machine config management CLI for canary.

Wraps the Viam AppClient + RobotClient to read/modify machine configs,
run discovery, and fetch logs. All output is JSON for agent consumption.

Usage:
    python3 config_helper.py --config canary.json --machine sean-framework <command> [args]

Commands:
    get-config                              Dump current machine config
    clear-resources [--preserve key,..]     Clear components/services/modules (optionally preserve some)
    add-module --namespace X --name Y --version Z   Add a registry module
    add-resource --kind component|service --api X --model Y --name Z [--attributes '{}']
                                            Add a resource (component or service)
    add-resource-from-discovery-result --json '{...}'
                                            Add a component directly from discovery output
    discover --service NAME                 Run discover_resources on a discovery service
    get-logs [--num N] [--levels L,..]      Fetch recent logs
    restart                                 Mark part for restart
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone

logging.getLogger("viam").setLevel(logging.WARNING)
logging.getLogger("grpc").setLevel(logging.WARNING)

from viam.app.viam_client import ViamClient
from viam.robot.client import RobotClient
from viam.rpc.dial import Credentials, DialOptions
from viam.services.discovery import DiscoveryClient
from google.protobuf.json_format import MessageToDict


def load_config(config_path, machine_name=None):
    with open(config_path) as f:
        cfg = json.load(f)
    machines = cfg["machines"]
    if machine_name:
        machines = [m for m in machines if m["name"] == machine_name]
    if not machines:
        print(json.dumps({"error": f"Machine '{machine_name}' not found"}))
        sys.exit(1)
    return cfg, machines[0]


async def get_app_client(machine_cfg):
    dial_opts = DialOptions.with_api_key(
        machine_cfg["api_key"], machine_cfg["api_key_id"]
    )
    client = await ViamClient.create_from_dial_options(dial_opts)
    return client


async def get_robot_client(machine_cfg):
    creds = Credentials(type="api-key", payload=machine_cfg["api_key"])
    opts = RobotClient.Options(
        dial_options=DialOptions(
            credentials=creds, auth_entity=machine_cfg["api_key_id"],
        )
    )
    robot = await RobotClient.at_address(machine_cfg["address"], opts)
    return robot


async def cmd_get_config(machine_cfg, args):
    client = await get_app_client(machine_cfg)
    part = await client.app_client.get_robot_part(machine_cfg["part_id"])
    client.close()
    print(json.dumps({
        "part_id": part.id,
        "name": part.name,
        "last_updated": part.last_updated.isoformat() if part.last_updated else None,
        "config": part.robot_config,
    }, indent=2, default=str))


async def cmd_clear_resources(machine_cfg, args):
    preserve = set(args.preserve.split(",")) if args.preserve else set()

    client = await get_app_client(machine_cfg)
    app = client.app_client
    part = await app.get_robot_part(machine_cfg["part_id"])
    cfg = deepcopy(part.robot_config) or {}

    removed = {"components": [], "services": [], "modules": []}

    for resource_type in ["components", "services", "modules"]:
        original = cfg.get(resource_type, [])
        kept = []
        for item in original:
            name = item.get("name", "")
            if name in preserve:
                kept.append(item)
            else:
                removed[resource_type].append(name)
        cfg[resource_type] = kept

    await app.update_robot_part(
        robot_part_id=machine_cfg["part_id"],
        name=part.name,
        robot_config=cfg,
    )
    client.close()

    print(json.dumps({
        "action": "clear-resources",
        "preserved": list(preserve),
        "removed": removed,
        "remaining_config": cfg,
    }, indent=2, default=str))


async def cmd_add_module(machine_cfg, args):
    client = await get_app_client(machine_cfg)
    app = client.app_client
    part = await app.get_robot_part(machine_cfg["part_id"])
    cfg = deepcopy(part.robot_config) or {}

    module_id = f"{args.namespace}:{args.name}"
    module_entry = {
        "type": "registry",
        "module_id": module_id,
        "name": f"{args.namespace}_{args.name}",
        "version": args.version,
    }

    modules = cfg.get("modules", [])
    modules = [m for m in modules if m.get("module_id") != module_id]
    modules.append(module_entry)
    cfg["modules"] = modules

    await app.update_robot_part(
        robot_part_id=machine_cfg["part_id"],
        name=part.name,
        robot_config=cfg,
    )
    client.close()

    print(json.dumps({
        "action": "add-module",
        "module": module_entry,
    }, indent=2))


async def cmd_add_resource(machine_cfg, args):
    client = await get_app_client(machine_cfg)
    app = client.app_client
    part = await app.get_robot_part(machine_cfg["part_id"])
    cfg = deepcopy(part.robot_config) or {}

    attrs = json.loads(args.attributes) if args.attributes else {}
    entry = {
        "api": args.api,
        "model": args.model,
        "name": args.resource_name,
        "attributes": attrs,
    }

    if args.kind == "component":
        config_key = "components"
    elif args.kind == "service":
        config_key = "services"
    else:
        print(json.dumps({"error": f"Unknown resource kind: {args.kind}"}))
        sys.exit(1)

    resources = cfg.get(config_key, [])
    resources = [r for r in resources if r.get("name") != args.resource_name]
    resources.append(entry)
    cfg[config_key] = resources

    await app.update_robot_part(
        robot_part_id=machine_cfg["part_id"],
        name=part.name,
        robot_config=cfg,
    )
    client.close()

    print(json.dumps({
        "action": "add-resource",
        "kind": args.kind,
        "resource": entry,
    }, indent=2))


async def cmd_add_resource_from_discovery(machine_cfg, args):
    client = await get_app_client(machine_cfg)
    app = client.app_client
    part = await app.get_robot_part(machine_cfg["part_id"])
    cfg = deepcopy(part.robot_config) or {}

    discovery_result = json.loads(args.json)

    # Determine if this is a component or service from the api field
    api = discovery_result.get("api", "")
    if ":component:" in api:
        config_key = "components"
    elif ":service:" in api:
        config_key = "services"
    else:
        # Default to components (discovery typically returns components)
        config_key = "components"

    # Strip fields that are discovery metadata or conflict with `api`.
    # viam-server rejects configs with both `api` and `namespace`/`type`.
    # Keep only: name, api, model, attributes
    # Strip: frame, logConfiguration, namespace, type (redundant with api)
    clean = {}
    for key in ["name", "api", "model", "attributes"]:
        if key in discovery_result:
            clean[key] = discovery_result[key]

    resources = cfg.get(config_key, [])
    resources = [r for r in resources if r.get("name") != clean.get("name")]
    resources.append(clean)
    cfg[config_key] = resources

    await app.update_robot_part(
        robot_part_id=machine_cfg["part_id"],
        name=part.name,
        robot_config=cfg,
    )
    client.close()

    print(json.dumps({
        "action": "add-resource-from-discovery-result",
        "config_key": config_key,
        "resource": clean,
        "stripped_fields": [k for k in discovery_result if k not in clean],
    }, indent=2))


async def cmd_discover(machine_cfg, args):
    t0 = time.monotonic()
    robot = await get_robot_client(machine_cfg)
    try:
        disc = DiscoveryClient.from_robot(robot, args.service)
        results = await disc.discover_resources()
        elapsed_ms = (time.monotonic() - t0) * 1000

        discoveries = []
        for r in results:
            d = MessageToDict(r, preserving_proto_field_name=True)
            discoveries.append(d)

        print(json.dumps({
            "action": "discover",
            "service": args.service,
            "elapsed_ms": round(elapsed_ms, 1),
            "count": len(discoveries),
            "results": discoveries,
        }, indent=2))
    finally:
        await robot.close()


async def cmd_get_logs(machine_cfg, args):
    client = await get_app_client(machine_cfg)
    app = client.app_client

    levels = args.levels.split(",") if args.levels else []
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=args.lookback)

    entries = await app.get_robot_part_logs(
        robot_part_id=machine_cfg["part_id"],
        num_log_entries=args.num,
        log_levels=levels,
        start=start_time,
        end=end_time,
    )
    client.close()

    def _utc_iso(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    serialized = []
    for e in entries:
        serialized.append({
            "time": _utc_iso(e.time),
            "level": e.level,
            "logger": e.logger_name,
            "message": e.message,
            "caller": dict(e.caller) if e.caller else None,
            "stack": e.stack if e.stack else None,
        })

    print(json.dumps({
        "action": "get-logs",
        "count": len(serialized),
        "lookback_minutes": args.lookback,
        "entries": serialized,
    }, indent=2))


async def cmd_get_version(machine_cfg, args):
    """Get viam-server version + module versions from config."""
    robot = await get_robot_client(machine_cfg)
    version_resp = await robot.get_version()
    await robot.close()

    # Also grab module versions from config
    client = await get_app_client(machine_cfg)
    part = await client.app_client.get_robot_part(machine_cfg["part_id"])
    client.close()

    cfg = part.robot_config or {}
    modules = []
    for m in cfg.get("modules", []):
        modules.append({
            "name": m.get("name"),
            "module_id": m.get("module_id"),
            "version": m.get("version"),
        })

    print(json.dumps({
        "action": "get-version",
        "viam_server": {
            "version": version_resp.version,
            "platform": version_resp.platform,
            "api_version": version_resp.api_version,
        },
        "modules": modules,
    }, indent=2, default=str))


async def cmd_restart(machine_cfg, args):
    client = await get_app_client(machine_cfg)
    app = client.app_client
    await app.mark_part_for_restart(robot_part_id=machine_cfg["part_id"])
    client.close()
    print(json.dumps({"action": "restart", "part_id": machine_cfg["part_id"]}))


async def main():
    parser = argparse.ArgumentParser(description="Canary machine config helper")
    parser.add_argument("--config", required=True, help="Path to canary.json")
    parser.add_argument("--machine", required=True, help="Machine name")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("get-config")

    p_clear = sub.add_parser("clear-resources")
    p_clear.add_argument("--preserve", help="Comma-separated resource names to keep")

    p_mod = sub.add_parser("add-module")
    p_mod.add_argument("--namespace", required=True)
    p_mod.add_argument("--name", required=True)
    p_mod.add_argument("--version", required=True)

    p_res = sub.add_parser("add-resource")
    p_res.add_argument("--kind", required=True, choices=["component", "service"])
    p_res.add_argument("--api", required=True)
    p_res.add_argument("--model", required=True)
    p_res.add_argument("--resource-name", required=True)
    p_res.add_argument("--attributes", default=None)

    p_disc_add = sub.add_parser("add-resource-from-discovery-result")
    p_disc_add.add_argument("--json", required=True)

    p_disc = sub.add_parser("discover")
    p_disc.add_argument("--service", required=True)

    p_logs = sub.add_parser("get-logs")
    p_logs.add_argument("--num", type=int, default=50)
    p_logs.add_argument("--levels", default=None)
    p_logs.add_argument("--lookback", type=int, default=10, help="Minutes to look back")

    sub.add_parser("get-version")
    sub.add_parser("restart")

    args = parser.parse_args()
    _, machine_cfg = load_config(args.config, args.machine)

    commands = {
        "get-config": cmd_get_config,
        "clear-resources": cmd_clear_resources,
        "add-module": cmd_add_module,
        "add-resource": cmd_add_resource,
        "add-resource-from-discovery-result": cmd_add_resource_from_discovery,
        "discover": cmd_discover,
        "get-logs": cmd_get_logs,
        "get-version": cmd_get_version,
        "restart": cmd_restart,
    }

    await commands[args.command](machine_cfg, args)


if __name__ == "__main__":
    asyncio.run(main())
