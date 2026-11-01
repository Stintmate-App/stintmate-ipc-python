# stintmate-ipc (Python)

The Python binding of [Stintmate IPC v2](https://github.com/Stintmate-App/stintmate-ipc): a
`ctypes` layer over the native `stintmate_ipc.dll`. The wheel bundles `stintmate_ipc.dll` and the
conformance tool `stintmate-ipc-check.exe` under `stintmate_ipc/_bin/`, so it works without a
Rust toolchain.

- Windows x64 only; Python 3.12 or newer. The one Python dependency is `msgpack`.
- The version of this package equals the version of the `stintmate-ipc` release whose native
  files it wraps (0.3.2 wraps `stintmate-ipc` v0.3.2).
- Licence: Apache-2.0 (`LICENSE`, `NOTICE`). Changes: `CHANGELOG.md`.

## Install

Download the wheel and `SHA256SUMS` from a
[release of this repository](https://github.com/Stintmate-App/stintmate-ipc-python/releases),
check the hash (`Get-FileHash .\stintmate_ipc-0.3.2-py3-none-win_amd64.whl -Algorithm SHA256`
must print the value listed for the wheel in `SHA256SUMS`), and install:

```powershell
uv pip install .\stintmate_ipc-0.3.2-py3-none-win_amd64.whl
```

The protocol, the C ABI and the contract are documented in the
[`stintmate-ipc`](https://github.com/Stintmate-App/stintmate-ipc) repository.

## Usage

An endpoint serves methods and channels; a client connects to it and calls or subscribes.
Both ends need the same 32-byte key, kept in `<workspace>\ipc\key` (`default_key_path()`; the
workspace is found as in SPEC section 4.3 of the protocol: `STINTMATE_WORKSPACE`, then the
pointer file `%APPDATA%\Stintmate\settings.json`). This runs as it is, in one process:

```python
import tempfile
from pathlib import Path

from stintmate_ipc import Endpoint, KeyFile, PeerInfo, connect, pipe_path

# Both ends need the same 32-byte key. Normally it is <workspace>\ipc\key
# (default_key_path()); here a throw-away file, created on first use.
key = KeyFile(Path(tempfile.mkdtemp()) / "key", create=True)

server_info = PeerInfo("demo", name="Demo server", version="1.0")
with Endpoint(server_info, key) as endpoint:        # pipe: stintmate-demo-<user>
    @endpoint.method("demo.shout")
    def shout(ctx, params):
        return {"text": params["text"].upper()}

    endpoint.start()

    peer = connect(pipe_path("demo"), PeerInfo("demo-client", name="Demo client"), key)
    print(peer.call("demo.shout", {"text": "hello"}))   # {'text': 'HELLO'}
    peer.close()
```

The public names are those of `stintmate_ipc.__all__`: `Endpoint` (a server), `Client` (a
connection that stays up and reconnects), `connect` (one connection, no reconnect), `Peer`
(calls and subscriptions on a connection), the channels (`EventChannel`, `StateChannel`,
`StreamChannel` with a `Layout`), the key and naming helpers (`KeyFile`, `ensure_key`,
`read_key`, `find_workspace`, `short_name`, `pipe_path`, ...) and the exceptions under
`IpcError`. `stintmate-ipc-check` (the conformance tool) is installed with the package.

## Building

The build hook (`hatch_build.py`) bundles the native files into the wheel. Two ways to provide
them:

- **Released native files (standalone).** `scripts/fetch-native.ps1` downloads
  `stintmate_ipc.dll`, `stintmate-ipc-check.exe`, `stintmate_ipc.h` and `SHA256SUMS` from the `stintmate-ipc`
  release `v<version>` (the version in `pyproject.toml`), checks the hashes and writes them to
  `.native\`. Only the newest release is guaranteed to still have its assets: for an older tag
  the download can fail, and that tag cannot be built from a release (build it from source with
  cargo instead). While the release is private, set `STINTMATE_IPC_READ_TOKEN` to a GitHub token that
  can read it; `STINTMATE_IPC_RELEASE` replaces the source with a local folder or a base URL.
  Then point the hook at the folder:

  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\fetch-native.ps1
  $env:STINTMATE_IPC_PREBUILT = "$PWD\.native"
  uv build --wheel
  ```

- **From source with cargo.** Without `STINTMATE_IPC_PREBUILT`, the hook runs
  `cargo build --release -p stintmate-ipc-ffi -p stintmate-ipc-check` in `../stintmate-ipc`, a
  checkout of the `stintmate-ipc` repository beside this one (the Rust toolchain it pins is
  required).

## Tests

With `STINTMATE_IPC_PREBUILT` set as above (the tests that check the ctypes function prototypes also read `stintmate_ipc.h` from that folder):

```powershell
uv run pytest -q
```

`uv run` installs the package editable and the hook copies the native files into
`src/stintmate_ipc/_bin/` (gitignored).

## Parity rule

`tests/test_parity.py` compares every public name (signatures, members, exception bases) with
`tests/parity_data.json`, frozen data; changing the public API means changing that data in the
same commit. The data is edited by hand, and no generator is kept here.

## Releasing

Actions > `release` > Run workflow, with the tag (`v<version>`, an ancestor of `main`, no `+`)
and `dry_run` (default: leave a draft). It checks the tag, builds and tests the tag's tree,
uploads the wheel and `SHA256SUMS` as a draft, downloads them again to compare hashes, and
publishes unless `dry_run`. The steps are the scripts in `scripts/`. `ci` runs the build and the
tests by hand.
