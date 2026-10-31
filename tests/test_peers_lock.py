"""The Peer registry's lock is reentrant: a Peer's __del__ can run (garbage collector) while the
same thread holds the lock in Peer._adopt; a plain Lock deadlocked there (Pitwall's cue service,
2026-10-27)."""

import threading

from stintmate_ipc import _client


def test_a_peer_dying_under_the_registry_lock_does_not_deadlock():
    done = threading.Event()

    def body():
        with _client._PEERS_LOCK:  # as Peer._adopt holds it
            p = _client.Peer(None, 987654321)  # no library handle: nothing to free
            del p  # __del__ runs here, on this thread, and takes the lock again
        done.set()

    t = threading.Thread(target=body, daemon=True)
    t.start()
    assert done.wait(5), "Peer.__del__ deadlocked on the registry lock"


def test_an_unregister_from_a_destructor_under_the_callback_registry_lock():
    from stintmate_ipc import _lib

    class Dying:
        def __init__(self, key):
            self.key = key

        def __del__(self):
            _lib.unregister(self.key)  # as Endpoint.__del__ forgetting its keys

    done = threading.Event()

    def body():
        key = _lib.register(object())
        with _lib._registry_lock:  # as register() holds it while inserting
            d = Dying(key)
            del d
        done.set()

    t = threading.Thread(target=body, daemon=True)
    t.start()
    assert done.wait(5), "an unregister from a destructor deadlocked on the registry lock"
