"""Stintmate IPC v2 for Python: ctypes over the native ``stintmate_ipc.dll``.

The public surface is the one of v0.2.0 (the PyO3 binding). The client side (``_client``) and
the endpoint side (``_endpoint``) names are imported lazily (module ``__getattr__``) so the
core of the package loads on its own.
"""

import atexit as _atexit
import importlib as _importlib

from stintmate_ipc import _lib, check, decl, keyfile
from stintmate_ipc._errors import (CallCancelled, CallTimeout, ConnectionLost, EndpointInUse,
                                   HandshakeError, IpcError, KeyFileError, NotConnected,
                                   PipeBusy, PipeClosed, PipeError, PipeNotFound,
                                   ProtocolError, RemoteError)
from stintmate_ipc._keys import (KeyFile, PeerInfo, default_key_path, ensure_key, find_workspace,
                                 pipe_path, pipe_user, read_key, registry_dir, short_name)
from stintmate_ipc._layout import Layout
from stintmate_ipc._lib import spec_revision, version
from stintmate_ipc._naming import RegistryEntry, list_endpoints

PROTOCOL_VERSION = _lib.protocol_version()
__version__ = version()
_ = (check, decl, keyfile)

# Library threads must not attach to a finalizing interpreter.
_atexit.register(_lib.shutdown)

_LAZY = {
    "_client": ("Client", "ClientSubscription", "Event", "Peer", "PendingCall", "Sample",
                "Subscription", "connect"),
    "_endpoint": ("Context", "Endpoint", "EventChannel", "StateChannel", "StreamChannel",
                  "SubscribeRequest", "SubscriptionView"),
}
_OWNER = {name: mod for mod, names in _LAZY.items() for name in names}


def __getattr__(name: str):
    mod = _OWNER.get(name)
    if mod is None:
        raise AttributeError(f"module 'stintmate_ipc' has no attribute {name!r}")
    value = getattr(_importlib.import_module(f"stintmate_ipc.{mod}"), name)
    globals()[name] = value
    return value


__all__ = [
    "PROTOCOL_VERSION", "CallCancelled", "CallTimeout", "Client", "ClientSubscription",
    "ConnectionLost", "Context", "Endpoint", "EndpointInUse", "Event", "EventChannel", "HandshakeError", "IpcError", "KeyFile",
    "KeyFileError", "Layout", "NotConnected", "Peer", "PeerInfo", "PendingCall", "PipeBusy",
    "PipeClosed", "PipeError", "PipeNotFound", "ProtocolError", "RegistryEntry",
    "RemoteError", "Sample", "StateChannel", "StreamChannel", "SubscribeRequest",
    "Subscription", "SubscriptionView", "connect", "default_key_path", "ensure_key",
    "find_workspace", "list_endpoints", "pipe_path", "pipe_user", "read_key", "registry_dir",
    "short_name", "spec_revision", "version",
]
