"""The `pandora` command: against the stand-in server, and against a made-up
receiver for what it does with no app."""
import threading

from fakelink import Link, packet_line
from standin import frame, raw, raw_cancelled

import pandora_sdk
from pandora_core_rs import receiver
from pandora_sdk.cli import main

RIGHT, LEFT = "NRF_GLOVE_E5D1", "NRF_GLOVE_A48D"
OPEN, FIST = 100, 900


def plugged_in(monkeypatch, *links: Link) -> None:
    """Each opening of the receiver's port gets the next of *links*."""
    waiting = iter(links)
    monkeypatch.setattr(receiver, "open_port", lambda _port, **_how: next(waiting))


def test_devices_prints_one_line_per_glove(app, capsys):
    code = main(["devices", "--port", str(app.port)])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines == ["NRF_GLOVE_E5D1  right  connected  Lab right",
                     "NRF_GLOVE_A48D  left   silent"]


def test_watch_prints_each_frame_until_its_count_is_reached(app, capsys):
    app.replies["subscribe"] = [
        {"type": "subscribed", "stream": "frames", "hands": ["right"], "rate_hz": 10.0,
         "bones": False},
        frame(seq=1), frame(seq=2), frame(seq=3)]

    code = main(["watch", "--port", str(app.port), "--hand", "right", "--count", "2"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert app.of_type("subscribe") == [
        {"type": "subscribe", "stream": "frames", "hands": ["right"], "rate_hz": 10.0,
         "bones": False}]
    assert len(lines) == 2 and lines[0].startswith("right  NRF_GLOVE_E5D1  #1  ")
    assert "curl 0.12 0.50 0.00 1.00 0.75" in lines[0]


def test_watch_raw_cancelled_asks_for_the_cancelled_counts_and_prints_them(app, capsys):
    app.replies["subscribe"] = [
        {"type": "subscribed", "stream": "raw", "hands": ["left", "right"], "rate_hz": 10.0,
         "cancelled": True},
        raw_cancelled()]

    code = main(["watch", "--port", str(app.port), "--raw", "--cancelled", "--count", "1"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert app.of_type("subscribe")[0]["cancelled"] is True
    assert len(lines) == 1 and lines[0].startswith("right  NRF_GLOVE_E5D1  #41  ")
    # Each finger's proximal sensor after cancellation, not as the glove sent it.
    assert "cancelled -1,2,3  7,8,-9  13,14,15  19,20,21  25,26,27  earth cancelled" in lines[0]


def test_watch_raw_cancelled_says_when_the_app_grants_no_cancelled_counts(app, capsys):
    """An app from before the option grants the subscription without them."""
    app.replies["subscribe"] = [
        {"type": "subscribed", "stream": "raw", "hands": ["left", "right"], "rate_hz": 10.0},
        raw()]

    code = main(["watch", "--port", str(app.port), "--raw", "--cancelled", "--count", "1"])

    captured = capsys.readouterr()
    assert code == 0
    assert "no cancelled counts" in captured.err
    assert "  cancelled -  " in captured.out


def test_with_no_app_the_command_fails_with_the_reason(free_port, capsys):
    code = main(["devices", "--port", str(free_port), "--timeout", "1"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Protocol" in captured.err and captured.out == ""


def test_calibrate_writes_the_profile_watch_local_then_shows_hands_from(
        monkeypatch, tmp_path, capsys):
    """The two commands a person runs with no app: calibrate, then look. The
    glove holds its hand open, then a fist; afterwards a fist reads as 1."""
    folder = tmp_path / "profiles"
    worn, later = Link(), Link()
    worn.feed(*[packet_line(RIGHT, ms, OPEN if ms < 2800 else FIST)
                for ms in range(0, 4000, 10)])
    plugged_in(monkeypatch, worn, later)

    made = main(["calibrate", "--hand", "right", "--receiver", "COM7", "--out", str(folder),
                 "--hold", "1", "--ready", "0.5", "--no-haptics"])
    told = capsys.readouterr().out
    # The glove sends once watch is up: nothing is delivered before it subscribes.
    threading.Timer(0.5, lambda: later.feed(
        *[packet_line(RIGHT, 9000 + ms, FIST) for ms in range(0, 30, 10)])).start()
    shown = main(["watch", "--local", "--profiles", str(folder), "--receiver", "COM7",
                  "--rate", "0", "--count", "2"])
    lines = capsys.readouterr().out.splitlines()

    assert (made, shown) == (0, 0)
    assert "Calibration complete" in told and str(folder / f"{RIGHT}.json") in told
    assert not worn.is_open
    assert len(lines) == 2 and lines[0].startswith(f"right  {RIGHT}  #1  ")
    assert "curl 1.00 1.00 1.00 1.00 1.00" in lines[0]


def test_watch_local_frames_with_no_profile_at_all_says_to_calibrate(tmp_path, capsys):
    """Frames need a profile; raw readings do not, which it says too."""
    code = main(["watch", "--local", "--profiles", str(tmp_path / "none")])

    captured = capsys.readouterr()
    assert code == 1
    assert "pandora calibrate" in captured.err and "--raw" in captured.err
    assert captured.out == ""


def test_watch_local_raw_runs_with_no_profile_at_all(monkeypatch, tmp_path, capsys):
    """A glove with no profile is read raw only, as the right hand; the
    command says so once."""
    link = Link()
    plugged_in(monkeypatch, link)
    # Sent once watch is up: nothing is delivered before it subscribes.
    threading.Timer(0.5, lambda: link.feed(
        *[packet_line(RIGHT, ms) for ms in range(0, 30, 10)])).start()

    code = main(["watch", "--local", "--raw", "--profiles", str(tmp_path / "none"),
                 "--receiver", "COM7", "--rate", "0", "--count", "2"])

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0
    assert len(lines) == 2 and lines[0].startswith(f"right  {RIGHT}  #1  hall ")
    told = captured.err.splitlines()
    assert len(told) == 1 and f"{RIGHT} has no profile" in told[0]
    assert "right hand" in told[0]


def test_watch_local_names_a_glove_left_out_of_the_frames(monkeypatch, tmp_path, capsys):
    """Its packets make no frames, which looks the same as a glove that is
    off: the command says which it is."""
    pandora_sdk.save_profile(tmp_path, LEFT, pandora_sdk.Profile("left"))
    link = Link()
    link.feed(packet_line(RIGHT))
    plugged_in(monkeypatch, link)
    threading.Timer(1.5, lambda: link.feed(packet_line(LEFT))).start()

    code = main(["watch", "--local", "--profiles", str(tmp_path), "--receiver", "COM7",
                 "--count", "1"])

    captured = capsys.readouterr()
    assert code == 0 and captured.out.startswith(f"left   {LEFT}  #1  ")
    assert f"{RIGHT} is sending but is left out of the frames" in captured.err
    assert f"pandora calibrate --device {RIGHT}" in captured.err
