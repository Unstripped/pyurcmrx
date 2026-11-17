"""Command-line tool for conformance-testing pyurcmrx against a controller."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiomqtt

from .control import ControlClient, Response, encode_trigger_code
from .discovery import discover
from .errors import UrcMrxError
from .project import Button, ButtonTrigger, CoreModel, MacroStep, parse_core_model
from .staging import DEFAULT_CLIENT_MODEL, fetch_core_model
from .telemetry import OtherTelemetry, subscribe

_ON_OFF = {"on": True, "off": False}


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
        # local_timestamp counts the controller's local wall-clock seconds, not UTC (spec §52
        # §3.9): format it without any zone conversion so the host's offset is not applied.
        wall_clock = datetime.fromtimestamp(time.local_timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S")
        print(f"controller time: {wall_clock} local (local_timestamp {time.local_timestamp})")
        print(f"time-zone rule: {time.time_zone_rule.hex()}")


async def _variables(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        for var_id, value in sorted((await client.get_variables()).items()):
            print(f"{var_id}\t{value}")


async def _macro(args: argparse.Namespace) -> None:
    if args.trigger_code is not None and args.object is None and args.button is None:
        code = args.trigger_code
    elif args.trigger_code is None and args.object is not None and args.button is not None:
        code = encode_trigger_code(args.object, args.button)
    else:
        raise SystemExit("macro needs either TRIGGER_CODE or both --object and --button")
    async with ControlClient(args.host) as client:
        await client.macro_action(code)
        print("acknowledged")


async def _volume(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        await client.set_volume(
            args.trigger_code, args.level, min_value=args.min_value, max_value=args.max_value, mute=args.mute
        )
        print("acknowledged")


def _print_reply(reply: Response | None) -> None:
    # The success reply for room/zone audio is not established (spec §52 §3.11): show it raw.
    if reply is None:
        print("no reply")
    else:
        print(f"reply opcode 0x{reply.opcode:04X} status 0x{reply.status:02X} body {reply.body.hex() or '(empty)'}")


async def _room_volume(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        _print_reply(await client.set_room_volume(args.room, args.value))


async def _room_mute(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        _print_reply(await client.set_room_mute(args.room, _ON_OFF[args.state]))


async def _zone_volume(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        _print_reply(await client.set_zone_volume(args.con_dev, args.zone, args.value))


async def _zone_mute(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        _print_reply(await client.set_zone_mute(args.con_dev, args.zone, _ON_OFF[args.state]))


def _describe_step(step: MacroStep) -> str:
    if isinstance(step, ButtonTrigger):
        return f"trigger {step.trigger_code} (object {step.object_id} button {step.button_id})"
    return f"room {step.room_id} power off"


def _print_buttons(buttons: tuple[Button, ...], indent: str) -> None:
    for button in buttons:
        page = f" [{button.page}]" if button.page else ""
        print(f"{indent}{button.label}{page}: {', '.join(_describe_step(s) for s in button.steps)}")


def _print_catalog(model: CoreModel) -> None:
    print(f"system: {model.system_name or '(unnamed)'}")
    for room in model.rooms:
        print(f"room {room.id}: {room.name}")
        if room.menu:
            print("  menu:")
            _print_buttons(room.menu, "    ")
        for device in room.devices:
            connected = f", connected device {device.con_dev_id}" if device.con_dev_id is not None else ""
            print(f"  device {device.id}: {device.name} ({device.device_type or '?'}){connected}")
            _print_buttons(device.buttons, "    ")


async def _catalog(args: argparse.Namespace) -> None:
    if args.file is not None:
        _print_catalog(parse_core_model(args.file.read_bytes()))
    elif args.host is not None:
        _print_catalog(await fetch_core_model(args.host, client_model=args.client_model))
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

    p = sub.add_parser("macro", help="trigger a project button with MACRO_ACTION (0x07DA)")
    p.add_argument("host")
    p.add_argument("trigger_code", type=int, nargs="?", help="button code from the catalog")
    p.add_argument("--object", type=int, help="UI object id; with --button, instead of TRIGGER_CODE")
    p.add_argument("--button", type=int, help="button id; with --object, instead of TRIGGER_CODE")
    p.set_defaults(run=_macro)

    p = sub.add_parser("volume", help="set an absolute level through a project volume control (0x07ED)")
    p.add_argument("host")
    p.add_argument("trigger_code", type=int, help="trigger code of the project volume control")
    p.add_argument("level", type=int, help="raw device-dependent level")
    p.add_argument("--min", dest="min_value", type=int, required=True, help="raw minimum of the control")
    p.add_argument("--max", dest="max_value", type=int, required=True, help="raw maximum of the control")
    p.add_argument("--mute", action="store_true")
    p.set_defaults(run=_volume)

    p = sub.add_parser("room-volume", help="set a room's audio zone level (0x007D)")
    p.add_argument("host")
    p.add_argument("room", type=int, help="room id")
    p.add_argument("value", type=int, help="raw zone level")
    p.set_defaults(run=_room_volume)

    p = sub.add_parser("room-mute", help="mute or unmute a room's audio zone (0x007E)")
    p.add_argument("host")
    p.add_argument("room", type=int, help="room id")
    p.add_argument("state", choices=_ON_OFF)
    p.set_defaults(run=_room_mute)

    p = sub.add_parser("zone-volume", help="set one zone of a connected audio device (0x0097)")
    p.add_argument("host")
    p.add_argument("con_dev", type=int, help="connected-device id (mcs.dat)")
    p.add_argument("zone", type=int, help="zone index")
    p.add_argument("value", type=int, help="raw zone level")
    p.set_defaults(run=_zone_volume)

    p = sub.add_parser("zone-mute", help="mute or unmute one zone of a connected audio device (0x0098)")
    p.add_argument("host")
    p.add_argument("con_dev", type=int, help="connected-device id (mcs.dat)")
    p.add_argument("zone", type=int, help="zone index")
    p.add_argument("state", choices=_ON_OFF)
    p.set_defaults(run=_zone_mute)

    p = sub.add_parser("catalog", help="print rooms, menus, devices and buttons from core_model.json")
    p.add_argument("host", nargs="?", help="fetch from the controller over anonymous FTP")
    p.add_argument("--file", type=Path, help="read a local core_model.json instead")
    p.add_argument(
        "--client-model",
        default=DEFAULT_CLIENT_MODEL,
        help=f"client model whose manifest names the core model (default {DEFAULT_CLIENT_MODEL})",
    )
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
