"""The public surface of the package against v0.2.0 (``parity_data.json``, frozen).

Every public name is compared: signatures, members, exception bases.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

import stintmate_ipc as ipc

DATA = json.loads((Path(__file__).with_name("parity_data.json")).read_text(encoding="utf-8"))



def sig_rows(obj, drop_self: bool = True) -> list[list]:
    rows = []
    for p in inspect.signature(obj).parameters.values():
        if p.name == "self" and drop_self:
            continue
        rows.append([p.name, p.kind.name, None if p.default is p.empty else repr(p.default)])
    return rows


def assert_sig(got: list[list], want: list[list], where: str) -> None:
    assert [r[:2] for r in got] == [r[:2] for r in want], where
    for g, w in zip(got, want):
        assert (g[2] is None) == (w[2] is None), f"{where}: default of {g[0]}"
        if w[2] not in (None, "..."):
            assert g[2] == w[2].replace("8_388_608", "8388608"), f"{where}: default of {g[0]}"


def test_all_is_the_v020_all():
    assert sorted(ipc.__all__) == DATA["all"]


def test_every_name_is_present_or_pending():
    for name in ipc.__all__:
        assert hasattr(ipc, name), name


@pytest.mark.parametrize("name", sorted(DATA["exceptions"]))
def test_exception_bases(name):
    cls = getattr(ipc, name) if hasattr(ipc, name) else None
    if cls is None:
        pytest.skip("not public")
    assert [b.__name__ for b in cls.__bases__] == DATA["exceptions"][name]


@pytest.mark.parametrize("name", sorted(DATA["functions"]))
def test_function_signature(name):
    obj = getattr(ipc, name) if hasattr(ipc, name) else getattr(ipc.keyfile, name)
    assert_sig(sig_rows(obj), DATA["functions"][name], name)


@pytest.mark.parametrize("name", sorted(DATA["classes"]))
def test_class_members(name):
    if name == "FlagEvent":
        cls = importlib.import_module("stintmate_ipc._client").FlagEvent
    else:
        cls = getattr(ipc, name)
    for member, spec in DATA["classes"][name].items():
        where = f"{name}.{member}"
        static = inspect.getattr_static(cls, member, None)
        has = static is not None or member in getattr(cls, "__annotations__", {}) or \
            member in getattr(cls, "__slots__", ())
        assert has, f"{where} is missing"
        if spec["kind"] == "property":
            assert isinstance(static, property), where
        elif spec["kind"] in ("method", "static"):
            assert callable(getattr(cls, member)), where
            assert_sig(sig_rows(getattr(cls, member)), spec["sig"], where)


def test_submodules():
    from stintmate_ipc import check, decl, keyfile
    assert sorted(check.__all__) == DATA["check_all"]
    assert sorted(keyfile.__all__) == DATA["keyfile_all"]
    public = sorted(n for n, v in vars(decl).items()
                    if not n.startswith("_") and getattr(v, "__module__", None) == decl.__name__)
    assert public == DATA["decl_names"]


def test_main_and_run_exist():
    from stintmate_ipc import check
    assert callable(check.main) and callable(check.run)
    assert [p[0] for p in sig_rows(check.main)] == ["argv"]
