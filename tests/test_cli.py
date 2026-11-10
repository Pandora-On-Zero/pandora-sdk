"""The `pandora` command against the stand-in server."""
from standin import frame

from pandora_sdk.cli import main


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


def test_with_no_app_the_command_fails_with_the_reason(free_port, capsys):
    code = main(["devices", "--port", str(free_port), "--timeout", "1"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Protocol" in captured.err and captured.out == ""
