"""The exception hierarchy (the tests that need a peer are in test_client.py)."""

import stintmate_ipc as ipc


def test_hierarchy():
    assert issubclass(ipc.PipeError, ipc.IpcError) and issubclass(ipc.PipeError, OSError)
    for c in (ipc.PipeNotFound, ipc.PipeBusy, ipc.PipeClosed, ipc.EndpointInUse):
        assert issubclass(c, ipc.PipeError)
    for c in (ipc.KeyFileError, ipc.ProtocolError, ipc.HandshakeError, ipc.NotConnected,
              ipc.ConnectionLost, ipc.CallTimeout, ipc.CallCancelled, ipc.RemoteError):
        assert issubclass(c, ipc.IpcError)
    assert issubclass(ipc.CallTimeout, TimeoutError)
    assert not issubclass(ipc.CallCancelled, ipc.CallTimeout)
    assert ipc.ProtocolError.code == "protocol_error" and ipc.CallCancelled.code == "cancelled"


def test_remote_error_fields():
    e = ipc.RemoteError("busy", "try later", {"a": 1}, 250)
    assert (e.code, e.message, e.data, e.details, e.retry_ms) == (
        "busy", "try later", {"a": 1}, {"a": 1}, 250)
    assert str(e) == "busy: try later" and str(ipc.RemoteError("x")) == "x"
    assert ipc.RemoteError("x", details={"d": 1}).data == {"d": 1}


def test_handshake_error_fields():
    e = ipc.HandshakeError("unauthorized", "bad mac")
    assert e.code == "unauthorized" and e.message == "bad mac"
    assert str(e) == "unauthorized: bad mac"
