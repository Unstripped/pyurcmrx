"""Command-line tool for conformance-testing pyurcmrx against a controller."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .control import ControlClient
from .discovery import discover
from .errors import UrcMrxError
from .project import build_catalog, parse_mcs, parse_mcs_info


async def _discover(args: argparse.Namespace) -> None:
    controllers = await discover(args.timeout)
    if not controllers:
        print("no controllers answered")
    for c in controllers:
        print(f"{c.ip}\t{c.mac}\tmodel=0x{c.model_id:04X} {c.model_name}\t{c.description}")


async def _status(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        print(f"online: {await client.get_net_status()}")
        print(f"project file time: {await client.get_file_time()}")


async def _variables(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        for var_id, value in sorted((await client.get_variables()).items()):
            print(f"{var_id}\t{value}")


async def _macro(args: argparse.Namespace) -> None:
    async with ControlClient(args.host) as client:
        handle = await client.macro_action(args.room, args.device, args.macro, args.flags)
        print(f"accepted, queue handle {handle}")


async def _catalog(args: argparse.Namespace) -> None:
    catalog = build_catalog(parse_mcs(args.mcs.read_bytes()), parse_mcs_info(args.mcs_info.read_bytes()))
    for entry in catalog:
        d, info = entry.device, entry.info
        kind = "multi-zone" if d.multi_zone else "single-zone"
        detail = f"{info.model} {info.transport or ''} {info.category or ''}".strip() if info else "not in mcs_info.dat"
        print(f"device {d.id}: {d.name} ({kind}; {detail})")
        for zone in d.zones:
            print(f"  zone {zone.idx}: " + ", ".join(f"{f.id}={f.name}" for f in zone.funcs))


def main() -> None:
    parser = argparse.ArgumentParser(prog="pyurcmrx")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="broadcast a discovery probe on UDP 61510")
    p.add_argument("--timeout", type=float, default=2.0)
    p.set_defaults(run=_discover)

    p = sub.add_parser("status", help="query network status and project file time")
    p.add_argument("host")
    p.set_defaults(run=_status)

    p = sub.add_parser("variables", help="dump the system variable table")
    p.add_argument("host")
    p.set_defaults(run=_variables)

    p = sub.add_parser("macro", help="send MACRO_ACTION")
    p.add_argument("host")
    p.add_argument("room", type=int)
    p.add_argument("device", type=int)
    p.add_argument("macro", type=int)
    p.add_argument("--flags", type=int, default=0)
    p.set_defaults(run=_macro)

    p = sub.add_parser("catalog", help="print the device catalog from local mcs.dat + mcs_info.dat")
    p.add_argument("mcs", type=Path)
    p.add_argument("mcs_info", type=Path)
    p.set_defaults(run=_catalog)

    args = parser.parse_args()
    try:
        asyncio.run(args.run(args))
    except (UrcMrxError, OSError, TimeoutError) as err:
        print(f"error: {err!r}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
