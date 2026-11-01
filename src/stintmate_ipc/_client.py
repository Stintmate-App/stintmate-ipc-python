"""The client side: ``Client``, ``Peer``, calls and subscriptions over the native library.

Blocking library calls run with the GIL
released (ctypes drops it around every foreign call); waits are cut into slices so Ctrl+C is
seen between them.
"""

from __future__ import annotations

import ctypes as C
import logging
import math
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from stintmate_ipc import _errors as E
from stintmate_ipc import _lib
from stintmate_ipc._codec import packb, unpackb
from stintmate_ipc._endpoint import Node
from stintmate_ipc._keys import KeyFile, PeerInfo, key_source_c
from stintmate_ipc._layout import Layout
from stintmate_ipc.decl import Declarations

_log = logging.getLogger("stintmate_ipc")

_SLICE = 0.1          # seconds: longest foreign wait before Python looks at its signals
_MAX_MS = 2**62       # a wait that no timer overflows on


# --------------------------------------------------------------------------- helpers
def _ms(name: str, seconds: float | None) -> int:
    """Seconds -> milliseconds for the library (``None``: forever)."""
    if seconds is None:
        return _lib.FOREVER
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or \
            math.isnan(seconds) or seconds < 0:
        raise ValueError(f"{name} must be a non-negative number of seconds")
    return min(int(math.ceil(seconds * 1000)), _MAX_MS)


def _free_quietly(name: str, ptr: int | None) -> None:
    if ptr:
        try:
            _lib.fn(name)(ptr)
        except BaseException:  # noqa: BLE001 - interpreter shutdown
            pass


def _record_error(rec: Any) -> BaseException:
    """The exception of an error record (``{status, message, code?, ...}``)."""
    if not isinstance(rec, dict):
        return E.IpcError("unknown error")
    return _lib.error_for(int(rec.get("status", -16)), rec)


def _out_ptr() -> C.c_void_p:
    return C.c_void_p()


class FlagEvent:
    """A ``threading.Event``-like view of a flag the library owns: ``is_set()`` and
    ``wait(timeout)`` (read live; it cannot be set or cleared from Python)."""

    __slots__ = ("_get", "_wait")

    def __init__(self, get: Callable[[], bool], wait: Callable[[int], bool]) -> None:
        self._get = get
        self._wait = wait

    def is_set(self) -> bool:
        return bool(self._get())

    def __bool__(self) -> bool:
        return bool(self._get())

    def wait(self, timeout: float | None = None) -> bool:
        end = None if timeout is None else time.monotonic() + _ms("timeout", timeout) / 1000
        while True:
            left = _SLICE if end is None else min(_SLICE, max(0.0, end - time.monotonic()))
            if self._wait(int(math.ceil(left * 1000))):
                return True
            if end is not None and time.monotonic() >= end:
                return bool(self._get())


# --------------------------------------------------------------------------- delivered objects
class Event:
    """One event or state value delivered to a subscriber."""

    __slots__ = ("channel", "seq", "ts", "data", "snapshot")

    def __init__(self, channel: str, seq: int, ts: int | None, data: Any, snapshot: bool) -> None:
        self.channel, self.seq, self.ts, self.data, self.snapshot = channel, seq, ts, data, snapshot

    def __repr__(self) -> str:
        return (f"Event({self.channel!r}, seq={self.seq}, snapshot={self.snapshot}, "
                f"data={self.data!r})")


class Sample:
    """One stream sample."""

    __slots__ = ("channel", "seq", "t_ns", "dropped", "body", "layout")

    def __init__(self, channel: str, seq: int, t_ns: int, dropped: bool, body: bytes,
                 layout: Layout) -> None:
        self.channel, self.seq, self.t_ns, self.dropped = channel, seq, t_ns, dropped
        self.body, self.layout = body, layout

    def values(self) -> tuple[Any, ...]:
        """The flat values of the body."""
        return self.layout.unpack(self.body)

    def as_dict(self) -> dict[str, Any]:
        """The values by field name."""
        return self.layout.unpack_dict(self.body)

    def __repr__(self) -> str:
        return f"Sample({self.channel!r}, seq={self.seq}, t_ns={self.t_ns}, dropped={self.dropped})"


# --------------------------------------------------------------------------- call options
_OPTS_CACHE: dict[tuple, _lib.CCallOpts] = {}  # read-only options of plain calls


def _call_opts(version: int, timeout: float | None, idempotency_key: str | None,
               on_progress: Callable[[Any], Any] | None) -> tuple[_lib.CCallOpts, tuple]:
    """The C options and what must stay alive for the call (the library releases ``user_data``
    on every return once it read the options)."""
    if idempotency_key is None and on_progress is None and type(timeout) in (int, float, type(None)) \
            and type(version) is int:
        ck = (version, timeout)
        o = _OPTS_CACHE.get(ck)
        if o is None:
            o = _lib.CCallOpts(C.sizeof(_lib.CCallOpts), version, _ms("timeout", timeout), None,
                               _lib._NULL_PROGRESS, 0, _lib._NULL_RELEASE)
            if len(_OPTS_CACHE) < 64:
                _OPTS_CACHE[ck] = o
        return o, ()
    ms = _ms("timeout", timeout)
    key = _lib.cstr(idempotency_key)
    user = 0
    release = _lib.RELEASE_CB()
    progress = _lib.PROGRESS_CB()
    if on_progress is not None:
        def run(data: bytes, cb=on_progress) -> None:
            try:
                cb(unpackb(data) if data else None)
            except BaseException:  # noqa: BLE001
                _log.exception("progress callback failed")

        user = _lib.register(run)
        release = _lib.release_tramp
        progress = _lib.progress_tramp
    o = _lib.CCallOpts(C.sizeof(_lib.CCallOpts), version, ms, key, progress, user, release)
    return o, (key,)


def _params(params: Any) -> bytes:
    return packb({} if params is None else params)


# --------------------------------------------------------------------------- PendingCall
class PendingCall:
    """A request in flight (future-like). ``result()`` waits and returns or raises."""

    def __init__(self, ptr: int, method: str) -> None:
        self._ptr = ptr
        self._method = method
        self._id = _lib.fn("call_id")(ptr)
        self._lock = threading.Lock()
        self._wait_lock = threading.Lock()
        self._outcome: tuple[bool, Any] | None = None
        self._callbacks: list[Callable[[PendingCall], Any]] = []
        self._hooked = False
        self._fired = False

    def __del__(self) -> None:
        _free_quietly("call_free", getattr(self, "_ptr", None))

    @property
    def id(self) -> int:
        return self._id

    @property
    def method(self) -> str:
        return self._method

    def done(self) -> bool:
        return self._outcome is not None or _lib.fn("call_is_done")(self._ptr) == 1

    # -- callbacks
    def _run(self, cb: Callable[[PendingCall], Any]) -> None:
        try:
            cb(self)
        except BaseException:  # noqa: BLE001
            _log.exception("done callback failed")

    def _fire(self, _ptr: int) -> None:
        with self._lock:
            self._fired = True
            cbs, self._callbacks = self._callbacks, []
        for cb in cbs:
            self._run(cb)

    def add_done_callback(self, cb: Callable[[PendingCall], Any]) -> None:
        """``cb(call)`` once done, on a library thread (or now, on this one, if already done)."""
        with self._lock:
            if not (self._fired or self.done()):
                self._callbacks.append(cb)
                if not self._hooked:
                    self._hooked = True
                    key = _lib.register(self._fire)
                    rc = _lib.fn("call_on_done")(self._ptr, _lib.done_tramp, key,
                                                 _lib.release_tramp)
                    if rc < 0:
                        _lib.unregister(key)
                        self._hooked = False
                        self._callbacks.remove(cb)
                        _lib.check(rc)
                return
        self._run(cb)

    # -- outcome
    def _take(self, wait_ms: int) -> tuple[bool, Any] | None:
        """Waits at most ``wait_ms`` for the end of the call; caches and returns the outcome."""
        if self._outcome is not None:
            return self._outcome
        with self._wait_lock:
            if self._outcome is not None:
                return self._outcome
            buf = _lib.Buffer()
            rc = _lib.fn("call_wait")(self._ptr, wait_ms, C.byref(buf))
            if rc == _lib.E_WAIT_TIMEOUT:
                return None
            if rc < 0:
                self._outcome = (False, _lib.error_for(rc))
            else:
                data = _lib.take(buf)
                try:
                    self._outcome = (True, unpackb(data) if data else None)
                except BaseException as e:  # noqa: BLE001
                    self._outcome = (False, e)
            return self._outcome

    def exception(self) -> BaseException | None:
        """The error of the call, or ``None`` (also while it is pending)."""
        if not self.done():
            return None
        out = self._take(0)
        return None if out is None or out[0] else out[1]

    def cancel(self, reason: str = "") -> bool:
        """Gives up: sends ``cancel`` and fails the call with ``CallCancelled``. False when the
        call was already done or its response won the race."""
        return _lib.check(_lib.fn("call_cancel")(self._ptr, _lib.cstr(reason))) == 1

    def result(self, timeout: float | None = None) -> Any:
        """Waits for the response. ``timeout`` only limits this wait (the call stays pending)."""
        end = None if timeout is None else time.monotonic() + _ms("timeout", timeout) / 1000
        while True:
            left = _SLICE if end is None else min(_SLICE, max(0.0, end - time.monotonic()))
            out = self._take(int(math.ceil(left * 1000)))
            if out is not None:
                if out[0]:
                    return out[1]
                raise out[1]
            if end is not None and time.monotonic() >= end:
                raise E.CallTimeout(f"{self._method}: no response yet")

    def __repr__(self) -> str:
        return f"PendingCall({self._method!r}, id={self._id}, done={self.done()})"


# --------------------------------------------------------------------------- subscriptions
class _SubState:
    """What the delivery and end trampolines of one subscription need."""

    def __init__(self, callback: Callable[[Event | Sample], Any],
                 on_end: Callable[[Any, str, str], Any] | None) -> None:
        self.callback = callback
        self.on_end = on_end
        self.obj: Any = None          # weakref to the Subscription / ClientSubscription
        self.ptr: int | None = None   # the library handle, once subscribe returned
        self.ready = threading.Event()
        self.layout: Layout | None = None
        self.ended_flag = False       # an end arrived (it may precede the object's _KEPT.add)

    def __call__(self, *args: Any) -> None:
        """The registry entry: one delivery, or (sub, channel, code, message) of an end."""
        if len(args) == 1:
            self.deliver(args[0])
        else:
            self.ended(*args)

    def _layout(self) -> Layout:
        if self.layout is None:
            self.ready.wait(5.0)
            self.layout = _sub_layout(self.ptr)
            if self.layout is None:
                raise E.ProtocolError("stream subscription without a layout")
        return self.layout

    def deliver(self, d: Any) -> None:
        try:
            if d.kind == _lib.KIND_SAMPLE:
                obj: Event | Sample = Sample(d.channel, d.seq, d.ts_ns or 0,
                                             bool(d.flags & _lib.DELIVERY_DROPPED), d.data,
                                             self._layout())
            else:
                obj = Event(d.channel, d.seq, d.ts_ns,
                            unpackb(d.data) if d.data else None,
                            bool(d.flags & _lib.DELIVERY_SNAPSHOT))
        except BaseException:  # noqa: BLE001
            _log.exception("subscription callback failed")
            return
        try:
            self.callback(obj)
        except BaseException:  # noqa: BLE001
            _log.exception("subscription callback failed")

    def ended(self, _sub: int | None, _channel: str, code: str, message: str) -> None:
        self.ended_flag = True
        obj = None if self.obj is None else self.obj()
        _KEPT.discard(obj)  # it ended: nobody needs it alive any more
        if self.on_end is None:
            return
        try:
            self.on_end(obj, code, message)
        except BaseException:  # noqa: BLE001
            _log.exception("on_end callback failed")


def _sub_info(ptr: int | None) -> dict[str, Any]:
    if not ptr:
        return {}
    info = unpackb(_lib.call_buf("subscription_info", ptr))
    return info if isinstance(info, dict) else {}


def _sub_layout(ptr: int | None) -> Layout | None:
    wire = _sub_info(ptr).get("layout")
    return None if wire is None else Layout.from_wire(wire)


def _subscribe_opts(state: _SubState, version: int, options: Any,
                    timeout: float) -> tuple[_lib.CSubscribeOpts, tuple, int]:
    raw = None if options is None else packb(options)
    ms = _ms("timeout", timeout)
    key = _lib.register(state)
    o = _lib.CSubscribeOpts(C.sizeof(_lib.CSubscribeOpts), version,
                            C.cast(raw, C.c_void_p).value if raw else None, len(raw or b""), ms,
                            _lib.delivery_tramp, _lib.end_tramp, key, _lib.release_tramp)
    return o, (raw,), key


def _attach(state: _SubState, ptr: int) -> None:
    state.ptr = ptr
    state.ready.set()


#: The live subscriptions of connections: a subscription stays subscribed when the caller drops
#: it until it ends or is closed. (The registry keeps only the state alive.)
_KEPT: set[Any] = set()


class Subscription:
    """A subscription on one connection."""

    def __init__(self, ptr: int, channel: str, peer: Peer, state: _SubState) -> None:
        self._ptr: int | None = ptr
        self._channel = channel
        self._peer = peer
        self._state = state
        self._ended: FlagEvent | None = None
        state.obj = weakref.ref(self)  # the registry keeps the state alive: no cycle back
        _KEPT.add(self)
        if state.ended_flag:  # the end beat the constructor
            _KEPT.discard(self)

    def __del__(self) -> None:
        try:
            self._release()
        except BaseException:  # noqa: BLE001 - interpreter shutdown
            pass

    def _release(self) -> None:
        _KEPT.discard(self)
        ptr, self._ptr = getattr(self, "_ptr", None), None
        _free_quietly("unsubscribe", ptr)

    @property
    def peer(self) -> Peer:
        """The connection it is on."""
        return self._peer

    @property
    def ended(self) -> FlagEvent:
        """Set once the subscription has ended (``is_set()``, ``wait(timeout)``)."""
        if self._ended is None:
            def get() -> bool:
                p = self._ptr
                return p is None or _lib.fn("subscription_is_active")(p) != 1

            def wait(ms: int) -> bool:
                p = self._ptr
                return p is None or _lib.fn("subscription_wait")(p, 2, ms) == 0

            self._ended = FlagEvent(get, wait)
        return self._ended

    @property
    def channel(self) -> str:
        """The name of the channel this subscription is on."""
        return self._channel

    @property
    def layout(self) -> Layout | None:
        """The stream's ``Layout`` (``None`` for an event or state channel)."""
        return self._state.layout or _sub_layout(self._ptr)

    def close(self) -> None:
        """Unsubscribes."""
        self._release()


class ClientSubscription:
    """A subscription that a ``Client`` keeps across reconnects."""

    def __init__(self, ptr: int, channel: str, state: _SubState) -> None:
        self._ptr: int | None = ptr
        self._channel = channel
        self._state = state
        self._closed = False
        self._subscribed: FlagEvent | None = None
        state.obj = weakref.ref(self)  # the registry keeps the state alive: no cycle back

    def __del__(self) -> None:
        try:
            self._release()
        except BaseException:  # noqa: BLE001 - interpreter shutdown
            pass

    def _release(self) -> None:
        self._closed = True
        ptr, self._ptr = getattr(self, "_ptr", None), None
        _free_quietly("unsubscribe", ptr)

    @property
    def channel(self) -> str:
        """The name of the channel this subscription was made on."""
        return self._channel

    @property
    def layout(self) -> Layout | None:
        """The stream's ``Layout`` once it is live (``None`` for other channels, or until then)."""
        try:
            return _sub_layout(self._ptr)
        except E.IpcError:
            return None

    @property
    def error(self) -> BaseException | None:
        """The error of the last attempt to subscribe, or ``None``."""
        err = _sub_info(self._ptr).get("error")
        return None if err is None else _record_error(err)

    @property
    def subscribed(self) -> FlagEvent:
        """Whether it is live on the current connection."""
        if self._subscribed is None:
            def get() -> bool:
                p = self._ptr
                return p is not None and _lib.fn("subscription_is_active")(p) == 1

            def wait(ms: int) -> bool:
                p = self._ptr
                if p is None:
                    time.sleep(ms / 1000)
                    return False
                return _lib.fn("subscription_wait")(p, 1, ms) == 0

            self._subscribed = FlagEvent(get, wait)
        return self._subscribed

    def close(self) -> None:
        """Unsubscribes and stops re-subscribing."""
        self._release()


# --------------------------------------------------------------------------- Peer
_PEERS: weakref.WeakValueDictionary[int, Peer] = weakref.WeakValueDictionary()
# Reentrant: creating a Peer under the lock can run the garbage collector, and a dying Peer's
# __del__ takes the lock on the same thread (a plain Lock deadlocked there).
_PEERS_LOCK = threading.RLock()


class Peer:
    """One connection."""

    __slots__ = ("_ptr", "_id", "_info", "_closed", "__weakref__")

    def __init__(self, ptr: int, peer_id: int) -> None:  # use Peer._adopt / Peer._borrow
        self._ptr = ptr
        self._id = peer_id
        self._info: dict[str, Any] | None = None
        self._closed: FlagEvent | None = None

    @staticmethod
    def _adopt(ptr: int) -> Peer:
        """The one ``Peer`` object of a connection, from an owned library reference (which it
        takes over)."""
        pid = _lib.fn("peer_id")(ptr)
        with _PEERS_LOCK:
            have = _PEERS.get(pid)
            if have is not None and getattr(have, "_ptr", None) is None:
                have = None  # dying: its __del__ is freeing it
            if have is not None:
                _free_quietly("peer_free", ptr)
                return have
            obj = Peer(ptr, pid)
            _PEERS[pid] = obj
            return obj

    @staticmethod
    def _borrow(ptr: int) -> Peer:
        """As ``_adopt`` for a pointer that is only borrowed (a hook's argument)."""
        return Peer._adopt(_lib.fn("peer_ref")(ptr))

    def __del__(self) -> None:
        try:
            ptr, self._ptr = getattr(self, "_ptr", None), None
            with _PEERS_LOCK:
                if _PEERS.get(getattr(self, "_id", None)) is self:
                    del _PEERS[self._id]
            _free_quietly("peer_free", ptr)
        except BaseException:  # noqa: BLE001 - interpreter shutdown
            pass

    def _wire_info(self) -> dict[str, Any]:
        if self._info is None:
            self._info = unpackb(_lib.call_buf("peer_get_info", self._ptr))
        return self._info

    # -- properties
    @property
    def info(self) -> PeerInfo:
        """Who is on the other end."""
        return PeerInfo.from_wire(self._wire_info())

    @property
    def version(self) -> int:
        """The protocol version of the connection (every connection speaks this library's)."""
        return _lib.protocol_version()

    @property
    def pid(self) -> int | None:
        """The other end's process id, from the operating system (either side)."""
        pid = self._wire_info().get("pid")
        return None if pid is None else int(pid)

    @property
    def run(self) -> str:
        """The server's ``run`` id."""
        return str(self._wire_info().get("run", ""))

    @property
    def is_open(self) -> bool:
        """Whether the connection is up."""
        return _lib.fn("peer_is_open")(self._ptr) == 1

    @property
    def closed(self) -> FlagEvent:
        """Set once the connection is closing or closed (``is_set()``, ``wait(timeout)``)."""
        if self._closed is None:
            # Captures the Peer, not the raw handle: the event may outlive a temporary Peer.
            self._closed = FlagEvent(lambda: _lib.fn("peer_is_open")(self._ptr) != 1,
                                     lambda ms: _lib.fn("peer_wait_closed")(self._ptr, ms) == 0)
        return self._closed

    def _reason(self) -> dict[str, Any] | None:
        r = unpackb(_lib.call_buf("peer_close_reason", self._ptr))
        return r if isinstance(r, dict) else None

    @property
    def close_code(self) -> str | None:
        """The ``bye`` code the connection ended with, once it has ended."""
        r = self._reason()
        return None if r is None else r.get("code")

    @property
    def close_message(self) -> str | None:
        """The ``bye`` text the connection ended with, once it has ended."""
        r = self._reason()
        return None if r is None else r.get("message")

    @property
    def decl(self) -> Declarations:
        """What the other end declares now (``stintmate_ipc.decl.Declarations``)."""
        return Declarations.from_wire(unpackb(_lib.call_buf("peer_decl", self._ptr)))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Peer) and other._id == self._id

    def __hash__(self) -> int:
        return hash(self._id)

    # -- actions
    def close(self, code: str = "normal", message: str = "") -> None:
        """Ends the connection with ``bye``."""
        _lib.fn("peer_close")(self._ptr, _lib.cstr(code), _lib.cstr(message))

    def call(self, method: str, params: Any = None, *, version: int = 1,
             timeout: float | None = 5.0, idempotency_key: str | None = None,
             on_progress: Callable[[Any], Any] | None = None) -> Any:
        """Calls a method and waits for the result."""
        return _call(_lib.peer_call, self._ptr, method, params, version, timeout,
                     idempotency_key, on_progress)

    def call_async(self, method: str, params: Any = None, *, version: int = 1,
                   timeout: float | None = 5.0, idempotency_key: str | None = None,
                   on_progress: Callable[[Any], Any] | None = None) -> PendingCall:
        """Sends a call and returns it in flight."""
        return _call_async(_lib.fn("peer_call_async"), self._ptr, method, params, version,
                           timeout, idempotency_key, on_progress)

    def subscribe(self, channel: str, callback: Callable[[Event | Sample], Any], *,
                  version: int = 1, options: dict[str, Any] | None = None,
                  on_end: Callable[[Subscription | None, str, str], Any] | None = None,
                  timeout: float = 5.0) -> Subscription:
        """Subscribes to a channel of the other end; ``callback(Event | Sample)`` runs on the
        connection's callback lane, in order."""
        state = _SubState(callback, on_end)
        opts, keep, key = _subscribe_opts(state, version, options, timeout)
        out = _out_ptr()
        rc = _lib.fn("peer_subscribe")(self._ptr, _lib.cstr(channel), C.byref(opts), C.byref(out))
        del keep
        _lib.check(rc)
        sub = Subscription(out.value, channel, self, state)
        _attach(state, out.value)
        return sub

    def __repr__(self) -> str:
        return f"Peer({self._wire_info().get('app')!r}, open={self.is_open})"


def _call(func: Any, handle: int, method: str, params: Any, version: int, timeout: float | None,
          idempotency_key: str | None, on_progress: Callable[[Any], Any] | None) -> Any:
    data = _params(params)
    opts, keep = _call_opts(version, timeout, idempotency_key, on_progress)
    buf = _lib.Buffer()
    rc = func(handle, _lib.cstr(method), data, len(data), C.byref(opts), C.byref(buf))
    del keep
    if rc < 0:
        raise _lib.error_for(rc)
    out = _lib.take(buf)
    return unpackb(out) if out else None


def _call_async(func: Any, handle: int, method: str, params: Any, version: int,
                timeout: float | None, idempotency_key: str | None,
                on_progress: Callable[[Any], Any] | None) -> PendingCall:
    data = _params(params)
    opts, keep = _call_opts(version, timeout, idempotency_key, on_progress)
    out = _out_ptr()
    rc = func(handle, _lib.cstr(method), data, len(data), C.byref(opts), C.byref(out))
    del keep
    _lib.check(rc)
    return PendingCall(out.value, method)


# --------------------------------------------------------------------------- Client
class Client(Node):
    """A client that stays connected: connects in the background, retries with backoff and
    re-subscribes after every reconnect.

    ``event_queue``: how many events of one subscription may wait for a slow callback before
    the subscription ends ``overloaded`` (and is made again 1 s later); state values coalesce
    to the latest and stream samples drop the oldest instead (SPEC 12.4).

    A client is also a node: it offers methods and channels to the endpoint it connects to,
    like an ``Endpoint`` does (``method``, ``event_channel``, ``on_connect``, ...).
    """

    on_connect_error: Callable[[BaseException], Any] | None

    def __init__(self, pipe: str, info: PeerInfo, key: KeyFile | bytes, *, reconnect: bool = True,
                 event_queue: int = 1024) -> None:
        self._ptr: int | None = None
        self._subs: list[ClientSubscription] = []
        self._subs_lock = threading.Lock()
        self._closed = False
        self.on_connect_error = None
        c_info, keep_info = info.to_c()
        c_key, keep_key = key_source_c(key)
        opts = _lib.CClientOpts(C.sizeof(_lib.CClientOpts), int(bool(reconnect)),
                                max(1, int(event_queue)))
        out = _out_ptr()
        rc = _lib.fn("client_new")(_lib.cstr(pipe), C.byref(c_info), C.byref(c_key),
                                   C.byref(opts), C.byref(out))
        del keep_info, keep_key
        _lib.check(rc)
        self._ptr = out.value
        Node.__init__(self, _lib.fn("client_node")(self._ptr))
        me = weakref.ref(self)

        def failed(data: bytes) -> None:
            self_ = me()
            cb = None if self_ is None else self_.on_connect_error
            if cb is not None:
                cb(_record_error(unpackb(data)))

        key_ = self._register(failed)
        rc = _lib.fn("on_connect_error")(self._ptr, _lib.error_tramp, key_, _lib.release_tramp)
        if rc < 0:
            _lib.unregister(key_)
        if type(self).reconnect_delay is not Client.reconnect_delay:
            def policy(code: bytes | None, attempt: int) -> int:
                self_ = me()
                try:
                    if self_ is None:
                        raise RuntimeError("client gone")
                    delay = self_.reconnect_delay(code.decode() if code else None, attempt)
                    return -1 if delay is None else max(0, int(delay * 1000))
                except BaseException:  # noqa: BLE001 - never escape into C; use the built-in
                    _lib._log.exception("exception in reconnect_delay; using the built-in policy")
                    return _lib.fn("reconnect_delay_ms")(code, attempt)

            pkey = self._register(policy)
            rc = _lib.fn("client_set_reconnect_policy")(self._ptr, _lib.reconnect_policy_tramp,
                                                        pkey, _lib.release_tramp)
            if rc < 0:
                _lib.unregister(pkey)

    def __del__(self) -> None:
        try:
            self._shutdown(free=True)
        except BaseException:  # noqa: BLE001
            pass

    # -- connecting
    def reconnect_delay(self, code: str | None = None, attempt: int = 0) -> float | None:
        """Seconds to wait (before jitter) before the next attempt, chosen by the ``bye`` code
        that ended the last connection or attempt (SPEC 15.3); ``None`` stops reconnecting.

        A subclass that overrides this is consulted by the reconnect loop (an exception in the
        override falls back to the built-in policy)."""
        ms = _lib.fn("reconnect_delay_ms")(_lib.cstr(code), attempt)
        return None if ms < 0 else ms / 1000

    def start(self) -> Client:
        """Connects in the background (once); returns the client."""
        _lib.check(_lib.fn("client_start")(self._ptr))
        return self

    def _wait(self, timeout: float | None, start: bool) -> Peer:
        end = None if timeout is None else time.monotonic() + _ms("timeout", timeout) / 1000
        first = True
        while True:
            left = _SLICE * 5 if end is None else min(_SLICE * 5, max(0.0, end - time.monotonic()))
            out = _out_ptr()
            func = _lib.fn("client_connect" if start and first else "client_wait_connected")
            first = False
            rc = func(self._ptr, int(math.ceil(left * 1000)), C.byref(out))
            if rc >= 0:
                return Peer._adopt(out.value)
            if rc != _lib.E_NOT_CONNECTED:
                raise _lib.error_for(rc)
            if end is not None and time.monotonic() >= end:
                raise self._not_connected()

    def _not_connected(self) -> BaseException:
        """``NotConnected``, with the last connect error as its cause."""
        rec = unpackb(_lib.call_buf("client_last_error", self._ptr))
        if not isinstance(rec, dict):
            return E.NotConnected("not connected")
        cause = _record_error(rec)
        err = E.NotConnected(f"not connected: {cause}")
        err.__cause__ = cause
        return err

    def connect(self, timeout: float = 5.0) -> Peer:
        """Starts and waits for the first connection."""
        return self._wait(timeout, True)

    def wait_connected(self, timeout: float | None = None) -> Peer:
        """Waits until connected (at most ``timeout`` seconds; ``None``: as long as it takes)."""
        return self._wait(timeout, False)

    @property
    def peer(self) -> Peer | None:
        """The connection, when it is up."""
        out = _out_ptr()
        rc = _lib.fn("client_peer")(self._ptr, C.byref(out))
        if rc == _lib.E_NOT_CONNECTED:
            return None
        _lib.check(rc)
        return Peer._adopt(out.value)

    @property
    def connected(self) -> bool:
        """Whether the connection is up."""
        return not self._closed and _lib.fn("client_is_connected")(self._ptr) == 1

    @property
    def last_error(self) -> BaseException | None:
        """The error of the last failed attempt, or ``None``."""
        rec = unpackb(_lib.call_buf("client_last_error", self._ptr))
        return _record_error(rec) if isinstance(rec, dict) else None

    # -- calls and subscriptions
    def call(self, method: str, params: Any = None, *, version: int = 1,
             timeout: float | None = 5.0, idempotency_key: str | None = None,
             on_progress: Callable[[Any], Any] | None = None) -> Any:
        """Calls a method and waits for the result."""
        return _call(_lib.client_call, self._ptr, method, params, version, timeout,
                     idempotency_key, on_progress)

    def call_async(self, method: str, params: Any = None, *, version: int = 1,
                   timeout: float | None = 5.0, idempotency_key: str | None = None,
                   on_progress: Callable[[Any], Any] | None = None) -> PendingCall:
        """Sends a call and returns it in flight."""
        return _call_async(_lib.fn("client_call_async"), self._ptr, method, params, version,
                           timeout, idempotency_key, on_progress)

    def subscribe(self, channel: str, callback: Callable[[Event | Sample], Any], *,
                  version: int = 1, options: dict[str, Any] | None = None,
                  on_end: Callable[[ClientSubscription | None, str, str], Any] | None = None,
                  timeout: float = 5.0) -> ClientSubscription:
        """Subscribes now and after every reconnect; ``callback(Event | Sample)`` runs on the
        connection's callback lane, in order."""
        state = _SubState(callback, on_end)
        opts, keep, key = _subscribe_opts(state, version, options, timeout)
        out = _out_ptr()
        rc = _lib.fn("client_subscribe")(self._ptr, _lib.cstr(channel), C.byref(opts),
                                         C.byref(out))
        del keep
        _lib.check(rc)
        sub = ClientSubscription(out.value, channel, state)
        _attach(state, out.value)
        with self._subs_lock:
            self._subs = [s for s in self._subs if not s._closed]
            self._subs.append(sub)
        return sub

    # -- closing
    def _shutdown(self, free: bool = False) -> None:
        with self._subs_lock:
            subs, self._subs = self._subs, []
        for s in subs:
            s._release()
        ptr = getattr(self, "_ptr", None)
        if ptr and not self._closed:
            self._closed = True
            _lib.fn("client_close")(ptr)
            self._forget_registrations()
        if free and ptr:
            self._ptr = None
            _lib.fn("client_free")(ptr)

    def close(self) -> None:
        """Closes the connection and stops reconnecting."""
        self._shutdown()

    def stop(self) -> None:
        """As ``close()``."""
        self.close()


def connect(pipe: str, info: PeerInfo, key: KeyFile | bytes, *, timeout: float = 5.0,
            node: Client | None = None, event_queue: int = 1024) -> Peer:
    """Opens one connection (no reconnect): handshake and return the ``Peer``.

    ``node``: a ``Client`` made with ``reconnect=False`` whose methods and channels this
    connection offers; it becomes that client's connection and the caller owns the
    reconnection (call ``connect`` again after the peer closes). ``pipe`` must then be the
    client's own pipe; ``info`` and ``key`` are the client's.
    """
    if node is not None and not isinstance(node, Client):
        raise TypeError("node must be a Client made with reconnect=False")
    c_info, keep_info = info.to_c()
    c_key, keep_key = key_source_c(key)
    out = _out_ptr()
    rc = _lib.fn("connect")(_lib.cstr(pipe), C.byref(c_info), C.byref(c_key),
                            _ms("timeout", timeout), None if node is None else node._ptr,
                            max(1, int(event_queue)), C.byref(out))
    del keep_info, keep_key
    _lib.check(rc)
    return Peer._adopt(out.value)
