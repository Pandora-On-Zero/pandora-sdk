"""Hands straight from a receiver, with no app (pandora_sdk/local.py), against
a made-up serial link. The packets are built here from the tracking packet's
layout; what the pipeline makes of real ones is pandora-core's own tests'
business. These check the plumbing around it."""
import json
import struct
import threading
import time

import pytest
from standin import wait

import pandora_sdk
from pandora_core import receiver
from pandora_core.packet_layout import TRACKING_FORMAT
from pandora_core.serial_framing import encode_frame
from pandora_sdk import Device, Subscription

RIGHT, LEFT, STRANGER = "NRF_GLOVE_E5D1", "NRF_GLOVE_A48D", "NRF_GLOVE_0000"
PROFILES = {RIGHT: {"hand": "right", "name": "Lab right"}, LEFT: {"hand": "left"}}


def packet_line(device: str, glove_ms: int = 1000) -> bytes:
    """One tracking packet from *device* as the receiver sends it: a framed
    line. Hall sensors count up from 100, so the index finger's proximal
    sensor (the fifth triple) reads 112, 113, 114."""
    payload = struct.pack(
        TRACKING_FORMAT, 1, device.encode().ljust(16, b"\0"), glove_ms, 87, 0,
        *range(100, 133), 5, -5, 0, 100, 200, 300, 0, 7, 8, 9, 4000, *([0] * 15))
    return encode_frame(payload)


class Link:
    """What the reader needs of a serial port, fed by the test."""
    is_open = True

    def __init__(self) -> None:
        self._arrived = bytearray()
        self._lock = threading.Lock()
        self.unplugged = False

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

    def close(self) -> None:
        self.is_open = False


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def link():
    return Link()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gloves(link, clock):
    with pandora_sdk.Local(PROFILES, link=link, defaults={}, clock=clock) as opened:
        yield opened


def test_a_gloves_packets_come_out_as_frames_of_the_hand_in_its_profile(link, gloves):
    gloves.subscribe(bones=False)

    link.feed(packet_line(RIGHT), packet_line(RIGHT))

    first, second = gloves.read(timeout=2.0), gloves.read(timeout=2.0)
    assert (first.device, first.hand, first.seq, second.seq) == (RIGHT, "right", 1, 2)
    assert first.time_us == 100_000_000
    assert len(first.curl) == 5 and len(first.angles_deg) == 5 and first.bones is None


def test_frames_carry_the_skeleton_unless_told_not_to(link, gloves):
    gloves.subscribe()

    link.feed(packet_line(RIGHT))

    assert len(gloves.read(timeout=2.0).bones) == 31


def test_nothing_is_delivered_before_subscribing_as_with_the_app(link, gloves):
    link.feed(packet_line(RIGHT))
    assert wait(lambda: gloves.devices())

    assert gloves.latest("right") is None
    with pytest.raises(TimeoutError):
        gloves.read(timeout=0.05)


def test_subscribe_returns_what_it_will_send(gloves):
    assert gloves.subscribe(hands=["left"], rate_hz=50, bones=False) == Subscription(
        "frames", None, ("left",), 50.0, False)


def test_a_subscription_that_cannot_be_met_is_refused_as_the_app_refuses_it(gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.subscribe(rate_hz=-1)

    assert refused.value.code == "bad_rate"


def test_a_subscription_by_hand_leaves_the_other_glove_out(link, gloves):
    gloves.subscribe(hands=["left"], bones=False)

    link.feed(packet_line(RIGHT), packet_line(LEFT))

    assert gloves.read(timeout=2.0).device == LEFT
    assert gloves.latest("right") is None


def test_a_stream_is_held_to_the_rate_asked_for(link, gloves, clock):
    """A glove every 5 ms for 200 ms, a subscription for 50 a second."""
    gloves.subscribe(rate_hz=50, bones=False)

    for sent in range(1, 41):
        link.feed(packet_line(RIGHT, glove_ms=sent))
        assert wait(lambda: gloves.packets == sent)
        clock.now += 0.005

    received = []
    while True:
        try:
            received.append(gloves.read(timeout=0))
        except TimeoutError:
            break
    assert 9 <= len(received) <= 11


def test_the_raw_stream_carries_the_gloves_own_readings(link, gloves):
    gloves.subscribe("raw")

    link.feed(packet_line(RIGHT, glove_ms=4321))

    reading = gloves.read("raw", timeout=2.0)
    assert (reading.device, reading.hand, reading.seq, reading.glove_ms) == (
        RIGHT, "right", 1, 4321)
    assert reading.hall[1][0] == (112, 113, 114)
    assert (reading.reference, reading.battery_pct) == ((7, 8, 9), 87)


def test_a_glove_with_no_profile_is_left_out_and_named(link, gloves):
    gloves.subscribe(bones=False)

    link.feed(packet_line(STRANGER), packet_line(RIGHT))

    assert gloves.read(timeout=2.0).device == RIGHT
    assert gloves.unprofiled == {STRANGER}


def test_devices_lists_the_gloves_heard_with_hand_name_and_whether_they_still_send(
        link, gloves, clock):
    link.feed(packet_line(RIGHT))
    assert wait(lambda: gloves.packets == 1)
    clock.now += 3.0
    link.feed(packet_line(LEFT))
    assert wait(lambda: gloves.packets == 2)

    assert gloves.devices() == [Device(LEFT, "left", "", True),
                                Device(RIGHT, "right", "Lab right", False)]


def test_reading_stops_with_an_error_when_the_receiver_is_unplugged(link, gloves):
    gloves.subscribe(bones=False)
    link.feed(packet_line(RIGHT))
    assert gloves.read(timeout=2.0).seq == 1

    link.unplugged = True

    with pytest.raises(pandora_sdk.Disconnected, match="unplugged"):
        gloves.read(timeout=2.0)


def test_closing_ends_iteration_and_closes_the_port(link, clock):
    opened = pandora_sdk.Local(PROFILES, link=link, defaults={}, clock=clock)
    threading.Timer(0.1, opened.close).start()

    assert list(opened.frames()) == []
    assert wait(lambda: not link.is_open)


def test_profiles_can_be_a_folder_of_one_file_per_glove(tmp_path, link):
    (tmp_path / f"{LEFT}.json").write_text(json.dumps({"hand": "left", "name": "Spare"}),
                                           encoding="utf-8")

    with pandora_sdk.Local(tmp_path, link=link, defaults={}) as gloves:
        link.feed(packet_line(LEFT))
        assert wait(lambda: gloves.devices())

        assert gloves.devices() == [Device(LEFT, "left", "Spare", True)]


def test_with_no_receiver_plugged_in_the_error_says_so(monkeypatch):
    monkeypatch.setattr(receiver, "find_receivers", lambda: [])

    with pytest.raises(pandora_sdk.ConnectionFailed, match="no receiver"):
        pandora_sdk.open_receiver(PROFILES, defaults={})


def test_with_two_receivers_the_error_names_them(monkeypatch):
    monkeypatch.setattr(receiver, "find_receivers", lambda: [
        receiver.ReceiverPort("COM7", "A", "01 Receiver"),
        receiver.ReceiverPort("COM9", "B", "01 Receiver")])

    with pytest.raises(pandora_sdk.ConnectionFailed, match="COM7, COM9"):
        pandora_sdk.open_receiver(PROFILES, defaults={})


def test_a_receiver_the_app_holds_is_reported_as_in_use(monkeypatch):
    def _held(port, **_kwargs):
        raise OSError(f"could not open port '{port}': PermissionError(13, 'Access is denied.')")

    monkeypatch.setattr(receiver, "open_port", _held)

    with pytest.raises(pandora_sdk.ConnectionFailed, match="COM7 is in use"):
        pandora_sdk.open_receiver(PROFILES, port="COM7", defaults={})


def test_without_the_pipelines_default_files_it_refuses_to_guess(link, monkeypatch):
    """A pandora-core from before it carried them; hands made without them
    would be wrong, silently."""
    monkeypatch.setattr("pandora_sdk.local.shipped_defaults", lambda: {})

    with pytest.raises(pandora_sdk.PandoraError, match="default files"):
        pandora_sdk.Local(PROFILES, link=link)
