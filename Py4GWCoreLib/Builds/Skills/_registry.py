"""Reflective ``skill_id -> helper`` dispatch registry over the skills tree.

The hand-written helpers under ``Py4GWCoreLib/Builds/Skills/`` are normally
reachable only from a concrete build's imperative ladder. When HeroAI's build
auction finds no match it falls back to its own declarative engine
(``HeroAI/combat.py::CombatClass``), and every helper here becomes unreachable.

This module turns the tree into a ``skill_id -> bound helper`` map so the
fallback path can dispatch to a helper whenever one exists for an equipped
skill. It is deliberately free of runtime ``Py4GWCoreLib`` imports -- the skill
name lookup arrives as an injected callable -- so it stays importable, and
therefore testable, outside the injected client. See
``tests/test_skill_helper_registry.py``.

Discovery convention, matching the existing tree:

- ``SkillsTemplate`` exposes one profession group per attribute (``Any``,
  ``Warrior``, ...). Groups are visited in ``GROUP_ORDER``.
- Each group exposes attribute-named helper owners (``PvE``, ``Communing``,
  ...). Owner attributes start with an uppercase letter; the lowercase
  ``build``/``owner`` back-references are skipped, which also keeps reflection
  from wandering into ``BuildMgr`` itself.
- Each owner exposes helper methods named exactly like the skill they cast, so
  the method name is the skill-name lookup key.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, Iterator, NamedTuple

if TYPE_CHECKING:
    from Py4GWCoreLib.BuildMgr import BuildCoroutine

__all__ = [
    "GROUP_ORDER",
    "HelperEntry",
    "HelperRegistry",
    "build_helper_registry",
]


# Mirrors the attribute declaration order in ``SkillsTemplate.__init__``.
# Order is contractual: it decides which helper wins when two owners define
# the same method name. ``Any`` leads because profession-agnostic PvE helpers
# are the ones intended to apply to every bar.
GROUP_ORDER: tuple[str, ...] = (
    "Any",
    "Warrior",
    "Ranger",
    "Monk",
    "Necromancer",
    "Mesmer",
    "Elementalist",
    "Assassin",
    "Ritualist",
    "Paragon",
    "Dervish",
)


class HelperEntry(NamedTuple):
    """One dispatchable helper, already bound to its owning object."""

    skill_id: int
    method_name: str
    qualified_name: str
    call: Callable[[], BuildCoroutine]


class HelperRegistry(NamedTuple):
    """Result of a registry build.

    ``shadowed`` records helpers that lost a name collision. They are reported
    rather than silently dropped: a collision means two owners claim the same
    skill, which is usually a defect in the tree.
    """

    entries: dict[int, HelperEntry]
    shadowed: tuple[HelperEntry, ...]


def _is_dispatchable(candidate: Any) -> bool:
    """True when ``candidate`` can be driven as a zero-argument helper.

    Two independent gates, because the tree violates both assumptions in
    places: ``ScytheMastery.Count_Active_Dervish_Enchantments`` is a plain
    ``int`` query rather than a coroutine, and ``DominationMagic.Wastrels_*``
    require a caller-supplied target.
    """
    if not callable(candidate) or not inspect.isgeneratorfunction(candidate):
        return False

    try:
        signature = inspect.signature(candidate)
    except (TypeError, ValueError):
        return False

    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is parameter.empty:
            return False
    return True


def _iter_helper_owners(group: object) -> Iterator[tuple[str, Any]]:
    """Yield ``(attribute_name, owner)`` for a group's helper containers."""
    group_attributes: dict[str, Any] = vars(group)
    for attribute_name, owner in group_attributes.items():
        if not attribute_name[:1].isupper():
            continue
        yield attribute_name, owner


def _iter_helper_methods(owner: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(method_name, bound_method)`` for an owner's public helpers."""
    for method_name in dir(owner):
        if not method_name[:1].isupper():
            continue
        try:
            candidate = getattr(owner, method_name)
        except Exception:
            continue
        if _is_dispatchable(candidate):
            yield method_name, candidate


def build_helper_registry(
    skills_root: Any,
    resolve_skill_id: Callable[[str], int],
    *,
    group_order: tuple[str, ...] = GROUP_ORDER,
) -> HelperRegistry:
    """Build the ``skill_id -> helper`` map for one skills tree.

    ``skills_root`` is a ``SkillsTemplate`` (duck-typed for testing) and
    ``resolve_skill_id`` maps a helper method name to a client skill id,
    returning ``0`` when the name is not a skill. A resolver that raises is
    treated as "not a skill" so partially loaded client skill data degrades to
    a smaller registry instead of an exception on the combat path.

    The caller is expected to rebuild rather than cache an empty result: an
    empty registry usually means skill data was not loaded yet.
    """
    entries: dict[int, HelperEntry] = {}
    shadowed: list[HelperEntry] = []

    for group_name in group_order:
        group = getattr(skills_root, group_name, None)
        if group is None:
            continue

        for owner_name, owner in _iter_helper_owners(group):
            for method_name, bound_method in _iter_helper_methods(owner):
                try:
                    skill_id = int(resolve_skill_id(method_name) or 0)
                except Exception:
                    continue
                if skill_id <= 0:
                    continue

                entry = HelperEntry(
                    skill_id=skill_id,
                    method_name=method_name,
                    qualified_name=f"{group_name}.{owner_name}.{method_name}",
                    call=bound_method,
                )
                if skill_id in entries:
                    shadowed.append(entry)
                    continue
                entries[skill_id] = entry

    return HelperRegistry(entries=entries, shadowed=tuple(shadowed))
