"""Hands straight from a receiver, with no app (pandora_sdk/local.py), against
a made-up serial link. The packets are built here from the tracking packet's
layout; what the pipeline makes of real ones is pandora-core's own tests'
business. These check the plumbing around it."""
import base64
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from fakelink import Link, packet_line, sent
from standin import wait

import pandora_sdk
from pandora_core import protocol, receiver
from pandora_core.finger_normalizer import CurlInterpolationMode, FingerInterpolator
from pandora_core.glove import Glove
from pandora_core.packet_decoder import PacketDecoder, SensorReading
from pandora_core.packet_layout import (
    GLOVE_PKT_HALL_CONFIG, GLOVE_PKT_HAPTIC_ROM, GLOVE_PKT_HAPTIC_RTP, HALL_DEFAULT_FILTER,
    HALL_DEFAULT_ODR, HALL_DEFAULT_OSR, HALL_RANGE_5MT, HALL_RANGE_50MT, HALL_TARGET_ALL,
    USB_HALL_CONFIG_FORMAT, USB_HAPTIC_ROM_FORMAT, USB_HAPTIC_RTP_FORMAT,
)
from pandora_core.serial_framing import encode_frame
from pandora_sdk import Device, Subscription

RIGHT, LEFT, STRANGER = "NRF_GLOVE_E5D1", "NRF_GLOVE_A48D", "NRF_GLOVE_0000"
PROFILES = {RIGHT: {"hand": "right", "name": "Lab right"}, LEFT: {"hand": "left"}}
# The right glove with its index finger's curl calibrated at 5 mT, the range
# it runs at; the left glove calibrated not at all.
INDEX = {"prox_min": {"x": -800.0, "y": 140.0, "z": 2200.0},
         "prox_max": {"x": 30.0, "y": -10.0, "z": 4000.0},
         "meta_min": {"x": 400.0, "y": -90.0, "z": 1300.0},
         "meta_max": {"x": 10.0, "y": 8.0, "z": 2900.0}}
CALIBRATED = {RIGHT: {"hand": "right", "range": HALL_RANGE_5MT,
                      "sections": {"CURL": {"mag_range": HALL_RANGE_5MT,
                                            "calibrations": {"Index": INDEX}}}},
              LEFT: {"hand": "left"}}


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
    assert (reading.battery_mv, reading.usb_power, reading.gatt_commands) == (4000, False, False)


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


def test_with_no_profiles_named_they_come_from_the_folder_calibrate_writes_to(link):
    """`pandora calibrate` and open_receiver() agree on one folder, so the
    second needs no argument after the first."""
    folder = pandora_sdk.default_profiles_dir()
    pandora_sdk.save_profile(folder, LEFT, pandora_sdk.Profile("left", name="Spare"))

    with pandora_sdk.Local(link=link, defaults={}) as gloves:
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


# ── error codes ──────────────────────────────────────────────────────────────

def test_no_receiver_has_its_code(monkeypatch):
    with pytest.raises(pandora_sdk.ConnectionFailed) as failed:
        pandora_sdk.open_receiver(PROFILES, defaults={})

    assert failed.value.code == "no_receiver"


def test_two_receivers_have_their_code(monkeypatch):
    monkeypatch.setattr(receiver, "find_receivers", lambda: [
        receiver.ReceiverPort("COM7", "A", "01 Receiver"),
        receiver.ReceiverPort("COM9", "B", "01 Receiver")])

    with pytest.raises(pandora_sdk.ConnectionFailed) as failed:
        pandora_sdk.open_receiver(PROFILES, defaults={})

    assert failed.value.code == "several_receivers"


def test_a_receiver_in_use_has_its_code_and_the_os_error_as_its_cause(monkeypatch):
    refusal = OSError("could not open port 'COM7': PermissionError(13, 'Access is denied.')")

    def _held(port, **_kwargs):
        raise refusal

    monkeypatch.setattr(receiver, "open_port", _held)

    with pytest.raises(pandora_sdk.ConnectionFailed) as failed:
        pandora_sdk.open_receiver(PROFILES, port="COM7", defaults={})

    assert failed.value.code == "in_use"
    assert failed.value.__cause__ is refusal


def test_a_port_that_will_not_open_for_another_reason_has_its_code(monkeypatch):
    refusal = OSError("could not open port 'COM7': FileNotFoundError(2, 'not found')")

    def _missing(port, **_kwargs):
        raise refusal

    monkeypatch.setattr(receiver, "open_port", _missing)

    with pytest.raises(pandora_sdk.ConnectionFailed) as failed:
        pandora_sdk.open_receiver(PROFILES, port="COM7", defaults={})

    assert failed.value.code == "open_failed"
    assert failed.value.__cause__ is refusal


def test_an_unplugged_receiver_has_its_code_and_the_os_error_as_its_cause(link, gloves):
    link.unplugged = True

    with pytest.raises(pandora_sdk.Disconnected) as lost:
        gloves.read(timeout=2.0)

    assert lost.value.code == "unplugged"
    assert isinstance(lost.value.__cause__, OSError)


def test_a_read_that_fails_otherwise_has_its_code(link, gloves, monkeypatch):
    def _broken(_count):
        raise OSError("the port said no")

    monkeypatch.setattr(link, "read", _broken)

    with pytest.raises(pandora_sdk.Disconnected, match="the port said no") as lost:
        gloves.read(timeout=2.0)

    assert lost.value.code == "read_failed"


def test_reading_a_closed_source_has_its_code(link, clock):
    opened = pandora_sdk.Local(PROFILES, link=link, defaults={}, clock=clock)
    opened.close()

    with pytest.raises(pandora_sdk.Disconnected) as lost:
        opened.read(timeout=2.0)

    assert lost.value.code == "closed"


def test_a_callback_that_raised_has_its_code_and_its_error_as_the_cause(link, gloves):
    def _broken(reading):
        raise ValueError("no such key")

    gloves.on("raw", _broken)
    gloves.subscribe("raw")
    link.feed(packet_line(RIGHT))

    with pytest.raises(pandora_sdk.Disconnected) as lost:
        gloves.read(timeout=2.0)

    assert lost.value.code == "callback_failed"
    assert isinstance(lost.value.__cause__, ValueError)


# ── opening the port, and the receivers plugged in ───────────────────────────

@pytest.fixture
def opened_with(monkeypatch):
    """The arguments each receiver.open_port call was given, the port
    returned being a made-up one."""
    calls = []

    def _open(port, **kwargs):
        calls.append((port, kwargs))
        return Link()

    monkeypatch.setattr(receiver, "open_port", _open)
    return calls


def test_open_link_opens_the_port_with_the_timeouts_asked_for(opened_with):
    pandora_sdk.open_link("COM7", timeout=0.05, write_timeout=1.0)

    assert opened_with == [("COM7", {"timeout": 0.05, "write_timeout": 1.0})]


def test_open_link_by_default_waits_a_fifth_of_a_second_to_read_and_two_to_write(opened_with):
    pandora_sdk.open_link("COM7")

    assert opened_with == [("COM7", {"timeout": 0.2, "write_timeout": 2.0})]


def test_list_receivers_gives_the_receivers_plugged_in_in_port_order(monkeypatch):
    found = [receiver.ReceiverPort("COM10", "B", "01 Receiver"),
             receiver.ReceiverPort("COM9", "A", "01 Receiver")]
    monkeypatch.setattr(receiver, "find_receivers", lambda: found)

    assert pandora_sdk.list_receivers() == [found[1], found[0]]


def test_list_receivers_with_none_plugged_in_is_empty():
    assert pandora_sdk.list_receivers() == []


# ── knowing it ended without reading ─────────────────────────────────────────

def test_a_running_source_has_not_ended(gloves):
    assert gloves.ended is False
    assert gloves.wait_ended(0.05) is False
    assert gloves.end_error is None


def test_a_source_whose_receiver_was_unplugged_has_ended_and_says_why(link, gloves):
    gloves.on("raw", lambda reading: None)
    gloves.subscribe("raw")

    link.unplugged = True

    assert gloves.wait_ended(2.0) is True
    assert gloves.ended is True
    error = gloves.end_error
    assert isinstance(error, pandora_sdk.Disconnected) and error.code == "unplugged"


def test_a_closed_source_has_ended(link, clock):
    opened = pandora_sdk.Local(PROFILES, link=link, defaults={}, clock=clock)
    opened.close()

    assert opened.ended and opened.wait_ended(0) is True
    assert opened.end_error is not None and opened.end_error.code == "closed"


@pytest.fixture
def no_default_files(monkeypatch):
    """A pandora-core that carries none of the pipeline's default files (an
    old one, or a frozen program built without them); hands made without
    them would be wrong, silently."""
    monkeypatch.setattr("pandora_sdk.local.shipped_defaults", lambda: {})


def test_without_the_pipelines_default_files_raw_readings_still_come(link, no_default_files):
    with pandora_sdk.Local(PROFILES, link=link) as gloves:
        gloves.subscribe("raw")
        link.feed(packet_line(RIGHT), packet_line(LEFT))

        assert {gloves.read("raw", timeout=2.0).device for _ in range(2)} == {RIGHT, LEFT}


@pytest.mark.parametrize("subscription", [{"stream": "frames"}, {"stream": "debug"},
                                          {"stream": "raw", "cancelled": True}],
                         ids=["frames", "debug", "raw-cancelled"])
def test_without_the_pipelines_default_files_a_stream_that_needs_it_is_refused(
        link, no_default_files, subscription):
    with pandora_sdk.Local(PROFILES, link=link) as gloves:
        with pytest.raises(pandora_sdk.PandoraError, match="default files"):
            gloves.subscribe(**subscription)


def test_without_the_pipelines_default_files_calibration_is_refused(link, no_default_files):
    with pandora_sdk.Local(PROFILES, link=link) as gloves:
        feed(link, gloves, packet_line(RIGHT))

        with pytest.raises(pandora_sdk.PandoraError, match="default files"):
            gloves.calibration(RIGHT)


# ── wall time ────────────────────────────────────────────────────────────────

def test_frames_and_raw_messages_carry_the_wall_time_they_were_made_at(link, clock):
    with pandora_sdk.Local(PROFILES, link=link, defaults={}, clock=clock,
                           wall_clock=lambda: 1_762_000_000.25) as gloves:
        gloves.subscribe(bones=False)
        gloves.subscribe("raw")
        link.feed(packet_line(RIGHT))

        assert gloves.read(timeout=2.0).wall_time_us == 1_762_000_000_250_000
        assert gloves.read("raw", timeout=2.0).wall_time_us == 1_762_000_000_250_000


# ── commands for a glove ─────────────────────────────────────────────────────

ID = RIGHT.encode().ljust(16, b"\0")


@pytest.fixture
def heard(link, gloves):
    """*gloves* once the right glove has sent a packet."""
    link.feed(packet_line(RIGHT))
    assert wait(lambda: gloves.devices())
    return gloves


def test_play_rom_writes_the_rom_command_for_the_glove_to_the_receiver(link, heard):
    heard.play_rom(RIGHT, [0, 0, 47, 0, 0, 0])

    assert sent(link, GLOVE_PKT_HAPTIC_ROM, USB_HAPTIC_ROM_FORMAT) == [
        (GLOVE_PKT_HAPTIC_ROM, ID, 0, 0, 47, 0, 0, 0)]


def test_play_rtp_writes_an_amplitude_and_a_duration_per_motor(link, heard):
    heard.play_rtp(RIGHT, [0, 90, 90, 0, 0, 0], [300] * 6)

    assert sent(link, GLOVE_PKT_HAPTIC_RTP, USB_HAPTIC_RTP_FORMAT) == [
        (GLOVE_PKT_HAPTIC_RTP, ID, 0, 90, 90, 0, 0, 0, 300, 300, 300, 300, 300, 300)]


def test_stop_writes_the_all_zero_frame(link, heard):
    heard.stop(RIGHT)

    assert sent(link, GLOVE_PKT_HAPTIC_RTP, USB_HAPTIC_RTP_FORMAT) == [
        (GLOVE_PKT_HAPTIC_RTP, ID, *([0] * 12))]


def test_a_command_for_a_glove_not_heard_is_refused_as_the_app_refuses_it(link, heard):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        heard.play_rom(LEFT, [1, 0, 0, 0, 0, 0])

    assert refused.value.code == "unknown_device"
    assert link.written == []


def test_a_command_for_a_glove_that_went_silent_is_refused(link, heard, clock):
    clock.now += 3.0

    with pytest.raises(pandora_sdk.ServerError) as refused:
        heard.stop(RIGHT)

    assert refused.value.code == "unknown_device"


def test_haptics_the_protocol_does_not_allow_are_refused_and_not_sent(link, heard):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        heard.play_rtp(RIGHT, [300, 0, 0, 0, 0, 0], [100] * 6)

    assert refused.value.code == "bad_haptics"
    assert link.written == []


def test_a_command_the_receiver_will_not_take_raises_send_failed(link, heard):
    link.refuses_writes = True

    with pytest.raises(pandora_sdk.ServerError) as refused:
        heard.stop(RIGHT)

    assert refused.value.code == "send_failed"


def test_set_hall_range_puts_every_sensor_on_it_with_the_power_up_settings(link, heard):
    assert heard.set_hall_range(RIGHT, HALL_RANGE_50MT) == ""

    assert sent(link, GLOVE_PKT_HALL_CONFIG, USB_HALL_CONFIG_FORMAT) == [
        (GLOVE_PKT_HALL_CONFIG, ID, 0, HALL_RANGE_50MT, HALL_DEFAULT_OSR, HALL_DEFAULT_ODR,
         HALL_DEFAULT_FILTER, HALL_TARGET_ALL)]


def test_a_hall_range_that_is_neither_5_nor_50_mt_is_refused(link, heard):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        heard.set_hall_range(RIGHT, 2)

    assert refused.value.code == "bad_hall_range"
    assert link.written == []


def test_after_a_range_change_raw_messages_say_the_new_range(link, heard):
    heard.subscribe("raw")
    link.feed(packet_line(RIGHT))
    assert heard.read("raw", timeout=2.0).hall_range is None

    heard.set_hall_range(RIGHT, HALL_RANGE_50MT)
    link.feed(packet_line(RIGHT))

    assert heard.read("raw", timeout=2.0).hall_range == HALL_RANGE_50MT


@pytest.fixture
def calibrated(link, clock):
    with pandora_sdk.Local(CALIBRATED, link=link, defaults={}, clock=clock) as opened:
        link.feed(packet_line(RIGHT), packet_line(LEFT))
        assert wait(lambda: len(opened.devices()) == 2)
        yield opened


def test_after_a_range_change_the_calibration_is_rescaled_to_it(link, calibrated):
    before = calibrated.calibration(RIGHT)

    calibrated.set_hall_range(RIGHT, HALL_RANGE_50MT)
    link.feed(packet_line(RIGHT))
    assert wait(lambda: calibrated.calibration(RIGHT).hall_range == HALL_RANGE_50MT)

    after = calibrated.calibration(RIGHT)
    assert (before.hall_range, before.extrapolated) == (HALL_RANGE_5MT, False)
    assert after.extrapolated
    assert after.calibrations["Index"]["prox_min"] != before.calibrations["Index"]["prox_min"]


def test_a_glove_that_comes_back_after_going_silent_is_put_on_its_range_again(
        link, heard, clock):
    """It may have been switched off and on, which puts it back on the range
    it powers up in; the app sends its range again then, too."""
    heard.set_hall_range(RIGHT, HALL_RANGE_50MT)
    clock.now += 3.0

    link.feed(packet_line(RIGHT))
    assert wait(lambda: len(sent(link, GLOVE_PKT_HALL_CONFIG, USB_HALL_CONFIG_FORMAT)) == 2)

    assert sent(link, GLOVE_PKT_HALL_CONFIG, USB_HALL_CONFIG_FORMAT)[1][3] == HALL_RANGE_50MT


# ── calibration bounds ───────────────────────────────────────────────────────

def test_calibration_of_a_glove_is_the_bounds_its_pipeline_normalises_with(calibrated):
    bounds = calibrated.calibration(RIGHT)

    assert (bounds.device, bounds.hall_range, bounds.extrapolated) == (
        RIGHT, HALL_RANGE_5MT, False)
    assert bounds.calibrations["Index"]["prox_min"] == (-800.0, 140.0, 2200.0)
    assert bounds.calibrations["Index"]["meta_max"] == (10.0, 8.0, 2900.0)


def test_calibration_of_an_uncalibrated_glove_has_no_bounds(calibrated):
    assert calibrated.calibration(LEFT).calibrations == {}


def test_calibration_with_no_glove_named_is_every_glove_heard(calibrated):
    assert sorted(calibrated.calibration()) == [LEFT, RIGHT]


def test_calibration_of_a_glove_not_heard_is_refused(calibrated):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        calibrated.calibration(STRANGER)

    assert refused.value.code == "unknown_device"


def test_the_latest_calibration_is_the_one_in_use_now(calibrated):
    assert calibrated.latest_calibration(RIGHT) == calibrated.calibration(RIGHT)
    assert calibrated.latest_calibration(STRANGER) is None


# ── the debug stream ─────────────────────────────────────────────────────────

def test_the_debug_stream_carries_the_pipelines_values_named_by_the_keys(link, gloves):
    granted = gloves.subscribe("debug", keys=["raw.*"])
    link.feed(packet_line(RIGHT), packet_line(RIGHT))

    first = gloves.read("debug", timeout=2.0)
    second = next(gloves.debug())

    assert granted.keys == ("raw.*",)
    assert (first.device, first.hand, first.seq, second.seq) == (RIGHT, "right", 1, 2)
    assert first.values and all(name.startswith("raw.") for name in first.values)


def test_the_debug_stream_has_the_values_of_every_stage_when_asked_for_all(link, gloves):
    gloves.subscribe("debug")
    link.feed(packet_line(RIGHT))

    stages = {name.split(".")[0] for name in gloves.read("debug", timeout=2.0).values}

    assert {"raw", "curl", "splay"} <= stages


# ── raw only: no hand made when none is asked for ────────────────────────────

@pytest.fixture
def pushes(monkeypatch):
    """Every packet a glove's pipeline is given: (the pipeline, device id,
    glove_ms), in order."""
    given = []
    push = Glove.push_decoded

    def _counted(glove, packet):
        given.append((glove, packet.device_id, packet.timestamp))
        return push(glove, packet)

    monkeypatch.setattr(Glove, "push_decoded", _counted)
    return given


def feed(link, gloves, *lines):
    """Feed *lines* and wait until the reader has handled them all."""
    before = gloves.packets
    link.feed(*lines)
    assert wait(lambda: gloves.packets == before + len(lines))


@pytest.mark.parametrize("streams", [(), ("raw",)])
def test_with_neither_frames_nor_debug_asked_for_a_glove_skips_the_pipeline(
        link, gloves, pushes, streams):
    """Not even its first packet: its pipeline is built only when one is
    needed (calibration() builds one when asked)."""
    for stream in streams:
        gloves.subscribe(stream)

    feed(link, gloves, *(packet_line(RIGHT, glove_ms=ms) for ms in range(1, 6)))

    assert pushes == []


def test_a_glove_no_frames_subscription_includes_skips_the_pipeline(link, gloves, pushes):
    gloves.subscribe(hands=["right"], bones=False)

    feed(link, gloves, *(packet_line(device, glove_ms=ms)
                         for ms in range(1, 4) for device in (LEFT, RIGHT)))

    assert [ms for _, device, ms in pushes if device == LEFT] == []
    assert [ms for _, device, ms in pushes if device == RIGHT] == [1, 2, 3]


def test_frames_asked_for_late_come_from_a_new_pipeline_started_at_that_packet(
        link, gloves, pushes):
    """Its filters begin there, as for a glove just switched on; none of them
    carries anything over from the packets before."""
    gloves.subscribe("raw")
    feed(link, gloves, *(packet_line(RIGHT, glove_ms=ms) for ms in range(1, 4)))

    gloves.subscribe(bones=False)
    feed(link, gloves, packet_line(RIGHT, glove_ms=4), packet_line(RIGHT, glove_ms=5))

    assert gloves.read(timeout=2.0).seq == 4
    pipeline = {ms: glove for glove, _, ms in pushes}
    assert sorted(pipeline) == [4, 5]
    assert pipeline[5] is pipeline[4]


def test_calibration_of_a_raw_only_glove_builds_its_pipeline_when_asked(link, clock, pushes):
    with pandora_sdk.Local(CALIBRATED, link=link, defaults={}, clock=clock) as gloves:
        gloves.subscribe("raw")
        feed(link, gloves, packet_line(RIGHT, glove_ms=1), packet_line(RIGHT, glove_ms=2))
        assert pushes == []

        bounds = gloves.calibration(RIGHT)

    assert bounds.calibrations["Index"]["prox_min"] == (-800.0, 140.0, 2200.0)
    assert [ms for _, _, ms in pushes] == [2]


# ── raw counts after earth-field cancellation ────────────────────────────────

def test_a_raw_subscription_can_ask_for_the_cancelled_counts(gloves):
    assert gloves.subscribe("raw", cancelled=True) == Subscription("raw", cancelled=True)
    assert gloves.subscribe("raw").cancelled is False


def test_a_cancelled_that_is_not_true_or_false_is_refused_as_the_app_refuses_it(gloves):
    with pytest.raises(pandora_sdk.ServerError) as refused:
        gloves.subscribe("raw", cancelled=cast(Any, "yes"))

    assert refused.value.code == "bad_cancelled"


def test_a_plain_raw_subscription_carries_no_cancelled_counts(link, gloves):
    gloves.subscribe("raw")

    link.feed(packet_line(RIGHT))

    reading = gloves.read("raw", timeout=2.0)
    assert (reading.hall_cancelled, reading.thumb_distal_cancelled, reading.earth_cancelled) == (
        None, None, None)


def test_with_no_earth_cancellation_the_cancelled_counts_are_the_gloves_own(link, gloves):
    """The profiles here switch cancellation on for no glove."""
    gloves.subscribe("raw", cancelled=True)

    link.feed(packet_line(RIGHT))

    reading = gloves.read("raw", timeout=2.0)
    assert reading.earth_cancelled is False
    assert (reading.hall_cancelled, reading.thumb_distal_cancelled) == (
        reading.hall, reading.thumb_distal)
    assert reading.hall[1][0] == (112, 113, 114)


def test_raw_with_the_cancelled_counts_keeps_the_gloves_pipeline_running(link, gloves, pushes):
    """Cancellation happens in the pipeline, and carries state from packet to
    packet, so each packet goes through it as with frames."""
    gloves.subscribe("raw", cancelled=True, hands=["right"])

    feed(link, gloves, *(packet_line(device, glove_ms=ms)
                         for ms in range(1, 6) for device in (LEFT, RIGHT)))

    assert [ms for _, device, ms in pushes if device == RIGHT] == [1, 2, 3, 4, 5]
    assert [ms for _, device, ms in pushes if device == LEFT] == []


# A real glove with earth-field cancellation on: tests/data/right_earth.json,
# recorded packets copied from pandora-core's golden vectors.
EARTH = json.loads((Path(__file__).parent / "data" / "right_earth.json").read_text(
    encoding="utf-8"))
EARTH_PAYLOADS = [base64.b64decode(packet) for packet in EARTH["packets"]]
EARTH_DEVICE = "NRF_GLOVE_CC8C"
EARTH_PROFILE = {"hand": EARTH["hand"], **EARTH["device"]}


def read_all(gloves, stream, count):
    return [gloves.read(stream, timeout=5.0) for _ in range(count)]


def earth_raw(link, subscriptions, tuning=None):
    """Every raw message Local makes of the earth vector's packets, with
    *subscriptions* (keyword arguments for subscribe) in place from the
    first; the frames, when one subscription is to them; and the glove's
    calibration bounds."""
    with pandora_sdk.Local({EARTH_DEVICE: EARTH_PROFILE}, link=link,
                           tuning=tuning or EARTH["tuning"]) as gloves:
        for subscription in subscriptions:
            gloves.subscribe(**subscription)
        link.feed(*(encode_frame(payload) for payload in EARTH_PAYLOADS))
        raws = read_all(gloves, "raw", len(EARTH_PAYLOADS))
        frames = (read_all(gloves, "frames", len(EARTH_PAYLOADS))
                  if any(s.get("stream", "frames") == "frames" for s in subscriptions) else [])
        bounds = gloves.calibration(EARTH_DEVICE)
    return raws, frames, bounds


def the_gloves_own(payload):
    """A packet's raw message before any pipeline has touched it."""
    packet = PacketDecoder().decode_packet(payload)
    assert packet is not None
    return protocol.decode_raw(protocol.encode_raw(packet, "right", 0, 0))


@pytest.mark.parametrize("subscriptions", [
    [{"stream": "raw"}],
    [{"stream": "raw", "cancelled": True}],
    [{"stream": "raw"}, {"bones": False}],
    [{"stream": "raw", "cancelled": True}, {"bones": False}],
], ids=["raw", "raw-cancelled", "raw-and-frames", "raw-cancelled-and-frames"])
def test_hall_is_the_gloves_own_counts_whatever_else_is_asked_for(link, subscriptions):
    raws, _, _ = earth_raw(link, subscriptions)

    for reading, payload in zip(raws, EARTH_PAYLOADS):
        own = the_gloves_own(payload)
        assert (reading.hall, reading.thumb_distal, reading.reference) == (
            own.hall, own.thumb_distal, own.reference)


def test_with_earth_cancellation_on_the_cancelled_counts_are_the_apps(link):
    """What the app's server sends for the same packets: the glove's pipeline
    over every one of them, then the core's encode_raw on the packet after
    it (SoftwareInterface/services/protocol_server.py)."""
    raws, _, _ = earth_raw(link, [{"stream": "raw", "cancelled": True}])
    decoder = PacketDecoder()
    apps = []
    with Glove(EARTH_PROFILE, EARTH["tuning"]) as glove:
        for payload in EARTH_PAYLOADS:
            packet = decoder.decode_packet(payload)
            assert packet is not None
            glove.push_decoded(packet)
            apps.append(protocol.decode_raw(protocol.encode_raw(
                packet, "right", 0, 0, cancelled=True)))

    assert [r.earth_cancelled for r in raws] == [True] * len(EARTH_PAYLOADS)
    for reading, app in zip(raws, apps):
        assert reading.hall_cancelled != reading.hall
        assert (reading.hall_cancelled, reading.thumb_distal_cancelled) == (
            app.hall_cancelled, app.thumb_distal_cancelled)
        assert reading.hall == app.hall         # the vector has gap fill off


def test_the_cancelled_counts_between_the_calibration_bounds_give_the_frames_curl(link):
    """They are in the counts of calibration()'s bounds: mapped between them
    with the curve the glove's profile names, they are the frame's curl. The
    glove's own counts are not, with cancellation on."""
    raws, frames, bounds = earth_raw(link, [{"stream": "raw", "cancelled": True},
                                            {"bones": False}])
    reading, frame = raws[-1], frames[-1]
    assert reading.seq == frame.seq and reading.hall_cancelled is not None

    def mapped(counts, finger, slot):
        sensor = bounds.calibrations[finger]
        return FingerInterpolator()._interpolate_curl(
            SensorReading(*counts), SensorReading(*sensor[f"{slot}_min"]),
            SensorReading(*sensor[f"{slot}_max"]), CurlInterpolationMode.CUBE_ROOT_A).to_curl()

    sensors = [(i, finger, j, slot) for i, finger in enumerate(protocol.FINGERS)
               for j, slot in enumerate(("prox", "meta"))]
    assert EARTH["device"]["sections"]["CURL"]["interpolation_mode"] == "cube_root_a"
    for i, finger, j, slot in sensors:
        assert mapped(reading.hall_cancelled[i][j], finger, slot) == frame.curl[i][j]
    assert any(mapped(reading.hall[i][j], finger, slot) != frame.curl[i][j]
               for i, finger, j, slot in sensors)


def test_with_gap_fill_on_a_dead_sensor_keeps_its_own_zeros_in_hall(link):
    """The vector's thumb proximal sensor reads all zeros. Gap fill gives the
    pipeline a neighbour's reading for it, which the cancelled counts carry;
    hall stays what the glove sent."""
    raws, _, _ = earth_raw(link, [{"stream": "raw", "cancelled": True}],
                           tuning={**EARTH["tuning"], "hall_gap_fill": True})

    reading = raws[-1]
    assert reading.hall[0][0] == (0, 0, 0)
    assert reading.hall_cancelled is not None and reading.hall_cancelled[0][0] != (0, 0, 0)


# ── a callback per message ───────────────────────────────────────────────────

def test_a_callback_gets_each_raw_message_on_the_reader_thread_as_it_is_made(link, gloves):
    called = []
    gloves.on("raw", lambda reading: called.append((reading.seq, threading.current_thread())))
    gloves.subscribe("raw")

    feed(link, gloves, packet_line(RIGHT), packet_line(RIGHT))

    assert [seq for seq, _ in called] == [1, 2]
    assert {thread.name for _, thread in called} == {"pandora-sdk-receiver"}


def test_messages_a_callback_takes_are_not_queued_but_latest_has_them(link, gloves):
    gloves.on("raw", lambda reading: None)
    gloves.subscribe("raw")

    feed(link, gloves, *([packet_line(RIGHT)] * 300))

    assert gloves.latest(RIGHT, "raw").seq == 300
    assert gloves.dropped == 0
    with pytest.raises(TimeoutError):
        gloves.read("raw", timeout=0.05)


def test_with_its_callback_removed_a_stream_is_queued_again(link, gloves):
    called = []
    gloves.on("raw", called.append)
    gloves.subscribe("raw")
    feed(link, gloves, packet_line(RIGHT))

    gloves.on("raw", None)
    feed(link, gloves, packet_line(RIGHT))

    assert [reading.seq for reading in called] == [1]
    assert gloves.read("raw", timeout=2.0).seq == 2


def test_a_callback_can_send_to_the_glove_and_read_its_calibration(link, clock):
    """It runs on the reader thread, inside the reader's handling of the
    packet; asking the source for something must not wait on that. From the
    glove's very first packet, its calibration is its profile's."""
    seen = []
    with pandora_sdk.Local(CALIBRATED, link=link, defaults={}, clock=clock) as gloves:
        def _tapped(reading):
            gloves.play_rom(reading.device, [0, 47, 0, 0, 0, 0])
            seen.append(gloves.calibration(reading.device).calibrations["Index"]["prox_min"])

        gloves.on("raw", _tapped)
        gloves.subscribe("raw", devices=[RIGHT])
        feed(link, gloves, packet_line(RIGHT))

    assert seen == [(-800.0, 140.0, 2200.0)]
    assert len(sent(link, GLOVE_PKT_HAPTIC_ROM, USB_HAPTIC_ROM_FORMAT)) == 1


def test_a_callback_that_raises_ends_the_source_with_its_error(link, gloves):
    def _broken(reading):
        raise ValueError("no such key")

    gloves.on("raw", _broken)
    gloves.subscribe("raw")
    gloves.subscribe(bones=False)
    link.feed(packet_line(RIGHT))

    with pytest.raises(pandora_sdk.Disconnected, match="callback for raw raised.*no such key"):
        gloves.read(timeout=2.0)


def test_a_callback_for_a_stream_that_does_not_exist_is_refused(gloves):
    with pytest.raises(ValueError, match="no stream 'hands'"):
        gloves.on("hands", print)
