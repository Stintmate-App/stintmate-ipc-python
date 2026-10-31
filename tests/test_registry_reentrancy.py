"""A registered object whose destructor unregisters must not deadlock the registry (shutdown)."""

import threading

from stintmate_ipc import _lib


def test_unregister_runs_destructors_outside_the_lock():
    class Owner:
        def __del__(self):
            _lib.unregister(other)

    other = _lib.register(object())
    key = _lib.register(Owner())
    t = threading.Thread(target=_lib.unregister, args=(key,), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "unregister deadlocked on a destructor that unregisters"
    assert other not in _lib.registry
