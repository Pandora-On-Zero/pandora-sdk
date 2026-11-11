"""Hands straight from a receiver, with no app (pandora_sdk/local.py), against
a made-up serial link. The packets are built here from the tracking packet's
layout; what the pipeline makes of real ones is pandora-core's own tests'
business. These check the plumbing around it."""
import json
import threading

import pytest
from fakelink import Link, packet_line, sent
from standin import wait

import pandora_sdk
from pandora_core import receiver
from pandora_core.glove import Glove
from pandora_core.packet_layout import (
    GLOVE_PKT_HALL_CONFIG, GLOVE_PKT_HAPTIC_ROM, GLOVE_PKT_HAPTIC_RTP, HALL_DEFAULT_FILTER,
    HALL_DEFAULT_ODR, HALL_DEFAULT_OSR, HALL_RANGE_5MT, HALL_RANGE_50MT, HALL_TARGET_ALL,
    USB_HALL_CONFIG_FORMAT, USB_HAPTIC_ROM_FORMAT, USB_HAPTIC_RTP_FORMAT,
)
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


def test_without_the_pipelines_default_files_it_refuses_to_guess(link, monkeypatch):
    """A pandora-core from before it carried them; hands made without them
    would be wrong, silently."""
    monkeypatch.setattr("pandora_sdk.local.shipped_defaults", lambda: {})

    with pytest.raises(pandora_sdk.PandoraError, match="default files"):
        pandora_sdk.Local(PROFILES, link=link)


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
    """Only its first packet goes through: that loads the glove's profile,
    which calibration() reads."""
    for stream in streams:
        gloves.subscribe(stream)

    feed(link, gloves, *(packet_line(RIGHT, glove_ms=ms) for ms in range(1, 6)))

    assert [ms for _, _, ms in pushes] == [1]


def test_a_glove_no_frames_subscription_includes_skips_the_pipeline(link, gloves, pushes):
    gloves.subscribe(hands=["right"], bones=False)

    feed(link, gloves, *(packet_line(device, glove_ms=ms)
                         for ms in range(1, 4) for device in (LEFT, RIGHT)))

    assert [ms for _, device, ms in pushes if device == LEFT] == [1]
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
    assert sorted(pipeline) == [1, 4, 5]
    assert pipeline[4] is not pipeline[1] and pipeline[5] is pipeline[4]


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
