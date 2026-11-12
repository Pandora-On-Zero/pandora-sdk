"""Hands from the 01 Glove in a few lines of Python.

    import pandora_sdk

    with pandora_sdk.connect() as gloves:              # through the running app
        gloves.subscribe(hands=["right"], rate_hz=50, bones=False)
        for hand in gloves.frames():
            print(hand.angles_deg)

connect() reads through the app, which must be running with its Protocol
adapter switched on. open_receiver() reads the receiver itself, with no app,
from the profiles the `pandora calibrate` command writes
(pandora_sdk/calibrate.py). Both give the same Frame, Raw and Debug, take the
same commands for a glove (haptics, Hall range) and give its Calibration;
what they hold is in PROTOCOL.md (Pandora repository).
"""
from pandora_core_rs.glove import Profile
from pandora_core_rs.protocol import (DEFAULT_PORT, Calibration, Debug, Device, Frame,
                                      ProtocolError, Raw, Subscription)
from pandora_core_rs.receiver import ReceiverPort

from pandora_sdk.client import Client, Server, connect
from pandora_sdk.local import (Local, default_profiles_dir, list_receivers, load_profiles,
                               open_link, open_receiver, save_profile)
from pandora_sdk.source import (CONNECTION_FAILED_CODES, DISCONNECTED_CODES, ConnectionFailed,
                                Disconnected, PandoraError, ServerError, Source)

__all__ = ["CONNECTION_FAILED_CODES", "DEFAULT_PORT", "DISCONNECTED_CODES", "Calibration",
           "Client", "ConnectionFailed", "Debug", "Device", "Disconnected", "Frame", "Local",
           "PandoraError", "Profile", "ProtocolError", "Raw", "ReceiverPort", "Server",
           "ServerError", "Source", "Subscription", "connect", "default_profiles_dir",
           "list_receivers", "load_profiles", "open_link", "open_receiver", "save_profile"]
