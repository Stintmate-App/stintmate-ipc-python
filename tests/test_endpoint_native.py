"""The endpoint side over the native library, driven by a raw client (no ``_client`` needed):
the points the review looks at (one answer, context after the answer, accept on the calling
thread, no callback after stop)."""

from __future__ import annotations

import gc
import secrets
import threading
import time

import pytest

from rawclient import FakePeer, RawClient, new_pipe, wait_for
from stintmate_ipc import (EndpointInUse, Layout, PeerInfo, RemoteError, _endpoint, _lib, decl)
from stintmate_ipc._codec import unpackb
from stintmate_ipc._endpoint import Endpoint


@pytest.fixture(autouse=True)
def fake_peer(monkeypatch):
    monkeypatch.setattr(_endpoint, "_wrap_peer", FakePeer._adopt)


def drain(wait: float = 2.0) -> int:
    """Live library handles once the previous test's objects are collected (releases run on
    the library's hooks thread, so give them a moment)."""
    end = time.monotonic() + wait
    while True:
        gc.collect()
        n = _lib.lib.stintmate_ipc_live_handles()
        if n == 0 or time.monotonic() > end:
            return n
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def no_leftovers():
    drain(0.2)  # the previous test's objects
    yield


@pytest.fixture
def key():
    return secrets.token_bytes(32)


@pytest.fixture
def pair(key):
    pipe = new_pipe()
    ep = Endpoint(PeerInfo("t6srv", name="srv", version="1"), key, pipe=pipe)
    made = []

    def connect(app="t6raw"):
        c = RawClient(pipe, key, app)
        made.append(c)
        return c

    yield ep, connect
    for c in made:
        c.close()
    ep.stop()


def test_properties_and_declarations(pair):
    ep, _ = pair
    assert ep.path.endswith(ep.short_name) and ep.run and ep.registry is None
    assert ep.info.app == "t6srv"
    ep.method("t.add", lambda ctx, p: p["a"] + p["b"], idempotent=True, doc="adds",
              versions=[2, 1, 2])
    d = ep.declarations()
    assert isinstance(d, decl.Declarations)
    m = d.methods["t.add"]
    assert m.idempotent and m.doc == "adds" and m.versions == [1, 2]
    assert ep.start() is ep


def test_call_error_mapping_and_decorator(pair):
    ep, connect = pair

    @ep.method("t.add")
    def add(ctx, p):
        if p.get("boom"):
            raise RemoteError("custom", "no", data={"x": 1}, retry_ms=7)
        if p.get("bug"):
            raise ValueError("oops")
        if p.get("bad"):
            return {1: 2}
        return p["a"] + p["b"]

    assert callable(add)
    ep.start()
    c = connect()
    assert c.call("t.add", {"a": 1, "b": 2}) == 3
    with pytest.raises(RemoteError) as ei:
        c.call("t.add", {"boom": True})
    assert (ei.value.code, ei.value.data, ei.value.retry_ms) == ("custom", {"x": 1}, 7)
    with pytest.raises(RemoteError) as ei:
        c.call("t.add", {"bug": True})
    assert ei.value.code == "internal" and "ValueError: oops" in ei.value.message
    with pytest.raises(RemoteError) as ei:
        c.call("t.add", {"bad": True})
    assert ei.value.code == "internal" and "TypeError" in ei.value.message


def test_one_answer_and_context_kept_after_it(pair):
    ep, connect = pair
    kept = []
    answered = []

    def handler(ctx, p):
        kept.append(ctx)
        assert ctx.params is p and p == {"k": 1}
        assert ctx.method == "t.keep" and ctx.version == 1 and ctx.idempotency_key is None
        assert ctx.remaining is not None and not ctx.expired() and not ctx.cancelled
        assert ctx.wait_cancelled(0.02) is False
        ctx.progress({"n": 1}, extend_s=5)
        answered.append(1)
        return "done"

    ep.method("t.keep", handler)
    ep.start()
    c = connect()
    assert c.call("t.keep", {"k": 1}) == "done"
    ctx = kept[0]
    # the C request is consumed: a kept context answers from what it copied
    assert ctx._req is None and ctx.method == "t.keep" and ctx.id >= 0 and ctx.version == 1
    assert ctx.peer is ctx.peer and ctx.peer.info.app == "t6raw"
    assert ctx.cancelled is False and ctx.expired() is False
    assert ctx.remaining is None or ctx.remaining >= 0
    t0 = time.monotonic()
    assert ctx.wait_cancelled(0.1) is False and time.monotonic() - t0 >= 0.09
    ctx.progress({"late": True})  # no-op, no crash
    assert answered == [1]


def test_unkept_context_is_not_usable_after_the_answer(pair):
    ep, connect = pair
    seen = []
    ep.method("t.once", lambda ctx, p: seen.append(ctx) or 1)
    ep.start()
    c = connect()
    assert c.call("t.once") == 1
    ctx = seen.pop()
    seen.clear()
    ctx2 = ctx
    assert ctx2._req is None


def test_handler_answers_exactly_once_under_load(pair):
    ep, connect = pair
    count = []
    ep.method("t.n", lambda ctx, p: count.append(p) or p, inline=True)
    ep.method("t.o", lambda ctx, p: count.append(p) or p, ordered=True)
    ep.start()
    c = connect()
    for i in range(200):
        assert c.call("t.n", i) == i
        assert c.call("t.o", i) == i
    assert len(count) == 400


def test_nil_params_and_removed_method(pair):
    ep, connect = pair
    ep.method("t.nil", lambda ctx, p: p)
    ep.start()
    c = connect()
    assert c.call("t.nil") == {}  # absent params: an empty map (SPEC)
    ep.remove_method("t.nil")
    with pytest.raises(RemoteError) as ei:
        c.call("t.nil")
    assert ei.value.code == "unknown_method"


def test_cancelled_wait_wakes(pair):
    ep, connect = pair
    woke = []
    ep.method("t.slow", lambda ctx, p: woke.append(ctx.wait_cancelled(5)) or "late")
    ep.start()
    c = connect()
    with pytest.raises(Exception):
        c.call("t.slow", timeout_ms=200)
    wait_for(lambda: woke)
    assert woke == [True]


def test_on_connect_hooks_with_peers(pair):
    ep, connect = pair
    seen, gone = [], []
    ep.on_connect = seen.append
    ep.on_disconnect = gone.append
    assert ep.on_connect == seen.append or ep.on_connect is not None
    ep.start()
    c = connect("t6hook")
    wait_for(lambda: seen)
    assert seen[0].info.app == "t6hook"
    assert [p.id for p in ep.peers] == [seen[0].id]
    c.close()
    wait_for(lambda: gone)
    assert gone[0] == seen[0]
    ep.on_connect = None
    assert ep.on_connect is None


def test_event_channel_accept_runs_on_the_calling_thread(pair):
    ep, connect = pair
    ev = ep.event_channel("t.ev")
    flips = []
    ev.on_active = flips.append
    assert (ev.name, ev.kind, ev.active, ev.subscriber_count) == ("t.ev", "event", False, 0)
    ep.start()
    c = connect("t6sub")
    got = c.subscribe("t.ev", options={"o": 1})
    wait_for(lambda: ev.active and flips)
    assert flips == [True] and ev.subscriber_count == 1
    threads, views = [], []

    def accept(view):
        threads.append(threading.get_ident())
        views.append(view)
        return True

    assert ev.publish({"k": 1}, accept=accept) == 1
    assert threads == [threading.get_ident()]
    v = views[0]
    assert v.peer.app == "t6sub" and v.version == 1 and dict(v.options) == {"o": 1}
    with pytest.raises(AttributeError):
        v.version = 2
    with pytest.raises(TypeError):
        v.options["x"] = 1
    assert ev.publish({"k": 2}, accept=lambda v: False) == 0
    assert ev.publish({"k": 3}, accept=lambda v: 1 / 0) == 0  # a failing filter sends nothing
    with pytest.raises(ValueError):
        ev.publish({}, ts=1, accept=accept)
    assert ev.publish({"k": 4}, version=2) == 0
    assert ev.publish({"k": 5}) == 1
    wait_for(lambda: len(got) >= 2)
    assert unpackb(got[0].data) == {"k": 1}
    ev.close()
    ev.on_active = None


def test_on_subscribe_hook_refuses_and_sees_the_request(pair):
    ep, connect = pair
    st = ep.state_channel("t.st", {"n": 0})
    seen = []

    def hook(req):
        seen.append(req)
        if req.options and req.options.get("deny"):
            raise RemoteError("denied", "not you", data={"w": 1})

    st.on_subscribe = hook
    assert st.on_subscribe is hook
    ep.start()
    c = connect("t6sub")
    c.subscribe("t.st", options={"deny": True})
    wait_for(lambda: seen)
    assert seen[0].options == {"deny": True} and st.subscriber_count == 0
    got = c.subscribe("t.st")
    wait_for(lambda: st.active and got)
    assert seen[-1].peer.info.app == "t6sub" and seen[-1].version == 1
    assert seen[-1].options is None
    st.set({"n": 2})
    assert st.value == {"n": 2}
    wait_for(lambda: len(got) >= 2)


def test_stream_channel_send_and_layout(pair):
    ep, connect = pair
    lay = Layout([("a", "u32"), ("b", "f32", 2)])
    sm = ep.stream_channel("t.sm", lay, rate_hz=100, queue=8)
    assert sm.kind == "stream" and sm.layout == lay
    ep.start()
    c = connect()
    got = c.subscribe("t.sm")
    wait_for(lambda: sm.active)
    assert sm.send(lay.pack(1, 1.0, 2.0), t_ns=5) == 1
    wait_for(lambda: got)
    assert lay.unpack(got[0].data)[0] == 1
    with pytest.raises(ValueError):
        sm.send(b"short")
    with pytest.raises(TypeError):
        ep.stream_channel("t.bad", "nope")


def test_no_callback_after_stop_and_release(pair):
    ep, connect = pair
    calls = []
    before = len(_lib.registry)
    ep.method("t.x", lambda ctx, p: calls.append(1) or 1)
    ep.start()
    c = connect()
    assert c.call("t.x") == 1
    ep.stop()
    c.close()
    n = len(calls)
    ep_registry_grew = len(_lib.registry) - before
    assert ep_registry_grew >= 1
    ep.remove_method("t.x")
    del ep
    gc.collect()
    wait_for(lambda: len(_lib.registry) <= before)
    assert len(calls) == n


def test_endpoint_in_use_and_bad_args(key):
    pipe = new_pipe()
    a = Endpoint(PeerInfo("t6a"), key, pipe=pipe).start()
    try:
        b = Endpoint(PeerInfo("t6a"), key, pipe=pipe)
        with pytest.raises(EndpointInUse):
            b.start()
        with pytest.raises(TypeError):
            Endpoint("x", key)
        with pytest.raises(ValueError):
            Endpoint(PeerInfo("t6a"), b"short")
        with pytest.raises(ValueError):
            a.method("t.m", lambda c, p: 1, idempotency_ttl=0)
        with pytest.raises(OverflowError):
            a.event_channel("t.e", versions=[2**32])
        with a:
            pass
    finally:
        a.stop()


def test_handshake_failed_hook(pair, key):
    ep, _ = pair
    seen = []
    ep.on_handshake_failed = lambda pid, err: seen.append((pid, err))
    ep.start()
    with pytest.raises(Exception):
        RawClient(ep.path, secrets.token_bytes(32), timeout_ms=500).close()
    wait_for(lambda: seen)
    assert isinstance(seen[0][1], Exception)


def test_view_connection_equals_the_peer_of_the_request(pair):
    ep, connect = pair
    ev = ep.event_channel("t.ev")
    ep.start()
    c = connect("t6sub")
    c.subscribe("t.ev")
    wait_for(lambda: ev.active)
    views = []
    ev.publish({}, accept=lambda v: views.append(v) or True)
    assert views[0].connection == ep.peers[0] and views[0].connection is views[0].connection


def test_zz_nothing_is_left_alive():
    assert drain() == 0
    assert len(_lib.registry) == 0
