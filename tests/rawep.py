"""A tiny test endpoint made straight over the native library (the client tests' server).

Independent of the package's ``Endpoint`` class: it uses only ``_lib``. Methods: ``t.echo``
(returns the params), ``t.sleep`` (``{"ms": n, "progress": bool}``: sleeps in 10 ms steps,
reporting ``i`` per step when asked, fails ``cancelled`` when cancelled), ``t.fail`` (a
``RemoteError`` with data and ``retry_ms``), ``t.count`` (a counter, to test idempotency).
Channels: event ``t.events``, state ``t.state``, stream ``t.stream`` (``a: u32``, ``b: f32 x 2``).
"""

from __future__ import annotations

import ctypes as C
import struct

from stintmate_ipc import PeerInfo, _lib
from stintmate_ipc._codec import packb, unpackb
from stintmate_ipc._keys import key_source_c

_VERSIONS = (C.c_uint32 * 1)(1)


class RawEndpoint:
    def __init__(self, pipe: str, key: bytes) -> None:
        self.path = pipe
        info, k1 = PeerInfo("t6test").to_c()
        ks, k2 = key_source_c(key)
        opts = _lib.CEndpointOpts(C.sizeof(_lib.CEndpointOpts), pipe.encode(), None, -1, 0, 0,
                                  0, 0, 0, None, 0)
        out = C.c_void_p()
        _lib.check(_lib.fn("endpoint_new")(C.byref(info), C.byref(ks), C.byref(opts),
                                           C.byref(out)))
        del k1, k2
        self._ep = out.value
        self._stopped = False
        self._counter = 0
        node = _lib.fn("endpoint_node")(self._ep)
        self._method(node, b"t.echo", 0, self._echo)
        self._method(node, b"t.fail", 0, self._fail)
        self._method(node, b"t.sleep", 0, self._sleep)
        self._method(node, b"t.count", _lib.METHOD_IDEMPOTENT, self._count)
        self.events = self._chan("event_channel", node, b"t.events")
        self.state = self._chan("state_channel", node, b"t.state", packb({"n": 0}))
        layout = packb({"name": "t", "fields": [["a", "u32", 1], ["b", "f32", 2]]})
        out = C.c_void_p()
        _lib.check(_lib.fn("stream_channel")(node, b"t.stream", layout, len(layout), _VERSIONS, 1,
                                             0.0, 0, C.byref(out)))
        self.stream = out.value
        _lib.check(_lib.fn("endpoint_start")(self._ep))

    # -- declaration helpers
    def _method(self, node, name: bytes, flags: int, fn) -> None:
        opts = _lib.CMethodOpts(C.sizeof(_lib.CMethodOpts), flags, None, 0, -1, None, None, 0,
                                None, 0)
        key = _lib.register(fn)
        _lib.check(_lib.fn("method")(node, name, C.byref(opts), _lib.handler_tramp, key,
                                     _lib.release_tramp))

    def _chan(self, kind: str, node, name: bytes, initial: bytes | None = None) -> int:
        out = C.c_void_p()
        if initial is None:
            rc = _lib.fn(kind)(node, name, _VERSIONS, 1, C.byref(out))
        else:
            rc = _lib.fn(kind)(node, name, initial, len(initial), _VERSIONS, 1, C.byref(out))
        _lib.check(rc)
        return out.value

    # -- handlers (run on the library's pool; ``req`` is answered exactly once)
    @staticmethod
    def _echo(req, params: bytes) -> None:
        _lib.fn("respond")(req, params, len(params))

    @staticmethod
    def _fail(req, _params: bytes) -> None:
        details = packb({"sim": "ams2"})
        _lib.fn("respond_error")(req, b"game_not_running", b"AMS2 is not running", details,
                                 len(details), 250)

    @staticmethod
    def _sleep(req, params: bytes) -> None:
        p = unpackb(params) if params else {}
        ms = int(p.get("ms", 1000))
        progress = bool(p.get("progress", False))
        for i in range(max(1, ms // 10)):
            if _lib.fn("request_wait_cancelled")(req, 10) == 0:
                _lib.fn("respond_error")(req, b"cancelled", b"cancelled", None, 0, -1)
                return
            if progress:
                d = packb(i)
                _lib.fn("request_progress")(req, d, len(d), -1)
        out = packb({"slept": ms})
        _lib.fn("respond")(req, out, len(out))

    def _count(self, req, _params: bytes) -> None:
        self._counter += 1
        out = packb(self._counter)
        _lib.fn("respond")(req, out, len(out))

    # -- the test's side
    def publish(self, data) -> int:
        raw = packb(data)
        n = C.c_size_t()
        _lib.check(_lib.fn("publish")(self.events, raw, len(raw), _lib.TS_NOW, 0,
                                      _lib.ACCEPT_CB(), None, C.byref(n)))
        return n.value

    def set_state(self, data) -> None:
        raw = packb(data)
        _lib.check(_lib.fn("publish")(self.state, raw, len(raw), _lib.TS_NOW, 0,
                                      _lib.ACCEPT_CB(), None, None))

    def send_sample(self, a: int, b0: float, b1: float) -> None:
        body = struct.pack("<Iff", a, b0, b1)
        _lib.check(_lib.fn("stream_send")(self.stream, body, len(body), _lib.TS_NOW, None))

    def subscriber_count(self) -> int:
        return _lib.fn("channel_subscribers")(self.events)

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            _lib.fn("endpoint_stop")(self._ep, None, None)
            for ch in (self.events, self.state, self.stream):
                _lib.fn("channel_free")(ch)
            _lib.fn("endpoint_free")(self._ep)


def start(pipe: str, key: bytes) -> RawEndpoint:
    return RawEndpoint(pipe, key)
