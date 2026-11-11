"""Hands straight from a receiver, with no app running.

    import pandora_sdk

    with pandora_sdk.open_receiver("profiles/") as gloves:
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

The same reading as connect() gives (subscribe, frames, raw, debug, read,
latest, on, devices), the same Frame, Raw and Debug, and the same commands for a
glove (play_rom, play_rtp, stop, set_hall_range) and calibration bounds, so a
program can change between the two by changing one line. Here the pipeline
(pandora-core) runs in this process: one thread reads the receiver's port and
pushes each glove's packets through that glove's own pipeline, and commands
are written to the same port.

A glove's pipeline runs only while a frames or debug subscription includes
it, or a raw one with cancelled (the counts after earth-field cancellation
exist only once the pipeline has had the packet); with plain raw alone, its
packets make the raw message and nothing else, and no pipeline is built at
all until calibration() asks for one, which loads the glove's profile with
its newest packet. The pipeline's default files (pandora-core's own, or
defaults=) are needed only then, so raw-only reading works without them. A
pipeline that missed packets starts again as a new one at the next packet it
is wanted for, so its smoothing begins there, as on a glove's first packet.

A glove's frames need a profile: its hand and the calibration it carries.
`pandora calibrate` (pandora_sdk/calibrate.py) writes one, into
default_profiles_dir() unless told otherwise, which is also where profiles
are read from when none are named. A glove with none is read raw only: its
raw messages are passed on as the default hand's (default_hand, "right"
unless told otherwise; the packet does not say), it takes commands, and it
never gets a pipeline, so no frames, debug values or cancelled counts; its id
is kept in `unprofiled`. With default_hand=None its packets are left out
altogether. Nothing is sent to a glove unless asked (set_hall_range, the
haptics), so a profile's calibration must have been taken at the range the
glove powers up in, which is the range `pandora calibrate` puts it on, or the
program puts the glove on the profile's range itself.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import (Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple,
                    Union)

from pandora_core import commands, protocol, receiver
from pandora_core.calibration_storage import active_bounds
from pandora_core.debug_tap import get_debug_tap
from pandora_core.glove import Glove, Profile
from pandora_core.packet_decoder import GlovePacket, PacketDecoder
from pandora_core.packet_layout import (HALL_DEFAULT_FILTER, HALL_DEFAULT_ODR, HALL_DEFAULT_OSR,
                                        HALL_DEFAULT_RANGE, HALL_FILTER_MAX, HALL_ODR_HZ,
                                        HALL_OSR_HIGH, HALL_RANGE_5MT, HALL_RANGE_50MT,
                                        HALL_SENSOR_KEYS, HALL_TARGET_ALL, hall_target_id)
from pandora_core.protocol import (Calibration, Device, HallRange, Haptics, Pacer, ProtocolError,
                                   Subscription)
from pandora_core.receiver import ReceiverPort
from pandora_core.scope import shipped_defaults
from pandora_core.serial_framing import encode_frame
from pandora_core.tuning import Tuning

from pandora_sdk.source import CallbackFailed, ConnectionFailed, PandoraError, ServerError, Source

# A glove with no packet for this long is listed as not connected, as the
# protocol's device list says, and takes no commands.
SILENT_AFTER_S = 2.0

# The streams that need a hand made: a glove's pipeline runs only while one of
# them, or raw with cancelled (_runs_pipeline), is subscribed for it.
HAND_STREAMS = ("frames", "debug")

Profiles = Union[str, Path, Mapping[str, Union[Profile, Mapping[str, Any]]]]

_log = logging.getLogger("pandora_sdk")


def default_profiles_dir() -> Path:
    """Where `pandora calibrate` writes profiles and open_receiver() reads
    them when no folder is named: `.pandora/profiles` in the home folder."""
    return Path.home() / ".pandora" / "profiles"


def load_profiles(folder: Union[str, Path]) -> Dict[str, Profile]:
    """The profiles in *folder*: one `<device id>.json` per glove, each the
    JSON form of a pandora_core Profile (hand, sections, range, ...). None
    when there is no such folder."""
    return {path.stem: Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(Path(folder).glob("*.json"))}


def save_profile(folder: Union[str, Path], device_id: str, profile: Profile) -> Path:
    """Write *profile* as *folder*'s `<device id>.json`, whole or not at all;
    the file's path."""
    path = Path(folder) / f"{device_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    unfinished = path.with_name(path.name + ".tmp")
    unfinished.write_text(json.dumps(profile.to_dict(), indent=1) + "\n", encoding="utf-8")
    unfinished.replace(path)
    return path


def list_receivers() -> List[ReceiverPort]:
    """The receivers plugged into this computer, by port name in number
    order (COM9 before COM10): each one's port (.device), USB serial number
    and product name."""
    return sorted(receiver.find_receivers(), key=lambda found: _port_order(found.device))


def _port_order(name: str) -> List[Union[int, str]]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def receiver_port(port: Optional[str] = None) -> str:
    """*port*, or the serial port of the one receiver plugged in."""
    if port is not None:
        return port
    found = list_receivers()
    if not found:
        raise ConnectionFailed("no receiver is plugged into this computer", "no_receiver")
    if len(found) > 1:
        raise ConnectionFailed(
            f"more than one receiver ({', '.join(f.device for f in found)}); "
            "say which one by its port", "several_receivers")
    return found[0].device


def open_link(port: Optional[str] = None, *, timeout: float = 0.2,
              write_timeout: float = 2.0) -> Any:
    """The receiver's serial port (a pyserial Serial), opened as the link
    needs: 4 Mbaud, RTS/CTS flow control, held by this program alone, and on
    Linux the CH343's flow control switched on. *port*, or the one receiver
    plugged in (list_receivers() for choosing among several). *timeout* is
    how long a read waits for its first byte, in seconds; *write_timeout*
    how long a write may wait on the receiver (a hung one stops taking
    bytes), after which it raises. ConnectionFailed when it cannot be
    opened; its code says why."""
    port = receiver_port(port)
    try:
        link = receiver.open_port(port, timeout=timeout, write_timeout=write_timeout)
    except OSError as failed:
        if receiver.is_port_locked(failed):
            raise ConnectionFailed(
                f"{port} is in use. If the 01 Glove app is running it holds the receiver: "
                "read through it with pandora_sdk.connect(), or close it.", "in_use") from failed
        raise ConnectionFailed(f"could not open {port}: {failed}", "open_failed") from failed
    receiver.prepare_link(port, link, _log.info)
    return link


def send_command(link: Any, payload: bytes) -> None:
    """Write one command for a glove (pandora_core.commands' bytes) to the
    receiver on *link*, framed. ServerError "send_failed" when the port will
    not take it."""
    try:
        link.write(encode_frame(payload))
    except OSError as failed:  # pyserial's errors, its write timeout among them, are OSErrors
        raise ServerError("send_failed",
                          f"could not send to the receiver ({failed}). If it stays like "
                          "this, unplug the receiver and plug it back in.") from None


def link_packets(link: Any,
                 decoder: Optional[PacketDecoder] = None) -> Iterator[Optional[GlovePacket]]:
    """Each tracking packet as *link* delivers it, from any glove, and None
    for a read that held none, so the reader can stop or lose patience.
    Never ends; a failing port raises its OSError (link_failure words it)."""
    decoder = decoder or PacketDecoder()
    splitter = receiver.LineSplitter()
    while True:
        found = False
        for line in splitter.feed(receiver.read_available(link)):
            packet = receiver.packet_from_line(line, decoder)
            if packet is not None:
                found = True
                yield packet
        if not found:
            yield None


def link_failure(failed: OSError) -> str:
    """Why reading the receiver ended, for the person at the keyboard."""
    return ("the receiver was unplugged" if receiver.is_disconnect_error(failed)
            else f"reading the receiver failed ({failed})")


def link_failure_code(failed: OSError) -> str:
    """Why reading the receiver ended, as Disconnected's code."""
    return "unplugged" if receiver.is_disconnect_error(failed) else "read_failed"


def open_receiver(profiles: Optional[Profiles] = None, *, port: Optional[str] = None,
                  tuning: Union[Tuning, Mapping[str, Any], None] = None,
                  defaults: Optional[Mapping[str, Any]] = None, buffer: int = 256,
                  default_hand: Optional[str] = "right") -> "Local":
    """Open the receiver on this computer and run the gloves' pipelines here.

    *profiles* is a folder of `<device id>.json` files, or a dict of device id
    to profile; left out, it is default_profiles_dir(), where `pandora
    calibrate` writes. *port* names the receiver's serial port when there is
    more than one. *tuning* is the pipeline's settings (pandora_core Tuning),
    *defaults* its shipped default files as parsed JSON by file name; left
    out, pandora-core's own are used, read when the first pipeline is built.
    *default_hand* is the hand a glove with no profile is read as, raw only;
    None leaves such a glove out.
    """
    return Local(profiles, port=port, tuning=tuning, defaults=defaults, buffer=buffer,
                 default_hand=default_hand)


class Local(Source):
    """A receiver read in this process. Use open_receiver(); *link* is an
    already open port (or anything with its read, in_waiting, write and
    close), *clock* the seconds frames are stamped and paced with, and
    *wall_clock* the seconds since the Unix epoch their wall_time_us is."""

    def __init__(self, profiles: Optional[Profiles] = None, *, port: Optional[str] = None,
                 tuning: Union[Tuning, Mapping[str, Any], None] = None,
                 defaults: Optional[Mapping[str, Any]] = None, buffer: int = 256,
                 link: Any = None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 default_hand: Optional[str] = "right") -> None:
        super().__init__(buffer)
        if default_hand is not None and default_hand not in protocol.HANDS:
            raise ValueError(f"default_hand is {default_hand!r}; it is 'left', 'right' or None")
        self._default_hand = default_hand
        if profiles is None or isinstance(profiles, (str, Path)):
            self._profiles = load_profiles(
                default_profiles_dir() if profiles is None else profiles)
        else:
            self._profiles = {device: profile if isinstance(profile, Profile)
                              else Profile.from_dict(profile)
                              for device, profile in profiles.items()}
        self._tuning, self._defaults = tuning, defaults
        self._clock, self._wall_clock = clock, wall_clock
        self._decoder = PacketDecoder()
        # Each glove's pipeline, built only when one is needed: for a frames,
        # debug or cancelled raw subscription, or for calibration().
        self._gloves: Dict[str, Glove] = {}
        # Each glove's newest packet, which a pipeline built for
        # calibration() loads the glove's profile with.
        self._last: Dict[str, GlovePacket] = {}
        self._seq: Dict[str, int] = {}
        self._seen_s: Dict[str, float] = {}
        self._streams: Dict[str, Tuple[Subscription, Pacer]] = {}
        # The Hall range each glove was put on with set_hall_range (resend
        # on), sent again when it comes back from silence; and the profile,
        # with the ranges a Hall command put it on, that its raw messages and
        # pipeline are yet to take, which the glove's next packet does.
        self._ranges: Dict[str, int] = {}
        self._new_profile: Dict[str, Profile] = {}
        # Gloves whose pipeline has had every packet since it was built: the
        # ones a frames, debug or cancelled raw subscription has included since.
        self._fed: Set[str] = set()
        # Held by the reader through each packet, so a glove's pipeline is
        # never read half-changed (calibration()). Re-entrant: a callback
        # (on()) runs inside it and may ask for calibration().
        self._pipeline = threading.RLock()
        # One command written to the port at a time.
        self._writing = threading.Lock()
        # Gloves heard that have no profile: read raw only, as the default
        # hand, or left out when there is none.
        self.unprofiled: Set[str] = set()
        # Tracking packets read so far, from any glove.
        self.packets = 0
        self._link = open_link(port) if link is None else link
        self._running = True
        self._reader = threading.Thread(target=self._read_link, name="pandora-sdk-receiver",
                                        daemon=True)
        self._reader.start()

    # ── the same requests as the app answers ─────────────────────────────

    def devices(self, timeout: Optional[float] = None) -> List[Device]:
        """The gloves heard so far, sending or not: those with a profile,
        and those read raw only as the default hand (unprofiled)."""
        now = self._clock()
        with self._changed:
            return [Device(device, self._profiles[device].hand, self._profiles[device].name,
                           now - seen <= SILENT_AFTER_S)
                    for device, seen in sorted(self._seen_s.items())]

    def subscribe(self, stream: str = "frames", *, devices: Optional[Sequence[str]] = None,
                  hands: Sequence[str] = protocol.HANDS, rate_hz: float = 0.0,
                  bones: bool = True, keys: Sequence[str] = ("*",), cancelled: bool = False,
                  timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream, as Client.subscribe does; nothing is delivered
        before. Judged by the same code as the app's server. Raw with
        *cancelled* keeps each glove it includes going through its pipeline,
        as frames do, and each of its messages is sent once the pipeline has
        had the packet; hall is still the glove's own counts."""
        asked = Subscription(stream, None if devices is None else tuple(devices), tuple(hands),
                             rate_hz, bones, tuple(keys), cancelled)
        granted = _judged(Subscription.from_message, asked.to_message())
        if _runs_pipeline(granted.stream, granted):
            self._require_defaults()
        with self._changed:
            self._streams[granted.stream] = (granted, Pacer(granted.rate_hz))
            if granted.stream == "debug":
                for glove in self._gloves.values():
                    _publish_debug(glove)
        return granted

    # ── commands for a glove (what each does is on Source) ───────────────

    def play_rom(self, device: str, effects: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        """As Client.play_rom. The app's haptic settings (which motors may
        run, the wearer's strength) are the app's: here every command goes
        as given."""
        request = _judged(Haptics.from_message,
                          Haptics(device, effects=tuple(effects)).to_message())
        self._connected(device)
        self._send(commands.haptic_rom(device, request.effects or ()))

    def play_rtp(self, device: str, amplitudes: Sequence[int], durations_ms: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        """As Client.play_rtp, with every command going as given (play_rom)."""
        request = _judged(Haptics.from_message,
                          Haptics(device, amplitudes=tuple(amplitudes),
                                  durations_ms=tuple(durations_ms)).to_message())
        self._connected(device)
        self._send(commands.haptic_rtp(device, request.amplitudes or (),
                                       request.durations_ms or ()))

    def set_hall_range(self, device: str, hall_range: int,
                       timeout: Optional[float] = None, *, resend: bool = True) -> str:
        """As Client.set_hall_range. Every sensor gets the range with the
        other settings it powers up with, as the app sends them before the
        user has changed them; a sensor the profile has on a range of its own
        is on this one too from now on. The glove's pipeline takes the new
        range from its next packet. With *resend*, the glove is put on it
        again when it comes back after going silent (it may have been
        switched off and on); without, it is not, and a range remembered
        before is forgotten. The profile file is not changed. Returns "":
        the words the app gives are the app's; calibration(device).extrapolated
        says whether the bounds are a rescale."""
        request = _judged(HallRange.from_message, HallRange(device, hall_range).to_message())
        self._connected(device)
        self._send(_hall_command(device, request.hall_range))
        with self._changed:
            self._new_profile[device] = replace(
                self._next_profile(device), range=request.hall_range, sensor_ranges={})
            if resend:
                self._ranges[device] = request.hall_range
            else:
                self._ranges.pop(device, None)
        return ""

    def send_hall_config(self, device: str, *, reset: bool = False,
                         hall_range: int = HALL_DEFAULT_RANGE, osr: int = HALL_DEFAULT_OSR,
                         odr: int = HALL_DEFAULT_ODR, filter: int = HALL_DEFAULT_FILTER,
                         target: Union[int, str] = HALL_TARGET_ALL,
                         timeout: Optional[float] = None) -> None:
        """One Hall config command, exactly as given (what each setting is
        is on Source). Nothing is sent again by itself afterwards, and a
        range set_hall_range remembered for the glove is forgotten: a program
        sending its own Hall settings sends them again itself, after the
        glove went silent (a glove switched off and on is on its firmware's
        defaults). The range the command puts the sensors on (the default
        range after a reset) is what the glove's raw messages say and its
        pipeline is rescaled to, from its next packet: the whole glove's for
        every sensor, one sensor's range (raw sensor_ranges) for one."""
        sensor = _hall_sensor(target)
        if hall_range not in (HALL_RANGE_5MT, HALL_RANGE_50MT):
            raise ServerError("bad_hall_range", f"hall_range is {hall_range!r}; it is 0 "
                                                "(+-5 mT) or 1 (+-50 mT)")
        for name, value, top in (("osr", osr, HALL_OSR_HIGH), ("odr", odr, len(HALL_ODR_HZ) - 1),
                                 ("filter", filter, HALL_FILTER_MAX)):
            if not isinstance(value, int) or not 0 <= value <= top:
                raise ServerError("bad_hall_config", f"{name} is {value!r}; it is 0 to {top}")
        self._connected(device)
        self._send(commands.hall_config(device, reset=reset, range_val=hall_range, osr=osr,
                                        odr=odr, filter_val=filter,
                                        target=HALL_TARGET_ALL if sensor is None
                                        else hall_target_id(sensor)))
        now_on = HALL_DEFAULT_RANGE if reset else hall_range
        with self._changed:
            before = self._next_profile(device)
            self._new_profile[device] = (
                replace(before, range=now_on, sensor_ranges={}) if sensor is None
                else replace(before, sensor_ranges={**before.sensor_ranges, sensor: now_on}))
            self._ranges.pop(device, None)

    def _next_profile(self, device: str) -> Profile:
        """*device*'s profile with the Hall commands sent so far: the one
        its next packet takes."""
        return self._new_profile.get(device) or self._profiles[device]

    def _connected(self, device: str) -> None:
        """ServerError "unknown_device" unless *device* has sent lately, as
        the app judges a command."""
        with self._changed:
            seen = self._seen_s.get(device)
        if seen is None or self._clock() - seen > SILENT_AFTER_S:
            raise ServerError("unknown_device", f"no glove {device!r} is sending")

    def _send(self, payload: bytes) -> None:
        with self._writing:
            send_command(self._link, payload)

    # ── calibration bounds ───────────────────────────────────────────────

    def latest_calibration(self, device: str) -> Optional[Calibration]:
        """The bounds *device*'s pipeline normalises with now; None for a
        glove not heard. Here they are read when asked, so they are never
        behind."""
        try:
            return self._calibration_of(device, None)
        except ServerError:
            return None

    def _calibration_of(self, device: str, timeout: Optional[float]) -> Calibration:
        """From the glove's own stores, as the app's server reads them. A
        glove with no pipeline yet gets one, loaded with its newest packet."""
        with self._pipeline, self._changed:
            if device not in self._seen_s:
                raise ServerError("unknown_device", f"no glove {device!r} has been heard")
            if device in self.unprofiled:
                # No profile, so no calibration: as an uncalibrated glove's.
                return Calibration(device, self._profiles[device].range, False, {})
            glove = self._gloves.get(device)
            if glove is None:
                glove = self._gloves[device] = self._glove(self._profiles[device])
                glove.push_decoded(self._last[device])
            with glove.stores():
                bounds = active_bounds(device)
        return protocol.parse_calibration(protocol.calibration_message(device, bounds))

    # ── the receiver ─────────────────────────────────────────────────────

    def _read_link(self) -> None:
        """The reader thread: packets to frames until the port ends."""
        why, code, cause = "", "closed", None
        try:
            for packet in link_packets(self._link, self._decoder):
                if not self._running:
                    break
                if packet is not None:
                    with self._pipeline:
                        self._on_packet(packet)
        except OSError as failed:
            why, code, cause = link_failure(failed), link_failure_code(failed), failed
        except CallbackFailed as failed:
            why, code, cause = str(failed), "callback_failed", failed.__cause__
        except Exception as failed:  # the pipeline's own failure must not pass for silence
            why, code, cause = f"a glove's pipeline failed ({failed!r})", "pipeline_failed", failed
        self._end(why, code, cause)

    def _on_packet(self, packet: GlovePacket) -> None:
        device = packet.device_id
        retired: Optional[Glove] = None
        with self._changed:
            self.packets += 1
            profile = self._profiles.get(device)
            if profile is None:
                self.unprofiled.add(device)
                if self._default_hand is None:
                    return
                profile = self._profiles[device] = Profile(self._default_hand)
            # A glove with no profile of its own is read raw only.
            raw_only = device in self.unprofiled
            if device in self._new_profile:
                # A new Glove on the new ranges, when one is next needed:
                # building one is how the core puts a glove's calibration on
                # the ranges its profile names.
                profile = self._profiles[device] = self._new_profile.pop(device)
                retired = self._gloves.pop(device, None)
                self._fed.discard(device)
            # A hand is made only for a glove that a frames or debug
            # subscription includes, or a raw one with cancelled. A pipeline
            # that missed packets starts again as a new Glove, as on the
            # glove's first packet: its filters would otherwise pick up from
            # a packet long gone.
            make_hand = not raw_only and any(wanted.wants(device, profile.hand)
                                             for stream, (wanted, _) in self._streams.items()
                                             if _runs_pipeline(stream, wanted))
            glove = self._gloves.get(device)
            if make_hand and (glove is None or device not in self._fed):
                if glove is not None:
                    retired = glove
                glove = self._gloves[device] = self._glove(profile)
            if make_hand:
                self._fed.add(device)
            else:
                self._fed.discard(device)
            now = self._clock()
            back = now - self._seen_s.get(device, now) > SILENT_AFTER_S
            resend = self._ranges.get(device) if back else None
            seq = self._seq[device] = self._seq.get(device, 0) + 1
            self._seen_s[device] = now
            self._last[device] = packet
            due = {stream: wanted for stream, (wanted, pacer) in self._streams.items()
                   if wanted.wants(device, profile.hand) and pacer.due(device, now)}
        if retired is not None:
            retired.close()
        if resend is not None:
            try:
                self._send(_hall_command(device, resend))
            except ServerError as failed:
                _log.warning("could not put %s back on its Hall range: %s", device, failed)
        time_us = int(now * 1_000_000)
        wall_time_us = int(self._wall_clock() * 1_000_000)
        # The Frame, Raw and Debug a client of the app would get: through the
        # protocol's own code, so the two modes cannot differ. A Raw is built
        # straight from the packet (raw_from_packet is decode_raw of
        # encode_raw, without the msgpack round trip).
        raw = None
        cancelled = "raw" in due and due["raw"].cancelled and not raw_only
        if "raw" in due:
            # Before the pipeline, which changes the packet in place: hall is
            # what the glove sent.
            raw = protocol.raw_from_packet(
                packet, profile.hand, seq, time_us, profile.range, profile.sensor_ranges,
                wall_time_us)
        if raw is not None and not cancelled:
            self._deliver("raw", raw)
        if not make_hand or glove is None:
            return
        # From here every packet goes through the pipeline, wanted now or
        # not: its filters carry state from one to the next.
        frame = glove.push_decoded(packet)
        if raw is not None and cancelled:
            # The counts the pipeline normalised exist only now, after gap
            # fill and earth-field cancellation changed the packet: read by
            # the core's raw_from_packet, as the app's server reads them
            # (encode_raw, the same code). The rest of the message stays the
            # one made before.
            after = protocol.raw_from_packet(
                packet, profile.hand, seq, time_us, profile.range, profile.sensor_ranges,
                wall_time_us, cancelled=True)
            self._deliver("raw", replace(
                raw, hall_cancelled=after.hall_cancelled,
                thumb_distal_cancelled=after.thumb_distal_cancelled,
                earth_cancelled=after.earth_cancelled))
        if "frames" in due:
            self._deliver("frames", protocol.decode_frame(protocol.encode_frame(
                frame, seq, time_us, due["frames"].bones, wall_time_us)))
        if "debug" in due:
            with glove.stores():
                values = get_debug_tap().values_of(device, due["debug"].keys)
            self._deliver("debug", protocol.decode_debug(protocol.encode_debug(
                device, profile.hand, seq, time_us, values, wall_time_us)))

    def _require_defaults(self) -> Mapping[str, Any]:
        """The pipeline's default files: those given, or pandora-core's,
        read when the first pipeline is wanted. PandoraError when there are
        none, rather than hands made wrong without them."""
        if self._defaults is None:
            found = shipped_defaults()
            if not found:
                raise PandoraError(
                    "this pandora-core carries none of its default files "
                    "(hall_reference.default.json, ic_overrides.default.json, "
                    "bone_pose_overrides.json), and hands made without them are wrong. Raw "
                    "readings need none of them. Reinstall pandora-sdk (a frozen program: "
                    "bundle pandora_core's data files), or pass them as defaults=, parsed, by "
                    "file name.")
            self._defaults = found
        return self._defaults

    def _glove(self, profile: Profile) -> Glove:
        """A pipeline for *profile*, publishing its debug values when they
        are subscribed to."""
        glove = Glove(profile, self._tuning, self._require_defaults())
        if "debug" in self._streams:
            _publish_debug(glove)
        return glove

    def close(self) -> None:
        """Stop reading and close the port. Unread messages can still be read."""
        with self._changed:
            self._closed = True
            self._changed.notify_all()
        self._running = False
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2.0)
        self._link.close()
        for glove in self._gloves.values():
            glove.close()


def _judged(parse: Callable[[Dict[str, Any]], Any], message: Dict[str, Any]) -> Any:
    """*message* read by the core's own parser, as the app's server reads a
    request; what it refuses is a ServerError with the protocol's code."""
    try:
        return parse(message)
    except ProtocolError as refused:
        raise ServerError(refused.code, str(refused)) from None


def _runs_pipeline(stream: str, wanted: Subscription) -> bool:
    """Whether a subscription to *stream* needs the gloves it includes to go
    through their pipelines: frames and debug are made by it, and raw's
    cancelled counts are the packet as it leaves it."""
    return stream in HAND_STREAMS or (stream == "raw" and wanted.cancelled)


def _hall_command(device: str, hall_range: int) -> bytes:
    """Every Hall sensor of *device* on *hall_range*, with the oversampling,
    data rate and filter it powers up with."""
    return commands.hall_config(device, reset=False, range_val=hall_range, osr=HALL_DEFAULT_OSR,
                                odr=HALL_DEFAULT_ODR, filter_val=HALL_DEFAULT_FILTER,
                                target=HALL_TARGET_ALL)


# Hall config target id -> the sensor's key, as a profile's sensor_ranges
# names it.
_HALL_SENSORS = {hall_target_id(key): key for key in HALL_SENSOR_KEYS}


def _hall_sensor(target: Union[int, str]) -> Optional[str]:
    """The sensor key *target* names (a target id or a key), None for every
    sensor; ServerError "bad_hall_config" for no sensor a glove has."""
    if target == HALL_TARGET_ALL:
        return None
    if isinstance(target, str) and target in HALL_SENSOR_KEYS:
        return target
    if isinstance(target, int) and not isinstance(target, bool) and target in _HALL_SENSORS:
        return _HALL_SENSORS[target]
    raise ServerError("bad_hall_config",
                      f"target is {target!r}; it is HALL_TARGET_ALL ({HALL_TARGET_ALL}), a "
                      f"sensor's id ({sorted(_HALL_SENSORS)}) or its key ({list(HALL_SENSOR_KEYS)})")


def _publish_debug(glove: Glove) -> None:
    """Have *glove*'s pipeline publish its named values (its own debug tap,
    one per glove)."""
    with glove.stores():
        get_debug_tap().want(glove)
