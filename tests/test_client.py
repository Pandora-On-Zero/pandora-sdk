"""The client against a stand-in server that was written from the spec
(tests/standin.py), so these also check pandora_core's decoding against
PROTOCOL.md."""
import threading

import msgpack
import pytest
from standin import HELLO, RAW, StandIn, frame, raw, wait

import pandora_sdk
from pandora_sdk import Device, Subscription


@pytest.fixture
def gloves(app):
    with pandora_sdk.connect(port=app.port, timeout=2.0) as client:
        yield client


def test_the_client_says_hello_with_its_name(app):
    with pandora_sdk.connect(port=app.port, name="my-robot"):
        assert wait(lambda: app.received)

    assert app.received[0] == {"type": "hello", "protocol": 1, "client": "my-robot"}


def test_what_the_app_said_in_its_hello_is_kept(gloves):
    assert gloves.server == pandora_sdk.Server(
        name="01glove-app", version="0.2.26", protocol=1,
        capabilities=("frames", "raw", "devices"))


def test_an_app_on_another_protocol_version_is_refused_and_told():
    newer = StandIn({**HELLO, "protocol": 2})
    try:
        with pytest.raises(pandora_sdk.ProtocolError) as refused:
            pandora_sdk.connect(port=newer.port)

        assert refused.value.code == "unsupported_protocol"
        assert wait(lambda: newer.of_type("error"))
        assert newer.of_type("error")[0]["code"] == "unsupported_protocol"
    finally:
        newer.close()


def test_with_no_app_listening_the_error_says_how_to_switch_the_protocol_on(free_port):
    with pytest.raises(pandora_sdk.ConnectionFailed, match="Protocol"):
        pandora_sdk.connect(port=free_port, timeout=1.0)


def test_devices_asks_the_app_for_its_gloves(gloves):
    assert gloves.devices() == [
        Device("NRF_GLOVE_E5D1", "right", "Lab right", True),
        Device("NRF_GLOVE_A48D", "left", "", False)]


def test_subscribe_sends_what_was_asked_and_returns_what_was_granted(app, gloves):
    granted = gloves.subscribe(hands=["right"], rate_hz=50, bones=False)

    assert app.of_type("subscribe") == [
        {"type": "subscribe", "stream": "frames", "hands": ["right"], "rate_hz": 50.0,
         "bones": False}]
    assert granted == Subscription("frames", None, ("right",), 50.0, False)


def test_a_subscription_can_name_devices(app, gloves):
    granted = gloves.subscribe("raw", devices=["NRF_GLOVE_A48D"])

    assert app.of_type("subscribe")[0]["devices"] == ["NRF_GLOVE_A48D"]
    assert granted.devices == ("NRF_GLOVE_A48D",)


def test_a_request_the_app_refuses_raises_with_the_apps_reason(app, gloves):
    app.replies["subscribe"] = [
        {"type": "error", "code": "bad_rate", "message": "rate_hz is a number from 0 to 1000"}]

    with pytest.raises(pandora_sdk.ServerError, match="rate_hz is a number") as refused:
        gloves.subscribe(rate_hz=50)

    assert refused.value.code == "bad_rate"


def test_a_request_the_app_never_answers_times_out(app, gloves):
    app.replies["devices"] = []

    with pytest.raises(TimeoutError):
        gloves.devices(timeout=0.1)


def test_a_frame_written_from_the_spec_is_read_as_its_values(app, gloves):
    app.send(frame())

    hand = gloves.read()

    assert (hand.device, hand.hand, hand.seq, hand.time_us, hand.mode) == (
        "NRF_GLOVE_E5D1", "right", 41, 5_000_000, "FULL")
    assert hand.curl[0] == (0.125, 0.25, 0.5) and hand.curl[4] == (0.75, 0.5, 0.0)
    assert hand.splay == (0.0, 0.5, 0.0, -0.25, -1.0)
    assert hand.angles_deg[1] == (45.0, 50.0, 25.0, 7.5)
    assert hand.wrist_deg == (1.5, -2.5, 90.0)
    assert (hand.trigger, hand.grip, hand.bones) == (False, True, None)


def test_a_frame_with_bones_carries_all_31(app, gloves):
    bones = [[0.0, 0.5 * i, 0.0, 1.0, 0.0, 0.0, 0.0] for i in range(31)]
    app.send(frame(bones=bones))

    hand = gloves.read()

    assert hand.bones is not None and len(hand.bones) == 31
    assert hand.bones[30] == (0.0, 15.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def test_a_frame_from_a_glove_with_no_orientation_has_no_wrist(app, gloves):
    app.send(frame(wrist_deg=None))

    assert gloves.read().wrist_deg is None


def test_raw_messages_are_read_on_their_own_stream(app, gloves):
    app.send(raw(), frame(seq=42))

    reading = gloves.read("raw")

    assert (reading.device, reading.hand, reading.seq, reading.glove_ms) == (
        "NRF_GLOVE_E5D1", "right", 41, 123456)
    assert reading.hall[1] == ((7, 8, 9), (10, 11, 12))
    assert (reading.thumb_distal, reading.reference) == ((31, 32, 33), None)
    assert (reading.hall_range, reading.sensor_ranges) == (1, {"Index/prox": 0})
    assert reading.finger_imu_deg == (None, (4.0, 5.0, 6.0), None, None, None)
    assert (reading.buttons, reading.joystick) == (RAW["buttons"], (100, -200))
    assert (reading.battery_pct, reading.charging) == (87, False)
    assert gloves.read().seq == 42


def test_latest_is_the_newest_frame_of_a_hand_or_of_a_device(app, gloves):
    app.send(frame(seq=1), frame(seq=2),
             frame(seq=9, device="NRF_GLOVE_A48D", hand="left"))
    assert wait(lambda: gloves.latest("left") is not None)

    assert gloves.latest("right").seq == 2
    assert gloves.latest("NRF_GLOVE_A48D").seq == 9
    assert gloves.latest("NRF_GLOVE_NOPE") is None


def test_a_reader_that_falls_behind_loses_the_oldest_and_can_see_how_many(app):
    with pandora_sdk.connect(port=app.port, buffer=3) as gloves:
        app.send(*[frame(seq=n) for n in range(1, 6)])
        assert wait(lambda: getattr(gloves.latest("right"), "seq", 0) == 5)

        assert [gloves.read().seq for _ in range(3)] == [3, 4, 5]
        assert gloves.dropped == 2


def test_a_read_gives_up_after_its_timeout(gloves):
    with pytest.raises(TimeoutError):
        gloves.read(timeout=0.05)


def test_a_binary_message_of_a_kind_this_version_does_not_know_is_skipped(app, gloves):
    app.send(msgpack.packb({"t": "haptics", "v": 1}), frame())

    assert gloves.read(timeout=2.0).seq == 41


def test_iterating_stops_with_an_error_when_the_app_goes_away(app, gloves):
    app.send(frame())
    frames = gloves.frames()
    assert next(frames).seq == 41

    app.drop()

    with pytest.raises(pandora_sdk.Disconnected):
        next(frames)


def test_closing_the_client_ends_iteration_without_an_error(gloves):
    threading.Timer(0.1, gloves.close).start()

    assert list(gloves.frames()) == []
