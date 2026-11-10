"""What both ways of getting hands share: the errors, and Source, which keeps
what arrives and hands it out.

A Source is fed by one background thread (the socket's, or the receiver's).
Per stream it keeps the newest message of each glove (latest()) and a short
queue of unread ones (read(), frames(), raw()); when the queue is full the
oldest is dropped and counted, so a slow reader gets fresh hands, not a
growing backlog.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, Dict, Iterator, List, Optional, Self, Sequence, Union

from pandora_core import protocol
from pandora_core.protocol import Device, Frame, Raw, Subscription

Message = Union[Frame, Raw]


class PandoraError(Exception):
    """What this package raises itself. A message the protocol does not allow
    is pandora_core's ProtocolError; a wait that ran out is TimeoutError."""


class ConnectionFailed(PandoraError):
    """Nothing answered at the address, or there is no receiver to open."""


class Disconnected(PandoraError):
    """The connection ended: the app quit, the receiver was unplugged, or
    this source was closed."""


class ServerError(PandoraError):
    """A request was refused. *code* is the protocol's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Source:
    """Hands and raw readings as they arrive, however they got here."""

    def __init__(self, buffer: int) -> None:
        self._changed = threading.Condition()
        self._unread: Dict[str, Deque[Any]] = {
            stream: deque(maxlen=buffer) for stream in protocol.STREAMS}
        self._latest: Dict[str, Dict[str, Any]] = {stream: {} for stream in protocol.STREAMS}
        self._closed = False
        # Why the source ended, when it was not close().
        self._lost = ""
        self._ended = False
        # Messages discarded unread because their queue was full.
        self.dropped = 0

    # ── for the thread that feeds it ─────────────────────────────────────

    def _deliver(self, stream: str, message: Message) -> None:
        with self._changed:
            unread = self._unread[stream]
            if len(unread) == unread.maxlen:
                self.dropped += 1
            unread.append(message)
            self._latest[stream][message.device] = message
            self._changed.notify_all()

    def _end(self, why: str) -> None:
        """The feeding thread is done; *why* unless close() was called."""
        with self._changed:
            if not self._closed:
                self._lost = why
            self._ended = True
            self._changed.notify_all()

    def _why_ended(self) -> str:
        return self._lost or "this source was closed"

    # ── for the reader ───────────────────────────────────────────────────

    def read(self, stream: str = "frames", timeout: Optional[float] = None) -> Any:
        """The oldest unread message of *stream* (a Frame, or a Raw for
        "raw"), waiting for one. TimeoutError after *timeout* seconds;
        Disconnected when the source has ended."""
        message = self._next(stream, timeout)
        if message is None:
            raise Disconnected(self._why_ended())
        return message

    def frames(self) -> Iterator[Frame]:
        """Each hand frame as it arrives. Ends when this source is closed;
        raises Disconnected when the app or the receiver goes away."""
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
        """The oldest unread message; None once this source is closed."""
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

    # ── what each kind of source answers itself ──────────────────────────

    def devices(self, timeout: Optional[float] = None) -> List[Device]:
        """The gloves known now, sending or not."""
        raise NotImplementedError

    def subscribe(self, stream: str = "frames", *, devices: Optional[Sequence[str]] = None,
                  hands: Sequence[str] = protocol.HANDS, rate_hz: float = 0.0,
                  bones: bool = True, timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream; nothing is delivered before."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
