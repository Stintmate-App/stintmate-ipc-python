"""A minimal client over the library's own functions, so the endpoint side is tested without
the package's ``Client``: connect, call, subscribe. Test support only."""

from __future__ import annotations

import ctypes as C
import secrets
import time

from stintmate_ipc import PeerInfo, _lib
from stintmate_ipc._codec import packb, unpackb
from stintmate_ipc._keys import key_source_c

fn = _lib.fn


def wait_for(cond, timeout: float = 5.0, interval: float = 0.005):
    end = time.monotonic() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        time.sleep(interval)


def new_pipe() -> str:
    return f"stintmate-t6test-{secrets.token_hex(6)}"


class FakePeer:
    """Stands in for ``_client.Peer``: owns one reference, equal by connection id."""

    def __init__(self, ptr: int) -> None:
        self.ptr = ptr
        self.id = fn("peer_id")(ptr)

    @classmethod
    def _adopt(cls, ptr: int) -> FakePeer:
        return cls(ptr)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakePeer) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __del__(self) -> None:
        if self.ptr:
            fn("peer_free")(self.ptr)
            self.ptr = 0

    @property
    def info(self) -> PeerInfo:
        return PeerInfo.from_wire(unpackb(_lib.call_buf("peer_get_info", self.ptr)))


class RawClient:
    def __init__(self, pipe: str, key: bytes, app: str = "t6raw", timeout_ms: int = 5000) -> None:
        info_c, k1 = PeerInfo(app).to_c()
        key_c, k2 = key_source_c(key)
        out = C.c_void_p()
        _lib.check(fn("client_new")(_lib.cstr(pipe), C.byref(info_c), C.byref(key_c), None,
                                    C.byref(out)))
        self.c = out.value
        self.subs: list = []
        peer = C.c_void_p()
        rc = fn("client_connect")(self.c, timeout_ms, C.byref(peer))
        if rc < 0:
            err = _lib.error_for(rc)
            self.close()
            raise err
        fn("peer_free")(peer)

    def call(self, method: str, params=None, timeout_ms: int = 5000):
        payload = packb(params)
        opts = _lib.CCallOpts(C.sizeof(_lib.CCallOpts), 1, timeout_ms, None,
                              _lib.PROGRESS_CB(), None, _lib.RELEASE_CB())
        return unpackb(_lib.call_buf("client_call", self.c, _lib.cstr(method), payload,
                                     len(payload), C.byref(opts)))

    def subscribe(self, channel: str, version: int = 1, options=None) -> list:
        got: list = []
        key = _lib.register(got.append)
        opt = packb(options) if options is not None else b""
        sopts = _lib.CSubscribeOpts(C.sizeof(_lib.CSubscribeOpts), version, _lib.vptr(opt or None),
                                    len(opt), 5000, _lib.delivery_tramp, _lib.END_CB(), key,
                                    _lib.release_tramp)
        out = C.c_void_p()
        _lib.check(fn("client_subscribe")(self.c, _lib.cstr(channel), C.byref(sopts),
                                          C.byref(out)))
        self.subs.append(out.value)
        return got

    def close(self) -> None:
        for s in self.subs:
            fn("unsubscribe")(s)
        self.subs.clear()
        if self.c:
            fn("client_close")(self.c)
            fn("client_free")(self.c)
            self.c = 0
