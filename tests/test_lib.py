"""The prototypes of ``_lib`` against the header of the library."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from stintmate_ipc import _lib

def _find_header() -> Path:
    """``stintmate_ipc.h`` next to the native files (STINTMATE_IPC_PREBUILT, filled by
    scripts/fetch-native.ps1), else in a ``../stintmate-ipc`` checkout."""
    prebuilt = os.environ.get("STINTMATE_IPC_PREBUILT")
    if prebuilt:
        return Path(prebuilt) / "stintmate_ipc.h"
    return (Path(__file__).resolve().parents[2] / "stintmate-ipc" / "crates" / "stintmate-ipc-ffi"
            / "include" / "stintmate_ipc.h")


HEADER = _find_header()


def header_functions() -> dict[str, int]:
    """Function name (without the prefix) -> number of parameters, from the C header."""
    text = re.sub(r"/\*.*?\*/", "", HEADER.read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"typedef[^;]*?\(\*[^)]*\)[^;]*;", "", text, flags=re.S)  # callback types
    out: dict[str, int] = {}
    for m in re.finditer(r"[\w *]+?\b(stintmate_ipc_\w+)\s*\(([^)]*)\)\s*;", text):
        params = m.group(2).strip()
        n = 0 if params in ("", "void") else params.count(",") + 1
        out[m.group(1)[len("stintmate_ipc_"):]] = n
    return out


def test_header_is_found_and_parsed():
    funcs = header_functions()
    assert len(funcs) > 90 and "client_call" in funcs and "shutdown" in funcs


def test_every_function_has_a_prototype():
    funcs = header_functions()
    assert set(funcs) - set(_lib.PROTOTYPES) == set()
    assert set(_lib.PROTOTYPES) - set(funcs) == set()


def test_parameter_counts_match_the_header():
    for name, n in header_functions().items():
        assert len(_lib.PROTOTYPES[name][1]) == n, name


@pytest.mark.parametrize("name", sorted(_lib.PROTOTYPES))
def test_prototype_is_applied(name):
    f = _lib.fn(name)
    res, args = _lib.PROTOTYPES[name]
    assert f.restype == res and list(f.argtypes) == args


def test_status_codes_match_the_header():
    text = HEADER.read_text(encoding="utf-8")
    for m in re.finditer(r"#define STINTMATE_IPC_(E_\w+|OK) (-?\d+)", text):
        assert getattr(_lib, m.group(1)) == int(m.group(2)), m.group(1)


def test_errors_map_to_the_classes():
    import stintmate_ipc as ipc
    err = _lib.error_for
    assert isinstance(err(_lib.E_NOT_FOUND, {"message": "m"}), ipc.PipeNotFound)
    assert err(_lib.E_NOT_FOUND, {}).errno == 2 and err(_lib.E_BUSY, {}).errno == 231
    assert err(_lib.E_IN_USE, {}).errno == 5
    h = err(_lib.E_HANDSHAKE, {"code": "unauthorized", "message": "x"})
    assert isinstance(h, ipc.HandshakeError) and h.code == "unauthorized"
    r = err(_lib.E_REMOTE, {"code": "busy", "message": "m", "data": {"a": 1}, "retry_ms": 5})
    assert (r.code, r.message, r.data, r.retry_ms) == ("busy", "m", {"a": 1}, 5)
    assert isinstance(err(_lib.E_TIMEOUT, {}), ipc.CallTimeout)
    assert isinstance(err(_lib.E_CANCELLED, {}), ipc.CallCancelled)
    assert isinstance(err(_lib.E_ENCODE, {}), TypeError)
    assert isinstance(err(_lib.E_INVALID_ARG, {}), ValueError)
    assert isinstance(err(_lib.E_PANIC, {}), ipc.IpcError)
    assert err(_lib.E_IO, {"os_error": 109}).errno == 109


def test_no_buffer_or_handle_leaks():
    from stintmate_ipc import Layout
    from stintmate_ipc.keyfile import format_key, parse_key
    for _ in range(50):
        parse_key(format_key(bytes(32)))
        Layout([("a", "u8")])
    import gc
    import time
    end = time.monotonic() + 3.0  # handles of earlier tests free with their Python objects
    while True:  # (releases of callbacks run on the library's hooks thread: give them a moment)
        gc.collect()
        if (_lib.lib.stintmate_ipc_live_buffers() == 0
                and _lib.lib.stintmate_ipc_live_handles() == 0) or time.monotonic() > end:
            break
        time.sleep(0.01)
    assert _lib.lib.stintmate_ipc_live_buffers() == 0
    assert _lib.lib.stintmate_ipc_live_handles() == 0


def test_log_bridge_reaches_logging(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="a.b"):
        _lib._log_tramp(None, 30, b"a::b", b"hello")
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("a.b", 30, "hello")]


def test_trampoline_catches_and_registry_releases(caplog):
    seen = []
    key = _lib.register(lambda data: seen.append(data) or 1 / 0)
    _lib.progress_tramp(key, None, 0)
    assert seen == [b""] and any("callback" in r.getMessage() for r in caplog.records)
    _lib.release_tramp(key)
    assert key not in _lib.registry
