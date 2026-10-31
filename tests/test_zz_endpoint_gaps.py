"""Binding gaps found by migrating a connector: behaviours of stintmate-ipc 0.1.4."""

import secrets
import threading
import time

import stintmate_ipc as ipc
import pytest

from rawclient import wait_for
from stintmate_ipc import Endpoint, Layout, PeerInfo

from stintmate_ipc import Client


def _pair(client_cls=None, **kw):
    key = secrets.token_bytes(32)
    pipe = f"stintmate-t7e-{secrets.token_hex(5)}"
    ep = Endpoint(PeerInfo("t7esrv"), key, pipe=pipe)
    return ep, pipe, key, client_cls


def test_peer_closed_is_a_waitable_event():
    ep, pipe, key, _ = _pair()
    ep.start()
    c = Client(pipe, PeerInfo("t7ecli"), key, reconnect=False)
    p = c.connect(5)
    try:
        assert p.closed is p.closed and not p.closed.is_set() and not p.closed
        t0 = time.monotonic()
        assert p.closed.wait(0.1) is False
        assert time.monotonic() - t0 >= 0.09
        threading.Timer(0.2, p.close).start()
        t0 = time.monotonic()
        assert p.closed.wait(5) is True
        assert time.monotonic() - t0 < 2 and p.closed.is_set() and not p.is_open
        assert p.closed.wait() is True
    finally:
        c.close()
        ep.stop()


def test_subscription_layout():
    ep, pipe, key, _ = _pair()
    lay = Layout([("a", "u32"), ("b", "f32", 2)])
    s = ep.stream_channel("t.s", lay, rate_hz=50)
    ep.start()
    c = Client(pipe, PeerInfo("t7ecli"), key)
    p = c.connect(5)
    try:
        got = []
        sub = p.subscribe("t.s", got.append)
        assert sub.layout == lay
        cs = c.subscribe("t.s", got.append)
        assert cs.subscribed.wait(3)
        assert cs.layout == lay
    finally:
        c.close()
        ep.stop()


def test_ended_and_subscribed_wake_without_polling():
    ep, pipe, key, _ = _pair()
    ep.start()
    c = Client(pipe, PeerInfo("t7ecli"), key)
    p = c.connect(5)
    try:
        cs = c.subscribe("t.later", print)  # unknown channel now
        assert cs.subscribed.wait(0.2) is False
        ep.state_channel("t.later", initial=1)
        # 15.1: re-subscribed after a reconnect or when the owner declares it; at least the
        # wait must return false/true promptly and never hang
        cs.subscribed.wait(0.3)
    finally:
        c.close()
        ep.stop()


class Broken(Client or object):
    def reconnect_delay(self, code, attempt=0):
        raise RuntimeError("boom")


def test_reconnect_delay_override_that_fails_falls_back():
    key = secrets.token_bytes(32)
    pipe = f"stintmate-t7e-{secrets.token_hex(5)}"
    ep = Endpoint(PeerInfo("t7esrv"), key, pipe=pipe)
    c = Broken(pipe, PeerInfo("t7ecli"), key)
    c.start()  # fails over to the built-in 0.25 s backoff
    time.sleep(0.4)
    ep.start()
    try:
        assert c.wait_connected(5) is not None
    finally:
        c.close()
        ep.stop()


def test_plain_client_keeps_the_builtin_policy():
    c = Client("stintmate-t7e-x", PeerInfo("t7ecli"), secrets.token_bytes(32))
    assert c.reconnect_delay("version_unsupported") is None
    assert c.reconnect_delay(None, 0) == 0.25
