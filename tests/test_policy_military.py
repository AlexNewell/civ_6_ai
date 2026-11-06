import asyncio
import logging
import math

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
    attack_landed,
    camp_force_ok,
    choose_promotion,
    expected_damage,
    garrison_required,
)
from civ6_ai.policies.settling import units_reserved_for_settling
from civ6_ai.state import read_cities, read_threats, read_units
from civ6_ai.state.models import Target, Threat
from civ6_ai.state.reader import build_unit
from fixtures.civ_state import (
    BARBARIANS,
    FakeGame,
    city_info,
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

    def did(self, name, *args, default=""):
        self.actions.append((name, *args))
        if name in self.raises:
            raise self.raises[name]
        return self.replies.get(name, default)

    async def move_unit(self, unit_index, target_x, target_y):
        return self.did(
            "move", unit_index, target_x, target_y,
            default=f"MOVING_TO|{target_x},{target_y}|from:0,0|now_at:{target_x},{target_y}",
        )  # fmt: skip

    async def attack_unit(self, unit_index, target_x, target_y):
        return self.did("attack", unit_index, target_x, target_y, default=RANGED_HIT)

    async def fortify_unit(self, unit_index):
        return self.did("fortify", unit_index, default="FORTIFIED")

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
    game = MilitaryGame([archer(health=40, targets=[target()]), warrior(uid=5)], [barbarian()])
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
    units = [warrior(uid=1, x=12, y=10), warrior(uid=2, x=14, y=10), warrior(uid=3, x=14, y=10)]
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


def test_a_heavily_threatened_city_asks_for_a_second_defender():
    threats = [barbarian(x=11, y=10 + i) for i in range(3)]
    game = MilitaryGame(
        [warrior(uid=1), warrior(uid=2, x=12, y=10), warrior(uid=3, x=40, y=40)], threats
    )
    run(MilitaryPolicy(), game)
    assert game.orders("move") == [("move", 2, 10, 10)]


def test_a_refused_garrison_move_tries_the_next_unit_and_stops():
    units = [warrior(uid=1, x=12, y=10), warrior(uid=2, x=13, y=10)]
    game = MilitaryGame(units, [barbarian(x=11, y=11)], cities=[city_info(garrison_unit="")])
    game.replies["move"] = "Error: STACKING_CONFLICT|Friendly UNIT_WARRIOR already on (10,10)."
    run(MilitaryPolicy(), game)
    assert len(game.orders("move")) == 2  # Both tried once, then no loop.


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
    # Warrior 1 (escort) is untouched. The crowd asks for two defenders, so 3 is called in.
    assert game.orders("move") == [("move", 3, 10, 10)]


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
    assert game.calls.count("get_map_area") == 1


def test_an_empty_camp_is_entered_not_staged_beside():
    game = camp_game(strong_pair(), [barbarian(x=15, y=9, cs=5)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert {d.target for d in camp_orders(policy)} == {(14, 10)}
    assert (("move", 1, 14, 10)) in game.orders("move")


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


def test_a_fogged_camp_is_not_acted_on():
    fogged = tile(x=14, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", visibility="revealed")
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5)], camp=fogged)
    policy = MilitaryPolicy()
    run(policy, game)
    assert not camp_orders(policy)


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


def test_the_camp_scan_runs_once_a_turn():
    game = camp_game(strong_pair(), [barbarian(x=15, y=10, cs=5), barbarian(x=15, y=11, cs=5)])
    run(MilitaryPolicy(), game)
    assert game.calls.count("get_map_area") == 1


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
    assert (14, 10) in policy._camp_turns
    game.data["units"] = []
    game.data["overview"] = overview(turn=20 + policy.settings.camp_idle_turns + 1, num_cities=1)
    run(policy, game)
    assert policy._camp_turns == {}
