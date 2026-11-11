"""The client against a stand-in server that was written from the spec
(tests/standin.py), so these also check pandora_core's decoding against
PROTOCOL.md."""
import threading
from typing import Any, cast

import msgpack
import pytest
from standin import (ABSENT, CANCELLED, DEBUG, HALL_RANGE_NOTE, HELLO, RAW, RAW_POWER_KEYS,
                     StandIn, calibration, debug, frame, raw, raw_cancelled, wait)

import pandora_sdk
from pandora_sdk import Calibration, Device, Subscription

RIGHT, LEFT = "NRF_GLOVE_E5D1", "NRF_GLOVE_A48D"


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
        capabilities=("frames", "raw", "devices", "haptics", "hall_range", "calibration",
                      "debug", "raw_cancelled"))


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


def test_with_no_app_listening_the_error_has_its_code_and_the_os_error_as_its_cause(free_port):
    with pytest.raises(pandora_sdk.ConnectionFailed) as failed:
        pandora_sdk.connect(port=free_port, timeout=1.0)

    assert failed.value.code == "app_not_running"
    assert failed.value.__cause__ is not None


class Silent(StandIn):
    """A websocket server that never says hello: not the app."""

    def _serve(self, connection: Any) -> None:
        for _ in connection:
            pass


def test_a_server_that_sends_no_hello_has_its_code():
    silent = Silent()
    try:
        with pytest.raises(pandora_sdk.ConnectionFailed) as failed:
            pandora_sdk.connect(port=silent.port, timeout=0.3)
    finally:
        silent.close()

    assert failed.value.code == "not_the_app"


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
    assert (reading.battery_mv, reading.usb_power, reading.gatt_commands) == (3987, True, True)
    assert gloves.read().seq == 42


def test_a_raw_message_from_an_older_app_has_no_battery_voltage_or_power_flags(app, gloves):
    app.send(raw(**{key: ABSENT for key in RAW_POWER_KEYS}))

    reading = gloves.read("raw")

    assert (reading.battery_mv, reading.usb_power, reading.gatt_commands) == (None, None, None)
    assert reading.battery_pct == 87


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


def test_an_app_that_hangs_up_has_its_code(app, gloves):
    app.drop()

    with pytest.raises(pandora_sdk.Disconnected) as lost:
        gloves.read(timeout=2.0)

    assert lost.value.code == "app_closed"


def test_a_client_whose_app_hung_up_has_ended_without_being_read(app, gloves):
    assert gloves.ended is False

    app.drop()

    assert gloves.wait_ended(2.0) is True
    assert gloves.end_error is not None and gloves.end_error.code == "app_closed"


def test_reading_a_closed_client_has_its_code(gloves):
    gloves.close()

    with pytest.raises(pandora_sdk.Disconnected) as lost:
        gloves.read(timeout=2.0)

    assert lost.value.code == "closed"


# ── wall time ────────────────────────────────────────────────────────────────

def test_frames_and_raw_messages_carry_the_apps_wall_time(app, gloves):
    app.send(frame(), raw())

    assert gloves.read().wall_time_us == 1_762_000_000_000_000
    assert gloves.read("raw").wall_time_us == 1_762_000_000_000_000


def test_from_an_app_that_sends_no_wall_time_it_is_none(app, gloves):
    app.send(frame(wall_time_us=ABSENT), raw(wall_time_us=ABSENT))

    assert gloves.read().wall_time_us is None
    assert gloves.read("raw").wall_time_us is None


# ── commands for a glove ─────────────────────────────────────────────────────

def test_play_rom_sends_one_effect_per_motor_and_returns_once_the_app_passed_it_on(
        app, gloves):
    gloves.play_rom(RIGHT, [0, 0, 47, 0, 0, 0])

    assert app.of_type("haptics") == [
        {"type": "haptics", "device": RIGHT, "effects": [0, 0, 47, 0, 0, 0]}]


def test_play_rtp_sends_an_amplitude_and_a_duration_per_motor(app, gloves):
    gloves.play_rtp(RIGHT, [0, 90, 90, 0, 0, 0], [300] * 6)

    assert app.of_type("haptics") == [
        {"type": "haptics", "device": RIGHT, "amplitudes": [0, 90, 90, 0, 0, 0],
         "durations_ms": [300, 300, 300, 300, 300, 300]}]


def test_stop_sends_the_all_zero_frame_that_stops_every_motor_at_once(app, gloves):
    gloves.stop(RIGHT)

    assert app.of_type("haptics") == [
        {"type": "haptics", "device": RIGHT, "amplitudes": [0] * 6, "durations_ms": [0] * 6}]


def test_a_command_for_a_glove_that_is_not_connected_is_refused_with_the_protocols_code(
        gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.play_rom(LEFT, [1, 0, 0, 0, 0, 0])

    assert refused.value.code == "unknown_device"


def test_haptics_the_spec_does_not_allow_are_refused_by_the_app(gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.play_rom(RIGHT, [200, 0, 0, 0, 0, 0])

    assert refused.value.code == "bad_haptics"


def test_a_command_the_app_could_not_send_raises_send_failed(app, gloves):
    """What the app answers with no receiver linked."""
    app.replies["haptics"] = [
        {"type": "error", "code": "send_failed", "message": f"could not send to {RIGHT}"}]

    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.stop(RIGHT)

    assert refused.value.code == "send_failed"


def test_set_hall_range_sends_the_range_and_returns_what_it_did_to_the_calibration(
        app, gloves):
    note = gloves.set_hall_range(RIGHT, 1)

    assert app.of_type("set_hall_range") == [
        {"type": "set_hall_range", "device": RIGHT, "hall_range": 1}]
    assert note == HALL_RANGE_NOTE


def test_a_hall_range_that_is_neither_5_nor_50_mt_is_refused(gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.set_hall_range(RIGHT, 2)

    assert refused.value.code == "bad_hall_range"


def test_a_reply_for_another_glove_is_not_taken_for_this_ones(app, gloves):
    app.replies["haptics"] = [{"type": "haptics_sent", "device": LEFT}]

    with pytest.raises(TimeoutError):
        gloves.play_rom(RIGHT, [1, 0, 0, 0, 0, 0], timeout=0.2)


# ── calibration ──────────────────────────────────────────────────────────────

SPEC_BOUNDS = {"Index": {"prox_min": (-812.0, 140.0, 2210.0), "prox_max": (35.0, -12.0, 4020.0),
                         "meta_min": (410.0, -96.0, 1388.0), "meta_max": (12.0, 8.0, 2950.0)}}


def test_calibration_of_a_glove_is_its_bounds_as_the_app_has_them(app, gloves):
    assert gloves.calibration(RIGHT) == Calibration(RIGHT, 0, False, SPEC_BOUNDS)
    assert app.of_type("calibration") == [{"type": "calibration", "device": RIGHT}]


def test_calibration_with_no_glove_named_is_every_glove_in_the_device_list(gloves):
    assert gloves.calibration() == {RIGHT: Calibration(RIGHT, 0, False, SPEC_BOUNDS),
                                    LEFT: Calibration(LEFT, 0, False, SPEC_BOUNDS)}


def test_calibration_of_a_glove_the_app_has_not_seen_is_refused(gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.calibration("NRF_GLOVE_NOPE")

    assert refused.value.code == "unknown_device"


def test_bounds_the_app_pushes_when_they_change_are_kept_as_the_latest(app, gloves):
    assert gloves.latest_calibration(RIGHT) is None

    app.send(calibration(hall_range=1, extrapolated=True))
    assert wait(lambda: gloves.latest_calibration(RIGHT) is not None)

    latest = gloves.latest_calibration(RIGHT)
    assert latest is not None and (latest.hall_range, latest.extrapolated) == (1, True)
    assert gloves.latest_calibration(LEFT) is None


def test_bounds_asked_for_are_the_latest_too(gloves):
    gloves.calibration(RIGHT)

    assert gloves.latest_calibration(RIGHT) == Calibration(RIGHT, 0, False, SPEC_BOUNDS)


# ── the debug stream ─────────────────────────────────────────────────────────

def test_a_debug_subscription_names_the_values_wanted(app, gloves):
    granted = gloves.subscribe("debug", keys=["mag.*", "curl.Index.?rox"])

    assert app.of_type("subscribe") == [
        {"type": "subscribe", "stream": "debug", "hands": ["left", "right"], "rate_hz": 0.0,
         "keys": ["mag.*", "curl.Index.?rox"]}]
    assert granted == Subscription("debug", keys=("mag.*", "curl.Index.?rox"))


def test_a_debug_message_written_from_the_spec_is_read_as_its_values(app, gloves):
    app.send(debug())

    values = gloves.read("debug")

    assert (values.device, values.hand, values.seq, values.time_us, values.wall_time_us) == (
        RIGHT, "right", 41, 5_000_000, 1_762_000_000_000_000)
    assert values.values == DEBUG["values"]


def test_debug_messages_come_from_their_iterator_and_as_the_latest(app, gloves):
    app.send(debug(seq=1), debug(seq=2), frame(seq=7))

    assert next(gloves.debug()).seq == 1
    assert gloves.read().seq == 7
    assert gloves.latest("right", "debug").seq == 2


# ── raw counts after earth-field cancellation ────────────────────────────────

def test_a_raw_subscription_can_ask_for_the_cancelled_counts(app, gloves):
    granted = gloves.subscribe("raw", cancelled=True)

    assert app.of_type("subscribe") == [
        {"type": "subscribe", "stream": "raw", "hands": ["left", "right"], "rate_hz": 0.0,
         "cancelled": True}]
    assert granted == Subscription("raw", cancelled=True)


def test_a_raw_subscription_leaves_the_cancelled_counts_out_unless_asked(app, gloves):
    assert gloves.subscribe("raw").cancelled is False
    assert app.of_type("subscribe")[0]["cancelled"] is False


def test_a_cancelled_that_is_not_true_or_false_is_refused_with_the_protocols_code(gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.subscribe("raw", cancelled=cast(Any, "yes"))

    assert refused.value.code == "bad_cancelled"


def test_an_app_without_the_cancelled_counts_grants_a_subscription_without_them(app, gloves):
    """An app from before raw_cancelled ignores the field it does not know,
    so what it granted says cancelled is off."""
    app.replies["subscribe"] = [{"type": "subscribed", "stream": "raw",
                                 "hands": ["left", "right"], "rate_hz": 0}]

    assert gloves.subscribe("raw", cancelled=True).cancelled is False


def test_a_raw_message_with_the_cancelled_counts_carries_them_beside_the_gloves(app, gloves):
    app.send(raw_cancelled())

    reading = gloves.read("raw")

    assert reading.hall_cancelled is not None and len(reading.hall_cancelled) == 5
    assert reading.hall_cancelled[0] == ((-1, 2, 3), (4, -5, 6))
    assert reading.hall_cancelled[1] == ((7, 8, -9), (10, 11, 120))
    assert (reading.thumb_distal_cancelled, reading.earth_cancelled) == ((31, -32, 33), True)
    assert reading.hall[1] == ((7, 8, 9), (10, 11, 12))     # the glove's own, untouched


def test_a_raw_message_with_nothing_cancelled_has_its_own_counts_twice(app, gloves):
    app.send(raw_cancelled(hall_cancelled=RAW["hall"], thumb_distal_cancelled=None,
                           thumb_distal=None, earth_cancelled=False))

    reading = gloves.read("raw")

    assert (reading.hall_cancelled, reading.earth_cancelled) == (reading.hall, False)
    assert reading.thumb_distal_cancelled is None


def test_a_raw_message_without_the_cancelled_counts_has_none(app, gloves):
    app.send(raw())

    reading = gloves.read("raw")

    assert (reading.hall_cancelled, reading.thumb_distal_cancelled, reading.earth_cancelled) == (
        None, None, None)
    assert set(CANCELLED).isdisjoint(RAW)


# ── a callback per message ───────────────────────────────────────────────────

def test_a_callback_gets_each_raw_message_on_the_reader_thread(app, gloves):
    called = []
    gloves.on("raw", lambda reading: called.append((reading.seq, threading.current_thread())))

    app.send(raw(seq=1), raw(seq=2))

    assert wait(lambda: len(called) == 2)
    assert [seq for seq, _ in called] == [1, 2]
    assert {thread.name for _, thread in called} == {"pandora-sdk-reader"}


def test_a_callback_that_raises_ends_the_connection_with_its_error(app, gloves):
    def _broken(reading):
        raise ValueError("no such key")

    gloves.on("raw", _broken)
    app.send(raw(), frame())

    with pytest.raises(pandora_sdk.Disconnected, match="callback for raw raised.*no such key"):
        gloves.read(timeout=2.0)


def test_a_callback_that_raised_has_its_code_and_its_error_as_the_cause(app, gloves):
    def _broken(reading):
        raise ValueError("no such key")

    gloves.on("raw", _broken)
    app.send(raw())

    with pytest.raises(pandora_sdk.Disconnected) as lost:
        gloves.read(timeout=2.0)

    assert lost.value.code == "callback_failed"
    assert isinstance(lost.value.__cause__, ValueError)
