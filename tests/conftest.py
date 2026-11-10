import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from standin import StandIn  # noqa: E402


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
