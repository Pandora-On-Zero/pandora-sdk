"""Hands from the 01 Glove app in a few lines of Python.

    import pandora_sdk

    with pandora_sdk.connect() as gloves:
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

The app must be running with its Protocol adapter switched on. What a Frame
and a Raw hold is in pandora-core's PROTOCOL.md.
"""
from pandora_core.protocol import (DEFAULT_PORT, Device, Frame, ProtocolError, Raw,
                                   Subscription)

from pandora_sdk.client import (Client, ConnectionFailed, Disconnected, PandoraError, Server,
                                ServerError, connect)

__all__ = ["DEFAULT_PORT", "Client", "ConnectionFailed", "Device", "Disconnected", "Frame",
           "PandoraError", "ProtocolError", "Raw", "Server", "ServerError", "Subscription",
           "connect"]
