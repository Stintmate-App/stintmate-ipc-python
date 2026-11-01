"""Client gaps found by migrating applications: behaviours the client keeps."""

import gc
import secrets
import threading

import pytest

import stintmate_ipc as ipc
from stintmate_ipc import Client, PeerInfo

from test_client import wait_for


def test_peer_is_one_object_per_connection():
    key = secrets.token_bytes(32)
    pipe = f"stintmate-t7d-{secrets.token_hex(5)}"
    from stintmate_ipc import Endpoint
    ep = Endpoint(PeerInfo("t7dsrv"), key, pipe=pipe)
    seen = []
    ep.on_connect = seen.append

    @ep.method("t.who")
    def who(ctx, p):
        return None

    ep.start()
    c = Client(pipe, PeerInfo("t7dcli"), key)
    p = c.connect(5)
    try:
        assert c.peer is p and c.wait_connected(1) is p and c.peer is c.peer
        assert wait_for(lambda: seen)
        assert ep.peers[0] is seen[0] and ep.peers[0] is ep.peers[0]
        d = {seen[0]: 1}
        assert d[ep.peers[0]] == 1
    finally:
        c.close()
        ep.stop()


def test_peer_object_is_released_when_unused():
    key = secrets.token_bytes(32)
    pipe = f"stintmate-t7d-{secrets.token_hex(5)}"
    from stintmate_ipc import Endpoint
    ep = Endpoint(PeerInfo("t7dsrv"), key, pipe=pipe)
    ep.start()
    c = Client(pipe, PeerInfo("t7dcli"), key)
    c.connect(5)
    try:
        a = c.peer
        b = c.peer
        assert a is b
        del a, b
        gc.collect()
        assert c.peer.info.app == "t7dsrv"
    finally:
        c.close()
        ep.stop()


def test_subscription_ended_and_peer(client, endpoint):
    got = []
    sub = client.peer.subscribe("t.events", got.append)
    assert sub.peer is client.peer
    assert not sub.ended.is_set()
    assert sub.ended.wait(0.05) is False
    sub.close()
    assert sub.ended.wait(2) is True and sub.ended.is_set()


def test_wait_connected_timeout_is_not_connected(key):
    c = Client("stintmate-nobody-here-t7d", PeerInfo("c"), key)
    c.start()
    try:
        assert wait_for(lambda: c.last_error is not None, 5)
        with pytest.raises(ipc.NotConnected) as ei:
            c.wait_connected(0.2)
        assert isinstance(ei.value.__cause__, ipc.PipeNotFound)
        with pytest.raises(ipc.NotConnected):
            c.connect(0.2)
    finally:
        c.close()


def test_client_subscription_subscribed_is_an_event(client, endpoint):
    sub = client.subscribe("t.events", lambda e: None)
    assert sub.subscribed.wait(3) is True and sub.subscribed.is_set()
    assert sub.subscribed
    sub.close()
    ev = threading.Event()
    assert hasattr(sub.subscribed, "wait") and hasattr(ev, "wait")
