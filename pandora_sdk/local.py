"""Hands straight from a receiver, with no app running.

    import pandora_sdk

    with pandora_sdk.open_receiver("profiles/") as gloves:
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

The same reading as connect() gives (subscribe, frames, raw, debug, read,
latest, devices), the same Frame, Raw and Debug, and the same commands for a
glove (play_rom, play_rtp, stop, set_hall_range) and calibration bounds, so a
program can change between the two by changing one line. Here the pipeline
(pandora-core) runs in this process: one thread reads the receiver's port and
pushes each glove's packets through that glove's own pipeline, and commands
are written to the same port.

Each glove needs a profile: its hand and the calibration it carries. `pandora
calibrate` (pandora_sdk/calibrate.py) writes one, into default_profiles_dir()
unless told otherwise, which is also where profiles are read from when none
are named. Packets from a glove with none are left out, and its id is kept in
`unprofiled`. Nothing is sent to a glove unless asked (set_hall_range, the
haptics), so a profile's calibration must have been taken at the range the
glove powers up in, which is the range `pandora calibrate` puts it on, or the
program puts the glove on the profile's range itself.
"""
from __future__ import annotations

import json
import logging
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
                                        HALL_TARGET_ALL)
from pandora_core.protocol import (Calibration, Device, HallRange, Haptics, Pacer, ProtocolError,
                                   Subscription)
from pandora_core.scope import shipped_defaults
from pandora_core.serial_framing import encode_frame
from pandora_core.tuning import Tuning

from pandora_sdk.source import ConnectionFailed, PandoraError, ServerError, Source

# A glove with no packet for this long is listed as not connected, as the
# protocol's device list says, and takes no commands.
SILENT_AFTER_S = 2.0

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


def receiver_port(port: Optional[str] = None) -> str:
    """*port*, or the serial port of the one receiver plugged in."""
    if port is not None:
        return port
    found = receiver.find_receivers()
    if not found:
        raise ConnectionFailed("no receiver is plugged into this computer")
    if len(found) > 1:
        raise ConnectionFailed(
            f"more than one receiver ({', '.join(f.device for f in found)}); "
            "say which one by its port")
    return found[0].device


def open_link(port: Optional[str] = None) -> Any:
    """The receiver's serial port, opened as the link needs: *port*, or the
    one receiver plugged in."""
    port = receiver_port(port)
    try:
        link = receiver.open_port(port, timeout=0.2)
    except OSError as failed:
        if receiver.is_port_locked(failed):
            raise ConnectionFailed(
                f"{port} is in use. If the 01 Glove app is running it holds the receiver: "
                "read through it with pandora_sdk.connect(), or close it.") from None
        raise ConnectionFailed(f"could not open {port}: {failed}") from None
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


def open_receiver(profiles: Optional[Profiles] = None, *, port: Optional[str] = None,
                  tuning: Union[Tuning, Mapping[str, Any], None] = None,
                  defaults: Optional[Mapping[str, Any]] = None, buffer: int = 256) -> "Local":
    """Open the receiver on this computer and run the gloves' pipelines here.

    *profiles* is a folder of `<device id>.json` files, or a dict of device id
    to profile; left out, it is default_profiles_dir(), where `pandora
    calibrate` writes. *port* names the receiver's serial port when there is
    more than one. *tuning* is the pipeline's settings (pandora_core Tuning),
    *defaults* its shipped default files as parsed JSON by file name; left
    out, pandora-core's own are used.
    """
    return Local(profiles, port=port, tuning=tuning, defaults=defaults, buffer=buffer)


class Local(Source):
    """A receiver read in this process. Use open_receiver(); *link* is an
    already open port (or anything with its read, in_waiting, write and
    close), *clock* the seconds frames are stamped and paced with, and
    *wall_clock* the seconds since the Unix epoch their wall_time_us is."""

    def __init__(self, profiles: Optional[Profiles] = None, *, port: Optional[str] = None,
                 tuning: Union[Tuning, Mapping[str, Any], None] = None,
                 defaults: Optional[Mapping[str, Any]] = None, buffer: int = 256,
                 link: Any = None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        super().__init__(buffer)
        if defaults is None and not shipped_defaults():
            raise PandoraError(
                "this pandora-core carries none of its default files (hall_reference.default.json, "
                "ic_overrides.default.json, bone_pose_overrides.json), and hands made without "
                "them are wrong. Reinstall pandora-sdk, or pass them as defaults=, parsed, by "
                "file name.")
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
        self._gloves: Dict[str, Glove] = {}
        self._seq: Dict[str, int] = {}
        self._seen_s: Dict[str, float] = {}
        self._streams: Dict[str, Tuple[Subscription, Pacer]] = {}
        # The Hall range each glove was put on with set_hall_range, sent again
        # when it comes back from silence; and the ones its pipeline is yet to
        # be put on, which the glove's next packet does.
        self._ranges: Dict[str, int] = {}
        self._new_range: Dict[str, int] = {}
        # Held by the reader through each packet, so a glove's pipeline is
        # never read half-changed (calibration()).
        self._pipeline = threading.Lock()
        # One command written to the port at a time.
        self._writing = threading.Lock()
        # Gloves heard that have no profile; their packets are left out.
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
        """The gloves heard so far that have a profile, sending or not."""
        now = self._clock()
        with self._changed:
            return [Device(device, glove.hand, glove.profile.name,
                           now - self._seen_s[device] <= SILENT_AFTER_S)
                    for device, glove in sorted(self._gloves.items())]

    def subscribe(self, stream: str = "frames", *, devices: Optional[Sequence[str]] = None,
                  hands: Sequence[str] = protocol.HANDS, rate_hz: float = 0.0,
                  bones: bool = True, keys: Sequence[str] = ("*",),
                  timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream, as Client.subscribe does; nothing is delivered
        before. Judged by the same code as the app's server."""
        asked = Subscription(stream, None if devices is None else tuple(devices), tuple(hands),
                             rate_hz, bones, tuple(keys))
        granted = _judged(Subscription.from_message, asked.to_message())
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
                       timeout: Optional[float] = None) -> str:
        """As Client.set_hall_range. Every sensor gets the range with the
        other settings it powers up with, as the app sends them before the
        user has changed them; a sensor the profile has on a range of its own
        is on this one too from now on. The glove's pipeline takes the new
        range from its next packet, and the glove is put on it again when it
        comes back after going silent (it may have been switched off and on).
        The profile file is not changed. Returns "": the words the app gives
        are the app's; calibration(device).extrapolated says whether the
        bounds are a rescale."""
        request = _judged(HallRange.from_message, HallRange(device, hall_range).to_message())
        self._connected(device)
        self._send(_hall_command(device, request.hall_range))
        with self._changed:
            self._ranges[device] = self._new_range[device] = request.hall_range
        return ""

    def _connected(self, device: str) -> None:
        """ServerError "unknown_device" unless *device* has sent lately, as
        the app judges a command."""
        with self._changed:
            seen = self._seen_s.get(device) if device in self._gloves else None
        if seen is None or self._clock() - seen > SILENT_AFTER_S:
            raise ServerError("unknown_device", f"no glove {device!r} with a profile is sending")

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
        """From the glove's own stores, as the app's server reads them."""
        with self._pipeline, self._changed:
            glove = self._gloves.get(device)
            if glove is None:
                raise ServerError("unknown_device", f"no glove {device!r} with a profile has "
                                                    "been heard")
            with glove.stores():
                bounds = active_bounds(device)
        return protocol.parse_calibration(protocol.calibration_message(device, bounds))

    # ── the receiver ─────────────────────────────────────────────────────

    def _read_link(self) -> None:
        """The reader thread: packets to frames until the port ends."""
        why = ""
        try:
            for packet in link_packets(self._link, self._decoder):
                if not self._running:
                    break
                if packet is not None:
                    with self._pipeline:
                        self._on_packet(packet)
        except OSError as failed:
            why = link_failure(failed)
        except Exception as failed:  # the pipeline's own failure must not pass for silence
            why = f"a glove's pipeline failed ({failed!r})"
        self._end(why)

    def _on_packet(self, packet: GlovePacket) -> None:
        device = packet.device_id
        retired: Optional[Glove] = None
        with self._changed:
            self.packets += 1
            glove = self._gloves.get(device)
            if glove is None:
                profile = self._profiles.get(device)
                if profile is None:
                    self.unprofiled.add(device)
                    return
                glove = self._gloves[device] = self._glove(profile)
            elif device in self._new_range:
                # A new Glove on the new range: building one is how the core
                # puts a glove's calibration on the range its profile names.
                retired = glove
                profile = self._profiles[device] = replace(
                    glove.profile, range=self._new_range.pop(device), sensor_ranges={})
                glove = self._gloves[device] = self._glove(profile)
            now = self._clock()
            back = now - self._seen_s.get(device, now) > SILENT_AFTER_S
            resend = self._ranges.get(device) if back else None
            seq = self._seq[device] = self._seq.get(device, 0) + 1
            self._seen_s[device] = now
            due = {stream: wanted for stream, (wanted, pacer) in self._streams.items()
                   if wanted.wants(device, glove.hand) and pacer.due(device, now)}
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
        # protocol's own encoding, so the two modes cannot differ.
        if "raw" in due:
            # Before the pipeline, which changes the packet in place.
            self._deliver("raw", protocol.decode_raw(protocol.encode_raw(
                packet, glove.hand, seq, time_us, glove.hall_range, glove.sensor_ranges,
                wall_time_us)))
        # Every packet goes through the pipeline, wanted or not: its filters
        # carry state from one to the next.
        frame = glove.push_decoded(packet)
        if "frames" in due:
            self._deliver("frames", protocol.decode_frame(protocol.encode_frame(
                frame, seq, time_us, due["frames"].bones, wall_time_us)))
        if "debug" in due:
            with glove.stores():
                values = get_debug_tap().values_of(device, due["debug"].keys)
            self._deliver("debug", protocol.decode_debug(protocol.encode_debug(
                device, glove.hand, seq, time_us, values, wall_time_us)))

    def _glove(self, profile: Profile) -> Glove:
        """A pipeline for *profile*, publishing its debug values when they
        are subscribed to."""
        glove = Glove(profile, self._tuning, self._defaults)
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


def _hall_command(device: str, hall_range: int) -> bytes:
    """Every Hall sensor of *device* on *hall_range*, with the oversampling,
    data rate and filter it powers up with."""
    return commands.hall_config(device, reset=False, range_val=hall_range, osr=HALL_DEFAULT_OSR,
                                odr=HALL_DEFAULT_ODR, filter_val=HALL_DEFAULT_FILTER,
                                target=HALL_TARGET_ALL)


def _publish_debug(glove: Glove) -> None:
    """Have *glove*'s pipeline publish its named values (its own debug tap,
    one per glove)."""
    with glove.stores():
        get_debug_tap().want(glove)
