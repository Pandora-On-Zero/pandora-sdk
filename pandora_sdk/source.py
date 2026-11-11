"""What both ways of getting hands share: the errors, and Source, which keeps
what arrives and hands it out, and names what can be sent to a glove.

A Source is fed by one background thread (the socket's, or the receiver's).
Per stream it keeps the newest message of each glove (latest()) and a short
queue of unread ones (read(), frames(), raw(), debug()); when the queue is
full the oldest is dropped and counted, so a slow reader gets fresh hands, not
a growing backlog. A stream given a callback (on()) skips the queue: each
message is handed to the callback on that thread as it arrives.

The commands for a glove (play_rom, play_rtp, stop, set_hall_range) and its
calibration bounds have the same names and arguments in both modes; each mode
answers them its own way.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import (Any, Callable, Deque, Dict, Iterator, List, Optional, Self, Sequence, Union,
                    overload)

from pandora_core import protocol
from pandora_core.packet_layout import HAPTIC_MOTOR_COUNT
from pandora_core.protocol import Calibration, Debug, Device, Frame, Raw, Subscription

Message = Union[Frame, Raw, Debug]

_log = logging.getLogger("pandora_sdk")


class PandoraError(Exception):
    """What this package raises itself. A message the protocol does not allow
    is pandora_core's ProtocolError; a wait that ran out is TimeoutError."""


class ConnectionFailed(PandoraError):
    """Nothing answered at the address, or there is no receiver to open.
    *code* says which, for a program to act on (CONNECTION_FAILED_CODES);
    the error from the OS or the socket library, when there was one, is the
    exception's __cause__."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class Disconnected(PandoraError):
    """The connection ended: the app quit, the receiver was unplugged, or
    this source was closed. *code* says which (DISCONNECTED_CODES); the
    error that ended it, when there was one (the port's OSError, what a
    callback raised), is the exception's __cause__."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# ConnectionFailed.code: why nothing could be opened.
CONNECTION_FAILED_CODES = {
    "no_receiver": "no receiver is plugged into this computer",
    "several_receivers": "more than one receiver is plugged in, and no port was named",
    "in_use": "another program holds the receiver's port (the app, when it runs)",
    "open_failed": "the receiver's port would not open for another reason",
    "app_not_running": "nothing answered at the app's address: it is not running, or its "
                       "Protocol adapter is off",
    "not_the_app": "something answered at the address but sent no hello",
}

# Disconnected.code: why a source ended.
DISCONNECTED_CODES = {
    "closed": "close() was called",
    "unplugged": "the receiver was unplugged",
    "read_failed": "reading the receiver's port failed for another reason",
    "pipeline_failed": "a glove's pipeline raised (without the app)",
    "app_closed": "the app closed the connection (it quit, or turned Protocol off)",
    "connection_lost": "the connection to the app broke",
    "callback_failed": "a callback given to on() raised",
}


class CallbackFailed(PandoraError):
    """Inside this package: a callback given to Source.on() raised, which
    ends the source."""


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
        # Why the source ended, when it was not close(): in words, its
        # Disconnected code, and the error that ended it.
        self._lost = ""
        self._lost_code = ""
        self._lost_cause: Optional[BaseException] = None
        self._ended = False
        # Messages discarded unread because their queue was full.
        self.dropped = 0
        # Per stream, the function given each message instead of the queue (on()).
        self._callbacks: Dict[str, Callable[[Any], object]] = {}

    # ── for the thread that feeds it ─────────────────────────────────────

    def _deliver(self, stream: str, message: Message) -> None:
        """Keep *message*, or hand it to the stream's callback. CallbackFailed
        when that raised: the feeding thread ends the source with it."""
        with self._changed:
            callback = self._callbacks.get(stream)
            if callback is None:
                unread = self._unread[stream]
                if len(unread) == unread.maxlen:
                    self.dropped += 1
                unread.append(message)
            self._latest[stream][message.device] = message
            self._changed.notify_all()
        if callback is not None:
            # Outside the lock, so the callback can ask this source anything.
            try:
                callback(message)
            except Exception as failed:
                _log.exception("the callback for %s raised", stream)
                raise CallbackFailed(f"the callback for {stream} raised {failed!r}") from failed

    def _end(self, why: str, code: str, cause: Optional[BaseException] = None) -> None:
        """The feeding thread is done; *why* (with its Disconnected *code*
        and the error that ended it) unless close() was called."""
        with self._changed:
            if not self._closed:
                self._lost, self._lost_code, self._lost_cause = why, code, cause
            self._ended = True
            self._changed.notify_all()

    def _disconnected(self) -> Disconnected:
        """The Disconnected that says why this source ended."""
        if not self._lost:
            return Disconnected("this source was closed", "closed")
        error = Disconnected(self._lost, self._lost_code)
        error.__cause__ = self._lost_cause
        return error

    # ── for the reader ───────────────────────────────────────────────────

    @property
    def ended(self) -> bool:
        """Whether this source has ended (closed, or lost: the app went away,
        the receiver was unplugged, a callback raised), without waiting. For
        a program that takes every message through on() and reads nothing."""
        with self._changed:
            return self._closed or self._ended

    def wait_ended(self, timeout: Optional[float] = None) -> bool:
        """Wait until this source has ended, *timeout* seconds at most (None
        for as long as it takes); whether it has. end_error then says why."""
        with self._changed:
            return self._changed.wait_for(lambda: self._closed or self._ended, timeout)

    @property
    def end_error(self) -> Optional[Disconnected]:
        """Why this source ended, as the Disconnected reading it would raise
        (its code, and the error that ended it as __cause__); None while it
        runs."""
        with self._changed:
            return self._disconnected() if self._closed or self._ended else None

    def on(self, stream: str, callback: Optional[Callable[[Any], object]]) -> None:
        """Call *callback* with each message of *stream* as it arrives,
        instead of queueing it; None goes back to queueing. For a program
        that must not wait on a queue.

        The callback runs on the thread that reads the app's socket or the
        receiver, so nothing else arrives until it returns: keep it short.
        latest() still has its messages; read() and the iterators get none
        of them. If it raises, the source ends: reading raises Disconnected
        with its error, and the error is logged. Subscribing is still what
        makes messages arrive."""
        if stream not in self._unread:
            raise ValueError(f"no stream {stream!r}; there is {list(self._unread)}")
        with self._changed:
            if callback is None:
                self._callbacks.pop(stream, None)
            else:
                self._callbacks[stream] = callback

    def read(self, stream: str = "frames", timeout: Optional[float] = None) -> Any:
        """The oldest unread message of *stream* (a Frame, or a Raw for
        "raw"), waiting for one. TimeoutError after *timeout* seconds;
        Disconnected when the source has ended."""
        message = self._next(stream, timeout)
        if message is None:
            raise self._disconnected()
        return message

    def frames(self) -> Iterator[Frame]:
        """Each hand frame as it arrives. Ends when this source is closed;
        raises Disconnected when the app or the receiver goes away."""
        return self._each("frames")

    def raw(self) -> Iterator[Raw]:
        """Each raw message as it arrives; ends as frames() does."""
        return self._each("raw")

    def debug(self) -> Iterator[Debug]:
        """Each debug message (the pipeline's named values for one glove) as
        it arrives; ends as frames() does."""
        return self._each("debug")

    def latest(self, glove: str, stream: str = "frames") -> Any:
        """The newest message of *stream* from *glove*, read or not; None
        before its first. *glove* is a device id, or "left" or "right" for the glove on
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
                raise self._disconnected()
            return None

    # ── what each kind of source answers itself ──────────────────────────

    def devices(self, timeout: Optional[float] = None) -> List[Device]:
        """The gloves known now, sending or not."""
        raise NotImplementedError

    def subscribe(self, stream: str = "frames", *, devices: Optional[Sequence[str]] = None,
                  hands: Sequence[str] = protocol.HANDS, rate_hz: float = 0.0,
                  bones: bool = True, keys: Sequence[str] = ("*",), cancelled: bool = False,
                  timeout: Optional[float] = None) -> Subscription:
        """Ask for a stream; nothing is delivered before. *cancelled* (raw
        only) adds each sensor's counts after earth-field cancellation to
        every Raw: hall_cancelled, thumb_distal_cancelled, earth_cancelled."""
        raise NotImplementedError

    # ── commands for a glove ─────────────────────────────────────────────
    #
    # Each names one glove by its id and returns once the command went to
    # the receiver. ServerError, with the protocol's code, when it could not:
    # "unknown_device" for a glove that is not connected, "bad_haptics" or
    # "bad_hall_range" for values the protocol does not allow, "send_failed"
    # when it could not be sent. Motors are in the order thumb, index, middle,
    # ring, pinky, back of the hand.

    def play_rom(self, device: str, effects: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        """Play one effect of the motor driver's library (DRV2625, Immersion
        Library A) per motor: six ids, 1 to 123, 0 leaving that motor alone."""
        raise NotImplementedError

    def play_rtp(self, device: str, amplitudes: Sequence[int], durations_ms: Sequence[int],
                 timeout: Optional[float] = None) -> None:
        """Drive the motors directly: per motor an amplitude (1 to 127
        drives, 128 to 255 brakes, 0 stops) held for a duration in
        milliseconds (0 to 65535, 0 the firmware's default). To stream, send
        each frame with a duration longer than the gap to the next."""
        raise NotImplementedError

    def stop(self, device: str, timeout: Optional[float] = None) -> None:
        """Stop every motor of *device* at once: an all-zero RTP frame."""
        self.play_rtp(device, [0] * HAPTIC_MOTOR_COUNT, [0] * HAPTIC_MOTOR_COUNT, timeout)

    def set_hall_range(self, device: str, hall_range: int,
                       timeout: Optional[float] = None) -> str:
        """Put every Hall sensor of *device* on *hall_range*: 0 is +-5 mT, 1
        is +-50 mT. Its calibration is rescaled to it, and its raw messages
        say it from then on. Returns what the change did to its calibration,
        in words for a person ("" when there is nothing to say)."""
        raise NotImplementedError

    # ── calibration bounds ───────────────────────────────────────────────

    @overload
    def calibration(self, device: str, timeout: Optional[float] = None) -> Calibration: ...

    @overload
    def calibration(self, device: None = None,
                    timeout: Optional[float] = None) -> Dict[str, Calibration]: ...

    def calibration(self, device: Optional[str] = None, timeout: Optional[float] = None
                    ) -> Union[Calibration, Dict[str, Calibration]]:
        """The Hall bounds *device*'s curl is normalised between now, or with
        no device named, every glove's in devices() by id. ServerError
        "unknown_device" for a glove not known."""
        if device is not None:
            return self._calibration_of(device, timeout)
        return {known.id: self._calibration_of(known.id, timeout)
                for known in self.devices(timeout)}

    def latest_calibration(self, device: str) -> Optional[Calibration]:
        """The newest bounds known for *device* without asking; None when
        there are none yet."""
        raise NotImplementedError

    def _calibration_of(self, device: str, timeout: Optional[float]) -> Calibration:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
