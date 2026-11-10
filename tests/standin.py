"""A stand-in for the app's protocol server, written from PROTOCOL.md alone.

It imports nothing of pandora_core on purpose: the client decodes with that
package, so a server that encoded with it too could not catch a mistake the
package makes on both sides. The messages below are typed out from the spec.
"""
import json
import threading
import time
from typing import Any, Callable, Dict, List

import msgpack
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

HELLO = {"type": "hello", "protocol": 1, "server": "01glove-app", "version": "0.2.26",
         "capabilities": ["frames", "raw", "devices"]}

DEVICES = {"type": "devices", "devices": [
    {"id": "NRF_GLOVE_E5D1", "hand": "right", "name": "Lab right", "connected": True},
    {"id": "NRF_GLOVE_A48D", "hand": "left", "name": "", "connected": False}]}

# One hand frame, key by key from the spec's table. Fingers thumb to pinky.
FRAME: Dict[str, Any] = {
    "t": "frame", "v": 1, "seq": 41, "time_us": 5_000_000,
    "device": "NRF_GLOVE_E5D1", "hand": "right", "mode": "FULL",
    "curl": [[0.125, 0.25, 0.5], [0.5, 0.25, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 0.0],
             [0.75, 0.5, 0.0]],
    "splay": [0.0, 0.5, 0.0, -0.25, -1.0],
    "angles_deg": [[10.0, 20.0, 30.0, 5.0], [45.0, 50.0, 25.0, 7.5], [0.0, 0.0, 0.0, 0.0],
                   [90.0, 100.0, 60.0, -4.0], [70.0, 55.0, 30.0, -15.0]],
    "wrist_deg": [1.5, -2.5, 90.0], "trigger": False, "grip": True}

# One raw message, key by key from the spec's table.
RAW: Dict[str, Any] = {
    "t": "raw", "v": 1, "seq": 41, "time_us": 5_000_000,
    "device": "NRF_GLOVE_E5D1", "hand": "right", "glove_ms": 123456,
    "hall": [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]], [[13, 14, 15], [16, 17, 18]],
             [[19, 20, 21], [22, 23, 24]], [[25, 26, 27], [28, 29, 30]]],
    "thumb_distal": [31, 32, 33], "reference": None,
    "hall_range": 1, "sensor_ranges": {"Index/prox": 0},
    "wrist_deg": [1.0, 2.0, 3.0],
    "finger_imu_deg": [None, [4.0, 5.0, 6.0], None, None, None],
    "buttons": {"trigger": False, "a": True, "b": False, "system": False, "power": False,
                "stick": False},
    "joystick": [100, -200], "battery_pct": 87, "charging": False}


def _pack(message: Dict[str, Any]) -> bytes:
    packed = msgpack.packb(message, use_bin_type=True)
    assert isinstance(packed, bytes)
    return packed


def frame(**changes: Any) -> bytes:
    """FRAME as its binary message, with *changes* to its keys."""
    return _pack({**FRAME, **changes})


def raw(**changes: Any) -> bytes:
    return _pack({**RAW, **changes})


def wait(done: Callable[[], Any], seconds: float = 3.0) -> bool:
    """Poll until *done()* is true; False when it never was."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.005)
    return False


class StandIn:
    """Listens on a free localhost port and answers as the spec says, unless
    told otherwise in *replies*."""

    def __init__(self, hello: Dict[str, Any] = HELLO) -> None:
        self.hello = hello
        # Every text message a client sent, parsed, in order.
        self.received: List[Dict[str, Any]] = []
        # Request type -> what to send back instead of the spec's answer: a
        # list of text messages (dicts) and binary ones (bytes).
        self.replies: Dict[str, List[Any]] = {}
        self._connections: List[Any] = []
        self._server = serve(self._serve, "127.0.0.1", 0, compression=None)
        self.port: int = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _serve(self, connection: Any) -> None:
        self._connections.append(connection)
        try:
            connection.send(json.dumps(self.hello))
            for text in connection:
                message = json.loads(text)
                self.received.append(message)
                for reply in self._answers(message):
                    connection.send(reply if isinstance(reply, bytes) else json.dumps(reply))
        except ConnectionClosed:
            pass

    def _answers(self, message: Dict[str, Any]) -> List[Any]:
        kind = message["type"]
        if kind in self.replies:
            return self.replies[kind]
        if kind == "devices":
            return [DEVICES]
        if kind == "subscribe":
            granted = {"stream": "frames", "hands": ["left", "right"], "rate_hz": 0, **message,
                       "type": "subscribed"}
            if granted["stream"] == "frames":
                granted.setdefault("bones", True)
            return [granted]
        return []

    def of_type(self, kind: str) -> List[Dict[str, Any]]:
        return [m for m in self.received if m["type"] == kind]

    def send(self, *messages: Any) -> None:
        """To every connected client: dicts as text, bytes as binary."""
        for connection in list(self._connections):
            for message in messages:
                connection.send(message if isinstance(message, bytes) else json.dumps(message))

    def drop(self) -> None:
        """Hang up on every client, as the app does when it quits."""
        for connection in list(self._connections):
            connection.close()

    def close(self) -> None:
        self.drop()
        self._server.shutdown()
