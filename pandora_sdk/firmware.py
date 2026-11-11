"""`pandora update-firmware`: update the receiver's or a glove's firmware over
the air, with no app.

    pandora update-firmware receiver dfu_application.zip
    pandora update-firmware glove build/ --device NRF_GLOVE_E5D1

What `tools/ota_flash.py flash` in the app does, over the same pandora-core
code: the image is found and checked by firmware_package.resolve_image, the
port is opened and read by receiver.ReceiverLink, and the update is
firmware_update's FirmwareUpdater driven by wait_version and run_update. This
file adds what a person at a terminal needs around it: which port and which
glove, the version running now, a question before anything is written, and
the progress as lines.

The receiver's port is exclusive, so the app must be closed: while it holds
the receiver this refuses to start.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from pandora_core.firmware_package import DONGLE, GLOVE, resolve_image
from pandora_core.firmware_update import DfuTarget, FirmwareUpdater, run_update, wait_version
from pandora_core.receiver import ReceiverLink, is_port_locked, streaming_gloves

from pandora_sdk.local import receiver_port
from pandora_sdk.source import ConnectionFailed, PandoraError

# The names the command takes for what is updated: (package kind, DFU target).
# "dongle" is the app's and ota_flash's word for the receiver.
TARGETS = {"receiver": (DONGLE, DfuTarget.DONGLE), "dongle": (DONGLE, DfuTarget.DONGLE),
           "glove": (GLOVE, DfuTarget.LEFT_CONTROLLER)}
# Seconds of listening for which glove is sending when none is named, and of
# waiting for a version reply: ota_flash's.
GLOVE_LISTEN_S = 3.0
VERSION_WAIT_S = 5.0


def update_firmware(target: str, image: str, *, port: Optional[str] = None,
                    device: Optional[str] = None, yes: bool = False, verbose: bool = False,
                    say: Callable[[str], None] = print,
                    ask: Optional[Callable[[str], str]] = None) -> int:
    """Flash *image* (a DFU zip, a signed .bin, or a build folder holding
    dfu_application.zip) onto *target*, "receiver" (or "dongle") or "glove",
    and wait until the new image is confirmed. The glove is *device*, or the
    only one sending. Unless *yes*, *ask* is asked first and anything but y
    flashes nothing; left out, the question is put to the terminal
    (input). With *verbose* every line the receiver sends is said.

    Returns the exit status: 0 confirmed, 1 failed or not flashed, 130
    stopped with Ctrl+C. PandoraError when it cannot start: an image that is
    none, no receiver, a receiver the app holds, no glove or two."""
    kind, dfu = TARGETS[target]
    try:
        bin_path, description = resolve_image(image, kind)
    except ValueError as refused:
        raise PandoraError(str(refused)) from None
    say(f"image: {description}\n       {bin_path}")

    port = receiver_port(port)
    started = time.monotonic()

    def stamped(text: str) -> None:
        say(f"[{time.monotonic() - started:7.3f}] {text}")

    updater = _Updater(dfu, stamped, verbose)
    try:
        link = ReceiverLink.open(port, updater.on_line, say)
    except OSError as failed:  # serial.SerialException is an OSError
        if is_port_locked(failed):
            raise ConnectionFailed(
                f"{port} is in use. If the 01 Glove app is running it holds the receiver: "
                "close it, then update the firmware.") from None
        raise ConnectionFailed(f"could not open {port}: {failed}") from None
    updater.link = link
    try:
        if kind == GLOVE:
            updater.glove_device_id = device or _only_glove(link, say)
        what = ("the receiver" if kind == DONGLE
                else f"the glove {updater.glove_device_id}")
        before = wait_version(updater, dfu, VERSION_WAIT_S)
        say(f"running: {before if before else 'no version reply'}")
        if not yes and not _agreed(ask or input, f"Flash {what} (running "
                                        f"{before if before else 'an unknown version'}) "
                                        f"with {description}? [y/N] "):
            say("Nothing was flashed.")
            return 1
        try:
            confirmed = run_update(updater, dfu, bin_path)
        except KeyboardInterrupt:
            say("aborting...")
            updater.abort_update(dfu)
            time.sleep(1.0)
            return 130
        say("DONE: the new image is confirmed" if confirmed else "FAILED")
        return 0 if confirmed else 1
    finally:
        link.close()


def _only_glove(link: ReceiverLink, say: Callable[[str], None]) -> str:
    sending = streaming_gloves(link, GLOVE_LISTEN_S)
    if not sending:
        raise PandoraError("no glove is sending through this receiver. Switch it on, or "
                           "name it with --device.")
    if len(sending) > 1:
        raise PandoraError(f"more than one glove is sending ({', '.join(sorted(sending))}); "
                           "say which with --device")
    device = next(iter(sending))
    say(f"glove: {device} (the only glove sending)")
    return device


def _agreed(ask: Callable[[str], str], question: str) -> bool:
    try:
        return ask(question).strip().lower() in ("y", "yes")
    except EOFError:  # no one at a terminal to answer
        return False


class _Updater(FirmwareUpdater):
    """The core's updater, saying what it does: its messages, state changes
    and errors as they come, its progress in steps of 10 %. It is handed the
    receiver's text lines (on_line) and takes the DFU ones."""

    def __init__(self, target: DfuTarget, say: Callable[[str], None], verbose: bool) -> None:
        super().__init__()
        self._target, self._say, self._verbose = target, say, verbose
        self._shown_pct = -10

    def on_line(self, text: str) -> None:
        if text.startswith("DFU:"):
            self.process_serial_line(text)
        if self._verbose and not text.startswith(("DFU:GLOVERX:", "DFU:SELFACK:")):
            self._say(text)

    def _on_info_message(self, pipe: int, message: str) -> None:
        self._say(message)

    def _on_error_occurred(self, pipe: int, message: str) -> None:
        self._say(f"ERROR {message}")

    def _on_state_changed(self, pipe: int, state_name: str) -> None:
        self._say(f"state {state_name}")

    def _on_progress_changed(self, pipe: int, percent: int) -> None:
        if pipe == self._target.value and percent >= self._shown_pct + 10:
            self._shown_pct = percent - percent % 10
            self._say(f"{percent:3d}%")

    _on_upload_progress = _on_progress_changed
