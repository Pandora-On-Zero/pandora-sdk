"""A made-up receiver: what the code needs of a serial port, fed by the test,
and tracking packets built from the packet's layout."""
import struct
import threading
import time
from typing import List

from pandora_core.packet_layout import TRACKING_FORMAT
from pandora_core.serial_framing import encode_frame


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
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.is_open = False
