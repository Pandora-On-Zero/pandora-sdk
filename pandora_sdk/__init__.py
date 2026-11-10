"""Hands from the 01 Glove in a few lines of Python.

    import pandora_sdk

    with pandora_sdk.connect() as gloves:              # through the running app
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

connect() reads through the app, which must be running with its Protocol
adapter switched on. open_receiver() reads the receiver itself, with no app.
Both give the same Frame and Raw; what they hold is in pandora-core's
PROTOCOL.md.
"""
from pandora_core.glove import Profile
from pandora_core.protocol import (DEFAULT_PORT, Device, Frame, ProtocolError, Raw,
                                   Subscription)

from pandora_sdk.client import Client, Server, connect
from pandora_sdk.local import Local, load_profiles, open_receiver
from pandora_sdk.source import (ConnectionFailed, Disconnected, PandoraError, ServerError,
                                Source)

__all__ = ["DEFAULT_PORT", "Client", "ConnectionFailed", "Device", "Disconnected", "Frame",
           "Local", "PandoraError", "Profile", "ProtocolError", "Raw", "Server", "ServerError",
           "Source", "Subscription", "connect", "load_profiles", "open_receiver"]
