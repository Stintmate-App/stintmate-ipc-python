"""Registry entries (SPEC 4.4): data class and listing on top of the native helpers."""

from __future__ import annotations

import ctypes
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any



@dataclass
class RegistryEntry:
    r"""One ``<workspace>\ipc\endpoints\<endpoint id>.json``."""

    endpoint: str
    pipe: str
    app: str
    process: str | None
    pid: int
    run: str
    versions: list[int]
    started_at: str
    name: str = ""
    version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def to_json(self) -> dict[str, Any]:
        d = {"schema": 1, "endpoint": self.endpoint, "pipe": self.pipe, "app": self.app,
             "process": self.process, "pid": self.pid, "run": self.run,
             "versions": list(self.versions), "started_at": self.started_at,
             "name": self.name, "version": self.version}
        d.update(self.extra)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any], path: Path | None = None) -> RegistryEntry:
        known = {"schema", "endpoint", "pipe", "app", "process", "pid", "run", "versions",
                 "started_at", "name", "version"}
        return cls(endpoint=str(d["endpoint"]), pipe=str(d["pipe"]), app=str(d["app"]),
                   process=d.get("process"), pid=int(d["pid"]), run=str(d["run"]),
                   versions=[int(v) for v in d.get("versions", [])],
                   started_at=str(d.get("started_at", "")), name=str(d.get("name", "")),
                   version=str(d.get("version", "")),
                   extra={k: v for k, v in d.items() if k not in known}, path=path)

    def alive(self) -> bool:
        """The pipe exists (the only liveness test that counts; the PID may be reused)."""
        return pipe_exists(self.pipe)


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
_kernel32.WaitNamedPipeW.restype = ctypes.c_int32
_ERROR_FILE_NOT_FOUND = 2


def pipe_exists(pipe: str) -> bool:
    """Whether a server listens on the pipe (it need not have a free instance)."""
    if _kernel32.WaitNamedPipeW(pipe, 1):
        return True
    return ctypes.get_last_error() != _ERROR_FILE_NOT_FOUND


def list_endpoints(directory: str | os.PathLike[str], alive_only: bool = True,
                   prune: bool = False) -> list[RegistryEntry]:
    """Entries of the registry; with ``alive_only`` only those whose pipe exists; ``prune``
    deletes the stale ones (older than 10 s, so a server that is starting is spared)."""
    base = Path(directory)
    try:
        files = sorted(p for p in base.iterdir() if p.suffix == ".json")
    except OSError:
        return []
    out: list[RegistryEntry] = []
    for p in files:
        try:
            entry = RegistryEntry.from_json(json.loads(p.read_bytes()), p)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if alive_only or prune:
            ok = entry.alive()
            if not ok and prune:
                try:
                    if time.time() - p.stat().st_mtime > 10:
                        p.unlink()
                except OSError:
                    pass
            if not ok and alive_only:
                continue
        out.append(entry)
    return out
