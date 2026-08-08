from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from Py4GWCoreLib import Range, Routines
from Py4GWCoreLib.BuildMgr import BuildCoroutine
from Py4GWCoreLib.Skill import Skill
from Py4GWCoreLib.Builds.Skills import _energy_denial
from Py4GWCoreLib.Builds.Skills._whiteboard import coordinates_whiteboard_skill_target
from Py4GWCoreLib.GlobalCache.HexRemovalPriority import HexRemovalPriority, cast_hex_removal_and_track, get_hexed_ally_for_removal

if TYPE_CHECKING:
    from Py4GWCoreLib.BuildMgr import BuildMgr

__all__ = ["DominationMagic"]


def _domination_magic_rank() -> int:
    """The player's Domination Magic attribute level, 0 when absent."""
    from Py4GWCoreLib import Agent, Player

    try:
        for attribute in Agent.GetAttributes(Player.GetAgentID()):
            if attribute.GetName() == "Domination Magic":
                return int(attribute.level)
    except Exception:
        pass
    return 0


def _adjacent_enemy_ids(target_agent_id: int, radius: float) -> list[int]:
    """Living foes within ``radius`` of ``target_agent_id``, excluding it.

    Measured around the target rather than the player, because that is where
    an on-target AoE actually applies.
    """
    from Py4GWCoreLib import Agent

    try:
        target_x, target_y = Agent.GetXY(target_agent_id)
        neighbours = Routines.Agents.GetFilteredEnemyArray(target_x, target_y, radius)
    except Exception:
        return []

    return [
        int(agent_id)
        for agent_id in neighbours
        if int(agent_id) != int(target_agent_id) and Agent.IsAlive(int(agent_id))
    ]


class DominationMagic:
    def __init__(self, build: BuildMgr) -> None:
        self.build: BuildMgr = build

    # region A
    @coordinates_whiteboard_skill_target(Skill.GetID("Aneurysm"))
    def Aneurysm(
        self,
        *,
        min_estimated_deficit: float = _energy_denial.ANEURYSM_DEFAULT_MIN_DEFICIT,
    ) -> BuildCoroutine:
        """Cast Aneurysm on a foe this build recently drained.

        Aneurysm reads backwards until you look at it: "Target foe regains all
        Energy. For each point of Energy gained in this way, that foe takes
        1..3 damage and all adjacent foes lose 1 Energy." Damage scales with the
        Energy the foe is *missing*, so the skill wants an emptied target.

        Foe Energy is not readable -- the client reports a zeroed pool for
        enemies, confirmed in-client -- so the gate runs on
        ``_energy_denial.estimated_deficit``: Energy this build knows it removed,
        decayed by assumed regeneration. ``min_estimated_deficit`` is the
        threshold in Energy points, defaulting to roughly one Energy Surge at
        combat ranks.

        That the ledger counts absolute points rather than a percentage is a
        convenience, not a compromise: Aneurysm's damage scales with points
        gained, so points are the quantity the skill actually cares about.

        No evidence of a drain means no cast. Firing blind would hand a full
        Energy bar to a foe for nothing, which is a strange way to win a fight.
        """
        from Py4GWCoreLib import Agent, GLOBAL_CACHE

        aneurysm_id: int = Skill.GetID("Aneurysm")

        if not self.build.IsSkillEquipped(aneurysm_id):
            return False

        # The Energy drain lands on foes adjacent to the cast target, so the
        # cluster that matters is measured around the target, not the player.
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(aneurysm_id) or Range.Adjacent.value

        def _is_drained(agent_id: int) -> bool:
            return _energy_denial.estimated_deficit(agent_id) >= min_estimated_deficit

        # Casters first: the adjacent drain only punishes foes that spend
        # Energy, and a caster cluster is where that half of the skill pays.
        # PickClusteredTarget scores neighbour *count*, not neighbour
        # profession, so "drained target ringed by casters" is not expressible
        # here -- caster-first targeting is the closest available proxy.
        tiers: list[Callable[[int], bool]] = [
            lambda agent_id: _is_drained(agent_id) and Agent.IsCaster(agent_id),
            _is_drained,
        ]

        target_agent_id: int = 0
        for tier_condition in tiers:
            target_agent_id = int(
                Routines.Targeting.PickClusteredTarget(
                    cluster_radius=aoe_range,
                    preferred_condition=tier_condition,
                    filter_radius=Range.Spellcast.value,
                )
                or 0
            )
            if target_agent_id:
                break

        if not target_agent_id:
            return False

        # Sample both of these *before* casting. The target's deficit is wiped
        # by the cast, and the neighbour set is only meaningful at the moment
        # the spell lands -- foes move, and dead ones leave the array.
        target_deficit = _energy_denial.estimated_deficit(target_agent_id)
        adjacent_agent_ids = _adjacent_enemy_ids(target_agent_id, aoe_range)

        cast_result = bool(
            (
                yield from self.build.CastSkillIDAndRestoreTarget(
                    skill_id=aneurysm_id,
                    target_agent_id=target_agent_id,
                    log=False,
                    aftercast_delay=250,
                )
            )
        )

        if cast_result:
            # The cast refills the target outright, so every drain we had
            # banked against it is spent. Clearing the ledger both models
            # reality and stops an immediate, worthless re-cast.
            _energy_denial.clear_agent(target_agent_id)

            # Each adjacent foe loses one Energy per point the target regained,
            # capped by rank. That Energy left the fight, so bank it: it is
            # what makes a second Aneurysm on a neighbour worth casting.
            #
            # Caveat worth being loud about -- this is an estimate derived from
            # an estimate. If the target's deficit was overstated, that error
            # propagates to every neighbour at once. The rank cap below and the
            # pool clamp in the ledger bound it; the live panel is how you spot
            # it running away.
            adjacent_drain = min(
                target_deficit,
                _energy_denial.aneurysm_adjacent_drain_cap(_domination_magic_rank()),
            )
            for neighbour_agent_id in adjacent_agent_ids:
                _energy_denial.record_drain(neighbour_agent_id, adjacent_drain)

        return cast_result

    # endregion

    #region E
    def Energy_Surge(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, Player, Range, GLOBAL_CACHE

        energy_surge_id: int = Skill.GetID("Energy_Surge")
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(energy_surge_id) or Range.Nearby.value

        if not self.build.IsSkillEquipped(energy_surge_id):
            return False

        def _is_enemy_casting_spell(agent_id: int) -> bool:
            if not Agent.IsCaster(agent_id):
                return False
            if not Agent.IsCasting(agent_id):
                return False
            casting_skill_id = Agent.GetCastingSkillID(agent_id)
            return bool(casting_skill_id and GLOBAL_CACHE.Skill.Flags.IsSpell(casting_skill_id))

        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=_is_enemy_casting_spell,
            filter_radius=Range.Spellcast.value,
        )

        if not target_agent_id:
            best_enemy_target_id = Routines.Targeting.PickClusteredTarget(
                cluster_radius=aoe_range,
                filter_radius=Range.Spellcast.value,
            )
            current_target_id = Player.GetTargetID()
            if Agent.IsValid(current_target_id) and not Agent.IsDead(current_target_id):
                current_target_score = Routines.Targeting.CountNearbyEnemies(current_target_id, aoe_range)
                best_enemy_score = Routines.Targeting.CountNearbyEnemies(best_enemy_target_id, aoe_range)
                if current_target_score >= best_enemy_score:
                    target_agent_id = current_target_id

            if not target_agent_id:
                target_agent_id = best_enemy_target_id

        if not target_agent_id:
            return False

        cast_result = bool(
            (
                yield from self.build.CastSkillIDAndRestoreTarget(
                    skill_id=energy_surge_id,
                    target_agent_id=target_agent_id,
                    log=False,
                    aftercast_delay=250,
                )
            )
        )

        if cast_result:
            # Energy Surge is the build's main source of a known-size drain, and
            # foe Energy cannot be read back, so this record is the only thing
            # that lets Aneurysm know the target is worth hitting.
            _energy_denial.record_drain(
                target_agent_id,
                _energy_denial.linear_drain(_domination_magic_rank()),
            )

        return cast_result

    #region C
    @coordinates_whiteboard_skill_target(Skill.GetID("Cry_of_Frustration"))
    def Cry_of_Frustration(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, Range, GLOBAL_CACHE

        cry_of_frustration_id: int = Skill.GetID("Cry_of_Frustration")
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(cry_of_frustration_id) or Range.Nearby.value

        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=lambda agent_id: Agent.IsCasting(agent_id),
            filter_radius=Range.Spellcast.value,
        )

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=cry_of_frustration_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))
    #endregion

    #region M
    @coordinates_whiteboard_skill_target(Skill.GetID("Mistrust"))
    def Mistrust(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, Player, Range, GLOBAL_CACHE

        mistrust_id: int = Skill.GetID("Mistrust")

        if not self.build.IsSkillEquipped(mistrust_id):
            return False

        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(mistrust_id) or Range.Nearby.value

        def _is_enemy_casting_spell(agent_id: int) -> bool:
            if not Agent.IsCaster(agent_id):
                return False
            if not Agent.IsCasting(agent_id):
                return False
            casting_skill_id = Agent.GetCastingSkillID(agent_id)
            return bool(casting_skill_id and GLOBAL_CACHE.Skill.Flags.IsSpell(casting_skill_id))

        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=_is_enemy_casting_spell,
            filter_radius=Range.Spellcast.value,
        )

        if not target_agent_id:
            best_enemy_target_id = Routines.Targeting.PickClusteredTarget(
                cluster_radius=aoe_range,
                filter_radius=Range.Spellcast.value,
            )
            current_target_id = Player.GetTargetID()
            if Agent.IsValid(current_target_id) and not Agent.IsDead(current_target_id):
                current_target_score = Routines.Targeting.CountNearbyEnemies(current_target_id, aoe_range)
                best_enemy_score = Routines.Targeting.CountNearbyEnemies(best_enemy_target_id, aoe_range)
                if current_target_score >= best_enemy_score:
                    target_agent_id = current_target_id

            if not target_agent_id:
                target_agent_id = best_enemy_target_id

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=mistrust_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))
    #endregion

    #region O
    @coordinates_whiteboard_skill_target(Skill.GetID("Overload"))
    def Overload(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, Range, GLOBAL_CACHE

        overload_id: int = Skill.GetID("Overload")
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(overload_id) or Range.Adjacent.value

        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=lambda agent_id: Agent.IsCasting(agent_id),
            filter_radius=Range.Spellcast.value,
        )

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=overload_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))
    #endregion

    #region P
    @coordinates_whiteboard_skill_target(Skill.GetID("Psychic_Instability"))
    def Psychic_Instability(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, GLOBAL_CACHE

        psychic_instability_id: int = Skill.GetID("Psychic_Instability")
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(psychic_instability_id) or Range.Adjacent.value

        if not self.build.IsSkillEquipped(psychic_instability_id):
            return False

        # PI interrupts any skill or spell being cast, not only spells.
        # The interrupt fires and knocks down the target plus all adjacent foes.
        # Cast condition is hard – no fallback to non-casting targets.
        # Among all casting enemies in spellcast range, prefer the one with the
        # most adjacent enemies to maximise the knockdown area.
        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=lambda agent_id: Agent.IsCasting(agent_id),
            filter_radius=Range.Spellcast.value,
        )

        # Require at least one casting enemy – do not cast into the void.
        if not target_agent_id or not Agent.IsCasting(target_agent_id):
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=psychic_instability_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))

    @coordinates_whiteboard_skill_target(Skill.GetID("Panic"))
    def Panic(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, Range, GLOBAL_CACHE

        panic_id: int = Skill.GetID("Panic")
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(panic_id) or Range.Nearby.value

        if not self.build.IsSkillEquipped(panic_id):
            return False

        # Tier 1: caster clusters. Panic's cascade only fires on activated
        # skills and spells (stances and shouts are instant and do NOT
        # trigger). Dense caster mobs cast spells constantly, maximising
        # the cascade.
        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=lambda agent_id: Agent.IsCaster(agent_id),
            filter_radius=Range.Spellcast.value,
        )

        # Tier 2: martial / melee clusters. Attack skills and signets have
        # activation times and trigger the cascade; stances and shouts do
        # not, so the trigger rate is lower than casters but still useful.
        if not target_agent_id:
            target_agent_id = Routines.Targeting.PickClusteredTarget(
                cluster_radius=aoe_range,
                preferred_condition=lambda agent_id: Agent.IsMartial(agent_id) or Agent.IsMelee(agent_id),
                filter_radius=Range.Spellcast.value,
            )

        # Tier 3: densest cluster of any foe. The hex still spreads on cast,
        # and any activated skill or spell from a hexed foe triggers the
        # cascade. Auto-attacks, stances, and shouts do not.
        if not target_agent_id:
            target_agent_id = Routines.Targeting.PickClusteredTarget(
                cluster_radius=aoe_range,
                filter_radius=Range.Spellcast.value,
            )

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=panic_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))
    #endregion

    #region S
    def Shatter_Hex(self, min_priority: int = HexRemovalPriority.LOW) -> BuildCoroutine:
        shatter_hex_id: int = Skill.GetID("Shatter_Hex")

        if not self.build.IsSkillEquipped(shatter_hex_id):
            return False

        target_agent_id = get_hexed_ally_for_removal(
            Range.Spellcast.value,
            reserve=True,
            skill_id=shatter_hex_id,
            min_priority=min_priority,
        )
        if not target_agent_id:
            return False

        return (yield from cast_hex_removal_and_track(
            self.build,
            skill_id=shatter_hex_id,
            target_agent_id=target_agent_id,
            aftercast_delay=250,
        ))
    #endregion

    #region W
    @coordinates_whiteboard_skill_target(Skill.GetID("Wastrels_Demise"))
    def Wastrels_Demise(self, *, target_agent_id: int) -> BuildCoroutine:
        wastrels_demise_id: int = Skill.GetID("Wastrels_Demise")

        if not self.build.IsSkillEquipped(wastrels_demise_id):
            return False

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=wastrels_demise_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))

    @coordinates_whiteboard_skill_target(Skill.GetID("Wastrels_Worry"))
    def Wastrels_Worry(self, *, target_agent_id: int) -> BuildCoroutine:
        wastrels_worry_id: int = Skill.GetID("Wastrels_Worry")

        if not self.build.IsSkillEquipped(wastrels_worry_id):
            return False

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=wastrels_worry_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))
    #endregion

    #region U
    def Unnatural_Signet(self) -> BuildCoroutine:
        from Py4GWCoreLib import Agent, Range, GLOBAL_CACHE

        unnatural_signet_id: int = Skill.GetID("Unnatural_Signet")
        aoe_range = GLOBAL_CACHE.Skill.Data.GetAoERange(unnatural_signet_id) or Range.Adjacent.value

        if not self.build.IsSkillEquipped(unnatural_signet_id):
            return False

        target_agent_id = Routines.Targeting.PickClusteredTarget(
            cluster_radius=aoe_range,
            preferred_condition=lambda agent_id: Agent.IsHexed(agent_id) or Agent.IsEnchanted(agent_id),
            filter_radius=Range.Spellcast.value,
        )
        if not target_agent_id:
            target_agent_id = Routines.Targeting.PickClusteredTarget(
                cluster_radius=aoe_range,
                filter_radius=Range.Spellcast.value,
            )

        if not target_agent_id:
            return False

        return (yield from self.build.CastSkillIDAndRestoreTarget(
            skill_id=unnatural_signet_id,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=250,
        ))
    #endregion
