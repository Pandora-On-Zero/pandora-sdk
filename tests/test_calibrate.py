"""`pandora calibrate` (pandora_sdk/calibrate.py) against a made-up receiver.
The glove here is a script: every Hall sensor at one level while the hand is
open and at another once it is a fist. What a calibration makes of real
readings is pandora-core's own tests' business; these check what the command
adds: picking the glove, what it says, what it sends, and the file it leaves."""
import json
import struct
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
from fakelink import Link, packet_line

import pandora_sdk
from pandora_core.calibration_session import Step, StepKind
from pandora_core.packet_layout import (
    GLOVE_PKT_HALL_CONFIG, GLOVE_PKT_HAPTIC_RTP, HALL_DEFAULT_RANGE, HALL_TARGET_ALL,
    HAPTIC_MOTOR_COUNT, USB_HALL_CONFIG_FORMAT, USB_HAPTIC_RTP_FORMAT,
)
from pandora_core.serial_framing import decode_frame
from pandora_sdk.calibrate import Outcome, _Screen, calibrate

RIGHT, LEFT = "NRF_GLOVE_E5D1", "NRF_GLOVE_A48D"
OPEN, FIST = 100, 900
# Glove seconds: 0.2 to see who is sending, 0.3 to get ready, 1 a pose. The
# open step then ends near 1.5 s and the fist near 2.5 s.
QUICK: Dict[str, Any] = {"hold_s": 1.0, "ready_s": 0.3, "listen_s": 0.2, "wait_s": 0.3}


def performance(*devices: str, seconds: float = 3.0, fist_from_s: float = 1.8) -> List[bytes]:
    """The gloves sending 100 packets a second each, hand open and then a fist."""
    return [packet_line(device, ms, OPEN if ms < fist_from_s * 1000 else FIST)
            for ms in range(0, int(seconds * 1000), 10) for device in devices]


def sent(link: Link, kind: int, layout: str) -> List[Tuple]:
    """The commands of type *kind* written to the port, unpacked, in order."""
    payloads = [decode_frame(line) for line in link.written]
    return [struct.unpack(layout, payload) for payload in payloads
            if payload is not None and payload[0] == kind]


def quiet(_text: str) -> None:
    pass


def kept_as(outcome: Outcome) -> Dict[str, Any]:
    """The profile file the run wrote, parsed."""
    assert outcome.path is not None
    return json.loads(outcome.path.read_text(encoding="utf-8"))


@pytest.fixture
def link():
    return Link()


@pytest.fixture
def folder(tmp_path):
    """Where the run is told to write; not there until it does."""
    return tmp_path / "profiles"


def test_a_run_writes_the_profile_open_receiver_reads_calibrated_hands_from(folder, link):
    link.feed(*performance(RIGHT))

    outcome = calibrate(link, hand="right", out=folder, haptics=False, say=quiet, **QUICK)

    assert outcome.ok and outcome.path == folder / f"{RIGHT}.json"
    again = Link()
    with pandora_sdk.Local(folder, link=again) as gloves:
        gloves.subscribe(bones=False)
        again.feed(packet_line(RIGHT, 5000, OPEN), packet_line(RIGHT, 5010, FIST))
        opened, closed = gloves.read(timeout=5.0), gloves.read(timeout=5.0)
    assert (opened.device, opened.hand) == (RIGHT, "right")
    assert opened.curl[1][0] == pytest.approx(0.0, abs=1e-3)
    assert closed.curl[1][0] == pytest.approx(1.0, abs=1e-3)


def test_each_step_its_countdown_and_the_outcome_are_said_as_the_run_goes(folder, link):
    """Two seconds a pose: each step is named with what to do, counted down a
    line a second, and the run ends on how it went and where the file is."""
    said: List[str] = []
    link.feed(*performance(RIGHT, seconds=5.0, fist_from_s=2.8))

    outcome = calibrate(link, hand="right", out=folder, haptics=False, say=said.append,
                        **{**QUICK, "hold_s": 2.0})

    def where(start: str) -> int:
        return next(i for i, line in enumerate(said) if line.startswith(start))

    first, second, done = (where("Step 1 of 2: Open your hand"),
                           where("Step 2 of 2: Make a fist"), where("Calibration complete"))
    assert first < second < done
    assert "Fingers straight" in said[first + 1]
    assert [line.strip() for line in said[first:second] if line.strip().isdigit()] == ["2", "1"]
    assert [line.strip() for line in said[second:done] if line.strip().isdigit()] == ["2", "1"]
    after = "\n".join(said[done:])
    assert "Right: curl range captured" in after and str(outcome.path) in after


def test_the_glove_is_put_on_its_power_up_hall_range_and_the_profile_says_so(folder, link):
    """open_receiver() sends a glove nothing, so the range a profile is good
    for is the one a glove powers up in. The run resets every sensor to it."""
    link.feed(*performance(RIGHT))

    outcome = calibrate(link, hand="right", out=folder, haptics=False, say=quiet, **QUICK)

    (_kind, device, reset, *_values, target), = sent(link, GLOVE_PKT_HALL_CONFIG,
                                                      USB_HALL_CONFIG_FORMAT)
    assert (device.rstrip(b"\0"), reset, target) == (RIGHT.encode(), 1, HALL_TARGET_ALL)
    kept = kept_as(outcome)
    assert (kept["range"], kept["sensor_ranges"]) == (HALL_DEFAULT_RANGE, {})
    assert kept["sections"]["CURL"]["mag_range"] == HALL_DEFAULT_RANGE


@pytest.mark.parametrize("haptics, buzzes", [(True, 3), (False, 0)])
def test_the_back_of_the_hand_buzzes_at_each_step_and_at_the_end_unless_told_not_to(
        folder, link, haptics, buzzes):
    link.feed(*performance(RIGHT))

    calibrate(link, hand="right", out=folder, haptics=haptics, say=quiet, **QUICK)

    cues = sent(link, GLOVE_PKT_HAPTIC_RTP, USB_HAPTIC_RTP_FORMAT)
    assert len(cues) == buzzes
    for _kind, device, *values in cues:
        amplitudes = values[:HAPTIC_MOTOR_COUNT]
        assert device.rstrip(b"\0") == RIGHT.encode()
        assert amplitudes[:-1] == [0] * (HAPTIC_MOTOR_COUNT - 1) and amplitudes[-1] > 0


def test_with_two_gloves_sending_it_asks_which(folder, link):
    link.feed(*performance(RIGHT, LEFT))

    with pytest.raises(pandora_sdk.PandoraError, match=f"{LEFT}, {RIGHT}.*--device"):
        calibrate(link, hand="right", out=folder, haptics=False, say=quiet, **QUICK)

    assert not folder.exists() and link.written == []


def test_a_named_glove_is_the_one_calibrated_whoever_else_is_sending(folder, link):
    link.feed(*performance(RIGHT, LEFT))

    outcome = calibrate(link, hand="left", device=LEFT, out=folder, haptics=False, say=quiet,
                        **QUICK)

    assert outcome.ok and [path.name for path in folder.iterdir()] == [f"{LEFT}.json"]
    assert kept_as(outcome)["hand"] == "left"


def test_with_no_glove_sending_it_gives_up_and_says_so(folder, link):
    with pytest.raises(pandora_sdk.PandoraError, match="no glove is sending"):
        calibrate(link, hand="right", out=folder, haptics=False, say=quiet, **QUICK)


def test_a_glove_that_stops_part_way_leaves_no_file(folder, link):
    """Half a calibration is not written: the file is either a whole run's or
    what it was before."""
    link.feed(*performance(RIGHT, seconds=1.0))

    with pytest.raises(pandora_sdk.PandoraError, match=f"{RIGHT} stopped sending"):
        calibrate(link, hand="right", out=folder, haptics=False, say=quiet, **QUICK)

    assert not folder.exists()


def test_calibrating_again_starts_from_the_gloves_own_file(folder, link):
    """The hand, the name and a section no run measures are the file's, so
    the second run needs no --hand and loses nothing."""
    joystick = {"x_min": 100, "x_max": 4000, "y_min": 0, "y_max": 4095, "deadzone": 0.1}
    pandora_sdk.save_profile(folder, RIGHT, pandora_sdk.Profile(
        "right", {"JOYSTICK": joystick}, name="Lab right"))
    link.feed(*performance(RIGHT))

    outcome = calibrate(link, out=folder, haptics=False, say=quiet, **QUICK)

    kept = kept_as(outcome)
    assert (kept["hand"], kept["name"]) == ("right", "Lab right")
    assert kept["sections"]["JOYSTICK"] == joystick and kept["sections"]["CURL"]


def test_a_glove_never_calibrated_needs_its_hand_said(folder, link):
    link.feed(*performance(RIGHT))

    with pytest.raises(pandora_sdk.PandoraError, match="--hand"):
        calibrate(link, out=folder, haptics=False, say=quiet, **QUICK)


def test_a_warning_is_said_once_a_second_while_the_session_has_one():
    """The session judges the pose on every packet, 200 a second, and a
    finger near the limit flips the warning's wording each time. A terminal
    gets one line a second while the session warns, and none in the next
    step, where the session's prompt is empty again."""
    said: List[str] = []
    screen = _Screen(said.append, lambda: None)
    together = Step(StepKind.TOGETHER, "TOGETHER", "Open hand, fingers together", "Hold.", 2.0)
    fist = Step(StepKind.FIST, "FIST", "Make a fist", "Hold.", 2.0)
    session: Any = SimpleNamespace(steps=(together, fist), index=0, step=together, prompt=[],
                              remaining_s=2.0)

    for packet in range(800):
        session.index = packet // 400
        session.step = session.steps[session.index]
        session.remaining_s = 2.0 - (packet % 400) * 0.005
        session.prompt = ([(False, "Point the fingers forward, not up or down"),
                           (False, "Right: Index" if packet % 2 else "Right: Index, Ring")]
                          if session.step is together else [])
        screen(session)

    warned = [line for line in said if "Point the fingers forward" in line]
    assert len(warned) == 2
    assert all(said.index(line) < said.index("Step 2 of 2: Make a fist") for line in warned)
