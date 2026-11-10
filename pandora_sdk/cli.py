"""The `pandora` command: look at hands, and calibrate a glove, without
writing code.

    pandora devices                      the gloves the app knows
    pandora watch --hand right           hands as they arrive, ten a second
    pandora watch --raw --rate 0         every raw packet
    pandora calibrate --hand right       calibrate a glove with no app running
    pandora watch --local                hands straight from the receiver

`devices` and `watch` read through the running app. `calibrate` and
`watch --local` open the receiver themselves, so the app must not be running.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Set

from pandora_core.calibration_session import Run
from pandora_core.protocol import DEFAULT_PORT, HANDS, Frame, ProtocolError, Raw

from pandora_sdk.calibrate import calibrate
from pandora_sdk.client import connect
from pandora_sdk.local import (Local, default_profiles_dir, load_profiles, open_link,
                               open_receiver)
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
    named: Set[str] = set()
    while not args.count or printed < args.count:
        try:
            message = gloves.read(stream, timeout=1.0)
        except TimeoutError:
            # A glove with no profile is left out, which looks like silence.
            if isinstance(gloves, Local):
                for device in sorted(gloves.unprofiled - named):
                    named.add(device)
                    print(f"pandora: {device} is sending but has no profile, so it is left "
                          f"out. Make one with: pandora calibrate --device {device} "
                          "--hand left|right", file=sys.stderr)
            continue
        print(_raw_line(message) if args.raw else _frame_line(message), flush=True)
        printed += 1
    return 0


def _local(args: argparse.Namespace) -> Local:
    folder = args.profiles or default_profiles_dir()
    profiles = load_profiles(folder)
    if not profiles:
        raise PandoraError(f"no profile in {folder}. Make one with: pandora calibrate "
                           "--hand left|right")
    return open_receiver(profiles, port=args.receiver)


def _calibrate(args: argparse.Namespace) -> int:
    link = open_link(args.receiver)
    try:
        outcome = calibrate(
            link, hand=args.hand, device=args.device, out=args.out,
            run=Run[args.run.upper()], hold_s=args.hold, ready_s=args.ready,
            haptics=not args.no_haptics, name=args.name,
            say=lambda text: print(text, flush=True))
    except KeyboardInterrupt:
        print("\nStopped. No profile was written.", flush=True)
        return 1
    finally:
        link.close()
    if outcome.path is not None:
        where = "" if args.out is None else f" --profiles {args.out}"
        print(f"Look at the hand with: pandora watch --local{where}", flush=True)
    return 0 if outcome.ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    app = argparse.ArgumentParser(add_help=False)
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=DEFAULT_PORT)
    app.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for the app")
    port = argparse.ArgumentParser(add_help=False)
    port.add_argument("--receiver", metavar="PORT",
                      help="the receiver's serial port (COM7, /dev/ttyACM0) when more than "
                           "one is plugged in")

    parser = argparse.ArgumentParser(
        prog="pandora",
        description="Look at the 01 Glove's hands, through the app or straight from the "
                    "receiver, and calibrate a glove with no app.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", parents=[app], help="list the gloves the app knows")

    watch = commands.add_parser("watch", parents=[app, port],
                                help="print hands as they arrive")
    watch.add_argument("--raw", action="store_true",
                       help="the gloves' raw readings instead of finished hands")
    watch.add_argument("--hand", action="append", choices=HANDS, help="only this hand")
    watch.add_argument("--device", action="append", help="only this glove id (repeatable)")
    watch.add_argument("--rate", type=float, default=10.0,
                       help="lines a second per glove at most; 0 is every message (default 10)")
    watch.add_argument("--count", type=int, default=0, help="stop after this many lines")
    watch.add_argument("--local", action="store_true",
                       help="read the receiver itself, with no app, using the profiles "
                            "`pandora calibrate` wrote")
    watch.add_argument("--profiles", type=Path, metavar="DIR",
                       help=f"with --local: the profiles folder (default "
                            f"{default_profiles_dir()})")

    cal = commands.add_parser(
        "calibrate", parents=[port],
        help="calibrate one glove read from the receiver and write its profile",
        description="Calibrate one glove read straight from the receiver, with no app "
                    "running, and write the profile open_receiver() and `pandora watch "
                    "--local` need. Wear the glove and follow the steps as they are printed.")
    cal.add_argument("--hand", choices=HANDS,
                     help="the hand the glove is worn on; needed the first time, then taken "
                          "from the glove's profile")
    cal.add_argument("--device", help="the glove's id, when more than one is sending")
    cal.add_argument("--run", choices=[run.name.lower() for run in Run], default="full",
                     help="what to calibrate (default full: curl range, and the splay zero "
                          "on a glove with finger IMUs)")
    cal.add_argument("--hold", type=float, default=3.0,
                     help="seconds each still pose is held (default 3)")
    cal.add_argument("--ready", type=float, default=3.0,
                     help="seconds before the first step (default 3)")
    cal.add_argument("--out", type=Path, metavar="DIR",
                     help=f"the profiles folder to write into (default {default_profiles_dir()})")
    cal.add_argument("--name", help="a label for the glove, kept in its profile")
    cal.add_argument("--no-haptics", action="store_true",
                     help="no buzz on the back of the hand when a step starts")
    args = parser.parse_args(argv)

    try:
        if args.command == "calibrate":
            return _calibrate(args)
        if getattr(args, "local", False):
            with _local(args) as gloves:
                return _watch(gloves, args)
        with connect(args.host, args.port, name="pandora-cli", timeout=args.timeout) as gloves:
            return _devices(gloves) if args.command == "devices" else _watch(gloves, args)
    except KeyboardInterrupt:
        return 0
    except (PandoraError, ProtocolError, TimeoutError) as failed:
        print(f"pandora: {failed}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
