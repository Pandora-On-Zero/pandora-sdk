"""`pandora update-firmware` (pandora_sdk/firmware.py) against a made-up
receiver that answers the DFU protocol as the receiver's and the glove's
firmware do (the answers pandora-core-rs's own firmware tests give). No real
port is opened and nothing is flashed: what is checked is what the command
adds around the core's update, which pandora-core-rs's tests check line by
line: picking the target, the question, refusing a held receiver, and the
outcome."""
import struct
import threading
import time
from typing import List

import pytest
from fakelink import Link, packet_line

from pandora_core_rs import receiver
from pandora_core_rs.firmware_update import FirmwareUpdater
from pandora_sdk import firmware
from pandora_sdk.cli import main

RIGHT, LEFT = "NRF_GLOVE_E5D1", "NRF_GLOVE_A48D"


def signed_image(folder, major: int = 1, minor: int = 3, revision: int = 0,
                 build: int = 9) -> str:
    """A file with the MCUboot header a signed image starts with, and a body."""
    body = bytes((i * 7 + 3) & 0xFF for i in range(600))
    header = (struct.pack("<IIHHI", 0x96F3B83D, 0, 32, 0, len(body)) + bytes(4)
              + struct.pack("<BBHI", major, minor, revision, build)).ljust(32, b"\0")
    path = folder / "app.signed.bin"
    path.write_bytes(header + body)
    return str(path)


def _glove_reply(device: str, rsp: int, payload: bytes) -> str:
    packet = bytes([0xDF, rsp, len(payload) & 0xFF, len(payload) >> 8]) + payload
    return f"DFU:GLOVERX:{device}:{packet.hex()}"


def _glove_version(confirmed: int) -> bytes:
    # proto, major, minor, revision (u16), build (u32), confirmed, state 0 (idle)
    return bytes([1, 1, 2]) + struct.pack("<HI", 0, 5) + bytes([confirmed, 0])


class Receiver(Link):
    """A receiver taking DFU lines and answering each as its firmware does,
    for itself and for the glove it relays to."""

    def write(self, data: bytes) -> int:
        written = super().write(data)
        for line in data.decode("ascii").splitlines():
            for reply in self._answer(line.strip()):
                self.feed(reply.encode("ascii") + b"\n")
        return written

    def lines(self) -> List[str]:
        return [line.decode("ascii").strip() for line in self.written]

    @staticmethod
    def _answer(line: str) -> List[str]:
        if line == "DFU:SELFVERSION":
            return ["DFU:SELFVERSION:01 Dongle:1.2.0:7:1"]
        if line.startswith("DFU:SELFBEGIN:"):
            return ["DFU:OK:SELFBEGIN"]
        if line.startswith("DFU:SELFDATA:"):
            return [f"DFU:SELFACK:{line.split(':')[2]}"]
        if line == "DFU:SELFFLASH":
            return ["DFU:OK:SELFFLASH", "DFU:SELFBOOT:PENDING:1.3.0:9"]
        if line == "DFU:SELFCONFIRM":
            return ["DFU:OK:SELFCONFIRM"]
        if line.startswith("DFU:GLOVETX:"):
            _, _, device, hex_packet = line.split(":")
            packet = bytes.fromhex(hex_packet)
            cmd, payload = packet[1], packet[4:]
            if cmd == 0x01:                                  # START: the first ACK is "ready"
                return [_glove_reply(device, 0x82, struct.pack("<II", 0, 0))]
            if cmd == 0x02:                                  # CHUNK
                seq = struct.unpack_from("<I", payload)[0]
                return [_glove_reply(device, 0x82, struct.pack("<II", seq + 1, 0))]
            if cmd == 0x03:                                  # COMPLETE
                return [_glove_reply(device, 0x84, struct.pack("<I", 0))]
            if cmd == 0x05:                                  # VERSION_REQ
                return [_glove_reply(device, 0x86, _glove_version(0))]
            if cmd == 0x06:                                  # CONFIRM
                return [_glove_reply(device, 0x86, _glove_version(1))]
        return []


@pytest.fixture
def plugged(monkeypatch):
    """A made-up receiver on COM7, the only one plugged in."""
    made = Receiver()
    monkeypatch.setattr(receiver, "find_receivers",
                        lambda: [receiver.ReceiverPort("COM7", "A", "01 Receiver")])
    monkeypatch.setattr(receiver, "open_port", lambda _port, **_how: made)
    # No waiting for a real reboot, nor three seconds to see who is sending.
    monkeypatch.setattr(FirmwareUpdater, "_SELF_CONFIRM_SETTLE", 0.0)
    monkeypatch.setattr(FirmwareUpdater, "_GLOVE_REBOOT_SETTLE", 0.0)
    monkeypatch.setattr(firmware, "GLOVE_LISTEN_S", 0.3)
    return made


def keep_sending(link: Link, *devices: str, seconds: float = 2.0) -> None:
    """The gloves sending 100 packets a second each, for *seconds*."""
    def _send() -> None:
        for ms in range(0, int(seconds * 1000), 10):
            link.feed(*[packet_line(device, ms) for device in devices])
            time.sleep(0.01)

    threading.Thread(target=_send, daemon=True).start()


def answer(monkeypatch, reply: str) -> List[str]:
    """Have the question answered with *reply*; the questions asked."""
    asked: List[str] = []

    def _input(question: str) -> str:
        asked.append(question)
        return reply

    monkeypatch.setattr("builtins.input", _input)
    return asked


def test_the_receiver_is_flashed_and_the_new_image_confirmed(plugged, tmp_path, capsys):
    code = main(["update-firmware", "receiver", signed_image(tmp_path), "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert "v1.3.0" in out and "running: 01 Dongle v1.2.0 (build 7)" in out
    assert "DONE" in out
    assert any(line.startswith("DFU:SELFBEGIN:") for line in plugged.lines())
    assert plugged.lines()[-1] == "DFU:SELFCONFIRM"
    assert not plugged.is_open


def test_it_asks_before_flashing_and_flashes_nothing_unless_told_yes(
        plugged, tmp_path, monkeypatch, capsys):
    asked = answer(monkeypatch, "n")

    code = main(["update-firmware", "dongle", signed_image(tmp_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert len(asked) == 1 and "v1.2.0" in asked[0] and "v1.3.0" in asked[0]
    assert plugged.lines() == ["DFU:SELFVERSION"]
    assert "Nothing was flashed" in captured.out


def test_yes_to_the_question_flashes(plugged, tmp_path, monkeypatch):
    answer(monkeypatch, "y")

    assert main(["update-firmware", "dongle", signed_image(tmp_path)]) == 0
    assert plugged.lines()[-1] == "DFU:SELFCONFIRM"


def test_with_no_one_to_answer_the_question_nothing_is_flashed(
        plugged, tmp_path, monkeypatch):
    def _no_terminal(_question: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _no_terminal)

    assert main(["update-firmware", "dongle", signed_image(tmp_path)]) == 1
    assert plugged.lines() == ["DFU:SELFVERSION"]


def test_a_glove_update_goes_to_the_only_glove_sending(plugged, tmp_path, capsys):
    keep_sending(plugged, RIGHT)

    code = main(["update-firmware", "glove", signed_image(tmp_path), "--yes"])

    out = capsys.readouterr().out
    glove_lines = [line for line in plugged.lines() if line.startswith("DFU:GLOVETX:")]
    assert code == 0 and "DONE" in out and RIGHT in out
    assert glove_lines and all(line.split(":")[2] == RIGHT for line in glove_lines)


def test_a_named_glove_is_the_one_updated(plugged, tmp_path):
    code = main(["update-firmware", "glove", signed_image(tmp_path), "--device", LEFT, "--yes"])

    glove_lines = [line for line in plugged.lines() if line.startswith("DFU:GLOVETX:")]
    assert code == 0
    assert glove_lines and all(line.split(":")[2] == LEFT for line in glove_lines)


def test_with_two_gloves_sending_and_none_named_it_asks_which(plugged, tmp_path, capsys):
    keep_sending(plugged, RIGHT, LEFT)

    code = main(["update-firmware", "glove", signed_image(tmp_path), "--yes"])

    err = capsys.readouterr().err
    assert code == 1 and "--device" in err and RIGHT in err and LEFT in err
    assert plugged.lines() == []


def test_while_the_app_holds_the_receiver_it_refuses_and_says_to_close_it(
        monkeypatch, tmp_path, capsys):
    def _held(port, **_how):
        raise OSError(f"could not open port '{port}': PermissionError(13, 'Access is denied.')")

    monkeypatch.setattr(receiver, "open_port", _held)

    code = main(["update-firmware", "dongle", signed_image(tmp_path), "--receiver", "COM7",
                 "--yes"])

    err = capsys.readouterr().err
    assert code == 1
    assert "COM7 is in use" in err and "01 Glove app" in err


def test_a_port_this_user_may_not_open_is_refused_naming_the_group(
        monkeypatch, tmp_path, capsys):
    def _forbidden(port, **_how):
        raise OSError(13, f"could not open port {port}: [Errno 13] Permission denied: '{port}'")

    monkeypatch.setattr(receiver, "open_port", _forbidden)

    code = main(["update-firmware", "dongle", signed_image(tmp_path), "--receiver",
                 "/dev/ttyACM0", "--yes"])

    err = capsys.readouterr().err
    assert code == 1
    assert "may not open /dev/ttyACM0" in err and "dialout" in err


def test_a_file_that_is_no_firmware_image_is_refused_before_the_receiver_is_opened(
        tmp_path, capsys):
    """The guard in conftest fails the test if any port is opened."""
    text = tmp_path / "notes.txt"
    text.write_text("These are notes, and no firmware image at all. " * 3, encoding="utf-8")

    code = main(["update-firmware", "dongle", str(text), "--receiver", "COM7", "--yes"])

    assert code == 1
    assert "not a signed image" in capsys.readouterr().err
