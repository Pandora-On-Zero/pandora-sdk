"""A client for the 01 Glove app's wire protocol (PROTOCOL.md in pandora-core).

    import pandora_sdk

    with pandora_sdk.connect() as gloves:
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

The messages are pandora_core.protocol's; this file only opens the socket and
keeps what arrives. One background thread reads the socket. Per stream it
keeps the newest message of each glove (latest()) and a short queue of unread
ones (read(), frames(), raw()); when the queue is full the oldest is dropped
and counted, so a slow reader gets fresh hands, not a growing backlog.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as _open_socket

from pandora_core import protocol
from pandora_core.protocol import DEFAULT_PORT, Device, Frame, ProtocolError, Raw, Subscription

Message = Union[Frame, Raw]


class PandoraError(Exception):
    """What this package raises itself. A message the protocol does not allow
    is pandora_core's ProtocolError; a wait that ran out is TimeoutError."""


class ConnectionFailed(PandoraError):
    """Nothing answered at the address, or what answered is not the app."""


class Disconnected(PandoraError):
    """The connection ended: the app quit, or this client was closed."""


class ServerError(PandoraError):
    """The app refused a request. *code* is the protocol's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


class Client:
    """One connection to the app. Use connect(); close it or use it in a with."""

    def __init__(self, host: str, port: int, *, name: str, timeout: float, buffer: int) -> None:
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

        self._changed = threading.Condition()
        self._unread: Dict[str, Deque[Any]] = {
            stream: deque(maxlen=buffer) for stream in protocol.STREAMS}
        self._latest: Dict[str, Dict[str, Any]] = {stream: {} for stream in protocol.STREAMS}
        # Answers to requests, oldest first: devices, subscribed and error.
        self._replies: Deque[Dict[str, Any]] = deque()
        # One request at a time: an error carries no word on what it answers.
        self._requesting = threading.Lock()
        self._closed = False
        # Why the connection ended, when it was not close().
        self._lost = ""
        self._ended = False
        # Messages discarded unread because their queue was full.
        self.dropped = 0
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
                  bones: bool = True, timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream: "frames" (finished hands) or "raw" (what the
        glove sent). Returns what the app granted.

        *devices* are glove ids, None for every glove; *hands* narrows by
        hand. *rate_hz* is messages a second per glove at most, 0 for every
        one. *bones* (frames only) is whether each frame carries the 31-bone
        skeleton, which is most of its size. Asking again for the same stream
        replaces the earlier subscription.
        """
        wanted = Subscription(stream, None if devices is None else tuple(devices), tuple(hands),
                              float(rate_hz), bones)
        reply = self._request(wanted.to_message(), "subscribed", timeout, stream)
        return Subscription.from_message(reply)

    def _request(self, message: Dict[str, Any], answer: str, timeout: Optional[float],
                 stream: Optional[str] = None) -> Dict[str, Any]:
        """Send *message* and return the app's *answer* message; ServerError
        when it answers with an error instead."""
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
                        if reply["type"] == answer and stream in (None, reply.get("stream")):
                            return reply
                    if self._closed or self._ended:
                        raise Disconnected(self._why_ended())
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"the app did not answer {message['type']!r} in {timeout:g} s")
                    self._changed.wait(remaining)

    # ── messages ─────────────────────────────────────────────────────────

    def read(self, stream: str = "frames", timeout: Optional[float] = None) -> Any:
        """The oldest unread message of *stream* (a Frame, or a Raw for
        "raw"), waiting for one. TimeoutError after *timeout* seconds;
        Disconnected when the connection has ended."""
        message = self._next(stream, timeout)
        if message is None:
            raise Disconnected(self._why_ended())
        return message

    def frames(self) -> Iterator[Frame]:
        """Each hand frame as it arrives. Ends when this client is closed;
        raises Disconnected when the app goes away."""
        return self._each("frames")

    def raw(self) -> Iterator[Raw]:
        """Each raw message as it arrives; ends as frames() does."""
        return self._each("raw")

    def latest(self, glove: str, stream: str = "frames") -> Any:
        """The newest message from *glove*, read or not; None before its
        first. *glove* is a device id, or "left" or "right" for the glove on
        that hand (with two on one hand, whichever sent last). For a control
        loop that wants the hand as it is now."""
        with self._changed:
            newest = self._latest[stream]
            if glove in protocol.HANDS:
                on_hand = [message for message in newest.values() if message.hand == glove]
                return max(on_hand, key=lambda message: message.time_us, default=None)
            return newest.get(glove)

    def _each(self, stream: str) -> Iterator[Any]:
        while (message := self._next(stream, None)) is not None:
            yield message

    def _next(self, stream: str, timeout: Optional[float]) -> Optional[Message]:
        """The oldest unread message; None once this client is closed."""
        if stream not in self._unread:
            raise ValueError(f"no stream {stream!r}; there is {list(self._unread)}")
        unread = self._unread[stream]
        with self._changed:
            if not self._changed.wait_for(
                    lambda: bool(unread) or self._closed or self._ended, timeout):
                raise TimeoutError(f"no {stream} message in {timeout:g} s")
            if unread:
                return unread.popleft()
            if self._lost:
                raise Disconnected(self._lost)
            return None

    # ── the socket ───────────────────────────────────────────────────────

    def _read_socket(self) -> None:
        """The reader thread: sort what arrives until the connection ends."""
        why = "the app closed the connection"
        try:
            for message in self._socket:
                if isinstance(message, str):
                    self._on_text(message)
                else:
                    self._on_binary(bytes(message))
        except ConnectionClosed as closed:
            why = f"the connection to the app was lost ({closed})"
        with self._changed:
            if not self._closed:
                self._lost = why
            self._ended = True
            self._changed.notify_all()

    def _on_text(self, text: str) -> None:
        try:
            message = protocol.parse(text)
        except ProtocolError:
            return
        # Anything else is a message this version does not know: ignored.
        if message["type"] in ("devices", "subscribed", "error"):
            with self._changed:
                self._replies.append(message)
                self._changed.notify_all()

    def _on_binary(self, data: bytes) -> None:
        kind = protocol.kind_of(data)
        try:
            if kind == "frame":
                stream, message = "frames", protocol.decode_frame(data)
            elif kind == "raw":
                stream, message = "raw", protocol.decode_raw(data)
            else:
                return
        except ProtocolError:
            return
        with self._changed:
            unread = self._unread[stream]
            if len(unread) == unread.maxlen:
                self.dropped += 1
            unread.append(message)
            self._latest[stream][message.device] = message
            self._changed.notify_all()

    def _why_ended(self) -> str:
        return self._lost or "this client was closed"

    def close(self) -> None:
        """Hang up. Iterations end; unread messages can still be read."""
        with self._changed:
            self._closed = True
            self._changed.notify_all()
        self._socket.close()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=2.0)

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
