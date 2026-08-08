from typing import TYPE_CHECKING

from Py4GWCoreLib import Agent, Map, Player, Profession, Routines
from Py4GWCoreLib import BuildMgr
from Py4GWCoreLib.BuildMgr import BuildCoroutine, BuildRegistry

if TYPE_CHECKING:
    from Py4GWCoreLib.Builds.Skills._registry import HelperRegistry
    from Py4GWCoreLib.Builds.Skills.SkillsTemplate import SkillsTemplate


class HeroAI_Build(BuildMgr):
    def __init__(self, cached_data=None, standalone_fallback: bool = False, match_only: bool = False):
        super().__init__(
            name="HeroAI",
            template_code="HEROAI",
            is_fallback_candidate=True,
            IsFixedBuild=True,
        )
        self._cached_data = cached_data
        self._standalone_fallback = standalone_fallback
        # Held under a private name on purpose: concrete builds park their
        # SkillsTemplate on ``self.skills``, which collides with the
        # ``list[int]`` that BuildMgr declares there.
        self._skill_helpers: SkillsTemplate | None = None
        self._helper_registry: HelperRegistry | None = None
        self._helper_dispatch_cache: tuple[tuple[int, ...], tuple[int, ...]] | None = None
        if match_only:
            self._build_registry = None
            self._contract_map_signature = None
            self._contract_build = None
            return
        self._build_registry = BuildRegistry(
            default_fallback_name=self.build_name,
            build_init_kwargs={"cached_data": cached_data},
        )
        self._contract_signature: tuple[int, ...] | None = None
        self._contract_build: BuildMgr | None = None

    def set_cached_data(self, cached_data):
        self._cached_data = cached_data
        if self._build_registry is not None:
            self._build_registry.build_init_kwargs["cached_data"] = cached_data

    def ApplyBlockedSkillIDs(self, blocked_skill_ids: list[int] | None = None) -> None:
        cached_data = self._get_cached_data()
        combat_handler = getattr(cached_data, "combat_handler", None)
        if combat_handler is not None and hasattr(combat_handler, "ApplyBlockedSkillIDs"):
            combat_handler.ApplyBlockedSkillIDs(blocked_skill_ids)

    def _get_cached_data(self):
        if self._cached_data is None:
            from Py4GWCoreLib.HeroAI.cache_data import CacheData

            self._cached_data = CacheData()
            if self._build_registry is not None:
                self._build_registry.build_init_kwargs["cached_data"] = self._cached_data
        return self._cached_data

    def _get_contract_signature(self) -> tuple[int, ...]:
        primary_profession, secondary_profession = Agent.GetProfessions(Player.GetAgentID())
        current_skills = tuple(int(skill_id) for skill_id in self._get_current_skills())
        return (
            int(Map.GetMapID()),
            int(Map.GetRegion()[0]),
            int(Map.GetDistrict()),
            int(Map.GetLanguage()[0]),
            int(primary_profession),
            int(secondary_profession),
            *current_skills,
        )

    def _reset_contract(self) -> None:
        self._contract_signature = None
        self._contract_build = None

    def ClearBuildContract(self) -> None:
        self._reset_contract()

    def EnsureBuildContract(self, cached_data=None):
        if cached_data is not None:
            self.set_cached_data(cached_data)
        cached_data = self._get_cached_data()

        if not Map.IsExplorable():
            self._reset_contract()
            return None

        contract_signature = self._get_contract_signature()
        if self._contract_build is not None and self._contract_signature == contract_signature:
            if self._contract_build is self:
                self.set_cached_data(cached_data)
            return self._contract_build

        if self._standalone_fallback:
            self.set_cached_data(cached_data)
            self._contract_signature = contract_signature
            self._contract_build = self
            return self

        if self._build_registry is None:
            self._reset_contract()
            return None

        current_primary_value, current_secondary_value = Agent.GetProfessions(Player.GetAgentID())
        current_primary = Profession(current_primary_value)
        current_secondary = Profession(current_secondary_value)
        current_skills = self._get_current_skills()

        resolved_build = None
        best_score = -1
        for build in self._build_registry._iter_matchable_builds():
            score = build.ScoreMatch(
                current_primary=current_primary,
                current_secondary=current_secondary,
                current_skills=current_skills,
            )
            if score > best_score:
                best_score = score
                resolved_build = build

        if resolved_build is None or best_score <= 0:
            resolved_build = self
        elif isinstance(resolved_build, HeroAI_Build):
            resolved_build = self

        if resolved_build is self:
            self.set_cached_data(cached_data)

        self._contract_signature = contract_signature
        self._contract_build = resolved_build
        return resolved_build

    def GetBuildContract(self):
        return self._contract_build

    def _prepare_combat(self):
        cached_data = self._get_cached_data()

        if not Routines.Checks.Map.MapValid():
            return None

        if not Map.IsExplorable() or Map.IsInCinematic():
            return None

        if not Agent.IsAlive(Player.GetAgentID()) or Agent.IsKnockedDown(Player.GetAgentID()):
            return None

        cached_data.Update()
        cached_data.UpdateCombat()
        return cached_data

    def _get_phase_cached_data(self):
        cached_data = self._get_cached_data()
        if cached_data is None:
            return None
        return self._prepare_combat()

    def _get_skill_helper_registry(self) -> "HelperRegistry | None":
        """Reflective ``skill_id -> helper`` map over ``Builds/Skills``.

        Built once, lazily: ``Skill.GetID`` needs loaded client skill data, so
        the first attempt can legitimately come up empty. An empty result is
        not cached, so a premature build is retried on the next tick instead of
        disabling helper dispatch for the session.
        """
        if self._helper_registry is not None:
            return self._helper_registry

        import PySystem

        from Py4GWCoreLib import ConsoleLog
        from Py4GWCoreLib.Builds.Skills._registry import build_helper_registry
        from Py4GWCoreLib.Builds.Skills.SkillsTemplate import SkillsTemplate
        from Py4GWCoreLib.Skill import Skill

        if self._skill_helpers is None:
            self._skill_helpers = SkillsTemplate(self)

        registry = build_helper_registry(self._skill_helpers, Skill.GetID)
        if not registry.entries:
            return None

        # Logged once per registry build. The count matters because the
        # registry is latched here: if client skill data was only partly loaded
        # the map is quietly smaller than it should be, and this line is the
        # only way to notice.
        ConsoleLog(
            self.build_name,
            f"skill helper registry built: {len(registry.entries)} helpers, {len(registry.shadowed)} shadowed",
            PySystem.Console.MessageType.Debug,
        )

        # A shadowed helper means two owners claim the same skill, which is a
        # defect in the skills tree rather than a runtime condition -- worth
        # seeing, not worth failing on.
        for shadowed_entry in registry.shadowed:
            winner = registry.entries[shadowed_entry.skill_id]
            ConsoleLog(
                self.build_name,
                f"skill {shadowed_entry.skill_id}: using {winner.qualified_name}, "
                f"shadowing {shadowed_entry.qualified_name}",
                PySystem.Console.MessageType.Debug,
            )

        self._helper_registry = registry
        return registry

    def _get_helper_dispatch_skill_ids(self, registry: "HelperRegistry") -> tuple[int, ...]:
        """Equipped skills this build drives through a hand-written helper.

        Skills carrying ``SkillLock`` or ``SpikeLock`` are deliberately left to
        ``CombatClass``. Those coordinate across heroes through the COOLDOWN /
        CALL_TARGET whiteboard locks that ``CombatClass`` posts; the helper
        path posts a different lock kind, or none, so taking them over would
        make peers see no claim and fire simultaneously.
        """
        current_skills = tuple(int(skill_id) for skill_id in self._get_current_skills())
        if self._helper_dispatch_cache is not None and self._helper_dispatch_cache[0] == current_skills:
            return self._helper_dispatch_cache[1]

        dispatch_skill_ids: list[int] = []
        for skill_id in current_skills:
            if skill_id not in registry.entries:
                continue
            custom_skill = self.GetCustomSkill(skill_id)
            if getattr(custom_skill, "SkillLock", False) or getattr(custom_skill, "SpikeLock", False):
                continue
            dispatch_skill_ids.append(skill_id)

        resolved = tuple(dispatch_skill_ids)
        self._helper_dispatch_cache = (current_skills, resolved)
        return resolved

    def _try_skill_helpers(
        self,
        cached_data,
        registry: "HelperRegistry",
        dispatch_skill_ids: tuple[int, ...],
        is_in_combat: bool,
    ) -> BuildCoroutine:
        """Drive the first helper that fires, in HeroAI's own priority order.

        Ordering, per-slot enable flags, recharge, the blocked-skill mask and
        the out-of-combat gate are all read from ``CombatClass`` rather than
        recomputed, so helper dispatch and the declarative engine cannot
        disagree about what is castable.
        """
        combat_handler = cached_data.combat_handler
        allowed_skill_ids = set(dispatch_skill_ids)

        for slot in range(len(combat_handler.skills)):
            if not combat_handler.IsSkillReady(slot):
                continue

            skill_id = int(combat_handler.skills[slot].skill_id)
            if skill_id not in allowed_skill_ids:
                continue

            if not is_in_combat and not combat_handler.IsOOCSkill(slot):
                continue

            entry = registry.entries.get(skill_id)
            if entry is None:
                continue

            if (yield from entry.call()):
                return True

        return False

    def _run_generic_fallback(self, cached_data, is_in_combat: bool) -> BuildCoroutine:
        """Generic path: hand-written helpers first, declarative engine after.

        The auction found no matching build, so ``Builds/Skills`` would
        otherwise be unreachable. Offer each equipped skill to its helper
        first, and let ``CombatClass`` handle everything the helpers decline.

        The blocked-skill mask is deliberately left alone. Masking every
        helper-covered skill would strand any skill whose helper is narrower
        than the declarative engine -- ``PvE.Reversal_of_Death`` declines
        outside the Dhuum encounter, and a masked copy would then be cast by
        nobody. It would also overwrite the claim a concrete build applies
        when it uses this build as its fallback handler.
        """
        registry = self._get_skill_helper_registry()
        dispatch_skill_ids: tuple[int, ...] = ()
        if registry is not None:
            dispatch_skill_ids = self._get_helper_dispatch_skill_ids(registry)

        if registry is not None and dispatch_skill_ids:
            if (yield from self._try_skill_helpers(cached_data, registry, dispatch_skill_ids, is_in_combat)):
                # The helper cast outside HandleCombat, so the aftercast window
                # the drivers gate on has to be armed explicitly.
                cached_data.combat_handler.MarkExternalCast()
                self.SetTickSuccess()
                return

        if cached_data.combat_handler.HandleCombat(cached_data, ooc=not is_in_combat):
            self.SetTickSuccess()
        else:
            self.SetTickFailure()
        yield

    def _run_contract(self, cached_data, is_in_combat: bool):
        contract_build = self.EnsureBuildContract(cached_data)
        if contract_build is None:
            self.SetTickFailure()
            yield from Routines.Yield.wait(250)
            return

        if contract_build is self:
            yield from self._run_generic_fallback(cached_data, is_in_combat)
            return

        contract_build.ResetTickState()
        if is_in_combat:
            yield from contract_build.ProcessCombat()
        else:
            yield from contract_build.ProcessOOC()

        self.tick_state = contract_build.tick_state
        if self.tick_state is None:
            self.SetTickFailure()

    def ProcessOOC(self):
        self.ResetTickState()
        cached_data = self._get_phase_cached_data()
        if cached_data is None:
            self.SetTickFailure()
            yield from Routines.Yield.wait(250)
            return

        if cached_data.data.in_aggro:
            self.SetTickFailure()
            yield
            return

        yield from self._run_contract(cached_data, is_in_combat=False)

    def ProcessCombat(self):
        self.ResetTickState()
        cached_data = self._get_phase_cached_data()
        if cached_data is None:
            self.SetTickFailure()
            yield from Routines.Yield.wait(250)
            return

        if not cached_data.data.in_aggro:
            self.SetTickFailure()
            yield
            return

        yield from self._run_contract(cached_data, is_in_combat=True)

    def ProcessSkillCasting(self):
        self.ResetTickState()
        cached_data = self._get_phase_cached_data()
        if cached_data is None:
            self.SetTickFailure()
            yield from Routines.Yield.wait(250)
            return

        if cached_data.data.in_aggro:
            yield from self.ProcessCombat()
        else:
            yield from self.ProcessOOC()


HeroAI = HeroAI_Build
