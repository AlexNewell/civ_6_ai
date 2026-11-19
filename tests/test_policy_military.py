import asyncio
import dataclasses
import json
import logging
import math
import types

import pytest
from civ_mcp import lua as lq

from civ6_ai.blockers import Blocker, BlockerResolver, BlockerType
from civ6_ai.policies import military as military_module
from civ6_ai.policies.military import (
    BASE_DAMAGE,
    MilitaryPolicy,
    MilitarySettings,
    MilitaryWeights,
    assess_attack,
    attack_failed,
    attack_landed,
    camp_attacks_needed,
    camp_force_ok,
    camp_matchup,
    choose_promotion,
    expected_damage,
    garrison_required,
    remembered_defenders,
)
from civ6_ai.policies.settling import units_reserved_for_settling
from civ6_ai.state import read_cities, read_threats, read_units
from civ6_ai.state.models import Target, Threat
from civ6_ai.state.reader import build_tile, build_unit, hex_distance
from fixtures.civ_state import (
    BARBARIANS,
    ROME,
    FakeGame,
    city_info,
    civ_info,
    overview,
    threat_info,
    tile,
    unit_info,
)

W = MilitaryWeights()

# Real shapes, from civ6-mcp's attack_unit, move_unit, heal_unit and fortify_unit.
ESTIMATE = (
    "Combat Estimate (Melee):\n"
    "  UNIT_WARRIOR (CS:8, HP:100) vs UNIT_WARRIOR (CS:8, HP:100)\n"
    "  Modifiers: none\n"
    "  Est damage to defender: ~24\n"
    "  Est damage to attacker: ~24\n"
)
RANGED_HIT = (
    "RANGE_ATTACK|target:UNIT_WARRIOR at (11,10)|pre_hp:100/100|your HP:100|range:2 dist:1"
    "|est damage dealt:~37\n  Post-combat: ~63/100 (estimate — verify with get_units)"
)
RANGED_KILL = (
    "RANGE_ATTACK|target:UNIT_WARRIOR at (11,10)|pre_hp:30/100|your HP:100|range:2 dist:1"
    "|damage dealt:30 (killed)\n  Post-combat: Target eliminated"
)
MELEE_HIT = (
    ESTIMATE + "MELEE_ATTACK|target:UNIT_WARRIOR at (11,10)|enemy HP:100 -> 76/100"
    "|your HP:100 -> 76 CS:8"
)
STACKING = "Error: STACKING_CONFLICT|Friendly UNIT_WARRIOR already on (10,10)."
CITY_HIT = (
    "CITY_RANGE_ATTACK|Alpha -> UNIT_WARRIOR@11,10|pre_hp:100/100|damage dealt:30\n"
    "  Post-combat: ~70/100"
)
ZOC_REFUSAL = (
    ESTIMATE + "Error: ZOC|Unit entered Zone of Control this turn — cannot attack until next turn."
)


def target(x=11, y=10, hp=100, kind="UNIT_WARRIOR"):
    return f"{kind}@{x},{y}({hp}hp)"


def warrior(uid=1, x=10, y=10, **changes):
    return unit_info(unit_id=uid, unit_index=uid, x=x, y=y, **changes)


def archer(uid=2, x=10, y=10, **changes):
    base = dict(
        name="Archer", unit_type="UNIT_ARCHER", combat_strength=15, ranged_strength=25
    )  # fmt: skip
    return unit_info(unit_id=uid, unit_index=uid, x=x, y=y, **{**base, **changes})


def scout(uid=9, x=10, y=10, **changes):
    base = dict(name="Scout", unit_type="UNIT_SCOUT", combat_strength=10)
    return unit_info(unit_id=uid, unit_index=uid, x=x, y=y, **{**base, **changes})


def settler(uid=8, x=10, y=10, **changes):
    base = dict(name="Settler", unit_type="UNIT_SETTLER", combat_strength=0)
    return unit_info(unit_id=uid, unit_index=uid, x=x, y=y, **{**base, **changes})


def barbarian(kind="UNIT_WARRIOR", x=11, y=10, cs=8, rs=0, hp=100, **changes):
    return threat_info(
        unit_type=kind, x=x, y=y, combat_strength=cs, ranged_strength=rs, hp=hp, **changes
    )


class MilitaryGame(FakeGame):
    """Records orders. `replies` overrides what an action answers, by action name."""

    def __init__(self, units=(), threats=(), cities=None, turn=20, **overrides):
        cities = cities if cities is not None else [city_info()]
        super().__init__(
            units=list(units),
            threats=list(threats),
            cities=cities,
            overview=overview(turn=turn, num_cities=len(cities)),
            civs=[],
            **overrides,
        )
        self.actions: list[tuple] = []
        self.replies: dict[str, str] = {}
        self.notifications: list = []
        self.promotions: dict[int, list[str]] = {}
        self.raises: dict[str, Exception] = {}
        self.refuse_moves_to: set[tuple[int, int]] = set()

    def did(self, name, *args, default=""):
        self.actions.append((name, *args))
        if name in self.raises:
            raise self.raises[name]
        return self.replies.get(name, default)

    async def move_unit(self, unit_index, target_x, target_y):
        if (target_x, target_y) in self.refuse_moves_to:
            return self.did("move", unit_index, target_x, target_y, default=STACKING)
        return self.did(
            "move", unit_index, target_x, target_y,
            default=f"MOVING_TO|{target_x},{target_y}|from:0,0|now_at:{target_x},{target_y}",
        )  # fmt: skip

    async def attack_unit(self, unit_index, target_x, target_y):
        return self.did("attack", unit_index, target_x, target_y, default=RANGED_HIT)

    async def fortify_unit(self, unit_index):
        return self.did("fortify", unit_index, default="FORTIFIED")

    async def city_attack(self, city_id, target_x, target_y):
        return self.did("city_attack", city_id, target_x, target_y, default=CITY_HIT)

    async def automate_explore(self, unit_index):
        return self.did("explore", unit_index, default="EXPLORING")

    async def heal_unit(self, unit_index):
        return self.did("heal", unit_index, default="HEALING|HP:40/100")

    async def get_notifications(self):
        return self.notifications

    async def get_unit_promotions(self, unit_id):
        names = self.promotions.get(unit_id, [])
        options = [lq.PromotionOption(n, n, "") for n in names]
        return lq.UnitPromotionStatus(unit_id, unit_id, "UNIT_WARRIOR", options)

    async def promote_unit(self, unit_id, promotion_type):
        return self.did("promote", unit_id, promotion_type, default="PROMOTED")

    def orders(self, name=None):
        return [a for a in self.actions if name is None or a[0] == name]


def run(policy, game):
    asyncio.run(policy.run(game))


def promote_notice():
    return lq.GameNotification(military_module.PROMOTION_AVAILABLE, "Promotion", 20, 10, 10)


# ----- the odds -----


def test_equal_strength_costs_the_base_damage_and_each_point_scales_it():
    assert expected_damage(10, 10) == BASE_DAMAGE
    assert expected_damage(20, 10) == pytest.approx(BASE_DAMAGE * math.exp(0.4))
    assert expected_damage(10, 20) < BASE_DAMAGE < expected_damage(20, 10)


def assess(unit, hp=100, cs=8, rs=0, weights=W, floor=None):
    unit = build_unit(unit)
    defender = Threat(BARBARIANS, "Barbarians", "UNIT_WARRIOR", 11, 10, hp, 100, cs, rs, 1, True)
    tgt = Target("UNIT_WARRIOR", 11, 10, hp)
    return assess_attack(unit, tgt, defender, weights, floor)


def test_a_ranged_unit_takes_nothing_back_from_a_melee_defender():
    odds = assess(archer())
    assert odds.taken == 0
    assert odds.favourable


def test_a_ranged_unit_is_hit_back_by_a_ranged_defender():
    odds = assess(archer(), rs=15)
    assert odds.taken > 0


def test_an_even_melee_fight_is_refused():
    # 8 against 8+6 (the assumed fortification and terrain) costs more than it gives.
    assert not assess(warrior()).favourable


def test_a_strong_melee_unit_takes_a_fight_it_wins():
    odds = assess(warrior(combat_strength=40), hp=30)
    assert odds.sure_kill
    assert odds.favourable


def test_a_fight_that_leaves_the_unit_too_weak_is_refused_even_if_it_kills():
    odds = assess(warrior(combat_strength=40, health=45), hp=20)
    assert odds.sure_kill
    assert odds.hp_after < W.min_hp_after_attack * 100
    assert not odds.favourable


def test_a_lone_garrison_uses_the_stricter_floor():
    unit = warrior(combat_strength=30, health=70)
    assert assess(unit, hp=60).favourable
    assert not assess(unit, hp=60, floor=W.garrison_min_hp_after).favourable


def test_a_civilian_is_not_judged():
    assert assess(archer(), cs=0, rs=0) is None


def test_weights_are_plain_inputs():
    cautious = MilitaryWeights(min_damage_ratio=100.0, min_hp_after_attack=0.99)
    assert not assess(warrior(combat_strength=14), weights=cautious).favourable


def test_a_kill_ranks_above_more_damage():
    kill = assess(archer(), hp=20)
    hit = assess(archer(), hp=100)
    assert kill.rank > hit.rank


def test_attack_landed_reads_the_result_not_its_first_word():
    assert attack_landed(MELEE_HIT)
    assert attack_landed(RANGED_KILL)
    # The estimate comes first, so this does not start with "Error".
    assert not ZOC_REFUSAL.startswith("Error")
    assert not attack_landed(ZOC_REFUSAL)
    assert not attack_landed("Error: NO_ENEMY|No hostile unit at (1,1)")


def test_garrison_required_rises_with_the_crowd():
    cities = asyncio.run(read_cities(MilitaryGame(threats=[barbarian(x=11 + i) for i in range(3)])))
    assert garrison_required(cities[0], MilitarySettings()) == 2
    quiet = asyncio.run(read_cities(MilitaryGame()))
    assert garrison_required(quiet[0], MilitarySettings()) == 1


def test_camp_force_needs_a_clear_margin_and_healthy_units():
    ours = [build_unit(warrior(uid=i, combat_strength=20)) for i in (1, 2)]
    theirs = [Threat(BARBARIANS, "B", "UNIT_WARRIOR", 5, 5, 100, 100, 8, 0, 3, True)]
    assert camp_force_ok(ours, theirs, W)
    assert not camp_force_ok(ours[:1], theirs, W)  # One unit is not a force.
    assert not camp_force_ok(ours, theirs * 3, W)
    hurt = [build_unit(warrior(uid=i, combat_strength=20, health=50)) for i in (1, 2)]
    assert not camp_force_ok(hurt, theirs, W)


def test_promotion_preference_then_first_offered():
    options = [lq.PromotionOption(n, n, "") for n in ("PROMOTION_X", "PROMOTION_BATTLECRY")]
    assert (
        choose_promotion(options, ("PROMOTION_BATTLECRY",)).promotion_type == "PROMOTION_BATTLECRY"
    )
    assert choose_promotion(options, ()).promotion_type == "PROMOTION_X"
    assert choose_promotion([], ("PROMOTION_BATTLECRY",)) is None


# ----- attacking -----


def test_a_ranged_unit_shoots_a_barbarian_at_the_gate():
    game = MilitaryGame(
        [archer(targets=[target()])], [barbarian()], cities=[city_info(garrison_unit="UNIT_ARCHER")]
    )
    policy = MilitaryPolicy()
    run(policy, game)
    assert game.orders("attack") == [("attack", 2, 11, 10)]
    assert not game.orders("move")
    assert policy.decisions[-1].action == "attack"
    assert policy.decisions[-1].odds["favourable"]


def test_a_hurt_ranged_unit_still_shoots_a_melee_unit_that_cannot_hit_back():
    game = MilitaryGame([archer(health=45, targets=[target()]), warrior(uid=5)], [barbarian()])
    run(MilitaryPolicy(), game)
    assert game.orders("attack") == [("attack", 2, 11, 10)]  # Free: nothing comes back.
    hurt_melee = MilitaryGame([warrior(health=45, targets=[target()])], [barbarian(hp=60, cs=8)])
    run(MilitaryPolicy(), hurt_melee)
    assert not hurt_melee.orders("attack")  # A hurt melee unit heals instead.


def test_a_target_on_the_doorstep_is_attacked_if_the_unit_would_survive_even_at_a_poor_ratio():
    def tried(weights, x=11):
        game = MilitaryGame(
            [warrior(targets=[target(x=x)]), warrior(uid=5)], [barbarian(x=x, cs=14)]
        )
        run(MilitaryPolicy(MilitarySettings(weights=weights)), game)
        return bool(game.orders("attack"))

    assert tried(MilitaryWeights())  # Beside the city: survival is the only test.
    assert not tried(MilitaryWeights(defend_damage_ratio=1.0))  # The old duel rule, one setting.
    assert not tried(MilitaryWeights(), x=16)  # Far from any city the duel rule still holds.
    assert not tried(MilitaryWeights(min_hp_after_attack=0.99, garrison_min_hp_after=0.99))


def test_a_losing_melee_fight_is_not_started():
    game = MilitaryGame([warrior(targets=[target()])], [barbarian(cs=20)])
    run(MilitaryPolicy(), game)
    assert not game.orders("attack")


def test_a_target_the_scan_does_not_show_is_not_attacked():
    game = MilitaryGame([archer(targets=[target(x=15)])], [barbarian(x=11)])
    run(MilitaryPolicy(), game)
    assert not game.orders("attack")


def test_a_unit_attacks_once_a_turn_and_picks_the_better_target():
    game = MilitaryGame(
        [archer(targets=[target(11, 10, 100), target(10, 11, 20)])],
        [barbarian(x=11, y=10), barbarian(x=10, y=11, hp=20)],
    )
    run(MilitaryPolicy(), game)
    assert game.orders("attack") == [("attack", 2, 10, 11)]  # The one it can kill.


def test_a_killed_target_is_not_shot_again():
    game = MilitaryGame(
        [archer(uid=2, targets=[target()]), archer(uid=3, targets=[target()])],
        [barbarian(hp=30)],
    )
    game.replies["attack"] = RANGED_KILL
    run(MilitaryPolicy(), game)
    assert len(game.orders("attack")) == 1


def test_a_target_that_survives_is_shot_by_the_next_unit():
    game = MilitaryGame(
        [archer(uid=2, targets=[target()]), archer(uid=3, targets=[target()])], [barbarian()]
    )
    run(MilitaryPolicy(), game)
    assert [a[1] for a in game.orders("attack")] == [2, 3]


def test_a_refused_attack_is_not_taken_for_a_success_and_not_repeated_next_turn():
    game = MilitaryGame([warrior(combat_strength=40, targets=[target()])], [barbarian(hp=30)])
    game.replies["attack"] = ZOC_REFUSAL
    policy = MilitaryPolicy()
    run(policy, game)
    run(policy, game)
    assert len(game.orders("attack")) == 1  # Set aside for `retry_turns`, not retried at once.
    assert "refused" in policy.decisions[0].reason
    game.data["overview"] = overview(turn=20 + policy.settings.retry_turns + 1, num_cities=1)
    run(policy, game)
    assert len(game.orders("attack")) == 2


def test_the_lone_defender_of_a_city_needs_a_safer_fight_than_a_spare_one():
    wounded = dict(combat_strength=30, health=70)
    alone = MilitaryGame([warrior(targets=[target()], **wounded)], [barbarian(hp=60)])
    run(MilitaryPolicy(), alone)
    assert not alone.orders("attack")
    spare = MilitaryGame(
        [warrior(uid=1, targets=[target()], **wounded), warrior(uid=5)], [barbarian(hp=60)]
    )
    run(MilitaryPolicy(), spare)
    assert spare.orders("attack")


def test_a_hurt_unit_goes_to_heal_instead_of_a_poor_attack():
    # A melee unit would take damage back, so it heals; a ranged unit's free shot is the test above.
    game = MilitaryGame(
        [warrior(uid=2, health=40, targets=[target()]), warrior(uid=5)], [barbarian()]
    )
    run(MilitaryPolicy(), game)
    assert not game.orders("attack")
    assert game.orders("heal") == [("heal", 2)]


# ----- healing -----


def test_a_hurt_unit_in_a_city_heals_once_and_is_left_to_mend():
    game = MilitaryGame([warrior(health=40)])
    policy = MilitaryPolicy()
    run(policy, game)
    run(policy, game)
    assert game.orders("heal") == [("heal", 1)]  # Fortified until healed, so not asked again.


def test_a_unit_that_healed_is_forgotten_and_heals_again_when_hurt_again():
    game = MilitaryGame([warrior(health=40)])
    policy = MilitaryPolicy()
    run(policy, game)
    game.data["units"] = [warrior(health=100)]
    run(policy, game)
    assert policy._resting == {}
    game.data["units"] = [warrior(health=40)]
    run(policy, game)
    assert len(game.orders("heal")) == 2


def test_a_hurt_unit_outside_walks_to_the_nearest_city_and_is_not_left_alone():
    game = MilitaryGame([warrior(x=12, y=10, health=40)])
    run(MilitaryPolicy(), game)
    assert game.orders("move") == [("move", 1, 10, 10)]
    assert not game.orders("heal")


def test_a_hurt_unit_too_far_from_any_city_heals_where_it_stands():
    game = MilitaryGame([warrior(x=40, y=40, health=40)])
    run(MilitaryPolicy(), game)
    assert game.orders("heal") == [("heal", 1)]


def test_a_unit_that_cannot_fortify_until_healed_just_fortifies():
    game = MilitaryGame([warrior(health=40)])
    game.replies["heal"] = "Error: CANNOT_HEAL|Unit cannot fortify-until-healed"
    run(MilitaryPolicy(), game)
    assert game.orders("fortify") == [("fortify", 1)]


def test_a_unit_healing_is_forgotten_when_it_dies():
    game = MilitaryGame([warrior(health=40)])
    policy = MilitaryPolicy()
    run(policy, game)
    game.data["units"] = [warrior(uid=7)]
    run(policy, game)
    assert 1 not in policy._resting
    assert policy._refused == {}


# ----- garrison -----


def test_an_empty_threatened_city_calls_the_nearest_spare_unit():
    cities = [city_info(garrison_unit=""), city_info(city_id=2, name="Beta", x=14, y=10)]
    units = [warrior(uid=1, x=12, y=10), warrior(uid=2, x=14, y=10), warrior(uid=3, x=15, y=10)]
    game = MilitaryGame(units, [barbarian(x=11, y=11, cs=8)], cities=cities)
    run(MilitaryPolicy(), game)
    assert game.orders("move") == [("move", 1, 10, 10)]


def test_the_lone_defender_of_another_city_is_never_called_away():
    cities = [city_info(garrison_unit=""), city_info(city_id=2, name="Beta", x=13, y=10)]
    game = MilitaryGame([warrior(uid=2, x=13, y=10)], [barbarian(x=11, y=11)], cities=cities)
    run(MilitaryPolicy(), game)
    assert not game.orders("move")


def test_a_far_unit_is_not_called():
    game = MilitaryGame([warrior(x=25, y=25)], [barbarian(x=11, y=11)])
    run(MilitaryPolicy(), game)
    assert not any(a[2:] == (10, 10) for a in game.orders("move"))


def test_a_heavily_threatened_city_does_not_get_a_second_unit_sent_onto_its_tile():
    # The game refuses it: `STACKING_CONFLICT|Friendly UNIT_WARRIOR already on (10,10)`.
    threats = [barbarian(x=11, y=10 + i) for i in range(3)]
    game = MilitaryGame(
        [warrior(uid=1), warrior(uid=2, x=12, y=10), warrior(uid=3, x=40, y=40)], threats
    )
    run(MilitaryPolicy(), game)
    assert (10, 10) not in [a[2:] for a in game.orders("move")]


def test_an_empty_city_tile_still_gets_its_defender():
    game = MilitaryGame(
        [warrior(uid=2, x=12, y=10)], [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")]
    )
    run(MilitaryPolicy(), game)
    assert game.orders("move") == [("move", 2, 10, 10)]


def test_flee_to_a_city_held_by_a_same_class_unit_sends_no_refused_move():
    # Real log: a slinger fleeing to (66,25) was refused 123 times with STACKING_CONFLICT.
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=12, y=10)], [barbarian(x=13, y=11)])
    game.replies["move"] = STACKING
    run(MilitaryPolicy(), game)
    assert (10, 10) not in [a[2:] for a in game.orders("move")]


def test_a_refused_garrison_move_tries_the_next_unit_and_stops():
    units = [warrior(uid=1, x=12, y=10), warrior(uid=2, x=13, y=10)]
    game = MilitaryGame(units, [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")])
    game.replies["move"] = STACKING
    run(MilitaryPolicy(), game)
    # Each unit: the city, then at most two other tiles, then a fortify. No loop.
    assert len(game.orders("move")) <= 6
    assert sorted(a[1] for a in game.orders("fortify")) == [1, 2]


def test_a_blocked_move_counts_as_a_refusal():
    game = MilitaryGame(
        [warrior(x=12, y=10)], [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")]
    )
    game.replies["move"] = "MOVING_TO|10,10|from:12,10|now_at:12,10|BLOCKED (no path)"
    policy = MilitaryPolicy()
    run(policy, game)
    assert "refused" in policy.decisions[-1].reason


def test_idle_defenders_in_a_threatened_city_fortify_once():
    game = MilitaryGame([warrior(), warrior(uid=3)], [barbarian(x=11, y=11)])
    policy = MilitaryPolicy()
    run(policy, game)
    run(policy, game)
    assert sorted(a[1] for a in game.orders("fortify")) == [1, 3]


def test_a_quiet_city_is_left_alone():
    game = MilitaryGame([warrior()], [])
    run(MilitaryPolicy(), game)
    assert game.actions == []


def test_an_exposed_unit_outside_runs_for_the_city():
    game = MilitaryGame([warrior(x=14, y=10)], [barbarian(x=15, y=10)])
    run(MilitaryPolicy(), game)
    assert game.orders("move") == [("move", 1, 10, 10)]


# ----- settling's units -----


def test_scouts_are_never_ordered():
    game = MilitaryGame(
        [scout(x=12, y=10, health=40, targets=[target()]), warrior(uid=5)], [barbarian(x=11, y=11)]
    )
    run(MilitaryPolicy(), game)
    assert not any(a[1] == 9 for a in game.actions)


def test_a_settlers_escort_is_not_taken_for_the_garrison():
    cities = [city_info(garrison_unit=""), city_info(city_id=2, name="Beta", x=30, y=20)]
    # The settler's escort is the nearest combat unit; the other warrior is the one to call.
    units = [
        settler(uid=8, x=13, y=10),
        warrior(uid=1, x=13, y=10),
        warrior(uid=2, x=12, y=10),
    ]
    game = MilitaryGame(units, [barbarian(x=11, y=11)], cities=cities)
    run(MilitaryPolicy(), game)
    moved = {a[1] for a in game.orders("move")}
    assert 1 not in moved
    assert 2 in moved


def test_the_unit_settling_would_take_as_its_escort_is_reserved_even_without_a_settler_move():
    units = [
        settler(uid=8, x=10, y=10, moves_remaining=0.0),
        warrior(uid=1, x=10, y=10),
        warrior(uid=2, x=11, y=10),
        scout(uid=9, x=30, y=30),
    ]
    reserved = units_reserved_for_settling([build_unit(u) for u in units], [(10, 10)], map_width=74)
    # The garrison stays; the warrior outside the city is the escort settling would take.
    assert reserved == {2, 9}


def test_a_unit_walking_with_a_settler_is_reserved():
    units = [build_unit(u) for u in (settler(uid=8, x=15, y=10), warrior(uid=1, x=15, y=10))]
    assert units_reserved_for_settling(units, [(10, 10)], map_width=74) == {1}


def test_the_only_unit_in_a_city_is_not_a_reserved_escort():
    units = [build_unit(u) for u in (settler(uid=8, x=10, y=10), warrior(uid=1, x=10, y=10))]
    assert units_reserved_for_settling(units, [(10, 10)], map_width=74) == frozenset()


def test_a_reserved_escort_does_not_count_as_a_defender():
    # Two warriors at the capital, one of them the settler's escort: the city has one defender.
    cities = [city_info(), city_info(city_id=2, name="Beta", x=30, y=20)]
    units = [settler(uid=8, x=10, y=10), warrior(uid=1), warrior(uid=2), warrior(uid=3, x=12, y=10)]
    game = MilitaryGame(units, [barbarian(x=11, y=11)] * 3, cities=cities)
    run(MilitaryPolicy(), game)
    # Warrior 1 (escort) is untouched. Warrior 2 holds the tile and the game allows no second
    # unit there, so 3 is not sent onto it.
    assert (10, 10) not in [a[2:] for a in game.orders("move")]


# ----- barbarian camps -----


CAMP = tile(x=14, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])
STRONG = dict(combat_strength=30)


def strong_pair():
    return [
        warrior(uid=1, x=12, y=10, **STRONG),
        warrior(uid=2, x=12, y=11, **STRONG),
        warrior(uid=3),
    ]


def camp_game(units, threats, camp=CAMP, **kw):
    game = MilitaryGame(units, threats, **kw)

    async def area(center_x, center_y, radius=2):
        game.calls.append("get_map_area")
        return [camp]

    game.get_map_area = area
    return game


def camp_orders(policy):
    return [d for d in policy.decisions if d.action == "camp"]


def test_two_strong_units_walk_on_a_weak_camp():
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert {d.unit_id for d in camp_orders(policy)} == {1, 2}


def test_an_empty_camp_is_entered_from_beside_it_and_walked_to_from_afar():
    units = [
        warrior(uid=1, x=13, y=10, **STRONG),
        warrior(uid=2, x=12, y=11, **STRONG),
        warrior(uid=3),
    ]
    game = camp_game(units, [barbarian(x=15, y=9, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert ("move", 1, 14, 10) in game.orders("move")  # Beside it: the capture move.
    assert ("move", 2, 13, 10) in game.orders("move")  # Two off: the tile beside it first.


def test_units_sent_at_a_camp_take_different_tiles_beside_it():
    units = [
        warrior(uid=1, x=11, y=10, **STRONG),
        warrior(uid=2, x=11, y=11, **STRONG),
        warrior(uid=3),
    ]
    game = camp_game(units, [barbarian(x=14, y=10, cs=5)])
    run(MilitaryPolicy(), game)
    goals = [order[2:] for order in game.orders("move")]
    assert len(goals) == 2 and goals[0] != goals[1]


def test_a_stage_tile_the_game_says_is_full_is_replaced_by_the_next_one():
    units = [
        warrior(uid=1, x=11, y=10, **STRONG),
        warrior(uid=2, x=11, y=11, **STRONG),
        warrior(uid=3),
    ]
    game = camp_game(units, [barbarian(x=14, y=10, cs=5)])
    first = None

    original = game.move_unit

    async def move(unit_index, x, y):
        nonlocal first
        if first is None:
            first = (x, y)
            game.actions.append(("move", unit_index, x, y))
            return f"STACKING_CONFLICT|Friendly UNIT_SLINGER already on ({x},{y}) (unit_id=9)"
        return await original(unit_index, x, y)

    game.move_unit = move
    run(MilitaryPolicy(), game)
    tried = [order[2:] for order in game.orders("move")]
    assert tried[0] == first and tried[1] != first  # Moved on to another tile, same turn.


def test_a_camp_with_a_garrison_is_approached_to_the_adjacent_tile():
    units = [
        warrior(uid=1, x=11, y=10, **STRONG),
        warrior(uid=2, x=11, y=11, **STRONG),
        warrior(uid=3),
    ]
    game = camp_game(units, [barbarian(x=14, y=10, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert camp_orders(policy)
    assert ("move", 1, 14, 10) not in game.orders("move")
    assert ("move", 1, 13, 10) in game.orders("move")


def test_a_camp_is_left_alone_when_the_odds_are_not_clear():
    units = [warrior(uid=1, x=12, y=10), warrior(uid=2, x=12, y=11), warrior(uid=3)]
    game = camp_game(units, [barbarian(x=14, y=10, cs=14), barbarian(x=14, y=11, cs=14)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert not camp_orders(policy)


def test_no_camp_means_no_camp_order():
    game = camp_game(
        strong_pair(),
        [barbarian(x=15, y=10, cs=5)],
        camp=tile(x=14, y=10, improvement="IMPROVEMENT_FARM", units=[]),
    )
    policy = MilitaryPolicy()
    run(policy, game)
    assert not camp_orders(policy)


def test_a_fogged_camp_is_remembered_and_acted_on():
    fogged = tile(x=14, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", visibility="revealed")
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)], camp=fogged)
    policy = MilitaryPolicy()
    run(policy, game)
    assert camp_orders(policy)


def test_the_lone_garrison_does_not_leave_for_a_camp():
    units = [warrior(uid=1, **STRONG), warrior(uid=2, x=12, y=11, **STRONG)]
    game = camp_game(units, [barbarian(x=15, y=10, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert not camp_orders(policy)  # Only one spare unit, and a camp needs two.


def test_a_failed_camp_scan_is_logged_and_the_turn_goes_on(caplog):
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])

    async def broken(*args, **kwargs):
        raise RuntimeError("no map")

    game.get_map_area = broken
    policy = MilitaryPolicy()
    with caplog.at_level(logging.ERROR):
        run(policy, game)
    assert "barbarian camp failed" in caplog.text


def test_a_camp_that_will_not_fall_is_set_aside_after_the_turn_limit():
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])
    policy = MilitaryPolicy(MilitarySettings(max_camp_turns=2))
    for _ in range(5):
        run(policy, game)
    assert len(camp_orders(policy)) == 4  # Two turns of two moves, then set aside.
    assert (14, 10) in policy._camp_aside
    assert (14, 10) not in policy._camp_turns


def test_a_unit_on_a_camp_mission_does_not_flee_the_guards_until_it_is_hurt():
    def moves(policy, hp):
        unit = warrior(uid=3, x=12, y=10, health=hp)
        game = MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)])
        run(policy, game)
        return game.orders("move")

    assert moves(MilitaryPolicy(), 100)  # No mission: it goes home.
    sent = MilitaryPolicy()
    sent._mission[3] = ((14, 10), 20)  # The game's turn in these tests is 20.
    sent._camps[(14, 10)] = 20  # The camp it was sent at is still known.
    assert not moves(sent, 100)
    assert moves(sent, 40)  # Hurt: the mission no longer holds it.


def spare_party(n=4):
    return [warrior(uid=1)] + [warrior(uid=10 + i, x=10, y=10) for i in range(n)]


def test_idle_spare_units_look_where_barbarians_were_last_seen_when_no_camp_is_known():
    policy = MilitaryPolicy()
    seen = MilitaryGame(spare_party(), [barbarian(x=22, y=10, cs=5)])
    run(policy, seen)  # Far off: remembered, not close enough to hold anyone home.
    quiet = MilitaryGame(spare_party(), [])
    run(policy, quiet)
    sent = [d for d in policy.decisions if d.action == "recon"]
    assert len({d.unit_id for d in sent}) == 3  # One party of three, no more.
    assert {d.target for d in sent} == {(24, 10)}  # Past the sighting, up to the 14-tile radius.
    assert len(quiet.orders("move")) >= 3


def test_nobody_looks_for_a_camp_with_too_few_spare_units_or_when_one_is_known():
    few = MilitaryGame([warrior(uid=1), warrior(uid=3)], [barbarian(x=22, y=10, cs=5)])
    policy = MilitaryPolicy()
    run(policy, few)
    run(policy, MilitaryGame([warrior(uid=1), warrior(uid=3)], []))
    assert not [d for d in policy.decisions if d.action == "recon"]
    known = MilitaryPolicy()
    known._camps[(30, 10)] = 5
    run(known, MilitaryGame(spare_party(), [barbarian(x=22, y=10, cs=5)]))
    run(known, MilitaryGame(spare_party(), []))
    assert not [d for d in known.decisions if d.action == "recon"]


def test_a_recon_party_is_scanned_where_it_stands_and_does_not_flee():
    policy = MilitaryPolicy()
    policy._recon[11] = ((22, 10), 20)
    out = warrior(uid=11, x=18, y=10)
    game = camp_game(
        [warrior(uid=1), out, warrior(uid=12), warrior(uid=13)], [barbarian(x=19, y=10, cs=40)]
    )
    run(policy, game)
    assert game.calls.count("get_map_area") >= 1  # The party's view was read for camps.
    assert not any(
        order[1] == 11 and (order[2], order[3]) == (10, 10) for order in game.orders("move")
    )


def test_the_camp_scan_reads_around_the_barbarian_and_each_city_once_a_turn():
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5), barbarian(x=15, y=11, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert (
        game.calls.count("get_map_area") == 2
    )  # One around the nearer barbarian, one at the city.
    run(policy, game)
    # The city is read only every few turns; the camp we are marching on is read to see it gone.
    assert game.calls.count("get_map_area") == 4


def test_a_camp_in_plain_view_is_attacked_though_the_only_barbarian_is_a_ship():
    ship = barbarian(kind="UNIT_GALLEY", x=20, y=10, cs=5)
    game = camp_game(strong_pair(), [ship])
    policy = MilitaryPolicy()
    run(policy, game)
    assert {d.unit_id for d in camp_orders(policy)} == {1, 2}


def test_a_camp_seen_once_is_remembered_while_it_is_fogged_and_forgotten_when_seen_gone():
    policy = MilitaryPolicy()
    seen = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])
    run(policy, seen)
    assert (14, 10) in policy._camps
    fogged = camp_game(
        strong_pair(), [barbarian(x=15, y=10, cs=5)], camp=tile(x=14, y=10, visibility="revealed")
    )
    run(policy, fogged)
    assert (14, 10) in policy._camps  # A fogged tile proves nothing either way.
    gone = camp_game(
        strong_pair(), [barbarian(x=15, y=10, cs=5)], camp=tile(x=14, y=10, improvement=None)
    )
    run(policy, gone)
    assert (14, 10) not in policy._camps


# ----- promotions -----


def test_a_promotion_notification_is_answered_without_waiting_for_a_blocker():
    game = MilitaryGame([warrior(), warrior(uid=5, combat_strength=0)])
    game.notifications = [promote_notice()]
    game.promotions = {1: ["PROMOTION_X", "PROMOTION_BATTLECRY"]}
    run(MilitaryPolicy(), game)
    assert game.orders("promote") == [("promote", 1, "PROMOTION_BATTLECRY")]


def test_no_notification_means_no_promotion_reads():
    game = MilitaryGame([warrior()])
    game.promotions = {1: ["PROMOTION_X"]}
    run(MilitaryPolicy(), game)
    assert not game.orders("promote")


def test_the_blocker_policy_spends_promotions_for_the_resolver():
    policy = MilitaryPolicy()
    assert policy.blocker_policies() == {BlockerType.UNIT_PROMOTION: policy.resolve_promotions}
    game = MilitaryGame([warrior()])
    game.promotions = {1: ["PROMOTION_X"]}
    assert asyncio.run(policy.resolve_promotions(game, Blocker(BlockerType.UNIT_PROMOTION)))
    assert game.orders("promote") == [("promote", 1, "PROMOTION_X")]


def test_a_refused_promotion_fails_the_blocker_so_the_default_runs_and_is_not_retried_at_once():
    policy = MilitaryPolicy()
    game = MilitaryGame([warrior()])
    game.promotions = {1: ["PROMOTION_X"]}
    game.replies["promote"] = "Error: PROMOTION_FAILED"
    assert not asyncio.run(policy.resolve_promotions(game, Blocker(BlockerType.UNIT_PROMOTION)))
    assert asyncio.run(policy.resolve_promotions(game, Blocker(BlockerType.UNIT_PROMOTION)))
    assert len(game.orders("promote")) == 1


def test_the_resolver_falls_back_to_the_default_when_the_policy_fails():
    policy = MilitaryPolicy()
    resolver = BlockerResolver(policy.blocker_policies())
    assert resolver._policies[BlockerType.UNIT_PROMOTION] == policy.resolve_promotions


def test_a_unit_that_cannot_report_experience_is_skipped():
    game = MilitaryGame([warrior(), warrior(uid=5)])
    game.notifications = [promote_notice()]
    original = game.get_unit_promotions

    async def raising(unit_id):
        if unit_id == 1:
            raise ValueError("no experience")
        return await original(unit_id)

    game.get_unit_promotions = raising
    game.promotions = {5: ["PROMOTION_X"]}
    run(MilitaryPolicy(), game)
    assert game.orders("promote") == [("promote", 5, "PROMOTION_X")]


def test_promotion_checks_and_promotions_are_bounded():
    units = [warrior(uid=i) for i in range(1, 9)]
    game = MilitaryGame(units)
    game.notifications = [promote_notice()]
    game.promotions = {i: ["PROMOTION_X"] for i in range(1, 9)}
    run(MilitaryPolicy(MilitarySettings(max_promotions=3)), game)
    assert len(game.orders("promote")) == 3


# ----- bounds and failures -----


def test_orders_per_turn_are_bounded():
    units = [warrior(uid=i, health=40, x=11 + i, y=10) for i in range(1, 8)]
    game = MilitaryGame(units)
    run(MilitaryPolicy(MilitarySettings(max_actions=3)), game)
    assert len(game.actions) == 3


def test_an_action_that_raises_is_logged_not_fatal_and_set_aside(caplog):
    game = MilitaryGame([warrior(health=40), warrior(uid=5, health=40)])
    game.raises["heal"] = RuntimeError("boom")
    policy = MilitaryPolicy()
    with caplog.at_level(logging.ERROR):
        run(policy, game)
        run(policy, game)
    assert "failed for unit" in caplog.text
    assert len(game.orders("heal")) == 2  # Once each, and not again the next turn.


def test_a_lost_connection_is_not_swallowed():
    game = MilitaryGame([warrior(health=40)])
    game.raises["heal"] = ConnectionError("gone")
    with pytest.raises(ConnectionError):
        run(MilitaryPolicy(), game)


def test_a_failed_notification_read_does_not_stop_the_turn(caplog):
    game = MilitaryGame([warrior(health=40)])

    async def broken():
        raise RuntimeError("no notifications")

    game.get_notifications = broken
    run(MilitaryPolicy(), game)
    assert game.orders("heal")


def test_no_units_means_no_calls_beyond_the_reads():
    game = MilitaryGame([])
    run(MilitaryPolicy(), game)
    assert game.actions == []


def test_a_unit_gets_no_more_calls_than_the_attempt_limit():
    game = MilitaryGame([warrior(health=40)])
    game.replies["heal"] = "Error: CANNOT_HEAL|x"
    game.replies["fortify"] = "Error: CANNOT_FORTIFY|x"
    run(MilitaryPolicy(), game)
    assert len(game.actions) == 2


def test_the_decision_log_is_capped():
    game = MilitaryGame([warrior(health=40)])
    policy = MilitaryPolicy(MilitarySettings(decisions_kept=3))
    for turn in range(10):
        game.data["overview"] = overview(turn=20 + turn * 10, num_cities=1)
        run(policy, game)
    assert len(policy.decisions) <= 3


def test_per_unit_state_does_not_leak_across_many_units():
    policy = MilitaryPolicy()
    game = MilitaryGame([warrior(uid=i, health=40) for i in range(1, 4)])
    run(policy, game)
    game.data["units"] = []
    run(policy, game)
    game.data["units"] = [warrior(uid=50)]
    run(policy, game)
    assert policy._resting == {} and policy._fortified == {}


def test_reads_are_the_fog_audited_state_layer():
    game = MilitaryGame([archer(targets=[target()])], [barbarian()])
    run(MilitaryPolicy(), game)
    assert "execute_lua" not in game.calls
    assert asyncio.run(read_threats(game)) and asyncio.run(read_units(game))


def test_a_cap_on_promotion_checks_cannot_starve_the_later_units():
    game = MilitaryGame([warrior(uid=i) for i in (1, 2, 3, 4)])
    game.notifications = [promote_notice()]
    game.promotions = {4: ["PROMOTION_X"]}
    policy = MilitaryPolicy(MilitarySettings(max_promotion_checks=2))
    run(policy, game)
    assert not game.orders("promote")
    run(policy, game)
    assert game.orders("promote") == [("promote", 4, "PROMOTION_X")]


def test_a_unit_that_attacked_fortifies_again_in_a_threatened_city():
    game = MilitaryGame([archer(targets=[target()])], [barbarian()])
    policy = MilitaryPolicy()
    policy._fortified[2] = (10, 10)
    run(policy, game)
    assert 2 not in policy._fortified
    game.data["units"] = [archer()]
    run(policy, game)
    assert game.orders("fortify") == [("fortify", 2)]


def test_a_camp_count_starts_again_after_the_camp_is_forgotten():
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert any(where == (14, 10) for where, _ in policy._camp_turns)
    game.data["units"] = []
    game.data["overview"] = overview(turn=20 + policy.settings.camp_idle_turns + 1, num_cities=1)
    run(policy, game)
    assert policy._camp_turns == {}


# ----- refused moves fall back -----


def test_a_refused_garrison_move_goes_to_the_next_tile_beside_the_city():
    game = MilitaryGame(
        [warrior(x=12, y=10)], [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")]
    )
    game.refuse_moves_to = {(10, 10)}
    policy = MilitaryPolicy()
    run(policy, game)
    moves = game.orders("move")
    assert moves[0] == ("move", 1, 10, 10)
    assert len(moves) == 2 and moves[1][2:] != (10, 10)
    assert not game.orders("fortify")  # The second tile took it.
    assert "tile beside" in policy.decisions[-1].reason


def test_a_unit_refused_everywhere_fortifies_where_it_stands():
    game = MilitaryGame(
        [warrior(x=12, y=10)], [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")]
    )
    game.replies["move"] = STACKING
    policy = MilitaryPolicy()
    run(policy, game)
    assert len(game.orders("move")) == 3  # The city and two fallback tiles, no more.
    assert game.orders("fortify") == [("fortify", 1)]
    assert "fortified where it stands" in policy.decisions[-1].reason


def test_the_fallback_skips_tiles_other_units_hold():
    units = [warrior(uid=1, x=12, y=10), warrior(uid=2, x=11, y=10)]
    game = MilitaryGame(units, [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")])
    game.refuse_moves_to = {(10, 10)}
    run(MilitaryPolicy(), game)
    assert ("move", 1, 11, 10) not in game.orders("move")


def test_a_fallback_that_is_refused_too_is_not_repeated_on_the_next_turn():
    game = MilitaryGame(
        [warrior(x=12, y=10)], [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")]
    )
    game.replies["move"] = STACKING
    game.replies["fortify"] = "Error: cannot fortify"
    policy = MilitaryPolicy()
    run(policy, game)
    first = len(game.actions)
    run(policy, game)
    assert len(game.actions) == first  # The refusal is remembered for a few turns.


# ----- rejected attacks are logged -----


def musketman(x=11, y=10):
    return barbarian(kind="UNIT_MUSKETMAN", x=x, y=y, cs=55)


def test_a_rejected_attack_is_logged_with_odds_and_reason_once_per_target():
    units = [
        warrior(uid=1, targets=[target(kind="UNIT_MUSKETMAN")]),
        warrior(uid=3, targets=[target(kind="UNIT_MUSKETMAN")]),
    ]
    game = MilitaryGame(units, [musketman()])
    policy = MilitaryPolicy()
    run(policy, game)
    assert not game.orders("attack")
    skips = [d for d in policy.decisions if d.action == "skip" and d.target == (11, 10)]
    assert len(skips) == 1
    assert skips[0].odds["dealt"] > 0
    assert skips[0].reason.startswith("no attack:")


def test_a_target_that_was_attacked_is_not_logged_as_rejected():
    units = [archer(targets=[target(hp=30)]), warrior(uid=3, targets=[target(hp=30)])]
    game = MilitaryGame(units, [barbarian(hp=30)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert game.orders("attack")
    assert not [d for d in policy.decisions if d.action == "skip"]


# ----- city strikes -----


def walled(**changes):
    return city_info(wall_hp=100, wall_max_hp=100, defense_strength=20, **changes)


def test_a_walled_city_strikes_a_hostile_unit_in_range_before_units_attack():
    game = MilitaryGame(
        [archer(targets=[target()])], [barbarian()], cities=[walled(garrison_unit="UNIT_ARCHER")]
    )
    run(MilitaryPolicy(), game)
    assert game.orders("city_attack") == [("city_attack", 65536, 11, 10)]
    names = [a[0] for a in game.actions]
    assert names.index("city_attack") < names.index("attack")


def test_a_city_without_walls_does_not_strike():
    game = MilitaryGame([warrior()], [barbarian()])
    run(MilitaryPolicy(), game)
    assert not game.orders("city_attack")


def test_a_city_strikes_only_what_is_within_its_range():
    game = MilitaryGame([warrior()], [barbarian(x=14, y=10)], cities=[walled()])
    run(MilitaryPolicy(), game)
    assert not game.orders("city_attack")


def test_a_city_does_not_strike_at_peace():
    game = MilitaryGame(
        [warrior()], [barbarian(owner_id=ROME, owner_name="Rome")], cities=[walled()]
    )
    game.data["civs"] = [civ_info()]  # Met, not at war.
    run(MilitaryPolicy(), game)
    assert not game.orders("city_attack")


def test_a_refused_strike_is_not_repeated_at_once():
    game = MilitaryGame([warrior()], [barbarian(), barbarian(x=11, y=9)], cities=[walled()])
    game.replies["city_attack"] = "Error: ALREADY_FIRED|City already attacked this turn"
    policy = MilitaryPolicy()
    run(policy, game)
    run(policy, game)
    assert len(game.orders("city_attack")) == 1
    assert any(d.action == "city_strike" and "refused" in d.reason for d in policy.decisions)


def test_a_city_strike_is_logged_with_its_odds():
    game = MilitaryGame([warrior()], [barbarian()], cities=[walled()])
    policy = MilitaryPolicy()
    run(policy, game)
    strike = next(d for d in policy.decisions if d.action == "city_strike")
    assert strike.target == (11, 10) and strike.odds["taken"] == 0


# ----- focus fire -----


def weak(uid):
    # Strength 15 against a strength 30 defender (36 with the assumed bonus): about 56 damage
    # back, so 100 - 56 * 1.2 is 33 hp left. Under the lone floor of 40, over the group's 25.
    return warrior(uid=uid, combat_strength=15, targets=[target(hp=20)])


def test_a_lone_attack_the_floor_refuses_is_still_refused():
    game = MilitaryGame([weak(1)], [barbarian(hp=20, cs=30)])
    game.replies["attack"] = MELEE_HIT
    run(MilitaryPolicy(), game)
    assert not game.orders("attack")


def test_units_that_together_kill_attack_above_the_planned_floor():
    game = MilitaryGame([weak(1), weak(3), weak(4)], [barbarian(hp=20, cs=30)])
    game.replies["attack"] = MELEE_HIT
    policy = MilitaryPolicy()
    run(policy, game)
    assert len(game.orders("attack")) >= 2
    assert all("planned kill" in d.reason for d in policy.decisions if d.action == "attack")


def test_a_group_that_cannot_kill_does_not_attack():
    units = [warrior(uid=i, combat_strength=15, targets=[target(hp=100)]) for i in (1, 3, 4)]
    game = MilitaryGame(units, [barbarian(hp=100, cs=30)])
    run(MilitaryPolicy(), game)
    assert not game.orders("attack")


def test_a_unit_that_would_not_survive_even_the_planned_floor_is_not_used():
    units = [warrior(uid=i, combat_strength=10, targets=[target(hp=20)]) for i in (1, 3, 4, 5, 6)]
    game = MilitaryGame(units, [barbarian(hp=20, cs=30)])
    run(MilitaryPolicy(), game)
    assert not game.orders("attack")


def test_a_hurt_unit_is_not_put_in_a_planned_group():
    hurt = warrior(uid=4, combat_strength=15, health=40, targets=[target(hp=20)])
    game = MilitaryGame([weak(1), weak(3), hurt], [barbarian(hp=20, cs=30)])
    game.replies["attack"] = MELEE_HIT
    run(MilitaryPolicy(), game)
    assert 4 not in [a[1] for a in game.orders("attack")]


def test_the_planned_floor_is_below_the_lone_floor():
    assert 0 < W.planned_kill_hp_after < W.min_hp_after_attack


def test_a_strike_and_one_unit_can_form_a_planned_kill():
    # The strike takes 30 off 35 hp; one weak unit then finishes it above the planned floor.
    unit = warrior(uid=1, combat_strength=15, targets=[target(hp=35)])
    spare = warrior(uid=3)  # A second defender, so the first is not the city's only one.
    game = MilitaryGame([unit, spare], [barbarian(hp=35, cs=30)], cities=[walled()])
    game.replies["attack"] = MELEE_HIT
    run(MilitaryPolicy(MilitarySettings(split_stacks=False)), game)
    assert game.orders("city_attack")
    assert [a[0] for a in game.actions][:2] == ["city_attack", "attack"]


# ----- spare units -----


def spare_decisions(policy, action):
    return [d for d in policy.decisions if d.action == action]


def test_a_spare_unit_in_a_quiet_city_fortifies_and_the_garrison_is_left_alone():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3)], [])
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    run(policy, game)
    # Neither leaves, so both may fortify; the floor matters only for a unit that walks away.
    assert game.orders("fortify") == [("fortify", 1), ("fortify", 3)]
    assert "spare" in spare_decisions(policy, "fortify")[0].reason


def test_a_spare_unit_explores_while_nothing_hostile_is_in_sight():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3), warrior(uid=4)], [])
    policy = MilitaryPolicy()
    run(policy, game)
    assert len(game.orders("explore")) == 2  # Up to two explorers.
    assert game.orders("fortify") == []  # The third unit is the city's garrison.
    assert "exploring" in spare_decisions(policy, "explore")[0].reason
    # Out exploring with nothing hostile close, it is left to the game's explorer.
    out = warrior(uid=3, x=14, y=10)
    later = MilitaryGame([warrior(uid=1), out, warrior(uid=4)], [])
    policy._explorers[3] = (13, 10)  # It was last seen elsewhere, so it is still moving.
    run(policy, later)
    assert all(order[1] != 3 for order in later.actions)


def test_an_explorer_that_stopped_moving_is_free_to_be_sent_again():
    stuck = warrior(uid=3, x=14, y=10)
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=1))
    policy._explorers[3] = (14, 10)  # Same tile as last turn: the game's explorer is done.
    game = MilitaryGame([warrior(uid=1), stuck, warrior(uid=4)], [])
    run(policy, game)
    assert game.orders("explore")  # With the cap of one, a stale entry would have blocked it.


def test_spare_units_stay_home_with_a_hostile_unit_in_sight_or_with_exploring_off():
    seen = MilitaryGame([warrior(uid=1), warrior(uid=3)], [barbarian(x=14, y=10)])
    run(MilitaryPolicy(), seen)
    assert seen.orders("explore") == []
    # A hostile unit far off, or one in a ship, could not reach an explorer: no reason to stay.
    far = MilitaryGame([warrior(uid=1), warrior(uid=3)], [barbarian(x=25, y=10)])
    run(MilitaryPolicy(), far)
    assert far.orders("explore")
    ship = MilitaryGame(
        [warrior(uid=1), warrior(uid=3)], [barbarian(kind="UNIT_GALLEY", x=12, y=10)]
    )
    run(MilitaryPolicy(), ship)
    assert ship.orders("explore")
    off = MilitaryGame([warrior(uid=1), warrior(uid=3)], [])
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    run(policy, off)
    assert off.orders("explore") == []
    assert off.orders("fortify")


def test_the_last_defender_of_a_city_is_never_given_a_spare_order():
    game = MilitaryGame([warrior(uid=1)], [])
    policy = MilitaryPolicy()
    run(policy, game)
    assert game.actions == []
    assert policy.decisions == []


def test_spare_units_never_leave_a_city_below_its_requirement_when_hunting():
    # One defender needed, and the one unit that could hunt is that defender.
    game = MilitaryGame([warrior(uid=1, combat_strength=40)], [barbarian(x=13, y=10, cs=5)])
    run(MilitaryPolicy(), game)
    assert game.orders("move") == []


def test_a_fortified_spare_unit_is_not_ordered_again_and_the_idleness_is_logged():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3)], [])
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    run(policy, game)
    run(policy, game)
    assert game.orders("fortify") == [("fortify", 1), ("fortify", 3)]
    assert "already fortified" in policy.decisions[-1].reason


def test_a_spare_unit_outside_a_city_fortifies_in_place():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3, x=12, y=10)], [])
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    run(policy, game)
    assert game.orders("fortify") == [("fortify", 3)]


def test_a_refused_spare_fortify_is_logged_and_not_repeated_next_turn():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3)], [])
    game.replies["fortify"] = "Error: cannot fortify"
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    run(policy, game)
    run(policy, game)
    assert len(game.orders("fortify")) == 2  # One try for each unit, none on the second turn.
    assert any("fortify refused" in d.reason for d in policy.decisions)


def test_a_unit_that_cannot_fortify_is_taken_as_holding_its_post():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3)], [])
    game.replies["fortify"] = "Error: CANNOT_FORTIFY|Unit cannot fortify or sleep"
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    run(policy, game)
    run(policy, game)
    assert len(game.orders("fortify")) == 2  # One try each, none on the second turn.
    assert not any("refused" in d.reason for d in policy.decisions)
    assert any("already holding" in d.reason for d in policy.decisions)


def test_a_settlers_escort_gets_no_spare_order():
    units = [settler(uid=8, x=13, y=10), warrior(uid=1, x=13, y=10), warrior(uid=2)]
    game = MilitaryGame(units, [])
    run(MilitaryPolicy(), game)
    assert not any(a[1] in (8, 1) for a in game.actions)


def test_a_hurt_spare_unit_heals_instead():
    game = MilitaryGame([warrior(uid=1), warrior(uid=3, health=40)], [])
    run(MilitaryPolicy(), game)
    assert [a[1] for a in game.orders("heal")] == [3]
    assert ("fortify", 3) not in game.orders("fortify")


def test_a_spare_unit_walks_toward_a_hostile_unit_it_can_beat():
    units = [warrior(uid=1), warrior(uid=3, combat_strength=40)]
    game = MilitaryGame(units, [barbarian(x=13, y=10, cs=5)])
    policy = MilitaryPolicy(MilitarySettings(split_stacks=False))
    run(policy, game)
    hunts = spare_decisions(policy, "hunt")
    assert [d.unit_id for d in hunts] == [3]
    assert hunts[0].target == (13, 10)
    assert game.orders("move") == [("move", 3, 12, 10)]
    assert ("fortify", 3) not in game.orders("fortify")


def test_a_spare_unit_does_not_hunt_when_the_odds_are_poor():
    units = [warrior(uid=1), warrior(uid=3)]
    game = MilitaryGame(units, [barbarian(x=13, y=10, cs=30)])
    policy = MilitaryPolicy(MilitarySettings(split_stacks=False))
    run(policy, game)
    assert not spare_decisions(policy, "hunt")
    assert game.orders("move") == []


def test_a_spare_unit_does_not_go_past_the_leash():
    units = [warrior(uid=1), warrior(uid=3, combat_strength=40)]
    game = MilitaryGame(units, [barbarian(x=13, y=10, cs=5)])
    policy = MilitaryPolicy(MilitarySettings(spare_leash=1, split_stacks=False))
    run(policy, game)
    assert game.orders("move") == []
    assert ("fortify", 3) in game.orders("fortify")  # Stays home instead.


def test_a_spare_unit_hunts_a_unit_of_a_civ_at_war_but_not_one_at_peace():
    def scene(at_war):
        units = [warrior(uid=1), warrior(uid=3, combat_strength=40)]
        game = MilitaryGame(units, [barbarian(x=13, y=10, cs=5, owner_id=ROME, owner_name="Rome")])
        game.data["civs"] = [civ_info(player_id=ROME, is_at_war=at_war)]
        return game

    war, peace = scene(True), scene(False)
    run(MilitaryPolicy(MilitarySettings(split_stacks=False)), war)
    run(MilitaryPolicy(MilitarySettings(split_stacks=False)), peace)
    assert war.orders("move") == [("move", 3, 12, 10)]
    assert peace.orders("move") == []


def test_a_spare_unit_in_reach_of_an_enemy_unit_attacks_it_by_the_attack_step():
    units = [warrior(uid=1), archer(uid=2, targets=[target()])]
    game = MilitaryGame(units, [barbarian()])
    policy = MilitaryPolicy()
    run(policy, game)
    assert game.orders("attack") == [("attack", 2, 11, 10)]


def test_spare_orders_are_bounded_per_turn_and_the_rest_logged():
    units = [warrior(uid=1)] + [warrior(uid=i, x=12, y=10 + i) for i in (3, 4, 5)]
    game = MilitaryGame(units, [])
    policy = MilitaryPolicy(MilitarySettings(max_spare_orders=1, spare_explorers=0))
    run(policy, game)
    assert len(game.orders("fortify")) == 1
    assert sum("used up" in d.reason for d in policy.decisions) == 2


def test_a_lua_error_from_a_strike_is_logged_once_and_backs_off(caplog):
    game = MilitaryGame([warrior()], [barbarian(), barbarian(x=11, y=9)], cities=[walled()])
    game.raises["city_attack"] = RuntimeError("ERR:Runtime Error: operator .. is not supported")
    policy = MilitaryPolicy()
    with caplog.at_level("ERROR"):
        run(policy, game)
        run(policy, game)  # The next turn: still inside the back-off.
    assert len(game.orders("city_attack")) == 1
    assert len([r for r in caplog.records if "City strike" in r.getMessage()]) == 1
    assert any(d.action == "city_strike" and "refused" in d.reason for d in policy.decisions)


def offense_rows(caplog):
    prefix = "military_offense "
    return [
        json.loads(r.getMessage()[len(prefix) :])
        for r in caplog.records
        if r.getMessage().startswith(prefix)
    ]


def test_the_offense_line_counts_an_attack_on_a_barbarian(caplog):
    game = MilitaryGame(
        [archer(targets=[target()])], [barbarian()], cities=[city_info(garrison_unit="UNIT_ARCHER")]
    )
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["attacks_barb"] == 1
    assert row["attacks_other"] == 0
    assert row["barb_visible"] == 1
    assert row["combat_units"] == 1


def test_the_offense_line_counts_camp_orders(caplog):
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["camp_orders"] == 2
    assert row["attacks_barb"] == 0


def test_a_recon_entry_for_a_dead_or_overdue_unit_is_dropped_each_turn():
    policy = MilitaryPolicy()
    policy._recon[99] = ((22, 10), 20)  # No such unit any more.
    policy._recon[1] = ((22, 10), 20 - policy.settings.recon_turns - 1)  # Out too long.
    run(policy, MilitaryGame([warrior(uid=1)], []))
    assert policy._recon == {}


def test_the_recon_target_across_the_map_seam_keeps_going_the_short_way():
    policy = MilitaryPolicy()
    width = policy.settings.map_width
    policy._sightings[(2, 10)] = 20
    city = types.SimpleNamespace(x=width - 2, y=10, city_id=1)
    t = types.SimpleNamespace(cities=[city], turn=20)
    x, y = policy._recon_target(t)
    assert 0 <= x < width and y == 10
    assert x < 12  # Continues east past the sighting, wrapped, not a huge negative number.


def test_camp_staging_retries_stay_within_the_per_unit_order_limit():
    units = [
        warrior(uid=1, x=11, y=10, **STRONG),
        warrior(uid=2, x=11, y=11, **STRONG),
        warrior(uid=3),
    ]
    game = camp_game(units, [barbarian(x=14, y=10, cs=5)])
    game.replies["move"] = STACKING
    policy = MilitaryPolicy()
    run(policy, game)
    limit = policy.settings.attempts_per_unit
    for uid in (1, 2):
        assert len([o for o in game.orders("move") if o[1] == uid]) <= limit


def test_wounded_spare_units_do_not_count_towards_a_recon_party():
    policy = MilitaryPolicy()
    hurt = [warrior(uid=10 + i, x=10, y=10, health=20) for i in range(3)]
    run(
        policy,
        MilitaryGame(
            [warrior(uid=1), *hurt, warrior(uid=20, x=10, y=10)], [barbarian(x=22, y=10, cs=5)]
        ),
    )
    game = MilitaryGame([warrior(uid=1), *hurt, warrior(uid=20, x=10, y=10)], [])
    run(policy, game)
    assert not [d for d in policy.decisions if d.action == "recon"]


def test_a_camp_that_spawned_in_fog_after_the_first_scan_is_not_learned():
    fogged = tile(x=14, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", visibility="revealed")
    policy = MilitaryPolicy()
    run(
        policy,
        camp_game(
            strong_pair(),
            [barbarian(x=15, y=10, cs=5)],
            camp=tile(x=14, y=10, improvement=None, units=[]),
        ),
    )
    run(policy, camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)], camp=fogged))
    assert (14, 10) not in policy._camps


def test_the_recon_target_stays_inside_the_map_rows():
    policy = MilitaryPolicy()
    policy._sightings[(10, 45)] = 20
    city = types.SimpleNamespace(x=10, y=43, city_id=1)
    t = types.SimpleNamespace(cities=[city], turn=20)
    assert policy._recon_target(t)[1] <= policy.settings.map_height - 1


def test_a_remembered_camp_is_rescanned_and_forgotten_when_seen_cleared():
    policy = MilitaryPolicy()
    policy._camps[(30, 10)] = 5  # Well beyond the city scan radius.
    seen = []

    async def area(cx, cy, radius=2):
        seen.append((cx, cy))
        return [tile(x=30, y=10, improvement=None, units=[])] if (cx, cy) == (30, 10) else []

    game = MilitaryGame(strong_pair() + [warrior(uid=3)], [])
    game.get_map_area = area
    run(policy, game)
    assert (30, 10) in seen and (30, 10) not in policy._camps


def test_a_far_force_gathers_before_any_unit_goes_in():
    units = [warrior(uid=1, x=2, y=10, **STRONG), warrior(uid=2, x=2, y=11, **STRONG)]
    game = camp_game(
        units, [], camp=tile(x=14, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", units=[])
    )
    policy = MilitaryPolicy()
    run(policy, game)
    goals = [(o[2], o[3]) for o in game.orders("move")]
    assert goals and all(
        hex_distance(*g, 14, 10, 74) <= policy.settings.camp_muster_radius for g in goals
    )
    assert not any(g == (14, 10) for g in goals)


def test_the_first_to_arrive_waits_for_the_rest_of_the_force():
    near = warrior(uid=1, x=12, y=10, **STRONG)  # Within the muster radius, alone.
    far = warrior(uid=2, x=4, y=10, **STRONG)
    game = camp_game([near, far], [])
    policy = MilitaryPolicy()
    run(policy, game)
    assert not any(o[1] == near.unit_index for o in game.orders("move"))
    assert any("waiting" in d.reason for d in policy.decisions if d.unit_id == 1)


def test_a_camp_on_the_threat_map_is_hunted_though_the_military_never_saw_it():
    from civ6_ai.threat_map import ThreatMap

    tm = ThreatMap()
    tm.see_tiles(
        52,
        [
            build_tile(
                tile(
                    x=14,
                    y=10,
                    improvement="IMPROVEMENT_BARBARIAN_CAMP",
                    visibility="revealed",
                    units=[],
                )
            )
        ],
        baseline=True,
    )
    game = camp_game(
        strong_pair(), [], camp=tile(x=14, y=10, improvement=None, units=[], visibility="revealed")
    )
    policy = MilitaryPolicy()
    policy.threat_map = tm
    run(policy, game)
    assert camp_orders(policy)


def test_a_force_is_released_from_its_mission_when_the_camp_is_gone():
    policy = MilitaryPolicy()
    unit = warrior(uid=3, x=12, y=10, **STRONG)
    policy._mission[3] = ((14, 10), 20)
    policy._camps[(14, 10)] = 20
    game = MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)])
    run(policy, game)
    assert 3 in policy._mission  # The camp stands: still on its mission.
    del policy._camps[(14, 10)]  # Taken, or seen empty.
    game = MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)])
    run(policy, game)
    assert 3 not in policy._mission


def test_a_force_still_walking_to_a_far_camp_is_not_cut_off_by_the_assault_limit():
    units = [warrior(uid=1, x=2, y=10, **STRONG), warrior(uid=2, x=2, y=11, **STRONG)]
    game = camp_game(units, [])
    policy = MilitaryPolicy()
    for _ in range(policy.settings.max_camp_turns + 3):  # More turns than the assault limit.
        run(policy, game)
    assert (14, 10) not in policy._camp_aside  # Still on its way: not set aside.


def test_a_force_that_never_arrives_is_set_aside_after_the_travel_limit():
    units = [warrior(uid=1, x=2, y=10, **STRONG), warrior(uid=2, x=2, y=11, **STRONG)]
    game = camp_game(units, [])
    policy = MilitaryPolicy()
    for _ in range(policy.settings.camp_travel_turns + 2):
        run(policy, game)
    assert (14, 10) in policy._camp_aside


def test_a_unit_marching_on_a_camp_is_not_sent_exploring_when_only_it_is_free_to_move():
    marching = warrior(uid=1, x=2, y=10, **STRONG)
    game = camp_game([marching], [])
    policy = MilitaryPolicy()
    policy._mission[1] = ((14, 10), 20)  # Sent this turn (the tests' turn is 20).
    policy._camps[(14, 10)] = 20
    run(policy, game)
    assert not game.orders("explore")
    assert not game.orders("fortify")


def test_a_muster_tile_the_game_refuses_is_remembered_and_the_next_one_tried():
    units = [warrior(uid=1, x=2, y=10, **STRONG), warrior(uid=2, x=2, y=11, **STRONG)]
    game = camp_game(units, [])
    game.replies["move"] = "MOVING_TO|0,0|BLOCKED (impassable terrain)"
    policy = MilitaryPolicy()
    run(policy, game)
    tried = {(o[2], o[3]) for o in game.orders("move") if o[1] in (0, 1)}
    assert len(tried) > 1  # It did not stop at the first refused tile.
    assert policy._muster_bad


def test_a_fogged_camp_the_threat_map_never_took_is_not_learned():
    from civ6_ai.threat_map import ThreatMap

    fogged = tile(x=14, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", visibility="revealed")
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)], camp=fogged)
    policy = MilitaryPolicy()
    policy.threat_map = ThreatMap()  # No baseline read: the camp spawned out of sight.
    run(policy, game)
    assert not camp_orders(policy)
    assert (14, 10) not in policy._camps


# ----- Run 16: kills and camps the offense line missed -----

# Real replies of Run 16: the game's reply to a lethal attack carries only an estimate, with no
# "killed" in it, so the offense line read 0 kills for 150 turns while targets died.
EST_KILL_RANGED = (
    "RANGE_ATTACK|target:UNIT_ARCHER at (11,10)|pre_hp:46/100|your HP:100|range:2 dist:1"
    "|est damage dealt:~46\n  Post-combat: ~0/46 (estimate — verify with get_units)"
)
NO_ENEMY = "Error: NO_ENEMY|No hostile unit at (11,10)"


def test_the_offense_line_counts_an_estimated_kill_the_reply_does_not_call_a_kill(caplog):
    game = MilitaryGame(
        [archer(targets=[target(hp=46)])],
        [barbarian(kind="UNIT_ARCHER", hp=46)],
        cities=[city_info(garrison_unit="UNIT_ARCHER")],
    )
    game.replies["attack"] = EST_KILL_RANGED
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["attacks_barb"] == 1
    assert row["kills_barb"] == 1


def test_a_hit_that_leaves_the_target_standing_is_no_kill(caplog):
    game = MilitaryGame(
        [archer(targets=[target()])], [barbarian()], cities=[city_info(garrison_unit="UNIT_ARCHER")]
    )
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["kills_barb"] == 0


def test_an_attack_that_finds_nobody_after_an_earlier_hit_counts_the_kill(caplog):
    game = MilitaryGame(
        [archer(uid=2, targets=[target()]), archer(uid=3, targets=[target()])],
        [barbarian()],
        cities=[city_info(garrison_unit="UNIT_ARCHER")],
    )
    replies = iter([RANGED_HIT, NO_ENEMY])

    async def attack(unit_index, x, y):
        game.actions.append(("attack", unit_index, x, y))
        return next(replies)

    game.attack_unit = attack
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["kills_barb"] == 1


def test_the_offense_line_counts_a_camp_marched_on_that_is_gone_once(caplog):
    policy = MilitaryPolicy()
    unit = warrior(uid=3, x=12, y=10, **STRONG)
    policy._mission[3] = ((14, 10), 20)
    policy._camps[(14, 10)] = 20
    game = MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)])
    with caplog.at_level(logging.INFO):
        run(policy, game)
        del policy._camps[(14, 10)]  # A unit stepped on the emptied tile, or it was seen empty.
        run(policy, MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)]))
        run(policy, MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)]))
    rows = offense_rows(caplog)
    assert [r["camps_gone"] for r in rows] == [0, 1, 0]


def test_a_camp_gone_is_counted_though_its_force_is_sent_at_another_camp(caplog):
    policy = MilitaryPolicy()
    policy._mission[1] = ((20, 10), 20)  # Sent at a camp that is no longer known.
    policy._mission[2] = ((20, 10), 20)
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)])  # The scan shows (14, 10).
    with caplog.at_level(logging.INFO):
        run(policy, game)
    (row,) = offense_rows(caplog)
    assert row["camps_gone"] == 1
    assert policy._mission[1][0] == (14, 10)  # The same units were sent at the other camp.


# ----- stacked units (issue 266) -----

MELEE_FALLBACK = (
    "Combat Estimate (Melee):\n  UNIT_ARCHER (CS:13, HP:100) vs UNIT_SLINGER (CS:7, HP:100)\n"
    "MELEE_ATTACK|target:UNIT_SLINGER at (11,10)|enemy HP:100 -> 100/100|your HP:100 -> 100 CS:15"
)


def decisions_of(policy, action):
    return [d for d in policy.decisions if d.action == action]


def test_a_stack_in_a_city_is_split_before_attacks_keeping_the_fortified_unit():
    keeper = archer(uid=2)
    other = archer(uid=3, targets=[target()])
    game = MilitaryGame([keeper, other], [barbarian()])
    policy = MilitaryPolicy()
    policy._fortified[2] = (10, 10)
    run(policy, game)
    first = game.orders()[0]
    assert first[0] == "move" and first[1] == 3  # The unit that was not fortified leaves.
    assert (first[2], first[3]) != (10, 10)
    assert hex_distance(10, 10, first[2], first[3], 0) == 1
    found = decisions_of(policy, "stack")
    assert "stack found" in found[0].reason and "keeping UNIT_ARCHER 2" in found[0].reason
    assert "stack split" in found[1].reason


def test_the_stronger_unit_stays_when_neither_is_fortified():
    game = MilitaryGame([warrior(uid=1), archer(uid=2)], [])
    run(MilitaryPolicy(), game)
    moves = game.orders("move")
    assert [m[1] for m in moves][:1] == [1]  # The warrior is weaker, so it leaves.


def test_a_refused_split_move_tries_the_next_tile():
    game = MilitaryGame([archer(uid=2), archer(uid=3)], [])
    policy = MilitaryPolicy()
    policy._fortified[2] = (10, 10)
    game.refuse_moves_to = {(11, 10)}
    run(policy, game)
    moves = [m for m in game.orders("move") if m[1] == 3]
    assert len(moves) >= 2 or (moves[0][2], moves[0][3]) != (11, 10)
    split = [d for d in decisions_of(policy, "stack") if d.reason.startswith("stack split:")]
    assert any("left 10,10" in d.reason for d in split)


def test_a_split_prefers_a_tile_from_which_the_unit_can_still_attack():
    game = MilitaryGame([archer(uid=2), archer(uid=3)], [barbarian(x=13, y=10)])
    policy = MilitaryPolicy()
    policy._fortified[2] = (10, 10)
    run(policy, game)
    move = next(m for m in game.orders("move") if m[1] == 3)
    assert hex_distance(move[2], move[3], 13, 10, 0) <= 2


def test_a_split_ranks_tiles_by_threat_map_cover_and_threat():
    class Map:
        def threat_at(self, x, y, turn=None):
            return 5.0 if (x, y) == (11, 10) else 0.0

        def cover_at(self, x, y):
            return 3.0 if (x, y) == (9, 10) else 0.0

        def forget_units_at(self, tiles):
            return 0

        def relocate(self, placed, health):
            return 0

        def camps(self):
            return []

        def see_tiles(self, turn, tiles, baseline=False):
            return None

    game = MilitaryGame([archer(uid=2), archer(uid=3)], [])
    policy = MilitaryPolicy()
    policy.threat_map = Map()
    policy._fortified[2] = (10, 10)
    run(policy, game)
    move = next(m for m in game.orders("move") if m[1] == 3)
    assert (move[2], move[3]) == (9, 10)


def test_no_stack_means_no_split_order():
    game = MilitaryGame([archer(uid=2), archer(uid=3, x=11, y=10)], [])
    policy = MilitaryPolicy()
    run(policy, game)
    assert not decisions_of(policy, "stack")


def test_a_ranged_order_that_comes_back_melee_is_a_failed_attack(caplog):
    game = MilitaryGame(
        [archer(targets=[target()])], [barbarian()], cities=[city_info(garrison_unit="UNIT_ARCHER")]
    )
    game.replies["attack"] = MELEE_FALLBACK
    policy = MilitaryPolicy()
    with caplog.at_level(logging.INFO):
        run(policy, game)
    (failed,) = [d for d in policy.decisions if d.action == "attack"]
    assert failed.reason.startswith("attack failed")
    (row,) = offense_rows(caplog)
    assert row["attacks_barb"] == 0
    assert row["kills_barb"] == 0
    assert len(game.orders("attack")) == 1  # Not tried again this turn.


# A real melee reply (iteration 1 of the turn-100 test): the enemy health is from before the
# combat, so it reads "58 -> 58" on a hit that took 33.
REAL_MELEE = (
    "MELEE_ATTACK|target:UNIT_CROSSBOWMAN at (11,10)|enemy HP:58 -> 58/100|your HP:100 -> 100 "
    "CS:36|est damage dealt:~33\n  Post-combat: ~25/58 (estimate — verify with get_units)"
)


def test_only_a_ranged_unit_with_a_melee_reply_is_a_failed_attack():
    assert attack_failed(build_unit(archer()), MELEE_FALLBACK)
    assert (
        attack_failed(build_unit(warrior()), REAL_MELEE) is None
    )  # Unchanged hp is not a failure.
    assert attack_failed(build_unit(warrior()), MELEE_HIT) is None
    assert attack_failed(build_unit(archer()), RANGED_HIT) is None


def melee_game(left, reply=REAL_MELEE):
    """A warrior beside a 58 hp barbarian. After the attack the threat read shows `left`."""
    game = MilitaryGame(
        [warrior(targets=[target(hp=58)])],
        [barbarian(hp=58)],
        cities=[city_info(garrison_unit="UNIT_WARRIOR")],
    )
    game.replies["attack"] = reply
    plain = game.attack_unit

    async def attack(unit_index, x, y):
        result = await plain(unit_index, x, y)
        game.data["threats"] = left
        return result

    game.attack_unit = attack
    return game


def test_a_melee_attack_with_an_unchanged_hp_reply_counts_and_a_kill_is_read_back(caplog):
    game = melee_game([])
    policy = MilitaryPolicy()
    with caplog.at_level(logging.INFO):
        run(policy, game)
    (hit,) = [d for d in policy.decisions if d.action == "attack"]
    assert "failed" not in hit.reason
    (row,) = offense_rows(caplog)
    assert row["attacks_barb"] == 1
    assert row["kills_barb"] == 1


def test_a_melee_attack_that_leaves_the_target_standing_is_not_a_kill(caplog):
    game = melee_game([barbarian(hp=25)])
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["attacks_barb"] == 1
    assert row["kills_barb"] == 0


def test_a_failed_attack_does_not_use_up_the_unit():
    game = MilitaryGame(
        [archer(targets=[target()])], [barbarian()], cities=[city_info(garrison_unit="UNIT_ARCHER")]
    )
    game.replies["attack"] = MELEE_FALLBACK
    policy = MilitaryPolicy()
    run(policy, game)
    assert not any(d.action == "attack" and "failed" not in d.reason for d in policy.decisions)
    assert policy.decisions  # It went on to another option, so there is more than one line.


def test_ranged_odds_use_ranged_strength_and_take_no_damage_from_a_melee_target():
    unit = build_unit(archer(combat_strength=13, ranged_strength=25))
    defender = Threat(BARBARIANS, "B", "UNIT_SLINGER", 11, 10, 100, 100, 7, 0, 1, True)
    odds = assess_attack(unit, Target("UNIT_SLINGER", 11, 10, 100), defender, W)
    assert odds.taken == 0
    assert odds.dealt == pytest.approx(expected_damage(25, 7 + W.assumed_defender_bonus))
    assert odds.dealt > expected_damage(13, 7 + W.assumed_defender_bonus)  # Not melee strength.


def test_a_split_refreshes_the_threats_so_a_revealed_enemy_can_be_attacked():
    game = MilitaryGame([archer(uid=2), archer(uid=3, targets=[target()])], [])
    policy = MilitaryPolicy()
    policy._fortified[2] = (10, 10)

    async def move(unit_index, x, y):
        game.actions.append(("move", unit_index, x, y))
        game.data["threats"] = [barbarian()]  # Seen only from the new tile.
        return f"MOVING_TO|{x},{y}|from:10,10|now_at:{x},{y}"

    game.move_unit = move
    run(policy, game)
    assert game.orders("attack")


# ----- a camp near our cities is a goal with its own force (issue 268) -----

CAMP_55_7 = tile(x=55, y=7, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])


def run20_turn100(extra_units=(), defender_cs=45, units=None):
    """Turn 100 of Run 20: a Warrior and two Archers in two cities, a camp five tiles off with a
    Man-at-Arms beside it. Thessalonica's lone Archer is its garrison; the two units in
    Constantinople are more than it needs."""
    from civ6_ai.policies.defence import DefenceState

    cities = [
        city_info(city_id=65536, name="Constantinople", x=50, y=7),
        city_info(city_id=65537, name="Thessalonica", x=50, y=11),
    ]
    units = [
        *(units if units is not None else [warrior(uid=1, x=50, y=7), archer(uid=2, x=50, y=7)]),
        archer(uid=3, x=50, y=11),
        *extra_units,
    ]
    game = camp_game(
        units,
        [barbarian(kind="UNIT_MAN_AT_ARMS", x=54, y=7, cs=defender_cs)],
        camp=CAMP_55_7,
        cities=cities,
        turn=100,
    )
    state = DefenceState()
    return game, MilitaryPolicy(defence=state), state


def test_a_camp_near_a_city_becomes_a_plan_when_nothing_is_spare(caplog):
    game, policy, state = run20_turn100()
    with caplog.at_level(logging.INFO, logger=military_module.log.name):
        run(policy, game)
    plan = state.camp_plan
    assert plan is not None and plan.target == (55, 7)
    assert not camp_orders(policy)  # Too few spare units: nothing is sent yet.
    assert plan.units_wanted >= 1 and not plan.met
    assert plan.needed > plan.available  # Attacks from simulated combat, not strength.
    assert plan.units_available == 1  # One of Constantinople's two is released.
    assert plan.eta >= plan.units_wanted * MilitarySettings().camp_build_turns
    lines = [r.message for r in caplog.records if "military_camp_plan" in r.message]
    assert len(lines) == 1 and '"target": [55, 7]' in lines[0] and '"needed"' in lines[0]


def test_the_plan_does_not_take_a_garrison_into_the_force():
    game, policy, state = run20_turn100()
    run(policy, game)
    # Constantinople keeps one of its two units; Thessalonica's lone Archer is not counted.
    assert state.camp_plan.units_available == 1
    assert not camp_orders(policy)


def test_the_plan_asks_for_a_melee_unit_when_the_force_has_none():
    game, policy, state = run20_turn100(units=[archer(uid=2, x=50, y=7), archer(uid=4, x=50, y=7)])
    run(policy, game)
    assert state.camp_plan.wants == "melee"
    game, policy, state = run20_turn100()  # With the Warrior, the force has a melee unit.
    run(policy, game)
    assert state.camp_plan.wants == "ranged"


def test_a_force_with_melee_and_strength_is_met_and_marches():
    extra = [
        warrior(uid=4, x=51, y=7, combat_strength=36),
        archer(uid=5, x=51, y=8),
        archer(uid=6, x=51, y=6),
        warrior(uid=7, x=50, y=8, combat_strength=30),
    ]
    game, policy, state = run20_turn100(extra)
    run(policy, game)
    assert state.camp_plan.met and state.camp_plan.wants == ""
    assert camp_orders(policy)  # The force is formed and sent at the camp.


def test_a_weak_defender_needs_a_smaller_force_than_a_strong_one():
    _, _, strong = run20_turn100()
    game, policy, weak = run20_turn100(defender_cs=10)
    run(policy, game)
    assert weak.camp_plan.needed < camp_attacks_needed(
        [], [man_at_arms(45)], W
    )  # Reference units, since the weak camp's force is on hand: fewer attacks.
    assert strong.camp_plan is None  # Not run yet.


def test_a_camp_beyond_the_goal_radius_is_no_goal():
    from civ6_ai.policies.defence import DefenceState

    state = DefenceState()
    far = tile(x=70, y=7, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])
    game = camp_game([warrior(uid=1, x=10, y=10)], [], camp=far)
    policy = MilitaryPolicy(MilitarySettings(camp_home_radius=40), defence=state)
    run(policy, game)
    assert state.camp_plan is None


def test_camps_are_ranked_by_cost_to_stand_over_cost_to_clear():
    from civ6_ai.policies.defence import DefenceState

    state = DefenceState()
    near = (13, 10)
    farther = (16, 10)
    tiles = {
        near: tile(x=13, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[]),
        farther: tile(x=16, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[]),
    }
    game = MilitaryGame([warrior(uid=1, x=10, y=10)], [])

    async def area(cx, cy, radius=2):
        return list(tiles.values())

    game.get_map_area = area
    run(MilitaryPolicy(defence=state), game)
    assert state.camp_plan.target == near
    # A guard on the near camp makes it dearer to clear than the one a few tiles further on.
    game2 = MilitaryGame(
        [warrior(uid=1, x=10, y=10)],
        [barbarian(kind="UNIT_MAN_AT_ARMS", x=13, y=11, cs=60)],
    )
    game2.get_map_area = area
    state2 = DefenceState()
    run(MilitaryPolicy(defence=state2), game2)
    assert state2.camp_plan.target == farther


def test_the_plan_is_cleared_when_no_camp_is_known():
    from civ6_ai.policies.defence import DefenceState

    state = DefenceState()
    state.camp_plan = object()
    game = MilitaryGame([warrior(uid=1)], [])
    run(MilitaryPolicy(defence=state), game)
    assert state.camp_plan is None


def test_plan_camp_force_counts_units_to_build():
    from civ6_ai.policies.military import plan_camp_force

    settings = MilitarySettings()
    thug = man_at_arms(45)
    ours = [build_unit(unit_info(unit_id=i, unit_index=i, combat_strength=20)) for i in (1, 2)]
    needed, have, wanted, kind = plan_camp_force(ours, [thug], W, settings)
    assert needed == camp_attacks_needed(ours, [thug], W) > have == 2 * W.camp_strike_turns
    assert wanted == min(settings.camp_max_build, math.ceil((needed - have) / W.camp_strike_turns))
    assert kind == "ranged"  # The force is all melee.
    assert plan_camp_force(ours, [], W, settings)[2] == 0


def test_the_force_is_sent_at_the_camp_the_plan_chose_not_the_nearest():
    from civ6_ai.policies.defence import DefenceState

    near, farther = (13, 10), (16, 10)
    tiles = [
        tile(x=x, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])
        for x in (13, 16)
    ]
    units = [
        warrior(uid=1, x=15, y=10, **STRONG),
        warrior(uid=2, x=15, y=11, **STRONG),
        warrior(uid=3),
    ]
    game = MilitaryGame(units, [barbarian(kind="UNIT_MAN_AT_ARMS", x=13, y=11, cs=60)])

    async def area(cx, cy, radius=2):
        return tiles

    game.get_map_area = area
    state = DefenceState()
    policy = MilitaryPolicy(defence=state)
    run(policy, game)
    assert state.camp_plan.target == farther and state.camp_plan.met
    assert {d.target for d in camp_orders(policy)} == {farther}
    assert near not in {d.target for d in camp_orders(policy)}


def test_a_unit_with_both_attacks_counts_as_melee_by_its_class():
    from civ6_ai.policies.military import _is_melee

    immortal = build_unit(
        unit_info(unit_type="UNIT_PERSIAN_IMMORTAL", combat_strength=35, ranged_strength=25)
    )
    archer_ = build_unit(unit_info(unit_type="UNIT_ARCHER", combat_strength=15, ranged_strength=25))
    assert _is_melee(immortal) and not _is_melee(archer_)


# ----- the camp campaign (issue 275) -----


def replace_id(unit, uid):
    return dataclasses.replace(unit, unit_id=uid, unit_index=uid)


def man_at_arms(cs, hp=100, ranged=0, x=1, y=1):
    return Threat(
        owner_id=-1, owner="Barbarians", unit_type="UNIT_MAN_AT_ARMS", x=x, y=y, hp=hp,
        max_hp=100, combat_strength=cs, ranged_strength=ranged, distance=1, hostile=True,
    )  # fmt: skip


def test_the_camp_defenders_are_the_strongest_group_of_the_last_ten_turns():
    # Iteration 1 saw 102, 0, 92, 184, 154, 62, 184 as defenders came and went from sight.
    window = MilitarySettings().camp_memory_turns
    memory, groups = [], []
    for turn, seen in enumerate([[45], [], [40], [45, 45], [45], [], [45, 45]], start=100):
        memory, group = remembered_defenders(memory, turn, [man_at_arms(c) for c in seen], window)
        groups.append(len(group))
    assert groups == [1, 1, 1, 2, 2, 2, 2]  # Never the empty turn.
    _, group = remembered_defenders(memory, 106 + window, [], window)
    assert [d.combat_strength for d in group] == [45, 45]  # Seen on turn 106: still in the window.
    _, group = remembered_defenders(memory, 107 + window, [], window)
    assert group == []  # Ten turns on, nothing is remembered: no 150 thirty turns later.


def test_the_need_is_attacks_from_simulated_combat_not_twice_the_strength():
    # Iteration 2's fixture: a Man-at-Arms (45) and a Spearman at the camp, our army of Archers,
    # Horsemen, Heavy Chariots and a Crossbowman, with three cities not short.
    guards = [man_at_arms(45), man_at_arms(36)]
    army = [
        build_unit(unit_info(unit_id=i, unit_index=i, unit_type=t, combat_strength=c,
                             ranged_strength=r))
        for i, (t, c, r) in enumerate(
            [("UNIT_ARCHER", 15, 25), ("UNIT_ARCHER", 15, 25), ("UNIT_CROSSBOWMAN", 20, 40),
             ("UNIT_HORSEMAN", 36, 0), ("UNIT_HEAVY_CHARIOT", 28, 0), ("UNIT_HORSEMAN", 36, 0)],
            start=1,
        )
    ]  # fmt: skip
    army += [replace_id(u, 10 + u.unit_id) for u in army]  # Twelve combat units, as at turn 130.
    old_need = 2.0 * (45 + W.assumed_defender_bonus + 36 + W.assumed_defender_bonus)
    needed = camp_attacks_needed(army, guards, W)
    assert old_need > 150  # The kind of need that was out of reach (102 to 279 in the log).
    assert 8 <= needed <= len(army) * W.camp_strike_turns  # Twelve units reach it in 3 turns.
    assert camp_force_ok(army, guards, W, 2, True)
    assert not camp_force_ok(army[:3], guards, W, 2, True)  # Too few units for that many attacks.
    assert camp_attacks_needed(army, [], W) == 0


def test_the_matchup_against_a_men_at_arms_rejects_the_heavy_chariot():
    # Iteration 1: the barbarians' Men-at-Arms (45) against our Heavy Chariot (28).
    chariot = camp_matchup("UNIT_HEAVY_CHARIOT", 45, 0, W)
    archer_odds = camp_matchup("UNIT_ARCHER", 45, 0, W)
    assert chariot == 0.0  # Loses one-on-one, and the force is not short of a melee unit.
    assert archer_odds is not None and archer_odds > 1.0  # Shoots and takes nothing back.
    assert camp_matchup("UNIT_HEAVY_CHARIOT", 45, 0, W, wants="melee") > 0  # To take the tile.
    assert camp_matchup("UNIT_ARCHER", 45, 40, W) < archer_odds  # A Crossbowman hits back.
    assert camp_matchup("UNIT_SCOUT", 45, 0, W) is None
    assert camp_matchup("UNIT_ARCHER", 0, 0, W) is None  # No defender known.


def test_the_remembered_defenders_stay_for_ten_turns_then_go():
    game, policy, state = run20_turn100()
    run(policy, game)
    seen = state.camp_plan
    assert seen.defender == 45 and seen.defenders == 1
    game.data["threats"] = []  # The Man-at-Arms walked out of sight.
    game.data["overview"] = overview(turn=101, num_cities=2)
    run(policy, game)
    assert state.camp_plan.needed == seen.needed and state.camp_plan.defender == 45
    game.data["overview"] = overview(
        turn=100 + MilitarySettings().camp_memory_turns + 1, num_cities=2
    )
    run(policy, game)
    assert state.camp_plan.defender == 0 and state.camp_plan.needed == 0


def camp_force_at(rallied_x, far_x):
    """Two strong warriors beside the rally ring and one far off, for a camp at (14, 10)."""
    return [
        warrior(uid=1, x=rallied_x, y=10, **STRONG),
        warrior(uid=2, x=rallied_x, y=11, **STRONG),
        warrior(uid=3, x=far_x, y=10, **STRONG),
        warrior(uid=4),  # The garrison.
    ]


def test_the_gathered_force_waits_until_it_meets_the_need():
    # The whole force (3 units, 9 attacks) is enough for a remembered defender needing 7, but the
    # two gathered (6) are not, so they wait and the third walks to the rally ring.
    game = camp_game(camp_force_at(11, 5), [])
    policy = MilitaryPolicy()
    policy._camp_memory[(14, 10)] = [(20, (man_at_arms(24),))]
    run(policy, game)
    waits = [d for d in camp_orders(policy) if "waiting" in d.reason]
    assert {d.unit_id for d in waits} == {1, 2}
    assert not policy._committed
    (move,) = [m for m in game.orders("move") if m[1] == 3]
    assert hex_distance(move[2], move[3], 14, 10) == MilitarySettings().camp_muster_radius


def test_the_force_commits_together_once_the_gathered_units_meet_the_need():
    game = camp_game(camp_force_at(11, 11), [])
    policy = MilitaryPolicy()
    policy._camp_memory[(14, 10)] = [(20, (man_at_arms(24),))]
    run(policy, game)
    assert (14, 10) in policy._committed
    assert not [d for d in camp_orders(policy) if "waiting" in d.reason]
    assert {m[1] for m in game.orders("move")} >= {1, 2, 3}  # All three go in.


def camp_wounded_game(hp, on_mission):
    game = camp_game(
        [
            warrior(
                uid=1, x=13, y=10, health=hp, combat_strength=60, targets=[target(x=14, y=11, hp=5)]
            )
        ],
        [barbarian(x=14, y=11, hp=5)],
        cities=[city_info(garrison_unit="UNIT_WARRIOR")],
    )
    policy = MilitaryPolicy()
    policy._camps[(14, 10)] = 19
    if on_mission:
        policy._mission[1] = ((14, 10), 19)
    return game, policy


def test_a_wounded_unit_of_the_campaign_pulls_back_instead_of_attacking():
    game, policy = camp_wounded_game(hp=49, on_mission=False)
    run(policy, game)
    assert game.orders("attack")  # A sure kill: a lone hurt unit takes it.
    game, policy = camp_wounded_game(hp=49, on_mission=True)
    run(policy, game)
    assert not game.orders("attack")  # Under camp_pullback_hp on the campaign: it heals.
    game, policy = camp_wounded_game(hp=60, on_mission=True)
    run(policy, game)
    assert game.orders("attack")  # Above the setting it fights on.


def test_a_campaign_that_loses_units_without_hurting_the_camp_is_dropped(caplog):
    game, policy, state = run20_turn100()
    for uid in (91, 92, 93):  # Three units of the mission that no longer exist.
        policy._mission[uid] = ((55, 7), 98)
    with caplog.at_level(logging.INFO, logger=military_module.log.name):
        run(policy, game)
    assert (55, 7) in policy._camp_aside  # Set aside, so defence comes first.
    assert state.camp_plan is None  # Production stops building for it.
    assert any("military_camp_suspended" in r.message for r in caplog.records)


def test_a_campaign_that_is_hurting_the_camp_is_kept_through_losses():
    game, policy, state = run20_turn100()
    for uid in (91, 92, 93):
        policy._mission[uid] = ((55, 7), 98)
    policy._camp_hit[(55, 7)] = 97  # One of our attacks hit its defenders lately.
    run(policy, game)
    assert (55, 7) not in policy._camp_aside
    assert state.camp_plan is not None


def test_a_melee_kill_is_not_confirmed_when_every_attacker_died(caplog):
    game = melee_game([])
    attack = game.attack_unit

    async def attack_and_die(unit_index, x, y):
        result = await attack(unit_index, x, y)
        game.data["units"] = []  # The attacker died; the target may only be out of sight.
        return result

    game.attack_unit = attack_and_die
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (row,) = offense_rows(caplog)
    assert row["attacks_barb"] == 1
    assert row["kills_barb"] == 0


# ----- issue 279: a force that commits, and says why it did not -----


def threatened_city(city_id, name, units_in=3, hostile=1):
    from civ6_ai.policies.defence import CityThreat

    return CityThreat(
        city_id=city_id, name=name, x=50, y=7, reasons=("barbarians",),
        hostile_units=hostile, defenders_in=units_in,
    )  # fmt: skip


def test_a_city_that_is_not_short_releases_all_but_one_unit_to_the_camp_force():
    units = [archer(uid=i, x=50, y=7) for i in (4, 5, 6)]
    game, policy, state = run20_turn100(extra_units=units)
    state.cities = {65536: threatened_city(65536, "Constantinople", units_in=5)}
    assert not state.cities[65536].short  # Five defenders against one hostile unit.
    run(policy, game)
    # Constantinople holds warrior, archer and three more: it keeps one, four are free.
    assert state.camp_plan.units_available == 4


def test_a_short_city_keeps_every_unit_even_with_a_camp_to_clear():
    units = [archer(uid=i, x=50, y=7) for i in (4, 5, 6)]
    game, policy, state = run20_turn100(extra_units=units)
    state.cities = {65536: threatened_city(65536, "Constantinople", units_in=2, hostile=6)}
    assert state.cities[65536].short
    run(policy, game)
    assert state.camp_plan.units_available == 0


def test_units_beside_a_city_that_is_not_short_are_available_but_not_beside_a_short_one():
    beside = [archer(uid=7, x=51, y=7), archer(uid=8, x=51, y=8)]
    game, policy, state = run20_turn100(extra_units=beside)
    state.cities = {65536: threatened_city(65536, "Constantinople", units_in=2, hostile=1)}
    run(policy, game)
    free = state.camp_plan.units_available
    game, policy, state = run20_turn100(extra_units=beside)
    state.cities = {65536: threatened_city(65536, "Constantinople", units_in=2, hostile=6)}
    run(policy, game)
    assert free - state.camp_plan.units_available >= 2


def hold_lines(caplog):
    return [
        json.loads(r.message.split(" ", 1)[1])
        for r in caplog.records
        if r.message.startswith("military_camp_hold")
    ]


def test_each_turn_the_force_does_not_commit_the_reason_need_gap_and_city_holds_are_logged(
    caplog,
):
    game, policy, state = run20_turn100()
    with caplog.at_level(logging.INFO, logger=military_module.log.name):
        run(policy, game)
    (row,) = hold_lines(caplog)
    assert row["turn"] == 100 and row["camp"] == [55, 7]
    assert row["reason"] and row["needed"] > row["available"]
    assert row["gap"] == pytest.approx(row["needed"] - row["available"])
    assert set(row["held"]) == {"Constantinople", "Thessalonica"}
    assert row["held"]["Constantinople"]["inside"] == 2
    assert row["held"]["Thessalonica"]["free"] == 0


def test_the_iteration_2_army_commits_once_the_need_comes_from_simulated_combat():
    # Man-at-Arms (45) and a Spearman at the camp, 3 cities not short, and an army of Archers,
    # Horsemen, Heavy Chariots and a Crossbowman: needed 102 to 279 and available 0 to 114 before.
    from civ6_ai.policies.defence import DefenceState

    cities = [
        city_info(city_id=65536 + i, name=n, x=x, y=7)
        for i, (n, x) in enumerate([("A", 50), ("B", 50), ("C", 50)])
    ]
    army = [
        archer(uid=1, x=52, y=7), archer(uid=2, x=52, y=8),
        warrior(uid=3, x=52, y=6, unit_type="UNIT_HORSEMAN", combat_strength=36),
        warrior(uid=4, x=52, y=9, unit_type="UNIT_HEAVY_CHARIOT", combat_strength=28),
        warrior(uid=5, x=51, y=7, unit_type="UNIT_HORSEMAN", combat_strength=36),
        archer(uid=6, x=51, y=8, ranged_strength=40, unit_type="UNIT_CROSSBOWMAN"),
        warrior(uid=7, x=51, y=6, unit_type="UNIT_HEAVY_CHARIOT", combat_strength=28),
        archer(uid=8, x=51, y=9), archer(uid=9, x=52, y=10),
        warrior(uid=10, x=50, y=7),
    ]  # fmt: skip
    game = camp_game(
        army,
        [barbarian(kind="UNIT_MAN_AT_ARMS", x=54, y=7, cs=45),
         barbarian(kind="UNIT_SPEARMAN", x=55, y=8, cs=36)],
        camp=CAMP_55_7, cities=cities, turn=130,
    )  # fmt: skip
    state = DefenceState()
    policy = MilitaryPolicy(defence=state)
    run(policy, game)
    assert state.camp_plan.met and state.camp_plan.needed <= state.camp_plan.available
    assert camp_orders(policy)


# ----- a camp that someone else cleared -----


def test_a_camp_cleared_by_someone_else_is_logged_released_and_counted_apart(caplog):
    policy = MilitaryPolicy()
    policy._mission[3] = ((14, 10), 20)
    policy._camps[(14, 10)] = 20
    unit = warrior(uid=3, x=12, y=10, **STRONG)
    game = MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)])
    with caplog.at_level(logging.INFO):
        run(policy, game)
        del policy._camps[(14, 10)]  # Vanished, none of our units near it.
        run(policy, MilitaryGame([warrior(uid=1), unit], [barbarian(x=13, y=10, cs=40)]))
    rows = offense_rows(caplog)
    assert [(r["camps_gone"], r["camps_cleared_by_us"]) for r in rows] == [(0, 0), (1, 0)]
    gone = [r.message for r in caplog.records if "military_camp_gone" in r.message]
    assert len(gone) == 1 and '"by": "other"' in gone[0]
    assert 3 not in policy._mission  # The force is released.


def test_a_camp_our_unit_took_counts_as_ours_not_as_gone(caplog):
    policy = MilitaryPolicy()
    policy._mission[3] = ((14, 10), 20)
    policy._camps[(14, 10)] = 20
    near = warrior(uid=3, x=14, y=10, **STRONG)
    with caplog.at_level(logging.INFO):
        run(policy, MilitaryGame([warrior(uid=1), near], []))
        del policy._camps[(14, 10)]  # Taken by the unit standing on it.
        run(policy, MilitaryGame([warrior(uid=1), near], []))
    rows = offense_rows(caplog)
    assert [(r["camps_gone"], r["camps_cleared_by_us"]) for r in rows][-1] == (0, 1)
    assert any('"by": "us"' in r.message for r in caplog.records)


def test_a_camp_that_vanishes_with_no_force_sent_is_still_noticed(caplog):
    game = camp_game(strong_pair(), [])
    policy = MilitaryPolicy()
    policy.threat_map = None
    with caplog.at_level(logging.INFO):
        run(policy, game)
        policy._camps.pop((14, 10), None)
        game2 = MilitaryGame([warrior(uid=1, x=2, y=2)], [])
        run(policy, game2)
    assert any('"by": "other"' in r.message for r in caplog.records)


def test_a_unit_merely_beside_a_camp_that_vanished_does_not_make_it_ours(caplog):
    policy = MilitaryPolicy()
    policy._mission[3] = ((14, 10), 20)
    policy._camps[(14, 10)] = 20
    beside = warrior(uid=3, x=13, y=10, **STRONG)
    with caplog.at_level(logging.INFO):
        run(policy, MilitaryGame([warrior(uid=1), beside], []))
        del policy._camps[(14, 10)]
        run(policy, MilitaryGame([warrior(uid=1), beside], []))
    assert any('"by": "other"' in r.message for r in caplog.records)


# ----- the camp target is kept while the force is on it (issue 295) -----

CAMP_58_11 = tile(x=58, y=11, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])


def two_camp_game(second=CAMP_58_11):
    game, policy, state = run20_turn100()
    camps = [CAMP_55_7, second]

    async def area(cx, cy, radius=2):
        return camps

    game.get_map_area = area
    return game, policy, state


def at_turn(game, turn, threats):
    game.data["overview"] = overview(turn=turn, num_cities=2)
    game.data["threats"] = threats


def test_the_target_does_not_flip_when_two_camps_alternate_in_rank(caplog):
    game, policy, state = two_camp_game()
    seen = [barbarian(kind="UNIT_MAN_AT_ARMS", x=54, y=7, cs=45)]
    other = [barbarian(kind="UNIT_MAN_AT_ARMS", x=57, y=11, cs=45)]
    run(policy, game)
    first = state.camp_plan.target
    policy._committed[first] = 100  # The force is going in.
    targets = []
    for turn, threats in zip(
        range(101, 109), [seen, [], other, [], seen, other, [], seen], strict=True
    ):
        at_turn(game, turn, threats)
        with caplog.at_level(logging.INFO, logger=military_module.log.name):
            run(policy, game)
        targets.append(state.camp_plan.target)
    assert set(targets) == {first}
    assert not [r for r in caplog.records if "military_camp_switch" in r.message]
    line = next(r.message for r in caplog.records if "military_camp_plan" in r.message)
    assert '"ranks"' in line and '"chosen_by": "kept: force is on it"' in line or "rank" in line


def test_the_rank_alone_may_change_the_target_while_no_force_is_on_a_camp(caplog):
    game, policy, state = two_camp_game()
    run(policy, game)
    first = state.camp_plan.target
    other = (58, 11) if first == (55, 7) else (55, 7)
    # A strong guard seen on the first camp makes the other cheaper to clear.
    at_turn(game, 101, [barbarian(kind="UNIT_MAN_AT_ARMS", x=first[0] - 1, y=first[1], cs=90)])
    with caplog.at_level(logging.INFO, logger=military_module.log.name):
        run(policy, game)
    assert state.camp_plan.target == other
    switch = [r.message for r in caplog.records if "military_camp_switch" in r.message]
    assert len(switch) == 1 and "rank decides" in switch[0]


def test_the_target_changes_when_the_camp_is_gone_and_the_switch_is_logged(caplog):
    game, policy, state = two_camp_game()
    run(policy, game)
    first = state.camp_plan.target
    policy._committed[first] = 100
    gone = tile(x=first[0], y=first[1], improvement="", owner_id=-1, units=[])
    rest = [c for c in (CAMP_55_7, CAMP_58_11) if (c.x, c.y) != first]

    async def area(cx, cy, radius=2):
        return [gone, *rest]

    game.get_map_area = area
    at_turn(game, 100 + MilitarySettings().camp_home_scan_turns, [])  # The next scan of the cities.
    with caplog.at_level(logging.INFO, logger=military_module.log.name):
        run(policy, game)
    assert state.camp_plan.target == (rest[0].x, rest[0].y)
    switch = [r.message for r in caplog.records if "military_camp_switch" in r.message]
    assert len(switch) == 1 and "gone" in switch[0]


def test_a_camp_at_a_city_pulls_the_force_off_a_far_one(caplog):
    near = tile(x=53, y=11, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])
    game, policy, state = two_camp_game(second=near)
    run(policy, game)
    policy._plan_target = (55, 7)
    policy._committed[(55, 7)] = 100
    at_turn(game, 101, [])
    with caplog.at_level(logging.INFO, logger=military_module.log.name):
        run(policy, game)
    assert state.camp_plan.target == (53, 11)
    switch = [r.message for r in caplog.records if "military_camp_switch" in r.message]
    assert len(switch) == 1 and "threatens a city" in switch[0]


def test_an_unseen_garrison_is_sized_at_the_default_strength_not_zero():
    game, policy, state = run20_turn100()
    game.data["threats"] = []  # Never seen: the camp is known only from the map.
    fogged = dataclasses.replace(CAMP_55_7, visibility="revealed")

    async def area(cx, cy, radius=2):
        return [fogged]

    game.get_map_area = area
    run(policy, game)
    plan = state.camp_plan
    assert plan.defender == MilitaryWeights().camp_unseen_strength and plan.needed > 0
