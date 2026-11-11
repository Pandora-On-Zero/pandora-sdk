"""A made-up receiver: what the code needs of a serial port, fed by the test,
and tracking packets built from the packet's layout."""
import struct
import threading
import time
from typing import List, Tuple

from pandora_core.packet_layout import TRACKING_FORMAT
from pandora_core.serial_framing import decode_frame, encode_frame


def packet_line(device: str, glove_ms: int = 1000, hall: int = 100) -> bytes:
    """One tracking packet from *device* as the receiver sends it: a framed
    line. Hall sensors count up from *hall*, so at 100 the index finger's
    proximal sensor (the fifth triple) reads 112, 113, 114. No finger IMUs."""
    payload = struct.pack(
        TRACKING_FORMAT, 1, device.encode().ljust(16, b"\0"), glove_ms, 87, 0,
        *range(hall, hall + 33), 5, -5, 0, 100, 200, 300, 0, 7, 8, 9, 4000, *([0] * 15))
    return encode_frame(payload)


class Link:
    """What the reader needs of a serial port, fed by the test."""
    is_open = True

    def __init__(self) -> None:
        self._arrived = bytearray()
        self._lock = threading.Lock()
        self.unplugged = False
        # Writes fail as a port's do when the receiver stops taking them.
        self.refuses_writes = False
        # Every write to the port, in order.
        self.written: List[bytes] = []

    def feed(self, *lines: bytes) -> None:
        with self._lock:
            for line in lines:
                self._arrived += line

    def read(self, count: int) -> bytes:
        if self.unplugged:
            raise OSError(5, "Input/output error")
        with self._lock:
            taken = bytes(self._arrived[:count])
            del self._arrived[:count]
        if not taken:
            time.sleep(0.005)
        return taken

    @property
    def in_waiting(self) -> int:
        return len(self._arrived)

    def write(self, data: bytes) -> int:
        if self.refuses_writes:
            raise OSError("Write timeout")
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.is_open = False


def sent(link: Link, kind: int, layout: str) -> List[Tuple]:
    """The commands of type *kind* written to *link*, unpacked, in order."""
    payloads = [decode_frame(line) for line in link.written]
    return [struct.unpack(layout, payload) for payload in payloads
            if payload is not None and payload[0] == kind]
