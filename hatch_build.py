"""Build hook: bundles the native library into the wheel.

With ``STINTMATE_IPC_PREBUILT=<dir>`` the two files ``stintmate_ipc.dll`` and
``stintmate-ipc-check.exe`` are copied from that folder (``scripts/fetch-native.ps1`` fills
``.native`` with the released ones). Without it, the hook runs
``cargo build --release -p stintmate-ipc-ffi -p stintmate-ipc-check`` in ``../stintmate-ipc``
(a checkout of the ``stintmate-ipc`` repository beside this one). Either way the files are
force-included as ``stintmate_ipc/_bin/*``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

FILES = ("stintmate_ipc.dll", "stintmate-ipc-check.exe")


class NativeBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        prebuilt = os.environ.get("STINTMATE_IPC_PREBUILT")
        if prebuilt:
            source = Path(prebuilt)
        else:
            source = self._cargo_build(root / ".." / "stintmate-ipc")
        # An editable install (``uv run`` in this folder) imports from ``src``: the files go there.
        # A wheel build stages in its own temp folder (two apps may build at once).
        if version == "editable":
            stage = root / "src" / "stintmate_ipc" / "_bin"
        else:
            self._tmp = tempfile.mkdtemp(prefix="stintmate-ipc-")
            stage = Path(self._tmp) / "_bin"
        stage.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            src = source / name
            if not src.is_file():
                raise RuntimeError(f"stintmate-ipc: {src} is missing")
            shutil.copy2(src, stage / name)
            if version != "editable":
                build_data["force_include"][str(stage / name)] = f"stintmate_ipc/_bin/{name}"
        build_data["pure_python"] = False
        build_data["tag"] = "py3-none-win_amd64"

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    def _cargo_build(self, repo: Path) -> Path:
        manifest = repo.resolve() / "Cargo.toml"
        if not manifest.is_file():
            raise RuntimeError(
                f"stintmate-ipc: {manifest} not found. Either set STINTMATE_IPC_PREBUILT=<folder with "
                "stintmate_ipc.dll and stintmate-ipc-check.exe> (scripts/fetch-native.ps1 "
                "fills .native), or check out the stintmate-ipc repository beside this one "
                "as ../stintmate-ipc to build with cargo")
        cargo = shutil.which("cargo") or shutil.which(
            "cargo", path=str(Path.home() / ".cargo" / "bin"))
        if cargo is None:
            raise RuntimeError(
                "stintmate-ipc: cargo not found. Install the Rust toolchain (rustup, pinned by "
                "../stintmate-ipc/rust-toolchain.toml) or set STINTMATE_IPC_PREBUILT=<folder>")
        cmd = [cargo, "build", "--release", "-p", "stintmate-ipc-ffi", "-p",
               "stintmate-ipc-check", "--manifest-path", str(manifest)]
        self.app.display_info("stintmate-ipc: " + " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=manifest.parent)
        target = os.environ.get("CARGO_TARGET_DIR")
        return (Path(target) if target else manifest.parent / "target") / "release"
