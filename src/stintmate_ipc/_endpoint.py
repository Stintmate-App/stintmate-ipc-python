"""The endpoint side: ``Endpoint``, the node API it shares with ``Client`` (methods, channels,
hooks), ``Context`` and the channels (ctypes over the native library).

It behaves as the v0.2.0 binding did. Handlers run on the library's handler pool (or the
connection's lane for ``inline``), hooks on its hooks thread; ``accept`` runs on the thread that
calls ``publish``. A handler's request is answered exactly once, by this module, when the handler
returns or raises; the C request is consumed by the answer, so a :class:`Context` that outlives
it (kept by the handler) is frozen first: what it knew stays readable (see :class:`Context`).

``Node`` takes the pointer of a ``stintmate_ipc_node`` (``endpoint_node`` or ``client_node``) so
the client can reuse it: ``Node.__init__(self, node_ptr)``. The node is borrowed from its owner,
which the subclass must keep alive.
"""

from __future__ import annotations

import ctypes as C
import functools
import logging
import math
import os
import sys
import threading
import time
import types
import weakref
from pathlib import Path
from typing import Any, Callable

from stintmate_ipc import _lib
from stintmate_ipc._codec import packb, unpackb
from stintmate_ipc._errors import RemoteError
from stintmate_ipc._keys import KeyFile, PeerInfo, key_source_c
from stintmate_ipc._layout import Layout

_log = logging.getLogger("stintmate_ipc")
_fn = _lib.fn
_check = _lib.check

#: Longest single wait inside the library for ``wait_cancelled``: the request may be answered
#: (and consumed) from another thread meanwhile, so long waits are made of slices.
_SLICE_MS = 50
_FOREVER_S = 60 * 60 * 24 * 365


def _wrap_peer(ptr: int) -> Any:
    """A ``Peer`` over an OWNED ``stintmate_ipc_peer*`` reference (the client side's class)."""
    from stintmate_ipc import _client

    return _client.Peer._adopt(ptr)


def _peer_from_borrowed(ptr: int) -> Any:
    return _wrap_peer(_fn("peer_ref")(ptr))


def _ms_up(seconds: float) -> int:
    return max(0, math.ceil(seconds * 1000))


def _remote_of(exc: BaseException, what: str) -> tuple[str, str, bytes, int]:
    """What an exception answers: a ``RemoteError`` its code, anything else ``internal``."""
    if isinstance(exc, RemoteError):
        data = exc.data
        details = b""
        if data is not None:
            try:
                details = packb(data)
            except Exception:  # noqa: BLE001 - an unencodable detail must not lose the code
                details = b""
        retry = exc.retry_ms
        return (str(exc.code), str(exc.message), details, -1 if retry is None else int(retry))
    _log.error("%s", what, exc_info=exc)
    return "internal", f"{type(exc).__name__}: {exc}", b"", -1


# --------------------------------------------------------------------------- Context
class Context:
    """What a method handler gets with its params (the handler's second argument is the same
    decoded ``params``).

    The request it describes is consumed when the handler's answer is sent. A context the
    handler kept (a thread, a queue) is frozen at that moment with what it knew: ``id``,
    ``method``, ``version``, ``idempotency_key``, ``params``, ``peer`` and the last ``cancelled``
    and deadline; ``remaining``/``expired`` keep counting down from the deadline, ``progress``
    does nothing and ``wait_cancelled`` waits out its timeout. A context that was not kept
    cannot be used after the answer (``RuntimeError``).
    """

    __slots__ = ("_req", "_params", "_lock", "_info", "_peer", "_deadline", "_frozen_cancelled",
                 "_frozen", "__weakref__")

    def __init__(self, req: int, params: Any) -> None:
        self._req: int | None = req
        self._params = params
        self._lock = threading.Lock()
        self._info: dict[str, Any] | None = None
        self._peer: Any = None
        self._deadline: float | None = None  # monotonic; None: no deadline
        self._frozen_cancelled = False
        self._frozen = False

    # -- helpers (the lock is held)
    def _live(self) -> int:
        if self._req is None:
            raise RuntimeError("the request was already answered")
        return self._req

    def _read_info(self) -> dict[str, Any]:
        if self._info is None:
            self._info = unpackb(_lib.call_buf("request_info", self._live()))
            ms = self._info.get("remaining_ms")
            self._deadline = None if ms is None else time.monotonic() + ms / 1000.0
        return self._info

    def _info_get(self, key: str) -> Any:
        if self._info is not None:
            return self._info.get(key)
        with self._lock:
            return self._read_info().get(key)

    def _freeze(self) -> None:
        """Copies what a kept context may still be asked (the lock is held, request live)."""
        self._read_info()
        if self._peer is None:
            ptr = _fn("request_peer")(self._req)
            if ptr:
                self._peer = _wrap_peer(ptr)
        self._frozen_cancelled = bool(_fn("request_is_cancelled")(self._req) == 1)
        ms = _fn("request_remaining_ms")(self._req)
        self._deadline = None if ms < 0 else time.monotonic() + ms / 1000.0
        self._frozen = True

    def _answer(self, escaped: bool, fn_name: str, *args: Any) -> int:
        """Sends the answer; the request is consumed whatever the status."""
        with self._lock:
            req = self._req
            if req is None:
                return 0
            try:
                if escaped:
                    try:
                        self._freeze()
                    except Exception:  # noqa: BLE001 - the answer matters more than the copy
                        _log.debug("freezing a context failed", exc_info=True)
            finally:
                self._req = None
            return _fn(fn_name)(req, *args)

    # -- the v0.2.0 surface
    @property
    def peer(self) -> Any:
        """The connection the request arrived on (the same object while this context lives)."""
        with self._lock:
            if self._peer is None:
                ptr = _fn("request_peer")(self._live())
                if not ptr:
                    raise RuntimeError("the request has no connection")
                self._peer = _wrap_peer(ptr)
            return self._peer

    @property
    def id(self) -> int:
        """The caller's id for the request."""
        return self._info_get("id")

    @property
    def method(self) -> str:
        """The method name."""
        return self._info_get("method")

    @property
    def version(self) -> int:
        """The version asked for."""
        return self._info_get("version")

    @property
    def params(self) -> Any:
        """The decoded params (also the handler's second argument)."""
        return self._params

    @property
    def idempotency_key(self) -> str | None:
        """The idempotency key, when the caller gave one."""
        return self._info_get("idempotency_key")

    @property
    def cancelled(self) -> bool:
        """The caller sent ``cancel`` or went away: stop and return anything."""
        with self._lock:
            if self._req is None:
                if self._frozen:
                    return self._frozen_cancelled
                raise RuntimeError("the request was already answered")
            return _fn("request_is_cancelled")(self._req) == 1

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        """Blocks until the request is cancelled (``True``) or ``timeout`` seconds pass
        (``False``)."""
        if timeout is None:
            timeout = _FOREVER_S
        elif not timeout >= 0:
            raise ValueError("timeout must be a non-negative number")
        end = time.monotonic() + min(timeout, _FOREVER_S)
        while True:
            left = end - time.monotonic()
            with self._lock:
                if self._req is None:
                    if not self._frozen:
                        raise RuntimeError("the request was already answered")
                    if self._frozen_cancelled:
                        return True
                    live = False
                else:
                    live = True
                    ms = max(0, min(_SLICE_MS, math.ceil(left * 1000)))
                    rc = _fn("request_wait_cancelled")(self._req, ms)
                    if rc == _lib.OK:
                        return True
            if left <= 0:
                return False
            if not live:
                time.sleep(min(left, 0.01))

    @property
    def remaining(self) -> float | None:
        """Seconds left before the caller's deadline, or ``None`` when it set none."""
        with self._lock:
            if self._req is not None:
                ms = _fn("request_remaining_ms")(self._req)
                return None if ms < 0 else ms / 1000.0
            if not self._frozen:
                raise RuntimeError("the request was already answered")
            if self._deadline is None:
                return None
            return max(0.0, self._deadline - time.monotonic())

    def expired(self) -> bool:
        """Whether the caller's deadline has passed."""
        left = self.remaining
        return left is not None and left <= 0

    def progress(self, data: Any = None, extend_s: float | None = None) -> None:
        """Sends ``prog`` (a long request is alive); ``extend_s`` gives the caller a new
        deadline that many seconds from now."""
        if extend_s is not None and not extend_s >= 0:
            raise ValueError("extend_s must be a non-negative number")
        payload = b"" if data is None else packb(data)
        extend = -1 if extend_s is None else _ms_up(extend_s)
        with self._lock:
            if self._req is None:
                return  # answered: the caller is not waiting any more
            _fn("request_progress")(self._req, payload, len(payload), extend)


def _kept(ctx: "Context") -> bool:
    """Whether the handler kept ``ctx``: otherwise only the caller's local, this parameter and
    the count's own argument hold it (call it before ``ctx._answer``: the method call adds one)."""
    return sys.getrefcount(ctx) > 3


def _dispatch(handler: Callable[[Context, Any], Any], req: int, raw: bytes) -> None:
    """Runs the Python handler for one request and answers it, exactly once."""
    ctx = Context(req, None)
    try:
        try:
            params = unpackb(raw) if raw else None
        except Exception as e:  # noqa: BLE001
            raise _Failed(e, "decoding the params failed") from None
        ctx._params = params
        try:
            result = handler(ctx, params)
        except BaseException as e:  # noqa: BLE001 - everything is answered
            raise _Failed(e, "method handler failed") from None
        try:
            payload = packb(result)
        except BaseException as e:  # noqa: BLE001
            raise _Failed(e, "the handler's result") from None
    except _Failed as f:
        code, message, details, retry = _remote_of(f.exc, f.what)
        del f
        kept = _kept(ctx)
        rc = ctx._answer(kept, "respond_error", _lib.cstr(code),
                         _lib.cstr(message), details, len(details), retry)
    except BaseException as e:  # noqa: BLE001 - the answer must go out whatever happened
        code, message, details, retry = _remote_of(e, "method dispatch failed")
        kept = _kept(ctx)
        rc = ctx._answer(kept, "respond_error", _lib.cstr(code),
                         _lib.cstr(message), details, len(details), retry)
    else:
        kept = _kept(ctx)
        rc = ctx._answer(kept, "respond", payload, len(payload))
    if rc < 0:
        # the caller went away or the deadline passed: nothing to tell
        _log.debug("answering a request failed (status %s)", rc)


class _Failed(Exception):
    def __init__(self, exc: BaseException, what: str) -> None:
        super().__init__(what)
        self.exc = exc
        self.what = what


# --------------------------------------------------------------------------- views
class SubscribeRequest:
    """What an ``on_subscribe`` hook gets."""

    __slots__ = ("peer", "version", "options")

    def __init__(self, peer: Any, version: int, options: Any) -> None:
        object.__setattr__(self, "peer", peer)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "options", options)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("SubscribeRequest is frozen")

    def __repr__(self) -> str:
        return (f"SubscribeRequest(peer={self.peer!r}, version={self.version!r}, "
                f"options={self.options!r})")


class SubscriptionView:
    """A read-only view of one subscription to a channel this process owns, for
    ``EventChannel.publish(accept=...)``."""

    __slots__ = ("peer", "pid", "version", "options", "_connection", "_resolve", "_conn_id")

    def __init__(self, peer: PeerInfo, pid: int | None, version: int, options: Any,
                 conn_id: int, resolve: Callable[[int], Any]) -> None:
        for name, value in (("peer", peer), ("pid", pid), ("version", version),
                            ("options", options), ("_connection", None),
                            ("_resolve", resolve), ("_conn_id", conn_id)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("SubscriptionView is frozen")

    @property
    def connection(self) -> Any:
        """The connection it arrived on: equal (``==``) to the ``peer`` of a request that came
        on it."""
        if self._connection is None:
            object.__setattr__(self, "_connection", self._resolve(self._conn_id))
        return self._connection

    def __repr__(self) -> str:
        return (f"SubscriptionView(peer={self.peer!r}, pid={self.pid!r}, "
                f"version={self.version!r}, options={dict(self.options)!r})")


# --------------------------------------------------------------------------- channels
def _versions_of(v: list[int] | tuple[int, ...] | None) -> list[int]:
    out = sorted(set([1] if v is None else (int(x) for x in v)))
    for x in out:
        if not 0 <= x < 2**32:
            raise OverflowError("a version must fit 32 bits")
    return out


def _u32_array(v: list[int]) -> Any:
    return (C.c_uint32 * len(v))(*v) if v else None


class _Channel:
    """The part every channel kind shares."""

    _KIND = ""
    __slots__ = ("_ch", "_name", "_node", "_hooks", "__weakref__")

    def __init__(self, node: Node, ch: int, name: str) -> None:
        self._ch = ch
        self._name = name
        self._node = node  # the node (hence its owner) outlives the channel's handle
        self._hooks: dict[str, Any] = {}

    def __del__(self) -> None:
        ch, self._ch = getattr(self, "_ch", 0), 0
        if ch and not sys.is_finalizing():
            try:
                _fn("channel_free")(ch)
            except Exception:  # noqa: BLE001
                pass

    @property
    def kind(self) -> str:
        """``"event"``, ``"state"`` or ``"stream"``."""
        return self._KIND

    @property
    def name(self) -> str:
        """The channel's name."""
        return self._name

    @property
    def subscriber_count(self) -> int:
        """How many subscriptions it has."""
        return max(0, _fn("channel_subscribers")(self._ch))

    @property
    def active(self) -> bool:
        """Whether anybody is subscribed."""
        return _fn("channel_is_active")(self._ch) == 1

    def close(self, code: str = "channel_closed", message: str = "") -> None:
        """Ends every subscription (``end`` with ``code``) and withdraws the channel."""
        _check(_fn("channel_close")(self._ch, _lib.cstr(code), _lib.cstr(message)))

    @property
    def on_subscribe(self) -> Callable[[SubscribeRequest], Any] | None:
        """``hook(request)`` runs before a subscription is accepted (on a pool thread);
        raise ``RemoteError`` to refuse it."""
        return self._hooks.get("on_subscribe")

    @on_subscribe.setter
    def on_subscribe(self, cb: Callable[[SubscribeRequest], Any] | None) -> None:
        if cb is None:
            self._hooks.pop("on_subscribe", None)
            _check(_fn("on_subscribe")(self._ch, _lib.SUBSCRIBE_CB(), None, _lib.RELEASE_CB()))
            return
        node_ref = weakref.ref(self._node)  # the registry must not keep the node alive

        def run(ctx: int, peer: int, raw: bytes) -> int:
            try:
                node = node_ref()
                if node is None:
                    raise RemoteError("shutting_down", "the endpoint is gone")
                req = unpackb(raw) if raw else {}
                opts = req.get("options")
                request = SubscribeRequest(node._peer_of(peer), int(req.get("version", 1)),
                                           None if opts is None else opts)
                cb(request)
                return 0
            except BaseException as e:  # noqa: BLE001
                code, message, details, _retry = _remote_of(e, "on_subscribe hook failed")
                _fn("refuse")(ctx, _lib.cstr(code), _lib.cstr(message), details, len(details))
                return 1

        key = self._node._register(run)
        self._hooks["on_subscribe"] = cb
        rc = _fn("on_subscribe")(self._ch, _lib.subscribe_tramp, key, _lib.release_tramp)
        if rc < 0:
            _lib.unregister(key)
            self._hooks.pop("on_subscribe", None)
            _check(rc)

    @property
    def on_active(self) -> Callable[[bool], Any] | None:
        """``hook(bool)``: the first subscriber came (``True``) or the last one left."""
        return self._hooks.get("on_active")

    @on_active.setter
    def on_active(self, cb: Callable[[bool], Any] | None) -> None:
        if cb is None:
            self._hooks.pop("on_active", None)
            _check(_fn("on_active")(self._ch, _lib.ACTIVE_CB(), None, _lib.RELEASE_CB()))
            return

        def run(active: bool) -> None:
            try:
                cb(active)
            except BaseException:  # noqa: BLE001
                _log.exception("on_active hook failed")

        key = self._node._register(run)
        self._hooks["on_active"] = cb
        rc = _fn("on_active")(self._ch, _lib.active_tramp, key, _lib.release_tramp)
        if rc < 0:
            _lib.unregister(key)
            self._hooks.pop("on_active", None)
            _check(rc)

    def _publish(self, data: bytes, ts_ns: int, version: int, accept: Any, user: int | None
                 ) -> int:
        queued = C.c_size_t(0)
        rc = _lib.publish(self._ch, data, len(data), ts_ns, version,
                          _lib.accept_tramp if accept else _lib.NULL_ACCEPT, user, C.byref(queued))
        if rc < 0:
            _check(rc)
        return queued.value


class EventChannel(_Channel):
    """An event channel this process owns."""

    _KIND = "event"
    __slots__ = ()

    def publish(self, data: Any, *, ts: int | None = None, version: int | None = None,
                accept: Callable[[SubscriptionView], Any] | None = None) -> int:
        """Sends ``data`` to every subscriber (never blocks); ``accept(view)`` picks
        subscriptions. Returns how many it was queued for."""
        if version is not None and not 0 <= version < 2**32:
            raise OverflowError("version must fit 32 bits")
        payload = packb(data)
        if accept is None:
            return self._publish(payload, _lib.TS_NOW if ts is None else ts, version or 0,
                                 None, None)
        if ts is not None:
            raise ValueError("ts cannot be combined with accept")
        node = self._node
        cache: dict[int, Any] = {}

        def resolve(conn_id: int) -> Any:
            if conn_id not in cache:
                cache[conn_id] = node._peer_by_id(conn_id)
            return cache[conn_id]

        def run(raw: bytes) -> bool:
            try:
                v = unpackb(raw)
                opts = v.get("options")
                view = SubscriptionView(
                    PeerInfo.from_wire(v.get("peer") or {}), v.get("pid"), int(v["version"]),
                    types.MappingProxyType({} if opts is None else opts),
                    int(v["connection"]), resolve)
                return bool(accept(view))
            except BaseException:  # noqa: BLE001
                _log.exception("accept failed")
                return False

        key = _lib.register(run)
        try:
            return self._publish(payload, _lib.TS_NOW, version or 0, True, key)
        finally:
            _lib.unregister(key)


class StateChannel(_Channel):
    """A state channel this process owns."""

    _KIND = "state"
    __slots__ = ()

    @property
    def value(self) -> Any:
        """The current value."""
        return unpackb(_lib.call_buf("state_get", self._ch))

    def set(self, value: Any) -> None:
        """Replaces the value and pushes it (always the whole value, never a delta)."""
        self._publish(packb(value), _lib.TS_NOW, 0, None, None)


class StreamChannel(_Channel):
    """A stream channel this process owns."""

    _KIND = "stream"
    __slots__ = ("_layout",)

    def __init__(self, node: Node, ch: int, name: str, layout: Layout) -> None:
        super().__init__(node, ch, name)
        self._layout = layout

    @property
    def layout(self) -> Layout:
        """The layout of the samples."""
        return self._layout

    def send(self, body: bytes, *, t_ns: int | None = None) -> int:
        """Sends one sample (``body``: the packed layout); returns how many subscribers got
        it."""
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError("body must be bytes")
        body = bytes(body)
        sent = C.c_size_t(0)
        rc = _lib.stream_send(self._ch, body, len(body),
                              _lib.TS_NOW if t_ns is None else t_ns, C.byref(sent))
        if rc < 0:
            raise ValueError(str(_lib.error_for(rc)))
        return sent.value


# --------------------------------------------------------------------------- Node
class Node:
    """The base of ``Endpoint`` and ``Client``: methods, channels and connection hooks.

    ``node`` is a ``stintmate_ipc_node*`` borrowed from its owner (an endpoint or a client
    handle); the subclass keeps the owner alive for as long as this object lives.
    """

    def __init__(self, node: int) -> None:
        self._node = node
        self._peer_hooks: dict[int, Callable[[Any], Any]] = {}
        self._peer_hook_on = False
        self._keys: set[int] = set()  # user_data keys this node's registrations hold

    def _register(self, obj: Any) -> int:
        key = _lib.register(obj)
        self._keys.add(key)
        return key

    def _forget_registrations(self) -> None:
        """Drops what the callbacks this node registered point at. The library releases a
        node's registrations when its owner is freed (each release unregisters its key); this
        also drops what is left, so late callbacks find nothing (and nothing is kept alive). A
        second unregister is harmless."""
        keys, self._keys = self._keys, set()
        for key in keys:
            _lib.unregister(key)

    # -- peers
    def _peer_ptrs(self) -> list[int]:
        """Owned references to the open connections (the caller frees or wraps them)."""
        cap = 16
        while True:
            arr = (C.c_void_p * cap)()
            count = C.c_size_t(0)
            _check(_fn("peers")(self._node, arr, cap, C.byref(count)))
            n = count.value
            if n <= cap:
                return [arr[i] for i in range(n)]
            for i in range(cap):
                _fn("peer_free")(arr[i])
            cap = n + 8

    @property
    def peers(self) -> list[Any]:
        """The open connections."""
        return [_wrap_peer(p) for p in self._peer_ptrs()]

    def _peer_by_id(self, peer_id: int) -> Any:
        found = None
        for p in self._peer_ptrs():
            if found is None and _fn("peer_id")(p) == peer_id:
                found = _wrap_peer(p)
            else:
                _fn("peer_free")(p)
        return found

    def _peer_of(self, borrowed: int) -> Any:
        return _peer_from_borrowed(borrowed)

    def _set_peer_hook(self, event: int, cb: Callable[[Any], Any] | None) -> None:
        if cb is None:
            self._peer_hooks.pop(event, None)
            return
        self._peer_hooks[event] = cb
        if self._peer_hook_on:
            return
        hooks = self._peer_hooks  # the closure holds the dict only, never this node

        def run(event: int, peer: int) -> None:
            cb = hooks.get(event)
            if cb is None:
                return
            try:
                cb(_peer_from_borrowed(peer))
            except BaseException:  # noqa: BLE001
                _log.exception("connection hook failed")

        key = self._register(run)
        rc = _fn("on_peer")(self._node, _lib.peer_tramp, key, _lib.release_tramp)
        if rc < 0:
            _lib.unregister(key)
            self._peer_hooks.pop(event, None)
            _check(rc)
        self._peer_hook_on = True

    @property
    def on_connect(self) -> Callable[[Any], Any] | None:
        """``hook(peer)``: a connection was established."""
        return self._peer_hooks.get(_lib.PEER_CONNECT)

    @on_connect.setter
    def on_connect(self, cb: Callable[[Any], Any] | None) -> None:
        self._set_peer_hook(_lib.PEER_CONNECT, cb)

    @property
    def on_disconnect(self) -> Callable[[Any], Any] | None:
        """``hook(peer)``: a connection ended."""
        return self._peer_hooks.get(_lib.PEER_DISCONNECT)

    @on_disconnect.setter
    def on_disconnect(self, cb: Callable[[Any], Any] | None) -> None:
        self._set_peer_hook(_lib.PEER_DISCONNECT, cb)

    @property
    def on_redeclare(self) -> Callable[[Any], Any] | None:
        """``hook(peer)``: a peer sent ``decl`` again."""
        return self._peer_hooks.get(_lib.PEER_REDECLARE)

    @on_redeclare.setter
    def on_redeclare(self, cb: Callable[[Any], Any] | None) -> None:
        self._set_peer_hook(_lib.PEER_REDECLARE, cb)

    # -- methods
    def method(self, name: str, handler: Callable[[Context, Any], Any] | None = None, *,
               versions: list[int] | None = None, ordered: bool = False,
               idempotent: bool = False, idempotency_cache: bool = True,
               idempotency_ttl: float | None = None, doc: str | None = None,
               params: Any = None, result: Any = None, inline: bool = False) -> Any:
        """Offers a method; ``handler(ctx, params) -> result`` runs on a pool thread
        (``ordered``: one at a time per connection, in arrival order; ``inline``: on the
        connection's lane, for handlers that answer from memory and never block). Usable as a
        decorator. Raise ``RemoteError`` from the handler to answer an error."""
        if idempotency_ttl is None:
            ttl_ms = -1
        elif idempotency_ttl > 0:
            if not idempotency_ttl < _FOREVER_S:
                raise ValueError("idempotency_ttl is out of range")
            ttl_ms = max(1, _ms_up(idempotency_ttl))
        else:
            raise ValueError("idempotency_ttl must be positive")
        flags = (_lib.METHOD_ORDERED if ordered else 0) | (_lib.METHOD_INLINE if inline else 0)
        flags |= (_lib.METHOD_IDEMPOTENT if idempotent else 0)
        flags |= 0 if idempotency_cache else _lib.METHOD_NO_IDEMPOTENCY_CACHE
        vers = _versions_of(versions)
        pschema = packb(params) if params is not None else b""
        rschema = packb(result) if result is not None else b""
        vers_c = _u32_array(vers)
        doc_c = _lib.cstr(doc)
        opts = _lib.CMethodOpts(C.sizeof(_lib.CMethodOpts), flags, C.cast(vers_c, _lib.vp).value, len(vers),
                                ttl_ms,
                                doc_c, _lib.vptr(pschema or None), len(pschema),
                                _lib.vptr(rschema or None),
                                len(rschema))
        cname = _lib.cstr(name)
        # ``opts`` holds only addresses: these buffers live as long as ``register`` (the decorator form).
        keep = (vers_c, pschema, rschema, doc_c)

        def register(h: Callable[[Context, Any], Any]) -> None:
            _ = keep
            key = self._register(functools.partial(_dispatch, h))
            rc = _fn("method")(self._node, cname, C.byref(opts), _lib.handler_tramp, key,
                               _lib.release_tramp)
            if rc < 0:
                _lib.unregister(key)
                _check(rc)

        if handler is not None:
            register(handler)
            return handler

        def decorate(f: Callable[[Context, Any], Any]) -> Callable[[Context, Any], Any]:
            register(f)
            return f

        return decorate

    def remove_method(self, name: str) -> None:
        """Withdraws a method."""
        _fn("remove_method")(self._node, _lib.cstr(name))

    # -- channels
    def event_channel(self, name: str, *, versions: list[int] | None = None) -> EventChannel:
        """Declares an event channel."""
        vers = _versions_of(versions)
        out = C.c_void_p()
        _check(_fn("event_channel")(self._node, _lib.cstr(name), _u32_array(vers), len(vers),
                                    C.byref(out)))
        return EventChannel(self, out.value, name)

    def state_channel(self, name: str, initial: Any = None, *,
                      versions: list[int] | None = None) -> StateChannel:
        """Declares a state channel with an initial value."""
        vers = _versions_of(versions)
        init = packb(initial)
        out = C.c_void_p()
        _check(_fn("state_channel")(self._node, _lib.cstr(name), init, len(init),
                                    _u32_array(vers), len(vers), C.byref(out)))
        return StateChannel(self, out.value, name)

    def stream_channel(self, name: str, layout: Layout, *, versions: list[int] | None = None,
                       rate_hz: float | None = None, queue: int | None = None
                       ) -> StreamChannel:
        """Declares a stream channel with a layout."""
        if not isinstance(layout, Layout):
            raise TypeError("layout must be a Layout")
        vers = _versions_of(versions)
        wire = packb(layout.to_wire())
        out = C.c_void_p()
        _check(_fn("stream_channel")(self._node, _lib.cstr(name), wire, len(wire),
                                     _u32_array(vers), len(vers), float(rate_hz or 0.0),
                                     int(queue or 0), C.byref(out)))
        return StreamChannel(self, out.value, name, layout)

    def declarations(self) -> Any:
        """What this node offers, as ``stintmate_ipc.decl.Declarations``."""
        from stintmate_ipc import decl

        return decl.Declarations.from_wire(unpackb(_lib.call_buf("declarations", self._node)))


# --------------------------------------------------------------------------- Endpoint
class Endpoint(Node):
    """The server of one endpoint: creates the pipe, authenticates every client and serves the
    methods and channels declared on it.

    ``pipe``: a full path or a short name (default ``stintmate-<app>[-<process>]-<user>``);
    ``registry``: a folder for the endpoint registry entry; ``key``: a ``KeyFile`` or 32 bytes.
    """

    def __init__(self, info: PeerInfo, key: KeyFile | bytes, *, pipe: str | None = None,
                 registry: str | os.PathLike[str] | None = None,
                 handshake_timeout: float | None = None, handler_threads: int = 8,
                 heartbeat_ms: int | None = None, event_queue: int = 1024,
                 event_queue_bytes: int = 8_388_608, max_inflight: int | None = None,
                 features: list[str] | None = None) -> None:
        if not isinstance(info, PeerInfo):
            raise TypeError("info must be a PeerInfo")
        if handshake_timeout is None:
            hs_ms = -1
        else:
            try:
                hs_ms = _ms_up(float(handshake_timeout))
            except (OverflowError, ValueError):
                raise ValueError("handshake_timeout is out of range") from None
            if not handshake_timeout >= 0:
                raise ValueError("handshake_timeout is out of range")
        info_c, keep_info = info.to_c()
        key_c, keep_key = key_source_c(key)
        feats = packb([str(f) for f in features]) if features is not None else b""
        reg = None if registry is None else os.fspath(registry).encode("utf-8")
        opts = _lib.CEndpointOpts(
            C.sizeof(_lib.CEndpointOpts), _lib.cstr(pipe), reg, hs_ms, max(1, handler_threads),
            heartbeat_ms or 0, event_queue, event_queue_bytes, max_inflight or 0,
            _lib.vptr(feats or None), len(feats))
        out = C.c_void_p()
        _check(_fn("endpoint_new")(C.byref(info_c), C.byref(key_c), C.byref(opts),
                                   C.byref(out)))
        self._ep = out.value
        self._info = info
        self._registry = None if registry is None else Path(registry)
        self._hs_failed: Callable[[int | None, BaseException], Any] | None = None
        Node.__init__(self, _fn("endpoint_node")(self._ep))
        meta = unpackb(_lib.call_buf("endpoint_info", self._ep))
        self._path: str = meta["path"]
        self._short_name: str = meta["short_name"]
        self._run_id: str = meta["run"]
        del keep_info, keep_key

    def __del__(self) -> None:
        ep, self._ep = getattr(self, "_ep", 0), 0
        if ep and not sys.is_finalizing():
            try:
                _fn("endpoint_free")(ep)
                self._forget_registrations()
            except Exception:  # noqa: BLE001
                pass

    @property
    def path(self) -> str:
        """The pipe path."""
        return self._path

    @property
    def short_name(self) -> str:
        """The short name of the pipe."""
        return self._short_name

    @property
    def run(self) -> str:
        """The run id announced in ``welcome``."""
        return self._run_id

    @property
    def info(self) -> PeerInfo:
        """Who this endpoint is."""
        return self._info

    @property
    def registry(self) -> Path | None:
        """The registry folder, or ``None``."""
        return self._registry

    @property
    def on_handshake_failed(self) -> Callable[[int | None, BaseException], Any] | None:
        """``hook(pid, error)``: a client failed its handshake."""
        return self._hs_failed

    @on_handshake_failed.setter
    def on_handshake_failed(self, cb: Callable[[int | None, BaseException], Any] | None) -> None:
        if cb is None:
            self._hs_failed = None
            _check(_fn("on_handshake_failed")(self._ep, _lib.HANDSHAKE_FAILED_CB(), None,
                                              _lib.RELEASE_CB()))
            return

        def run(pid: int, raw: bytes) -> None:
            try:
                rec = unpackb(raw) if raw else {}
                err = _lib.error_for(int(rec.get("status", _lib.E_HANDSHAKE)), rec)
                cb(None if pid < 0 else pid, err)
            except BaseException:  # noqa: BLE001
                _log.exception("on_handshake_failed failed")

        key = self._register(run)
        self._hs_failed = cb
        rc = _fn("on_handshake_failed")(self._ep, _lib.handshake_failed_tramp, key,
                                        _lib.release_tramp)
        if rc < 0:
            _lib.unregister(key)
            self._hs_failed = None
            _check(rc)

    def start(self) -> Endpoint:
        """Creates the pipe and serves; ``EndpointInUse`` when another process serves this
        name."""
        _check(_fn("endpoint_start")(self._ep))
        return self

    def stop(self, code: str = "shutting_down", message: str = "the endpoint stops") -> None:
        """Stops serving and says ``bye`` to every peer."""
        if self._ep:
            _fn("endpoint_stop")(self._ep, _lib.cstr(code), _lib.cstr(message))

    def __enter__(self) -> Endpoint:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop("shutting_down", "")


__all__ = ["Context", "Endpoint", "EventChannel", "Node", "StateChannel", "StreamChannel",
           "SubscribeRequest", "SubscriptionView"]
