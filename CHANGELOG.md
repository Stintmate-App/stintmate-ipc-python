# Changelog

## 0.3.2+docs.2 (2026-11-01)

- Documentation corrected against the code: a usage section in the README (an example that runs),
  how to check the wheel's hash, which releases can still be built from their assets, docstrings for
  the parameters of `Endpoint`, `method` and the channels and for the key and naming functions, and
  the wording about earlier versions removed. The code is unchanged.

## 0.3.2+docs.1 (2026-11-01)

- The README names the `stintmate-ipc` version this release wraps. The code is unchanged.

## 0.3.2 (2026-11-01)

- Wraps `stintmate-ipc` v0.3.2. The Python API is unchanged.

## 0.3.1+docs.3 (2026-11-01)

- The `release` workflow expects `gh` on the runner, as GitHub's Windows image has it, and no
  longer brings its own copy. The code is unchanged.

## 0.3.1+docs.2 (2026-11-01)

- The `release` workflow uploads only the wheel and `SHA256SUMS`: the `.gitignore` that uv writes
  into `dist\` is left out. The code is unchanged.

## 0.3.1+docs.1 (2026-11-01)

- The `release` workflow brings a pinned GitHub CLI when the runner has none. The code is unchanged.

## 0.3.1 (2026-11-01)

First release from this repository: the same code as the 0.3.1 binding. The version equals the
`stintmate-ipc` version whose native library the wheel bundles.
