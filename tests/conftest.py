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
