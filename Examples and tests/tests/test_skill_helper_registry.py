"""Offline test harness for the reflective skill-helper dispatch registry.

This file is intentionally runnable outside the injected client:

    python tests/test_skill_helper_registry.py

``Py4GWCoreLib`` cannot be imported outside Guild Wars (it needs the injected
native modules), so the module under test is loaded directly from its file
path. That only works because ``_registry.py`` keeps every ``Py4GWCoreLib``
import behind ``TYPE_CHECKING`` -- if someone adds a runtime import there,
this harness fails loudly, which is the intended alarm.

The registry is the one part of the auto-dispatch feature with no runtime
dependency, so it is the seam worth testing. Everything downstream of it
(target resolution, casting, whiteboard locks) needs a live client.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any, Callable

MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "Py4GWCoreLib" / "Builds" / "Skills" / "_registry.py"


def _load_registry_module() -> Any:
    spec = importlib.util.spec_from_file_location("_py4gw_skill_registry_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry_module = _load_registry_module()


# region fakes
class _FakeAny:
    """Stands in for ``SkillsTemplate.Any``: one attribute-named sub-object."""

    def __init__(self) -> None:
        self.PvE = _FakePvE()


class _FakePvE:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def Good_Skill(self):
        self.calls.append("Good_Skill")
        yield
        return True

    def Defaulted_Skill(self, *, threshold: int = 3):
        self.calls.append(f"Defaulted_Skill:{threshold}")
        yield
        return True

    def Never_Yields(self) -> int:
        """Non-generator helper -- must not be dispatchable."""
        return 7

    def Requires_Argument(self, *, target_agent_id: int):
        yield
        return target_agent_id

    def Unknown_To_Client(self):
        yield
        return True

    def _private_helper(self):
        yield
        return True

    def lowercase_helper(self):
        yield
        return True


class _FakeRitualist:
    def __init__(self) -> None:
        self.Communing = _FakeCommuning()


class _FakeCommuning:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def Good_Skill(self):
        """Same method name as ``Any.PvE.Good_Skill`` -- a genuine collision."""
        self.calls.append("Good_Skill")
        yield
        return True

    def Ritual_Only(self):
        self.calls.append("Ritual_Only")
        yield
        return True


class _FakeRoot:
    def __init__(self) -> None:
        self.Any = _FakeAny()
        self.Ritualist = _FakeRitualist()


GROUP_ORDER = ("Any", "Ritualist")

SKILL_IDS = {
    "Good_Skill": 101,
    "Defaulted_Skill": 102,
    "Never_Yields": 103,
    "Requires_Argument": 104,
    "Ritual_Only": 105,
    # "Unknown_To_Client" deliberately absent -> resolves to 0.
}


def _resolve(name: str) -> int:
    return SKILL_IDS.get(name, 0)


# endregion


# region assertions
_failures: list[str] = []


def _check(label: str, expected: object, observed: object) -> None:
    if expected == observed:
        print(f"  PASS  {label}")
        return
    message = f"  FAIL  {label}\n          expected: {expected!r}\n          observed: {observed!r}"
    print(message)
    _failures.append(f"{label}: expected {expected!r}, observed {observed!r}")


def _check_true(label: str, observed: object) -> None:
    _check(label, True, bool(observed))


# endregion


def _build(root: Any = None, resolver: Callable[[str], int] = _resolve) -> Any:
    return registry_module.build_helper_registry(
        root if root is not None else _FakeRoot(),
        resolver,
        group_order=GROUP_ORDER,
    )


def test_dispatchable_helpers_are_registered() -> None:
    print("\n[test] dispatchable helpers are registered by skill id")
    result = _build()
    _check("Good_Skill registered under its resolved id", True, 101 in result.entries)
    _check("Defaulted_Skill registered (all params defaulted)", True, 102 in result.entries)
    _check("Ritual_Only registered from a non-Any group", True, 105 in result.entries)
    _check("registered skill id count", 3, len(result.entries))


def test_non_dispatchable_helpers_are_rejected() -> None:
    print("\n[test] non-dispatchable helpers are rejected, each for its own reason")
    result = _build()
    _check("non-generator helper excluded (Never_Yields)", False, 103 in result.entries)
    _check("helper with a required parameter excluded (Requires_Argument)", False, 104 in result.entries)
    _check("unresolvable skill name excluded (Unknown_To_Client)", False, 0 in result.entries)
    registered_names = {entry.method_name for entry in result.entries.values()}
    _check("private helper excluded", False, "_private_helper" in registered_names)
    _check("lowercase helper excluded", False, "lowercase_helper" in registered_names)


def test_collisions_resolve_by_group_order_and_are_reported() -> None:
    print("\n[test] duplicate helper names resolve deterministically by group order")
    result = _build()
    entry = result.entries[101]
    _check("earlier group wins the collision", "Any.PvE.Good_Skill", entry.qualified_name)
    _check("shadowed helper is reported for diagnostics", 1, len(result.shadowed))
    _check(
        "shadowed report names the losing owner",
        "Ritualist.Communing.Good_Skill",
        result.shadowed[0].qualified_name,
    )
    _check("shadowed report carries the contested skill id", 101, result.shadowed[0].skill_id)


def test_group_order_is_authoritative_for_collisions() -> None:
    print("\n[test] reversing group order reverses which helper wins")
    result = registry_module.build_helper_registry(
        _FakeRoot(),
        _resolve,
        group_order=("Ritualist", "Any"),
    )
    _check(
        "later-listed group no longer wins",
        "Ritualist.Communing.Good_Skill",
        result.entries[101].qualified_name,
    )


def test_registered_entry_is_callable_and_bound() -> None:
    print("\n[test] a registered entry calls back into the owning helper object")
    root = _FakeRoot()
    result = _build(root)
    generator = result.entries[102].call()
    # Drive the generator the way the injected runtime does: one step per frame.
    next(generator, None)
    _check_true("bound helper actually ran", root.Any.PvE.calls)
    _check("defaults were applied at call time", ["Defaulted_Skill:3"], root.Any.PvE.calls)


def test_missing_group_attribute_is_tolerated() -> None:
    print("\n[test] a group named in group_order but absent from the tree is skipped")

    class _SparseRoot:
        def __init__(self) -> None:
            self.Any = _FakeAny()

    result = registry_module.build_helper_registry(_SparseRoot(), _resolve, group_order=GROUP_ORDER)
    _check("Any group still registered", True, 101 in result.entries)
    _check("absent Ritualist group contributed nothing", False, 105 in result.entries)


def test_resolver_failure_does_not_abort_the_build() -> None:
    print("\n[test] a resolver that raises degrades to 'skill unknown' instead of exploding")

    def _angry_resolver(name: str) -> int:
        if name == "Good_Skill":
            raise RuntimeError("skill data not loaded yet")
        return _resolve(name)

    result = _build(resolver=_angry_resolver)
    _check("raising helper name skipped", False, 101 in result.entries)
    _check("remaining helpers still registered", True, 105 in result.entries)


def main() -> int:
    print(f"module under test: {MODULE_PATH}")
    tests = [
        test_dispatchable_helpers_are_registered,
        test_non_dispatchable_helpers_are_rejected,
        test_collisions_resolve_by_group_order_and_are_reported,
        test_group_order_is_authoritative_for_collisions,
        test_registered_entry_is_callable_and_bound,
        test_missing_group_attribute_is_tolerated,
        test_resolver_failure_does_not_abort_the_build,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - harness must report, not crash
            import traceback

            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            _failures.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    if _failures:
        print(f"FAILED ({len(_failures)})")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"OK - {len(tests)} test groups passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
