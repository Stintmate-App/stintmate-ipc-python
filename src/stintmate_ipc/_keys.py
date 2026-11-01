"""Peer info, key sources, key files and endpoint naming (over the library's functions)."""

from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
from typing import Any

from stintmate_ipc import _lib
from stintmate_ipc._codec import packb
from stintmate_ipc._errors import KeyFileError

_fspath = os.fspath


class PeerInfo:
    """Who a peer says it is (``auth.peer`` / ``welcome.peer``)."""

    __slots__ = ("app", "name", "version", "process", "meta")

    def __init__(self, app: str, name: str = "", version: str = "", process: str | None = None,
                 meta: dict[str, Any] | None = None) -> None:
        self.app = app
        self.name = name
        self.version = version
        self.process = process
        self.meta = {} if meta is None else meta

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PeerInfo):
            return NotImplemented
        return all(getattr(self, a) == getattr(other, a) for a in self.__slots__)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (f"PeerInfo(app={self.app!r}, name={self.name!r}, version={self.version!r}, "
                f"process={self.process!r}, meta={self.meta!r})")

    def to_c(self) -> tuple[_lib.CPeerInfo, tuple]:
        """The C struct and the objects that keep its pointers alive (hold them for the call)."""
        meta = packb(self.meta) if self.meta else None
        keep = (_lib.cstr(self.app), _lib.cstr(self.name), _lib.cstr(self.version),
                _lib.cstr(self.process), meta)
        return (_lib.CPeerInfo(keep[0], keep[1], keep[2], keep[3], _lib.vptr(meta),
                              len(meta or b"")),
                keep)

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> PeerInfo:
        """A ``PeerInfo`` from the ``peer`` map of a ``hello``, ``auth`` or ``welcome`` message."""
        return cls(str(d.get("app", "")), str(d.get("name", "")), str(d.get("version", "")),
                   d.get("process"), dict(d.get("meta") or {}))


class KeyFile:
    """A key source that re-reads the file each time it is asked; with ``create=True`` a
    missing file is created first (``ensure_key``), otherwise it must exist (``read_key``)."""

    def __init__(self, path: str | os.PathLike[str], create: bool = False) -> None:
        self._path = Path(path)
        self._create = bool(create)

    @property
    def path(self) -> Path:
        """The key file's path."""
        return self._path

    @property
    def create(self) -> bool:
        """Whether a missing key file is created when the key is asked for."""
        return self._create

    def __call__(self) -> bytes:
        return ensure_key(self._path) if self._create else read_key(self._path)

    def __repr__(self) -> str:
        return f"KeyFile({str(self._path)!r})"


def key_source_c(key: KeyFile | bytes) -> tuple[_lib.CKeySource, tuple]:
    """A ``KeyFile`` or 32 bytes as the C key source (and what keeps it alive)."""
    if isinstance(key, KeyFile):
        path = _fspath(key.path).encode("utf-8")
        return (_lib.CKeySource(C.sizeof(_lib.CKeySource), path, None, 0, int(key.create)),
                (path,))
    if isinstance(key, (bytes, bytearray)):
        if len(key) != 32:
            raise ValueError("a key is 32 bytes")
        raw = bytes(key)
        return _lib.CKeySource(C.sizeof(_lib.CKeySource), None, _lib.vptr(raw), 32, 0), (raw,)
    raise TypeError("key must be a KeyFile or 32 bytes")


def _path_arg(p: str | os.PathLike[str] | None) -> bytes | None:
    return None if p is None else _fspath(p).encode("utf-8")


def _text(raw: bytes) -> str:
    return raw.decode("utf-8")


def default_key_path(workspace: str | os.PathLike[str] | None = None) -> Path:
    """``<workspace>\\ipc\\key`` (the found workspace when ``workspace`` is None)."""
    return Path(_text(_lib.call_buf("default_key_path", _path_arg(workspace))))


def ensure_key(path: str | os.PathLike[str]) -> bytes:
    """The key of the file, creating it (atomically, once) when missing."""
    return _lib.call_buf("ensure_key", _path_arg(path))


def read_key(path: str | os.PathLike[str]) -> bytes:
    """The 32 key bytes of the key file at ``path``, which must exist."""
    return _lib.call_buf("read_key", _path_arg(path))


def parse_key(text: bytes) -> bytes:
    """32 raw bytes from key file text (64 hex digits; a BOM and whitespace are accepted)."""
    text = bytes(text)
    return _lib.call_buf("parse_key", text, len(text))


def format_key(key: bytes) -> bytes:
    """Key file text (hex and CRLF) of 32 raw bytes."""
    key = bytes(key)
    return _lib.call_buf("format_key", key, len(key))


def pipe_user(user: str | None = None) -> str:
    """The user part of an endpoint name: ``user`` (default: the current Windows user) with
    ASCII letters lowercased."""
    return _text(_lib.call_buf("pipe_user", _lib.cstr(user)))


def short_name(app: str, process: str | None = None, user: str | None = None) -> str:
    """The endpoint's short name, ``stintmate-<app>[-<process>]-<user>``: the name bound into
    the handshake's MACs."""
    return _text(_lib.call_buf("short_name", _lib.cstr(app), _lib.cstr(process), _lib.cstr(user)))


def pipe_path(app: str, process: str | None = None, user: str | None = None) -> str:
    """The pipe path of the same name: ``\\\\.\\pipe\\`` followed by ``short_name``."""
    return _text(_lib.call_buf("pipe_path", _lib.cstr(app), _lib.cstr(process), _lib.cstr(user)))


def find_workspace() -> Path | None:
    """The workspace folder (SPEC section 4.3: ``STINTMATE_WORKSPACE``, then the pointer file
    ``%APPDATA%\\Stintmate\\settings.json``), or ``None`` when there is none."""
    raw = _lib.call_buf("find_workspace")
    return Path(_text(raw)) if raw else None


def registry_dir(workspace: str | os.PathLike[str]) -> Path:
    """The endpoint registry folder of ``workspace``: ``<workspace>\\ipc\\endpoints``."""
    return Path(_text(_lib.call_buf("registry_dir", _path_arg(workspace))))


__all__ = ["KeyFile", "KeyFileError", "PeerInfo", "default_key_path", "ensure_key",
           "find_workspace", "format_key", "key_source_c", "parse_key", "pipe_path", "pipe_user",
           "read_key", "registry_dir", "short_name"]
