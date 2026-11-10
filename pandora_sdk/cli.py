"""The `pandora` command: look at what the app is sending without writing code.

    pandora devices                  the gloves the app knows
    pandora watch --hand right       hands as they arrive, ten a second
    pandora watch --raw --rate 0     every raw packet
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from pandora_core.protocol import DEFAULT_PORT, HANDS, Frame, ProtocolError, Raw

from pandora_sdk.client import connect
from pandora_sdk.source import PandoraError, Source


def _frame_line(hand: Frame) -> str:
    curl = " ".join(f"{finger[0]:.2f}" for finger in hand.curl)
    splay = " ".join(f"{value:+.2f}" for value in hand.splay)
    wrist = "-" if hand.wrist_deg is None else " ".join(f"{a:.0f}" for a in hand.wrist_deg)
    return (f"{hand.hand:<5}  {hand.device}  #{hand.seq}  curl {curl}  splay {splay}  "
            f"wrist {wrist}")


def _raw_line(reading: Raw) -> str:
    hall = "  ".join(",".join(f"{axis:.0f}" for axis in finger[0]) for finger in reading.hall)
    return (f"{reading.hand:<5}  {reading.device}  #{reading.seq}  hall {hall}  "
            f"battery {reading.battery_pct}%")


def _devices(gloves: Source) -> int:
    for device in gloves.devices():
        state = "connected" if device.connected else "silent"
        print(f"{device.id}  {device.hand:<5}  {state}  {device.name}".rstrip())
    return 0


def _watch(gloves: Source, args: argparse.Namespace) -> int:
    stream = "raw" if args.raw else "frames"
    gloves.subscribe(stream, devices=args.device, hands=args.hand or HANDS, rate_hz=args.rate,
                     bones=False)
    printed = 0
    while not args.count or printed < args.count:
        message = gloves.read(stream)
        print(_raw_line(message) if args.raw else _frame_line(message), flush=True)
        printed += 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default="127.0.0.1")
    common.add_argument("--port", type=int, default=DEFAULT_PORT)
    common.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for the app")

    parser = argparse.ArgumentParser(
        prog="pandora", description="Talk to the 01 Glove app over its wire protocol.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", parents=[common], help="list the gloves the app knows")
    watch = commands.add_parser("watch", parents=[common], help="print hands as they arrive")
    watch.add_argument("--raw", action="store_true",
                       help="the gloves' raw readings instead of finished hands")
    watch.add_argument("--hand", action="append", choices=HANDS, help="only this hand")
    watch.add_argument("--device", action="append", help="only this glove id (repeatable)")
    watch.add_argument("--rate", type=float, default=10.0,
                       help="lines a second per glove at most; 0 is every message (default 10)")
    watch.add_argument("--count", type=int, default=0, help="stop after this many lines")
    args = parser.parse_args(argv)

    try:
        with connect(args.host, args.port, name="pandora-cli", timeout=args.timeout) as gloves:
            return _devices(gloves) if args.command == "devices" else _watch(gloves, args)
    except KeyboardInterrupt:
        return 0
    except (PandoraError, ProtocolError, TimeoutError) as failed:
        print(f"pandora: {failed}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
