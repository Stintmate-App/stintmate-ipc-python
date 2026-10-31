"""Stream layouts (SPEC 9.3): validation by the library, packing with ``struct``."""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from stintmate_ipc import _lib
from stintmate_ipc._codec import packb, unpackb

_FORMAT = {"u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i", "u64": "Q",
           "i64": "q", "f32": "f", "f64": "d"}


class Layout:
    """A declared fixed layout of stream samples (little-endian, no padding)."""

    __slots__ = ("_fields", "_name", "_size", "_struct", "_hash")

    def __init__(self, fields: Iterable[Sequence[Any]], name: str | None = None) -> None:
        parsed = []
        for f in fields:
            if not 2 <= len(f) <= 3:
                raise ValueError(f"field {f}: (name, type) or (name, type, count)")
            parsed.append(list(f))
        doc: dict[str, Any] = {"fields": parsed}
        if name is not None:
            doc["name"] = name
        raw = packb(doc)
        canon = unpackb(_lib.call_buf("layout_normalize", raw, len(raw)))
        self._fields = tuple((str(n), str(t), int(c)) for n, t, c, *_ in canon["fields"])
        self._name = canon.get("name")
        self._size = int(canon["size"])
        self._struct = struct.Struct("<" + "".join(
            (str(c) if c > 1 else "") + _FORMAT[t] for _, t, c in self._fields))
        self._hash = hash(self._fields)

    @property
    def struct(self) -> struct.Struct:
        return self._struct

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def size(self) -> int:
        return self._size

    @property
    def fields(self) -> list[tuple[str, str, int]]:
        return list(self._fields)

    @property
    def names(self) -> list[str]:
        return [f[0] for f in self._fields]

    def _count(self) -> int:
        return sum(c for _, _, c in self._fields)

    def pack(self, *values: Any) -> bytes:
        if len(values) != self._count():
            raise ValueError(f"expected {self._count()} values, got {len(values)}")
        try:
            return self._struct.pack(*values)
        except (struct.error, OverflowError, TypeError) as e:
            raise ValueError(str(e)) from None

    def pack_dict(self, values: Mapping[str, Any]) -> bytes:
        flat: list[Any] = []
        for name, _, count in self._fields:
            v = values[name]
            if count == 1:
                flat.append(v)
            else:
                if len(v) != count:
                    raise ValueError(f"{name}: expected {count} values")
                flat.extend(v)
        return self.pack(*flat)

    def unpack(self, body: bytes) -> tuple[Any, ...]:
        try:
            return self._struct.unpack(body)
        except struct.error as e:
            raise ValueError(str(e)) from None

    def unpack_dict(self, body: bytes) -> dict[str, Any]:
        flat = self.unpack(body)
        out: dict[str, Any] = {}
        i = 0
        for name, _, count in self._fields:
            out[name] = flat[i] if count == 1 else tuple(flat[i:i + count])
            i += count
        return out

    def to_wire(self) -> dict[str, Any]:
        d: dict[str, Any] = {"size": self._size}
        if self._name is not None:
            d["name"] = self._name
        d["fields"] = [[n, t] if c == 1 else [n, t, c] for n, t, c in self._fields]
        return d

    @staticmethod
    def from_wire(d: Mapping[str, Any]) -> Layout:
        if not isinstance(d, Mapping) or not isinstance(d.get("fields"), (list, tuple)):
            raise ValueError("a layout is a map with 'fields'")
        layout = Layout([tuple(f[:3]) for f in d["fields"]], d.get("name"))
        if "size" in d and d["size"] != layout.size:
            raise ValueError(f"layout size {d['size']} does not match its fields ({layout.size})")
        return layout

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Layout) and other._fields == self._fields

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"Layout({self.fields!r}, name={self._name!r})"
