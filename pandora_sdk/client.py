"""A client for the 01 Glove app's wire protocol (PROTOCOL.md in pandora-core).

    import pandora_sdk

    with pandora_sdk.connect() as gloves:
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

The messages are pandora_core.protocol's; this file only opens the socket,
sends requests (subscriptions, commands for a glove, calibration) and sorts
what arrives. One background thread reads the socket; keeping and
handing out the messages is Source's (pandora_sdk/source.py), shared with the
receiver read without the app (pandora_sdk/local.py).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as _open_socket

from pandora_core import protocol
from pandora_core.protocol import (DEFAULT_PORT, Calibration, Device, HallRange, Haptics,
                                   ProtocolError, Subscription)

from pandora_sdk.source import (CallbackFailed, ConnectionFailed, Disconnected, PandoraError,
                                ServerError, Source)

__all__ = ["Client", "ConnectionFailed", "Disconnected", "PandoraError", "Server",
           "ServerError", "connect"]

# The text messages that answer a request. "devices" and "calibration" are
# also sent unasked, when they change.
REPLIES = ("devices", "subscribed", "error", "haptics_sent", "hall_range", "calibration")


@dataclass(frozen=True)
class Server:
    """What the app said about itself in its hello."""
    name: str
    version: str
    protocol: int
    # The streams and requests it accepts.
    capabilities: Tuple[str, ...]


def connect(host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, name: str = "pandora-sdk",
            timeout: float = 5.0, buffer: int = 256) -> "Client":
    """Connect to the app and exchange hellos.

    *name* is what the app is told this program is. *timeout* is how long to
    wait for the connection, and by default for each answer after it. *buffer*
    is how many unread messages are kept per stream before the oldest go.
    """
    return Client(host, port, name=name, timeout=timeout, buffer=buffer)


class Client(Source):
    """One connection to the app. Use connect(); close it or use it in a with."""

    def __init__(self, host: str, port: int, *, name: str, timeout: float, buffer: int) -> None:
        super().__init__(buffer)
        url = f"ws://{host}:{port}"
        self._timeout = timeout
        try:
            # No proxy and no compression: this is localhost. __enter__ because
            # the library wants a `with`, and this connection outlives one
            # block; close() is what a `with` would have called.
            self._socket = _open_socket(
                url, open_timeout=timeout, compression=None, proxy=None).__enter__()
        except (OSError, TimeoutError, WebSocketException) as failed:
            raise ConnectionFailed(
                f"nothing answered at {url} ({failed}). Is the 01 Glove app running with "
                "Protocol switched on (Settings > General > Advanced > Protocol)?") from None
        self.server = self._exchange_hellos(url, name)

        # Answers to requests, oldest first (REPLIES).
        self._replies: Deque[Dict[str, Any]] = deque()
        # Per glove, the newest calibration message, asked for or pushed.
        self._calibrations: Dict[str, Calibration] = {}
        # One request at a time: an error carries no word on what it answers.
        self._requesting = threading.Lock()
        self._reader = threading.Thread(target=self._read_socket, name="pandora-sdk-reader",
                                        daemon=True)
        self._reader.start()

    def _exchange_hellos(self, url: str, name: str) -> Server:
        try:
            first = self._socket.recv(timeout=self._timeout)
            if not isinstance(first, str):
                raise ProtocolError("hello_expected", "the first message must be a hello")
            hello = protocol.parse(first)
            protocol.check_hello(hello)
            self._socket.send(protocol.dumps(protocol.client_hello(name)))
        except ProtocolError as refused:
            # The spec: say why, then close.
            try:
                self._socket.send(protocol.dumps(refused.to_message()))
            except ConnectionClosed:
                pass
            self._socket.close()
            raise
        except (TimeoutError, ConnectionClosed) as failed:
            self._socket.close()
            raise ConnectionFailed(
                f"{url} answered but sent no hello ({failed or 'timed out'}); "
                "is that port the 01 Glove app's?") from None
        return Server(str(hello.get("server", "")), str(hello.get("version", "")),
                      hello["protocol"], tuple(hello.get("capabilities", ())))

    # ── requests ─────────────────────────────────────────────────────────

    def devices(self, timeout: Optional[float] = None) -> List[Device]:
        """The gloves the app knows now, connected or not."""
        return protocol.parse_devices(self._request({"type": "devices"}, "devices", timeout))

    def subscribe(self, stream: str = "frames", *, devices: Optional[Sequence[str]] = None,
                  hands: Sequence[str] = protocol.HANDS, rate_hz: float = 0.0,
                  bones: bool = True, keys: Sequence[str] = ("*",), cancelled: bool = False,
                  timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream: "frames" (finished hands), "raw" (what the
        glove sent) or "debug" (values from inside the pipeline). Returns
        what the app granted.

        *devices* are glove ids, None for every glove; *hands* narrows by
        hand. *rate_hz* is messages a second per glove at most, 0 for every
        one. *bones* (frames only) is whether each frame carries the 31-bone
        skeleton, which is most of its size. *keys* (debug only) are glob
        patterns over the value names to send. *cancelled* (raw only) adds
        to each Raw the counts the pipeline normalised, after earth-field
        cancellation (hall_cancelled, thumb_distal_cancelled,
        earth_cancelled); an app without the "raw_cancelled" capability
        grants the subscription with cancelled off and sends none. Asking
        again for the same stream replaces the earlier subscription.
        """
        wanted = Subscription(stream, None if devices is None else tuple(devices), tuple(hands),
                              float(rate_hz), bones, tuple(keys), cancelled)
        reply = self._request(wanted.to_message(), "subscribed", timeout, stream=stream)
        return Subscription.from_message(reply)

    # ── commands for a glove (what each does is on Source) ───────────────

    def play_rom(self, device: str, effects: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        self._request(Haptics(device, effects=tuple(effects)).to_message(), "haptics_sent",
                      timeout, device=device)

    def play_rtp(self, device: str, amplitudes: Sequence[int], durations_ms: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        self._request(Haptics(device, amplitudes=tuple(amplitudes),
                              durations_ms=tuple(durations_ms)).to_message(),
                      "haptics_sent", timeout, device=device)

    def set_hall_range(self, device: str, hall_range: int,
                       timeout: Optional[float] = None) -> str:
        reply = self._request(HallRange(device, hall_range).to_message(), "hall_range", timeout,
                              device=device)
        return str(reply.get("note", ""))

    # ── calibration bounds ───────────────────────────────────────────────

    def latest_calibration(self, device: str) -> Optional[Calibration]:
        """The newest bounds the app sent for *device*, asked for or pushed
        when they changed; None before the first."""
        with self._changed:
            return self._calibrations.get(device)

    def _calibration_of(self, device: str, timeout: Optional[float]) -> Calibration:
        reply = self._request({"type": "calibration", "device": device}, "calibration", timeout,
                              device=device)
        return protocol.parse_calibration(reply)

    def _request(self, message: Dict[str, Any], answer: str, timeout: Optional[float],
                 **match: Any) -> Dict[str, Any]:
        """Send *message* and return the app's *answer* message whose keys
        hold the values in *match* (the stream, the device); ServerError when
        it answers with an error instead."""
        timeout = self._timeout if timeout is None else timeout
        with self._requesting:
            with self._changed:
                self._replies.clear()
            try:
                self._socket.send(protocol.dumps(message))
            except ConnectionClosed:
                raise Disconnected(self._why_ended()) from None
            deadline = time.monotonic() + timeout
            with self._changed:
                while True:
                    while self._replies:
                        reply = self._replies.popleft()
                        if reply["type"] == "error":
                            raise ServerError(str(reply.get("code", "")),
                                              str(reply.get("message", "")))
                        if reply["type"] == answer and all(
                                reply.get(key) == value for key, value in match.items()):
                            return reply
                    if self._closed or self._ended:
                        raise Disconnected(self._why_ended())
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"the app did not answer {message['type']!r} in {timeout:g} s")
                    self._changed.wait(remaining)

    # ── the socket ───────────────────────────────────────────────────────

    def _read_socket(self) -> None:
        """The reader thread: sort what arrives until the connection ends, or
        a callback (on()) raises."""
        why = "the app closed the connection"
        try:
            for message in self._socket:
                if isinstance(message, str):
                    self._on_text(message)
                else:
                    self._on_binary(bytes(message))
        except ConnectionClosed as closed:
            why = f"the connection to the app was lost ({closed})"
        except CallbackFailed as failed:
            why = str(failed)
        self._end(why)

    def _on_text(self, text: str) -> None:
        try:
            message = protocol.parse(text)
        except ProtocolError:
            return
        # Anything else is a message this version does not know: ignored.
        if message["type"] not in REPLIES:
            return
        calibration = None
        if message["type"] == "calibration":
            try:
                calibration = protocol.parse_calibration(message)
            except ProtocolError:
                return
        with self._changed:
            if calibration is not None:
                self._calibrations[calibration.device] = calibration
            self._replies.append(message)
            self._changed.notify_all()

    def _on_binary(self, data: bytes) -> None:
        kind = protocol.kind_of(data)
        try:
            if kind == "frame":
                self._deliver("frames", protocol.decode_frame(data))
            elif kind == "raw":
                self._deliver("raw", protocol.decode_raw(data))
            elif kind == "debug":
                self._deliver("debug", protocol.decode_debug(data))
        except ProtocolError:
            return

    def close(self) -> None:
        """Hang up. Iterations end; unread messages can still be read."""
        with self._changed:
            self._closed = True
            self._changed.notify_all()
        self._socket.close()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2.0)
