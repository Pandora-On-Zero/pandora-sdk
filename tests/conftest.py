import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from standin import StandIn  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    """A home folder of the test's own: no test reads or writes the real
    ~/.pandora, where a person's calibrations are."""
    made = tmp_path / "home"
    made.mkdir()
    monkeypatch.setattr(Path, "home", lambda: made)
    return made


@pytest.fixture(autouse=True)
def no_real_receiver(monkeypatch) -> None:
    """No test finds or opens a real serial port: the receiver plugged into
    this computer, and the glove behind it, stay out of reach. A test that
    wants a port patches these with a made-up one (tests/fakelink.py)."""
    from pandora_core_rs import receiver

    def _refused(port, *_args, **_kwargs):
        raise AssertionError(f"a test tried to open the real port {port}")

    monkeypatch.setattr(receiver, "find_receivers", lambda: [])
    monkeypatch.setattr(receiver, "open_port", _refused)
    # It asks the OS about the named port's driver; a made-up one has none.
    monkeypatch.setattr(receiver, "prepare_link", lambda *_args, **_kwargs: None)


@pytest.fixture
def app():
    """A stand-in for the app's protocol server on a free port."""
    made = StandIn()
    yield made
    made.close()


@pytest.fixture
def free_port() -> int:
    """A localhost port nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
