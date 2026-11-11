"""A stand-in for the app's protocol server, written from PROTOCOL.md alone.

It imports nothing of pandora_core_rs on purpose: the client decodes with that
package, so a server that encoded with it too could not catch a mistake the
package makes on both sides. The messages below are typed out from the spec,
and requests are checked against its tables as the spec says the server
checks them.
"""
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import msgpack
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

HELLO = {"type": "hello", "protocol": 1, "server": "01glove-app", "version": "0.2.26",
         "capabilities": ["frames", "raw", "devices", "haptics", "hall_range", "calibration",
                          "debug", "raw_cancelled"]}

DEVICES = {"type": "devices", "devices": [
    {"id": "NRF_GLOVE_E5D1", "hand": "right", "name": "Lab right", "connected": True},
    {"id": "NRF_GLOVE_A48D", "hand": "left", "name": "", "connected": False}]}

# Given for a key to leave it out of a message, as an older server does.
ABSENT = object()

# One hand frame, key by key from the spec's table. Fingers thumb to pinky.
FRAME: Dict[str, Any] = {
    "t": "frame", "v": 1, "seq": 41, "time_us": 5_000_000,
    "wall_time_us": 1_762_000_000_000_000,
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
    "wall_time_us": 1_762_000_000_000_000,
    "device": "NRF_GLOVE_E5D1", "hand": "right", "glove_ms": 123456,
    "hall": [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]], [[13, 14, 15], [16, 17, 18]],
             [[19, 20, 21], [22, 23, 24]], [[25, 26, 27], [28, 29, 30]]],
    "thumb_distal": [31, 32, 33], "reference": None,
    "hall_range": 1, "sensor_ranges": {"Index/prox": 0},
    "wrist_deg": [1.0, 2.0, 3.0],
    "finger_imu_deg": [None, [4.0, 5.0, 6.0], None, None, None],
    "buttons": {"trigger": False, "a": True, "b": False, "system": False, "power": False,
                "stick": False},
    "joystick": [100, -200], "battery_pct": 87, "charging": False,
    "battery_mv": 3987, "usb_power": True, "gatt_commands": True}

# The raw keys an older server does not send (the spec: none of them, then).
RAW_POWER_KEYS = ("battery_mv", "usb_power", "gatt_commands")

# The keys a raw message also carries when the subscription said cancelled,
# from the spec's second raw table: per finger the counts after earth-field
# cancellation, shaped as hall, and whether any sensor was cancelled.
CANCELLED: Dict[str, Any] = {
    "hall_cancelled": [[[-1, 2, 3], [4, -5, 6]], [[7, 8, -9], [10, 11, 120]],
                       [[13, 14, 15], [16, 17, 18]], [[19, 20, 21], [22, 23, 24]],
                       [[25, 26, 27], [28, 29, 30]]],
    "thumb_distal_cancelled": [31, -32, 33], "earth_cancelled": True}

# One debug message, key by key from the spec's table, with the value names
# it gives as examples.
DEBUG: Dict[str, Any] = {
    "t": "debug", "v": 1, "seq": 41, "time_us": 5_000_000,
    "wall_time_us": 1_762_000_000_000_000,
    "device": "NRF_GLOVE_E5D1", "hand": "right",
    "values": {"mag.yaw": 12.5, "curl.Index.prox": 0.25, "raw.link.rssi_dbm": -61}}

# The spec's calibration example.
CALIBRATION: Dict[str, Any] = {
    "type": "calibration", "device": "NRF_GLOVE_E5D1", "hall_range": 0, "extrapolated": False,
    "calibrations": {"Index": {
        "prox_min": {"x": -812, "y": 140, "z": 2210}, "prox_max": {"x": 35, "y": -12, "z": 4020},
        "meta_min": {"x": 410, "y": -96, "z": 1388}, "meta_max": {"x": 12, "y": 8, "z": 2950}}}}

# The note the spec's set_hall_range reply carries.
HALL_RANGE_NOTE = "curl calibration extrapolated to the new range"

# Motors per glove: thumb, index, middle, ring, pinky, back of the hand.
MOTORS = 6


def _pack(message: Dict[str, Any]) -> bytes:
    packed = msgpack.packb({k: v for k, v in message.items() if v is not ABSENT},
                           use_bin_type=True)
    assert isinstance(packed, bytes)
    return packed


def frame(**changes: Any) -> bytes:
    """FRAME as its binary message, with *changes* to its keys; a key given
    ABSENT is left out."""
    return _pack({**FRAME, **changes})


def raw(**changes: Any) -> bytes:
    return _pack({**RAW, **changes})


def raw_cancelled(**changes: Any) -> bytes:
    """RAW with the CANCELLED keys, as sent to a subscription that asked for
    them, with *changes* to its keys."""
    return _pack({**RAW, **CANCELLED, **changes})


def debug(**changes: Any) -> bytes:
    return _pack({**DEBUG, **changes})


def calibration(**changes: Any) -> Dict[str, Any]:
    """CALIBRATION, a text message, with *changes* to its keys."""
    return {**CALIBRATION, **changes}


def error(code: str, message: str) -> Dict[str, Any]:
    return {"type": "error", "code": code, "message": message}


def wait(done: Callable[[], Any], seconds: float = 3.0) -> bool:
    """Poll until *done()* is true; False when it never was."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.005)
    return False


def _per_motor(value: Any, high: int) -> bool:
    """One int per motor, each 0 to *high*, as the haptics table says."""
    return (isinstance(value, list) and len(value) == MOTORS
            and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= high
                    for v in value))


def _bad_device(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    device = message.get("device")
    if not isinstance(device, str) or not device:
        return error("bad_device", "device is the id of one glove")
    return None


def _bad_haptics(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ("effects" in message) == ("amplitudes" in message):
        return error("bad_haptics", "a haptics request has effects or amplitudes")
    if "effects" in message:
        good = _per_motor(message["effects"], 123)
    else:
        good = _per_motor(message["amplitudes"], 255) and _per_motor(
            message.get("durations_ms"), 65535)
    return None if good else error("bad_haptics", "six ints per motor field, in range")


def _bad_hall_range(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = message.get("hall_range")
    if isinstance(value, bool) or value not in (0, 1):
        return error("bad_hall_range", "hall_range is 0 (5 mT) or 1 (50 mT)")
    return None


def _bad_subscribe(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The subscribe fields this stand-in checks; the others it grants as
    asked."""
    if not isinstance(message.get("cancelled", False), bool):
        return error("bad_cancelled", "cancelled is true or false")
    return None


def _not_connected(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    connected = [d["id"] for d in DEVICES["devices"] if d["connected"]]
    if message["device"] not in connected:
        return error("unknown_device", f"no glove {message['device']!r} is connected")
    return None


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
        if kind == "hello":
            return []
        if kind == "devices":
            return [DEVICES]
        if kind == "subscribe":
            refused = _bad_subscribe(message)
            if refused is not None:
                return [refused]
            granted = {"stream": "frames", "hands": ["left", "right"], "rate_hz": 0, **message,
                       "type": "subscribed"}
            if granted["stream"] == "frames":
                granted.setdefault("bones", True)
            if granted["stream"] == "debug":
                granted.setdefault("keys", ["*"])
            if granted["stream"] == "raw":
                granted.setdefault("cancelled", False)
            else:
                granted.pop("cancelled", None)  # raw only
            return [granted]
        if kind == "calibration":
            known = [d["id"] for d in DEVICES["devices"]]
            if "device" not in message:
                return [calibration(device=device) for device in known]
            refused = _bad_device(message)
            if refused is None and message["device"] not in known:
                refused = error("unknown_device", f"the app has not seen {message['device']!r}")
            return [refused or calibration(device=message["device"])]
        if kind == "haptics":
            refused = _bad_device(message) or _bad_haptics(message) or _not_connected(message)
            return [refused or {"type": "haptics_sent", "device": message["device"]}]
        if kind == "set_hall_range":
            refused = _bad_device(message) or _bad_hall_range(message) \
                or _not_connected(message)
            return [refused or {"type": "hall_range", "device": message["device"],
                                "hall_range": message["hall_range"], "note": HALL_RANGE_NOTE}]
        return [error("unknown_type", f"this server has no {kind!r} request")]

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
