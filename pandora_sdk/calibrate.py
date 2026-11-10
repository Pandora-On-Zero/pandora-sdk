"""`pandora calibrate`: calibrate one glove read straight from the receiver,
with no app, and write the profile open_receiver() reads.

    link = open_link()
    outcome = calibrate(link, hand="right")
    print(outcome.path)

The run itself is pandora-core's (CalibrationSession, driven over the glove's
packets by calibrate_packets): which steps there are, what each captures and
how it went. This adds what a person at a terminal needs around it: which
glove, each step and its countdown as text, a buzz on the back of the hand
when a step starts, and the file at the end.

The clock is the glove's own (its packets' timestamps), so a countdown stops
while the glove is out of range and nothing is captured from old data.

A profile is good for one Hall range, and open_receiver() sends a glove
nothing, so the range it will meet is the one a glove powers up in. The run
therefore starts by resetting every Hall sensor to its power-up settings, and
the profile records that range. The glove does not report back, so this is
what it was told, not what it confirmed.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from pandora_core import commands
from pandora_core.calibration_session import CalibrationSession, Run, Step, calibrate_packets
from pandora_core.glove import Glove, Profile
from pandora_core.packet_decoder import GlovePacket
from pandora_core.packet_layout import (
    HALL_DEFAULT_FILTER, HALL_DEFAULT_ODR, HALL_DEFAULT_OSR, HALL_DEFAULT_RANGE, HALL_TARGET_ALL,
    HAPTIC_BACKHAND_MOTOR, HAPTIC_MOTOR_COUNT,
)
from pandora_core.serial_framing import encode_frame
from pandora_core.tuning import Tuning

from pandora_sdk.local import (default_profiles_dir, link_failure, link_packets, load_profiles,
                               save_profile)
from pandora_sdk.source import Disconnected, PandoraError

# The cue: the back-of-hand motor at full drive (pandora_core commands.haptic_rtp
# takes 1 to 127), for this long when a step starts and when the run is over.
CUE_AMPLITUDE = 127
CUE_MS = 150
DONE_MS = 500

Packets = Iterator[Optional[GlovePacket]]


@dataclass
class Outcome:
    """How a run went."""
    device: str
    hand: str
    # The run's heading and one (good, text) line per result, as the session
    # gives them.
    title: str
    lines: List[Tuple[bool, str]]
    # The profile written; None when the run stored nothing.
    path: Optional[Path]

    @property
    def ok(self) -> bool:
        """A profile was written and no part of the run failed."""
        return self.path is not None and all(good for good, _text in self.lines)


def calibrate(link: Any, *, hand: Optional[str] = None, device: Optional[str] = None,
              out: Union[str, Path, None] = None, run: Run = Run.FULL, hold_s: float = 3.0,
              ready_s: float = 3.0, haptics: bool = True, name: Optional[str] = None,
              listen_s: float = 1.0, wait_s: float = 10.0,
              say: Callable[[str], None] = print,
              tuning: Union[Tuning, Mapping[str, Any], None] = None,
              defaults: Optional[Mapping[str, Any]] = None) -> Outcome:
    """Calibrate one glove sending on *link* (an open receiver port,
    local.open_link) and write its profile into *out*, a folder
    (default_profiles_dir() when left out).

    The glove is *device*, or the only one heard in the first *listen_s*
    seconds. *hand* is "left" or "right"; it can be left out for a glove that
    already has a profile in the folder, which the run then starts from, so
    what it does not measure is kept. *run* and *hold_s* are the session's:
    what to calibrate and how long each still pose is held. The first step
    starts *ready_s* seconds after the glove is picked. *name* is a label for
    the glove, kept in the profile. With *haptics* the back of the hand
    buzzes when a step starts and when the run is over.

    Every line for the person goes to *say*. PandoraError when no run could
    be made: no glove heard for *wait_s* seconds, two heard and none named, a
    hand that is not known, a glove that stopped part way. Nothing is written
    then.
    """
    folder = default_profiles_dir() if out is None else Path(out)
    feed = _packets(link)
    glove_id = _pick(feed, device, listen_s, wait_s)
    profile = _starting_profile(folder, glove_id, hand, name, say)
    say(f"Glove {glove_id}, {profile.hand} hand.")
    _send(link, commands.hall_config(
        glove_id, reset=True, range_val=HALL_DEFAULT_RANGE, osr=HALL_DEFAULT_OSR,
        odr=HALL_DEFAULT_ODR, filter_val=HALL_DEFAULT_FILTER, target=HALL_TARGET_ALL))
    say(f"First step in {ready_s:g} s."
        + (" The back of the hand buzzes when a step starts." if haptics else ""))

    def cue(ms: int = CUE_MS) -> None:
        if haptics:
            _buzz(link, glove_id, ms)

    mine = _from(feed, glove_id, wait_s)
    with Glove(profile, tuning, defaults) as glove:
        _settle(glove, mine, ready_s)
        session = calibrate_packets(glove, mine, run, hold_s, watch=_Screen(say, cue))
        path = (save_profile(folder, glove_id, glove.export_profile())
                if session.changed else None)
    if session.steps:
        cue(DONE_MS)
    say("")
    say(session.title)
    for good, text in session.lines:
        say(f"  {'ok  ' if good else 'FAIL'}  {text}")
    say(f"Profile written: {path}" if path is not None else "No profile was written.")
    return Outcome(glove_id, profile.hand, session.title, list(session.lines), path)


# ── which glove, and what it starts from ─────────────────────────────────────

def _pick(feed: Packets, device: Optional[str], listen_s: float, wait_s: float) -> str:
    """The glove to calibrate: *device* once it is heard; with none named,
    the only one heard by the time the first has sent for *listen_s* seconds
    of its own clock."""
    heard: Dict[str, int] = {}
    started = time.monotonic()
    for packet in feed:
        if packet is not None:
            first_ms = heard.setdefault(packet.device_id, packet.timestamp)
            if packet.device_id == device:
                return packet.device_id
            if device is None and packet.device_id == next(iter(heard)) \
                    and packet.timestamp - first_ms >= listen_s * 1000:
                break
        if time.monotonic() - started > wait_s:
            break
    sending = ", ".join(sorted(heard))
    if device is not None:
        raise PandoraError(f"{device} was not heard in {wait_s:g} s. "
                           + (f"Sending now: {sending}." if heard else "No glove is sending."))
    if not heard:
        raise PandoraError(f"no glove is sending (waited {wait_s:g} s). Switch the glove on "
                           "and keep it near the receiver.")
    if len(heard) > 1:
        raise PandoraError(f"more than one glove is sending ({sending}); "
                           "say which with --device")
    return next(iter(heard))


def _starting_profile(folder: Path, device: str, hand: Optional[str], name: Optional[str],
                      say: Callable[[str], None]) -> Profile:
    """What the run starts from: the glove's profile in *folder* when it has
    one for this hand, else an empty one. On the power-up Hall range either
    way, so a calibration kept from another range is rescaled to it."""
    try:
        earlier = load_profiles(folder).get(device)
    except (ValueError, TypeError, OSError) as unreadable:
        raise PandoraError(f"a profile in {folder} cannot be read ({unreadable}). "
                           "Move it away, or name another folder with --out.") from None
    if earlier is not None and hand not in (None, earlier.hand):
        say(f"{device} was calibrated as the {earlier.hand} hand; starting again as the "
            f"{hand} hand.")
        earlier = None
    if earlier is None:
        if hand is None:
            raise PandoraError(f"{device} has no profile in {folder} yet: say which hand it "
                               "is worn on with --hand left or --hand right")
        earlier = Profile(hand)
    return replace(earlier, range=HALL_DEFAULT_RANGE, sensor_ranges={},
                   name=earlier.name if name is None else name)


# ── the glove's packets ──────────────────────────────────────────────────────

def _packets(link: Any) -> Packets:
    """link_packets, with a port that fails reported as the SDK reports it."""
    try:
        yield from link_packets(link)
    except OSError as failed:
        raise Disconnected(link_failure(failed)) from None


def _from(feed: Packets, device: str, wait_s: float) -> Iterator[GlovePacket]:
    """*device*'s packets out of *feed*, until it has sent none for *wait_s*
    seconds."""
    heard = time.monotonic()
    for packet in feed:
        if packet is not None and packet.device_id == device:
            heard = time.monotonic()
            yield packet
        elif time.monotonic() - heard > wait_s:
            raise PandoraError(f"{device} stopped sending: nothing from it for {wait_s:g} s. "
                               "No profile was written.")


def _settle(glove: Glove, packets: Iterator[GlovePacket], ready_s: float) -> None:
    """Run *glove*'s pipeline for *ready_s* seconds of its own clock: time
    for the wearer to get ready, for the sensors to settle on their range and
    for the pipeline's filters to fill."""
    if ready_s <= 0:
        return
    started_ms: Optional[int] = None
    for packet in packets:
        glove.push_decoded(packet)
        started_ms = packet.timestamp if started_ms is None else started_ms
        if packet.timestamp - started_ms >= ready_s * 1000:
            return


# ── what the person sees and feels ───────────────────────────────────────────

class _Screen:
    """calibrate_packets' watch: says each step when it starts, with a cue,
    then a line for each second left, with the session's warning on it when
    it has one. It is called for every packet; it speaks once a second."""

    def __init__(self, say: Callable[[str], None], cue: Callable[[], None]) -> None:
        self._say, self._cue = say, cue
        self._step: Optional[Step] = None
        self._left = 0

    def __call__(self, session: CalibrationSession) -> None:
        step = session.step
        if step is None:
            return
        if step is not self._step:
            self._step, self._left = step, 0
            self._say("")
            self._say(f"Step {session.index + 1} of {len(session.steps)}: {step.title}")
            self._say(f"  {step.detail}")
            self._cue()
        left = math.ceil(session.remaining_s - 1e-6)
        if left != self._left and left > 0:
            self._left = left
            warned = [text for good, text in session.prompt if not good]
            self._say(f"  {left}" + (f"    ! {'; '.join(warned)}" if warned else ""))


def _buzz(link: Any, device: str, ms: int) -> None:
    amplitudes, durations = [0] * HAPTIC_MOTOR_COUNT, [0] * HAPTIC_MOTOR_COUNT
    amplitudes[HAPTIC_BACKHAND_MOTOR], durations[HAPTIC_BACKHAND_MOTOR] = CUE_AMPLITUDE, ms
    _send(link, commands.haptic_rtp(device, amplitudes, durations))


def _send(link: Any, payload: bytes) -> None:
    """Write one command for a glove to the receiver."""
    try:
        link.write(encode_frame(payload))
    except OSError as failed:  # pyserial's errors, its write timeout among them, are OSErrors
        raise PandoraError(f"could not send to the receiver ({failed}). If it stays like "
                           "this, unplug the receiver and plug it back in.") from None

