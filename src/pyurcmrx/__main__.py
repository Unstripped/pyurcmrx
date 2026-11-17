"""Command-line tool for conformance-testing pyurcmrx against a controller."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiomqtt

from .control import ControlClient
from .discovery import discover
from .errors import UrcMrxError
from .project import CoreModel, FunctionRef, parse_core_model
from .staging import fetch_core_model
from .telemetry import OtherTelemetry, subscribe


async def _discover(args: argparse.Namespace) -> None:
    controllers = await discover(args.timeout, target=args.target)
    if not controllers:
        print("no controllers answered")
    for c in controllers:
        print(f"{c.ip}\t{c.mac}\tmodel=0x{c.model_id:04X} {c.model_name}\t{c.description}")


async def _status(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        print(f"online: {await client.get_net_status()}")
        time = await client.get_file_time()
        # The timestamp already includes the controller's zone offset: render it as wall-clock time.
        wall_clock = datetime.fromtimestamp(time.local_timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S")
        print(f"controller time: {wall_clock} (local_timestamp {time.local_timestamp})")
        print(f"time-zone rule: {time.time_zone_rule.hex()}")


async def _variables(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        for var_id, value in sorted((await client.get_variables()).items()):
            print(f"{var_id}\t{value}")


async def _macro(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        handle = await client.macro_action(args.room, args.device, args.macro, args.flags)
        print(f"accepted, queue handle {handle}")


async def _volume(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        handle = await client.set_volume(args.device, args.level, mute=args.mute)
        print(f"accepted, queue handle {handle}")


def _print_catalog(model: CoreModel) -> None:
    print(f"system: {model.system_name or '(unnamed)'}")
    for room in model.rooms:
        print(f"room {room.id}: {room.name}")
        for device in room.devices:
            print(f"  device {device.id}: {device.name} ({device.device_type or '?'})")
            for button in device.buttons:
                steps = ", ".join(
                    f"device {s.device_id} function {s.func_id}" if isinstance(s, FunctionRef) else f"unsupported {s.data}"
                    for s in button.steps
                )
                page = f" [{button.page}]" if button.page else ""
                runnable = "" if button.executable else " (not executable)"
                print(f"    {button.label}{page}{runnable}: {steps}")


async def _catalog(args: argparse.Namespace) -> None:
    if args.file is not None:
        _print_catalog(parse_core_model(args.file.read_bytes()))
    elif args.host is not None:
        _print_catalog(await fetch_core_model(args.host))
    else:
        raise SystemExit("catalog needs HOST or --file")


async def _watch(args: argparse.Namespace) -> None:
    async for telemetry in subscribe(args.host):
        event = telemetry.event
        detail = event.body if isinstance(event, OtherTelemetry) else event
        print(f"{telemetry.topic}\t{detail}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pyurcmrx")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="send a discovery probe on UDP 61510")
    p.add_argument(
        "target",
        nargs="?",
        help="controller or broadcast address (default: every local subnet broadcast address)",
    )
    p.add_argument("--timeout", type=float, default=2.0)
    p.set_defaults(run=_discover)

    p = sub.add_parser("status", help="query network status and controller time")
    p.add_argument("host")
    p.set_defaults(run=_status)

    p = sub.add_parser("variables", help="dump the system variable table")
    p.add_argument("host")
    p.set_defaults(run=_variables)

    p = sub.add_parser("macro", help="send MACRO_ACTION")
    p.add_argument("host")
    p.add_argument("room", type=int, help="room ID, or 0 to let the controller resolve a device's room")
    p.add_argument("device", type=int, help="device ID, or 0 for a room/house macro")
    p.add_argument("macro", type=int, help="device function ID, or room macro ID / trigger code")
    p.add_argument("--flags", type=int, default=0, help="0 single, 1 press, 2 release, 3 hold")
    p.set_defaults(run=_macro)

    p = sub.add_parser("volume", help="set absolute volume with 0x07ED")
    p.add_argument("host")
    p.add_argument("device", type=int)
    p.add_argument("level", type=int, help="0..100")
    p.add_argument("--mute", action="store_true")
    p.set_defaults(run=_volume)

    p = sub.add_parser("catalog", help="print rooms, devices and buttons from core_model.json")
    p.add_argument("host", nargs="?", help="fetch from the controller over anonymous FTP")
    p.add_argument("--file", type=Path, help="read a local core_model.json instead")
    p.set_defaults(run=_catalog)

    p = sub.add_parser("watch", help="stream MQTT state telemetry")
    p.add_argument("host")
    p.set_defaults(run=_watch)

    args = parser.parse_args()
    # Library warnings (e.g. a discovery probe that could not be sent) go to stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(args.run(args))
    except KeyboardInterrupt:
        pass
    except (UrcMrxError, OSError, TimeoutError, ValueError, aiomqtt.MqttError) as err:
        print(f"error: {err!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
