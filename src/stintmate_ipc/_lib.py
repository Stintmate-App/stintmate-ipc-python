"""The native library: loading, prototypes, callbacks, thread hooks, logging, errors.

Everything that touches ``stintmate_ipc.dll`` directly lives here; the other modules call
``lib.stintmate_ipc_*`` through the helpers below. The design:

* one module-level ``CFUNCTYPE`` trampoline per callback type, never freed; ``user_data`` is an
  integer key into :data:`registry` and the ``release`` trampoline deletes the key;
* the DLL's thread hooks pin a Python thread state per library thread, so a callback does not
  create and destroy a thread state on every invocation;
* every trampoline catches ``BaseException`` and logs it (an exception escaping a ctypes
  callback would only be printed);
* library log records go to :mod:`logging`; errors are read from the DLL's error record.
"""

from __future__ import annotations

import ctypes as C
import struct
import logging
import sys
import threading
from collections import namedtuple
from pathlib import Path
from typing import Any

import msgpack

from stintmate_ipc import _errors as E

_log = logging.getLogger("stintmate_ipc")

if not getattr(sys, "_is_gil_enabled", lambda: True)():
    raise ImportError("stintmate_ipc does not support free-threaded Python builds (the GIL is "
                      "disabled); use a standard CPython 3.12 build")

ABI_MAJOR = 1

# --------------------------------------------------------------------------- constants
OK = 0
E_NOT_FOUND, E_BUSY, E_IN_USE, E_HANDSHAKE, E_KEYFILE = -1, -2, -3, -4, -5
E_NOT_CONNECTED, E_LOST, E_TIMEOUT, E_CANCELLED, E_PROTOCOL = -6, -7, -8, -9, -10
E_INVALID_ARG, E_PANIC, E_REMOTE, E_TOO_LARGE, E_IO = -11, -12, -13, -14, -15
E_INTERNAL, E_WAIT_TIMEOUT, E_ENCODE, E_CLOSED, E_SHUTDOWN = -16, -17, -18, -19, -20
FOREVER = -1
METHOD_ORDERED, METHOD_INLINE, METHOD_IDEMPOTENT, METHOD_NO_IDEMPOTENCY_CACHE = 1, 2, 4, 8
TS_NOW = -(2**63)
PEER_CONNECT, PEER_DISCONNECT, PEER_REDECLARE = 1, 2, 3
DELIVERY_SNAPSHOT, DELIVERY_DROPPED = 1, 2
KIND_EVENT, KIND_SAMPLE = 0, 1

# --------------------------------------------------------------------------- structs
vp = C.c_void_p
sz = C.c_size_t
i32, i64, u32, u64 = C.c_int32, C.c_int64, C.c_uint32, C.c_uint64


class Buffer(C.Structure):
    """``stintmate_ipc_buffer``: owned by the caller, released with ``free_buffer``."""

    _fields_ = [("ptr", vp), ("len", sz)]


class CPeerInfo(C.Structure):
    _fields_ = [("app", C.c_char_p), ("name", C.c_char_p), ("version", C.c_char_p),
                ("process", C.c_char_p), ("meta", vp), ("meta_len", sz)]


class CKeySource(C.Structure):
    _fields_ = [("struct_size", sz), ("path", C.c_char_p), ("key", vp), ("key_len", sz),
                ("create", i32)]


class CClientOpts(C.Structure):
    _fields_ = [("struct_size", sz), ("reconnect", i32), ("event_queue", sz)]


# Callback types (cdecl; the DLL calls them from its own threads).
ERROR_CB = C.CFUNCTYPE(None, vp, vp, sz)
RELEASE_CB = C.CFUNCTYPE(None, vp)
PROGRESS_CB = C.CFUNCTYPE(None, vp, vp, sz)
RECONNECT_POLICY_CB = C.CFUNCTYPE(i64, vp, C.c_char_p, u32)


class CCallOpts(C.Structure):
    _fields_ = [("struct_size", sz), ("version", u32), ("timeout_ms", i64),
                ("idempotency_key", C.c_char_p), ("on_progress", PROGRESS_CB),
                ("user_data", vp), ("release", RELEASE_CB)]


class CDelivery(C.Structure):
    _fields_ = [("kind", i32), ("flags", u32), ("channel", vp), ("channel_len", sz),
                ("seq", u64), ("ts_ns", i64), ("has_ts", i32), ("data", vp), ("len", sz)]


DELIVERY_CB = C.CFUNCTYPE(None, vp, vp)  # the second argument is a CDelivery address
END_CB = C.CFUNCTYPE(None, vp, vp, C.c_char_p, C.c_char_p, C.c_char_p)
HANDSHAKE_FAILED_CB = C.CFUNCTYPE(None, vp, i64, vp, sz)
HANDLER_CB = C.CFUNCTYPE(None, vp, vp, vp, sz)
PEER_CB = C.CFUNCTYPE(None, vp, i32, vp)
ACCEPT_CB = C.CFUNCTYPE(i32, vp, vp, sz)
SUBSCRIBE_CB = C.CFUNCTYPE(i32, vp, vp, vp, vp, sz)
ACTIVE_CB = C.CFUNCTYPE(None, vp, i32)
DONE_CB = C.CFUNCTYPE(None, vp, vp)
THREAD_CB = C.CFUNCTYPE(None, vp)
LOG_CB = C.CFUNCTYPE(None, vp, i32, C.c_char_p, C.c_char_p)


class CSubscribeOpts(C.Structure):
    _fields_ = [("struct_size", sz), ("version", u32), ("options", vp), ("options_len", sz),
                ("timeout_ms", i64), ("on_delivery", DELIVERY_CB), ("on_end", END_CB),
                ("user_data", vp), ("release", RELEASE_CB)]


class CEndpointOpts(C.Structure):
    _fields_ = [("struct_size", sz), ("pipe", C.c_char_p), ("registry", C.c_char_p),
                ("handshake_timeout_ms", i64), ("handler_threads", u32),
                ("heartbeat_ms", u32), ("event_queue", sz), ("event_queue_bytes", sz),
                ("max_inflight", sz), ("features", vp), ("features_len", sz)]


class CMethodOpts(C.Structure):
    _fields_ = [("struct_size", sz), ("flags", u32), ("versions", vp), ("n_versions", sz),
                ("idempotency_ttl_ms", i64), ("doc", C.c_char_p), ("params_schema", vp),
                ("params_schema_len", sz), ("result_schema", vp), ("result_schema_len", sz)]


# --------------------------------------------------------------------------- prototypes
S = C.c_char_p
PBUF = C.POINTER(Buffer)
PPTR = C.POINTER(vp)
PSZ = C.POINTER(sz)
_PI, _KS, _CO = C.POINTER(CPeerInfo), C.POINTER(CKeySource), C.POINTER(CClientOpts)
_CALLO, _SUBO = C.POINTER(CCallOpts), C.POINTER(CSubscribeOpts)
_EPO, _MO = C.POINTER(CEndpointOpts), C.POINTER(CMethodOpts)

# name (without the ``stintmate_ipc_`` prefix) -> (restype, argtypes). One entry per function of
# ``stintmate_ipc.h``; tests/test_lib.py checks the table against the header.
PROTOTYPES: dict[str, tuple[Any, list[Any]]] = {
    "abi_version": (u32, []),
    "version": (S, []),
    "spec_revision": (S, []),
    "protocol_version": (u32, []),
    "last_error_message": (sz, [vp, sz]),
    "last_error": (i32, [PBUF]),
    "free_buffer": (None, [Buffer]),
    "live_buffers": (C.c_ssize_t, []),
    "client_new": (i32, [S, _PI, _KS, _CO, PPTR]),
    "client_start": (i32, [vp]),
    "client_wait_connected": (i32, [vp, i64, PPTR]),
    "client_connect": (i32, [vp, i64, PPTR]),
    "client_is_connected": (i32, [vp]),
    "client_peer": (i32, [vp, PPTR]),
    "client_last_error": (i32, [vp, PBUF]),
    "on_connect_error": (i32, [vp, ERROR_CB, vp, RELEASE_CB]),
    "client_close": (i32, [vp]),
    "client_free": (None, [vp]),
    "reconnect_delay_ms": (i64, [S, u32]),
    "client_set_reconnect_policy": (i32, [vp, RECONNECT_POLICY_CB, vp, RELEASE_CB]),
    "connect": (i32, [S, _PI, _KS, i64, vp, sz, PPTR]),
    "client_call": (i32, [vp, S, vp, sz, _CALLO, PBUF]),
    "client_call_async": (i32, [vp, S, vp, sz, _CALLO, PPTR]),
    "client_subscribe": (i32, [vp, S, _SUBO, PPTR]),
    "endpoint_new": (i32, [_PI, _KS, _EPO, PPTR]),
    "endpoint_node": (vp, [vp]),
    "endpoint_start": (i32, [vp]),
    "endpoint_info": (i32, [vp, PBUF]),
    "endpoint_stop": (i32, [vp, S, S]),
    "on_handshake_failed": (i32, [vp, HANDSHAKE_FAILED_CB, vp, RELEASE_CB]),
    "endpoint_free": (None, [vp]),
    "ensure_key": (i32, [S, PBUF]),
    "read_key": (i32, [S, PBUF]),
    "parse_key": (i32, [vp, sz, PBUF]),
    "format_key": (i32, [vp, sz, PBUF]),
    "default_key_path": (i32, [S, PBUF]),
    "pipe_user": (i32, [S, PBUF]),
    "short_name": (i32, [S, S, S, PBUF]),
    "pipe_path": (i32, [S, S, S, PBUF]),
    "find_workspace": (i32, [PBUF]),
    "registry_dir": (i32, [S, PBUF]),
    "valid_app": (i32, [S]),
    "valid_name": (i32, [S]),
    "layout_normalize": (i32, [vp, sz, PBUF]),
    "method": (i32, [vp, S, _MO, HANDLER_CB, vp, RELEASE_CB]),
    "remove_method": (i32, [vp, S]),
    "declarations": (i32, [vp, PBUF]),
    "peers": (i32, [vp, PPTR, sz, PSZ]),
    "on_peer": (i32, [vp, PEER_CB, vp, RELEASE_CB]),
    "request_info": (i32, [vp, PBUF]),
    "request_peer": (vp, [vp]),
    "request_is_cancelled": (i32, [vp]),
    "request_wait_cancelled": (i32, [vp, i64]),
    "request_remaining_ms": (i64, [vp]),
    "request_progress": (i32, [vp, vp, sz, i64]),
    "respond": (i32, [vp, vp, sz]),
    "respond_error": (i32, [vp, S, S, vp, sz, i64]),
    "event_channel": (i32, [vp, S, vp, sz, PPTR]),
    "state_channel": (i32, [vp, S, vp, sz, vp, sz, PPTR]),
    "stream_channel": (i32, [vp, S, vp, sz, vp, sz, C.c_double, sz, PPTR]),
    "channel_info": (i32, [vp, PBUF]),
    "channel_subscribers": (i64, [vp]),
    "channel_is_active": (i32, [vp]),
    "channel_close": (i32, [vp, S, S]),
    "publish": (i32, [vp, vp, sz, i64, u32, ACCEPT_CB, vp, PSZ]),
    "state_get": (i32, [vp, PBUF]),
    "stream_send": (i32, [vp, vp, sz, i64, PSZ]),
    "refuse": (i32, [vp, S, S, vp, sz]),
    "on_subscribe": (i32, [vp, SUBSCRIBE_CB, vp, RELEASE_CB]),
    "on_active": (i32, [vp, ACTIVE_CB, vp, RELEASE_CB]),
    "channel_free": (None, [vp]),
    "client_node": (vp, [vp]),
    "peer_ref": (vp, [vp]),
    "peer_free": (None, [vp]),
    "peer_id": (u64, [vp]),
    "peer_get_info": (i32, [vp, PBUF]),
    "peer_decl": (i32, [vp, PBUF]),
    "peer_is_open": (i32, [vp]),
    "peer_wait_closed": (i32, [vp, i64]),
    "peer_close_reason": (i32, [vp, PBUF]),
    "peer_close": (i32, [vp, S, S]),
    "peer_call": (i32, [vp, S, vp, sz, _CALLO, PBUF]),
    "peer_call_async": (i32, [vp, S, vp, sz, _CALLO, PPTR]),
    "call_id": (u64, [vp]),
    "call_is_done": (i32, [vp]),
    "call_wait": (i32, [vp, i64, PBUF]),
    "call_cancel": (i32, [vp, S]),
    "call_on_done": (i32, [vp, DONE_CB, vp, RELEASE_CB]),
    "call_free": (None, [vp]),
    "peer_subscribe": (i32, [vp, S, _SUBO, PPTR]),
    "subscription_info": (i32, [vp, PBUF]),
    "subscription_is_active": (i32, [vp]),
    "subscription_wait": (i32, [vp, i32, i64]),
    "unsubscribe": (None, [vp]),
    "live_handles": (C.c_ssize_t, []),
    "set_thread_hooks": (i32, [THREAD_CB, THREAD_CB, vp]),
    "set_log_callback": (i32, [LOG_CB, vp, RELEASE_CB, i32]),
    "set_log_level": (i32, [i32]),
    "shutdown": (i32, [i64]),
}


def _load() -> C.WinDLL:
    path = Path(__file__).resolve().parent / "_bin" / "stintmate_ipc.dll"
    if not path.is_file():
        raise ImportError(f"stintmate_ipc.dll not found at {path}: the wheel was built without "
                          "the native library (see the package README)")
    dll = C.WinDLL(str(path))
    for name, (res, args) in PROTOTYPES.items():
        fn = getattr(dll, "stintmate_ipc_" + name)
        fn.restype = res
        fn.argtypes = args
    major = (dll.stintmate_ipc_abi_version() >> 16) & 0xFFFF
    # abi_version() packs major in the high 16 bits (minor in the low ones)
    if major != ABI_MAJOR:
        raise ImportError(f"stintmate_ipc.dll has ABI major {major}, this package needs "
                          f"{ABI_MAJOR}")
    return dll


lib = _load()


def fn(name: str):
    """The foreign function ``stintmate_ipc_<name>`` (prototype already set)."""
    return _FUNCS[name]


_FUNCS = {name: getattr(lib, "stintmate_ipc_" + name) for name in PROTOTYPES}
# bound once for the hot paths (no lookup per call)
peer_call = _FUNCS["peer_call"]
client_call = _FUNCS["client_call"]
respond = _FUNCS["respond"]
_free_buffer = lib.stintmate_ipc_free_buffer
_string_at = C.string_at
publish = _FUNCS["publish"]
stream_send = _FUNCS["stream_send"]
NULL_ACCEPT = ACCEPT_CB()
_NULL_RELEASE = RELEASE_CB()
_NULL_PROGRESS = PROGRESS_CB()
_cstr_cache: dict[str, bytes] = {}


# --------------------------------------------------------------------------- strings, buffers
def vptr(b: bytes | None) -> int | None:
    """The address of a ``bytes`` object for a ``void *`` struct field (keep ``b`` alive)."""
    return None if b is None else C.cast(b, vp).value


def cstr(s: str | None) -> bytes | None:
    if s is None:
        return None
    b = _cstr_cache.get(s)
    if b is None:
        b = s.encode("utf-8")
        if len(_cstr_cache) < 256 and len(s) < 128:
            _cstr_cache[s] = b
    return b


def take(buf: Buffer) -> bytes:
    """Copies a library buffer and releases it."""
    try:
        return _string_at(buf.ptr, buf.len) if buf.ptr and buf.len else b""
    finally:
        _free_buffer(buf)


def version() -> str:
    return lib.stintmate_ipc_version().decode()


def spec_revision() -> str:
    return lib.stintmate_ipc_spec_revision().decode()


def protocol_version() -> int:
    return lib.stintmate_ipc_protocol_version()


# --------------------------------------------------------------------------- errors
def _record() -> dict[str, Any]:
    buf = Buffer()
    if lib.stintmate_ipc_last_error(C.byref(buf)) != 0:
        return {"message": "unknown error"}
    data = take(buf)
    try:
        return msgpack.unpackb(data, raw=False) if data else {}
    except Exception:  # noqa: BLE001 - never mask the original failure
        return {"message": "unreadable error record"}


def error_for(rc: int, rec: dict[str, Any] | None = None) -> BaseException:
    """The exception for a failing status (the same classes and attributes as 0.2)."""
    rec = _record() if rec is None else rec
    msg = str(rec.get("message", ""))
    os_error = int(rec.get("os_error") or 0)
    if rc == E_NOT_FOUND:
        return E.PipeNotFound(os_error or 2, msg)
    if rc == E_BUSY:
        return E.PipeBusy(os_error or 231, msg)
    if rc == E_IN_USE:
        return E.EndpointInUse(os_error or 5, msg)
    if rc == E_HANDSHAKE:
        return E.HandshakeError(str(rec.get("code") or "protocol_error"), msg)
    if rc == E_KEYFILE:
        return E.KeyFileError(msg)
    if rc == E_NOT_CONNECTED:
        return E.NotConnected(msg)
    if rc == E_LOST:
        return E.ConnectionLost(msg)
    if rc in (E_TIMEOUT, E_WAIT_TIMEOUT):
        return E.CallTimeout(msg)
    if rc == E_CANCELLED:
        return E.CallCancelled(msg)
    if rc == E_REMOTE:
        data = rec.get("data")
        retry = rec.get("retry_ms")
        return E.RemoteError(str(rec.get("code") or "internal"), msg, data,
                             None if retry is None else int(retry))
    if rc in (E_PROTOCOL, E_TOO_LARGE):
        return E.ProtocolError(msg)
    if rc == E_ENCODE:
        return TypeError(msg)
    if rc == E_INVALID_ARG:
        return ValueError(msg)
    if rc == E_IO:
        return E.PipeError(os_error, msg)
    if rc == E_CLOSED:
        return E.PipeClosed(os_error, msg)
    return E.IpcError(msg or f"status {rc}")


def check(rc: int) -> int:
    """Raises the exception for a negative status (read on the calling thread); else ``rc``."""
    if rc < 0:
        raise error_for(rc)
    return rc


def call_buf(name: str, *args: Any) -> bytes:
    """Calls ``stintmate_ipc_<name>(*args, &buf)``; returns the checked, released buffer."""
    buf = Buffer()
    check(fn(name)(*args, C.byref(buf)))
    return take(buf)


# --------------------------------------------------------------------------- user_data registry
registry: dict[int, Any] = {}
# Reentrant: inserting under the lock can run the garbage collector, and a dying object's
# __del__ (an Endpoint forgetting its keys) unregisters on the same thread.
_registry_lock = threading.RLock()
_next_key = 1  # 0 is the log callback's key


def register(obj: Any) -> int:
    """Stores ``obj`` (a callable or any state) and returns the integer to pass as user_data."""
    global _next_key
    with _registry_lock:
        key = _next_key
        _next_key += 1
        registry[key] = obj
    return key


def unregister(key: int) -> None:
    with _registry_lock:
        dropped = registry.pop(key, None)
    # Released outside the lock: dropping the last reference can run a __del__ that unregisters
    # (an Endpoint forgetting its keys); nothing user-defined runs while the lock is held.
    del dropped


def _guarded(default: Any = None):
    """A trampoline body: look the callable up, call it, log what it raises."""

    def run(user_data: int | None, *args: Any) -> Any:
        target = registry.get(user_data or 0)
        if target is None:
            return default
        try:
            return target(*args)
        except BaseException:  # noqa: BLE001 - must never escape into C
            _log.exception("exception in a stintmate_ipc callback")
            return default

    return run


_run = _guarded()
_run_accept = _guarded(0)    # a failing accept filter sends nothing
_run_subscribe = _guarded(1)  # a failing subscribe hook refuses (nonzero)

Delivery = namedtuple("Delivery", "kind flags channel seq ts_ns data")


@RELEASE_CB
def release_tramp(user_data):
    unregister(user_data or 0)


@ERROR_CB
def error_tramp(user_data, error, length):
    _run(user_data, C.string_at(error, length) if error and length else b"")


@RECONNECT_POLICY_CB
def reconnect_policy_tramp(user_data, code, attempt):
    # The registered callable owns the fallback to the built-in policy (it needs the client).
    try:
        target = registry.get(user_data or 0)
        if target is None:
            return lib.stintmate_ipc_reconnect_delay_ms(code, attempt)
        return int(target(code, attempt))
    except BaseException:  # noqa: BLE001 - must never escape into C
        _log.exception("exception in the reconnect policy")
        return lib.stintmate_ipc_reconnect_delay_ms(code, attempt)


@PROGRESS_CB
def progress_tramp(user_data, data, length):
    _run(user_data, C.string_at(data, length) if data and length else b"")


_DELIVERY = struct.Struct("<IIQQQqi4xQQ")  # CDelivery: kind flags channel clen seq ts has_ts data len
assert _DELIVERY.size == C.sizeof(CDelivery)
_channels: dict[bytes, str] = {}
_new_delivery = tuple.__new__


@DELIVERY_CB
def delivery_tramp(user_data, d):
    target = registry.get(user_data or 0)
    if target is None:
        return
    try:
        kind, flags, cptr, clen, seq, ts, has_ts, dptr, dlen = _DELIVERY.unpack(
            _string_at(d, _DELIVERY.size))
        if cptr:
            raw = _string_at(cptr, clen)
            channel = _channels.get(raw)
            if channel is None:
                channel = raw.decode("utf-8")
                if len(_channels) < 256:
                    _channels[raw] = channel
        else:
            channel = ""
        target(_new_delivery(Delivery, (kind, flags, channel, seq, ts if has_ts else None,
                                        _string_at(dptr, dlen) if dptr and dlen else b"")))
    except BaseException:  # noqa: BLE001 - must never escape into C
        _log.exception("exception in a stintmate_ipc callback")


@END_CB
def end_tramp(user_data, sub, channel, code, message):
    # ``replace``: a bad byte must not lose the end (the subscription would never finish).
    _run(user_data, sub, (channel or b"").decode(errors="replace"),
         (code or b"").decode(errors="replace"), (message or b"").decode(errors="replace"))


@HANDSHAKE_FAILED_CB
def handshake_failed_tramp(user_data, pid, error, length):
    _run(user_data, pid, C.string_at(error, length) if error and length else b"")


@HANDLER_CB
def handler_tramp(user_data, req, params, length):
    # ``req`` is valid until the handler answers it; ``params`` is copied here.
    target = registry.get(user_data or 0)
    if target is None:
        return
    try:
        target(req, _string_at(params, length) if params and length else b"")
    except BaseException:  # noqa: BLE001 - must never escape into C
        _log.exception("exception in a stintmate_ipc callback")


@PEER_CB
def peer_tramp(user_data, event, peer):
    _run(user_data, event, peer)


@ACCEPT_CB
def accept_tramp(user_data, view, length):
    return _run_accept(user_data, C.string_at(view, length) if view and length else b"")


@SUBSCRIBE_CB
def subscribe_tramp(user_data, ctx, peer, req, length):
    return _run_subscribe(user_data, ctx, peer, C.string_at(req, length) if req and length else b"")


@ACTIVE_CB
def active_tramp(user_data, active):
    _run(user_data, bool(active))


@DONE_CB
def done_tramp(user_data, pending):
    _run(user_data, pending)


# --------------------------------------------------------------------------- thread hooks
_pin = threading.local()
_gil = C.pythonapi.PyGILState_Ensure
_gil.restype = C.c_int
_gil.argtypes = []
_gil_release = C.pythonapi.PyGILState_Release
_gil_release.restype = None
_gil_release.argtypes = [C.c_int]


@THREAD_CB
def _thread_start(_user_data):
    # The unmatched Ensure keeps this library thread's Python thread state alive, so the
    # callbacks that follow do not create and destroy one each (about 7 us saved per callback).
    try:
        _pin.state = _gil()
    except BaseException:  # noqa: BLE001
        pass


@THREAD_CB
def _thread_stop(_user_data):
    try:
        state = getattr(_pin, "state", None)
        _pin.state = None
        if state is not None and not sys.is_finalizing():
            _gil_release(state)
    except BaseException:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- log bridge
@LOG_CB
def _log_tramp(_user_data, level, target, message):
    try:
        name = (target or b"stintmate_ipc").decode("utf-8", "replace").replace("::", ".")
        logging.getLogger(name).log(level, (message or b"").decode("utf-8", "replace"))
    except BaseException:  # noqa: BLE001
        pass


def _library_level() -> int:
    """The DLL's level filter for the ``stintmate_ipc`` logger's effective level (0: off)."""
    lvl = _log.getEffectiveLevel()
    if lvl <= 10:
        return 10
    if lvl <= 20:
        return 20
    if lvl <= 30:
        return 30
    return 40 if lvl <= 40 else 0


def refresh_log_level() -> None:
    """Applies the ``stintmate_ipc`` logger's level to the library's log filter."""
    lib.stintmate_ipc_set_log_level(_library_level())


def shutdown(timeout_ms: int = 5000) -> int:
    """``stintmate_ipc_shutdown`` (registered with ``atexit`` by the package)."""
    return lib.stintmate_ipc_shutdown(timeout_ms)


# Set before any handle exists (the hooks must precede the first handle).
check(lib.stintmate_ipc_set_thread_hooks(_thread_start, _thread_stop, None))
check(lib.stintmate_ipc_set_log_callback(_log_tramp, None, RELEASE_CB(), _library_level()))
