"""Python objects <-> MessagePack under the binding's rules (SPEC 6.1).

Tuples are arrays, ``bytes``/``bytearray`` are bin, map keys must be strings, integers must fit
64 bits (signed or unsigned), nesting is limited to 64 levels, extension types are refused.
Violations raise ``TypeError`` (``ValueError`` for depth) before anything is sent.
"""

from __future__ import annotations

from typing import Any

import threading

import msgpack

MAX_DEPTH = 64
_SCALARS = frozenset({int, float, str, bool, type(None), bytes, bytearray})
_SEQ = (list, tuple)
_STR = frozenset({str})
_local = threading.local()  # one Packer per thread (a Packer is not thread-safe)


def _packer() -> Any:
    p = _local.packer = msgpack.Packer(use_bin_type=True, default=_reject)
    return p


def _reject(obj: Any) -> Any:
    if isinstance(obj, int):
        raise TypeError("integers outside the 64-bit range cannot be sent (SPEC 6.1)")
    raise TypeError(f"cannot encode {type(obj).__name__} as MessagePack")


def _ext(code: int, data: bytes) -> Any:
    raise TypeError("extension types are not supported")


def _check(obj: Any, depth: int = 0) -> None:
    """Walks the containers level by level (the scalars and keys of a container are scanned at
    C speed): string keys, at most ``MAX_DEPTH`` levels."""
    level = [obj]
    while level:
        if depth > MAX_DEPTH:
            raise ValueError("value nested too deeply")
        nxt: list[Any] = []
        for o in level:
            if type(o) is dict:
                if not _STR.issuperset(map(type, o)):
                    for k in o:
                        if not isinstance(k, str):
                            raise TypeError(f"map keys must be strings, not {type(k).__name__}")
                o = o.values()
            elif type(o) is not list and type(o) is not tuple and not isinstance(o, (dict, list, tuple)):
                continue
            elif isinstance(o, dict):
                for k in o:
                    if not isinstance(k, str):
                        raise TypeError(f"map keys must be strings, not {type(k).__name__}")
                o = o.values()
            if not _SCALARS.issuperset(map(type, o)):
                nxt.extend([v for v in o if isinstance(v, (dict, list, tuple))])
        level = nxt
        depth += 1


def packb(obj: Any) -> bytes:
    """The MessagePack form of ``obj``."""
    if isinstance(obj, (dict, list, tuple)):
        _check(obj)
    try:
        try:
            pk = _local.packer
        except AttributeError:
            pk = _packer()
        return pk.pack(obj)
    except OverflowError:
        raise TypeError("integers outside the 64-bit range cannot be sent (SPEC 6.1)") from None
    except ValueError as e:  # msgpack's own recursion limit
        raise ValueError("value nested too deeply") from e


def unpackb(data: bytes | bytearray | memoryview) -> Any:
    """The Python object of a MessagePack value (arrays as lists, maps as dicts, bin as bytes)."""
    return msgpack.unpackb(data, raw=False, use_list=True, strict_map_key=True, ext_hook=_ext)
