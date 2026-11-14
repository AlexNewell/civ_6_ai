import asyncio
import logging

from civ_mcp import lua as lq

from civ6_ai.policies import gold
from civ6_ai.policies.gold import (
    MAX_PURCHASES,
    MAX_REFUSALS,
    MAX_TILES,
    MAX_UPGRADES,
    RESERVE_CAP,
    GoldPolicy,
    reserve_for,
)
from fixtures import civ_state as fx


class GoldGame(fx.FakeGame):
    def __init__(self, gold_amount=500, gold_per_turn=5.0, tiles=None, replies=None, **data):
        data.setdefault("threats", [])
        count = len(data["cities"]) if "cities" in data else 2
        data.setdefault(
            "overview", fx.overview(gold=gold_amount, gold_per_turn=gold_per_turn, num_cities=count)
        )
        super().__init__(**data)
        self.tiles = tiles or {}
        self.replies = replies or {}
        self.actions: list[tuple] = []

    def _reply(self, key, default):
        return self.replies.get(key, default)

    async def upgrade_unit(self, unit_id):
        self.actions.append(("upgrade", unit_id))
        return self._reply("upgrade", "OK:UPGRADED|UNIT_WARRIOR -> UNIT_SWORDSMAN")

    async def purchase_item(self, city_id, item_type, item_name, yield_type="YIELD_GOLD"):
        self.actions.append(("buy", city_id, item_type, item_name))
        return self._reply("buy", f"OK:PURCHASED|{item_name}|cost=1g (had 2g)")

    async def get_purchasable_tiles(self, city_id):
        return self.tiles.get(city_id, [])

    async def purchase_tile(self, city_id, x, y):
        self.actions.append(("tile", city_id, x, y))
        return self._reply("tile", f"OK:TILE_PURCHASED|({x},{y})|cost:1")


def run(game):
    asyncio.run(GoldPolicy().run(game))


def kinds(game):
    return [a[0] for a in game.actions]


def upgradable(unit_id, cost=60, **changes):
    return fx.unit_info(
        unit_id=unit_id,
        unit_index=unit_id,
        can_upgrade=True,
        upgrade_target="UNIT_SWORDSMAN",
        upgrade_cost=cost,
        **changes,
    )


def option(item, gold_cost, category="BUILDING", cost=60, turns=6):
    return lq.ProductionOption(category, item, cost, turns, gold_cost)


def ptile(x, cost, cls=None):
    return lq.PurchasableTile(x, 10, cost, "Grass", None, cls)


def test_reserve_grows_with_cities_and_deficit_and_is_capped():
    assert reserve_for(1) == gold.RESERVE_FLOOR + gold.RESERVE_PER_CITY
    assert reserve_for(5) > reserve_for(1)
    assert reserve_for(2, -5) == reserve_for(2) + 5 * gold.DEFICIT_TURNS
    assert reserve_for(50, -100) == RESERVE_CAP


def test_nothing_is_spent_at_or_below_the_reserve():
    reserve = reserve_for(2, 5.0)
    for amount in (reserve - 1, reserve):
        game = GoldGame(
            gold_amount=amount,
            units=[upgradable(1)],
            production=[option("BUILDING_LIBRARY", 10)],
            tiles={65536: [ptile(1, 1, "luxury")]},
        )
        run(game)
        assert game.actions == []


def test_spending_never_takes_gold_below_the_reserve():
    reserve = reserve_for(2, 5.0)
    game = GoldGame(
        gold_amount=reserve + 100,
        units=[upgradable(1, 60), upgradable(2, 60)],
        production=[option("BUILDING_LIBRARY", 50)],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    # Listed costs are 60, 60, 50 (a building) and 30 (a tile) against 100 spendable.
    prices = {"upgrade": 60, "buy": 50, "tile": 30}
    assert sum(prices[a[0]] for a in game.actions) <= 100
    assert game.actions  # Something is bought, so the check above is not vacuous.


def test_upgrades_come_before_purchases_before_tiles():
    game = GoldGame(
        units=[upgradable(1)],
        production=[option("BUILDING_LIBRARY", 50)],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    order = kinds(game)
    assert order[0] == "upgrade" and order[-1] == "tile" and "buy" in order
    assert order == sorted(order, key=["upgrade", "buy", "tile"].index)


def test_upgrade_goes_to_the_unit_nearest_a_hostile_unit():
    near = upgradable(1, x=11, y=11)
    far = upgradable(2, x=40, y=30)
    game = GoldGame(units=[far, near], threats=[fx.threat_info(x=12, y=11)], production=[])
    run(game)
    assert game.actions[0] == ("upgrade", 1)


def test_upgrade_skips_units_that_cannot_upgrade_or_cost_too_much():
    cannot = fx.unit_info(unit_id=1, unit_index=1)
    dear = upgradable(2, cost=10_000)
    game = GoldGame(units=[cannot, dear], production=[])
    run(game)
    assert game.actions == []


def test_upgrades_are_bounded_per_turn():
    game = GoldGame(gold_amount=5000, units=[upgradable(i, 10) for i in range(1, 9)], production=[])
    run(game)
    assert kinds(game).count("upgrade") == MAX_UPGRADES


def test_purchases_are_bounded_per_turn_and_one_per_city():
    cities = [fx.city_info(city_id=65536 + i, name=f"C{i}", x=10 + 3 * i) for i in range(6)]
    game = GoldGame(
        gold_amount=5000,
        cities=cities,
        production=[option("BUILDING_LIBRARY", 40), option("BUILDING_MARKET", 40)],
        units=[],
    )
    run(game)
    buys = [a for a in game.actions if a[0] == "buy"]
    assert len(buys) == MAX_PURCHASES
    assert len({a[1] for a in buys}) == len(buys)


def test_threatened_city_buys_defence_first():
    game = GoldGame(
        gold_amount=1000,
        threats=[fx.threat_info(x=11, y=10, distance=1)],
        production=[option("BUILDING_LIBRARY", 40), option("UNIT_ARCHER", 40, "UNIT", turns=3)],
        units=[],
    )
    run(game)
    first = next(a for a in game.actions if a[0] == "buy")
    assert first[3] == "UNIT_ARCHER"


def test_builder_production_chose_can_still_be_bought():
    game = GoldGame(
        gold_amount=500,
        cities=[
            fx.city_info(currently_building="UNIT_BUILDER", unimproved_resources=[]),
            fx.city_info(city_id=65537, name="Beta", x=30, y=20),
            fx.city_info(city_id=65538, name="Gamma", x=40, y=20),
        ],
        production=[option("UNIT_BUILDER", 50, "UNIT", turns=4), option("BUILDING_MONUMENT", 40)],
        units=[fx.unit_info(unit_type="UNIT_WARRIOR")],
    )
    run(game)
    assert ("buy", 65536, "UNIT", "UNIT_BUILDER") in game.actions


def test_items_that_cannot_be_bought_are_skipped():
    game = GoldGame(
        production=[
            option("BUILDING_GREAT_LIBRARY", -1),
            option("DISTRICT_CAMPUS", 100, "DISTRICT"),
            option("BUILDING_LIBRARY", 50),
        ],
        units=[],
    )
    run(game)
    items = {a[3] for a in game.actions if a[0] == "buy"}
    assert items == {"BUILDING_LIBRARY"}


def test_failed_purchase_strings_are_refusals_and_are_logged(caplog):
    caplog.set_level(logging.INFO, logger=gold.log.name)
    game = GoldGame(
        replies={
            "buy": "ERR:CANNOT_PURCHASE|costs 50g but you only have 10g",
            "upgrade": "ERR:CANNOT_UPGRADE|UNIT_WARRIOR -> UNIT_SWORDSMAN | cost:60g have:10g",
            "tile": "ERR:INSUFFICIENT_GOLD|Need 30 gold, have 10",
        },
        units=[upgradable(1)],
        production=[option("BUILDING_LIBRARY", 50)],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    text = caplog.text
    assert "refused" in text and "CANNOT_PURCHASE" in text and "CANNOT_UPGRADE" in text
    assert "INSUFFICIENT_GOLD" in text
    assert "nothing bought" in text


def test_a_run_of_refusals_stops_after_the_bound():
    game = GoldGame(
        gold_amount=5000,
        replies={"upgrade": "ERR:CANNOT_UPGRADE|no", "buy": "Error: no"},
        units=[upgradable(i, 10) for i in range(1, 20)],
        production=[option("BUILDING_LIBRARY", 40)],
    )
    run(game)
    assert len(game.actions) <= MAX_REFUSALS + 1


def test_an_exception_from_a_call_is_a_refusal():
    class Raises(GoldGame):
        async def purchase_item(self, *args, **kwargs):
            raise RuntimeError("boom")

    run(Raises(production=[option("BUILDING_LIBRARY", 40)], units=[]))


def test_tiles_come_last_resource_first_and_are_bounded():
    game = GoldGame(
        gold_amount=2000,
        production=[],
        units=[],
        tiles={
            65536: [ptile(1, 10), ptile(2, 90, "bonus"), ptile(3, 90, "luxury")],
            65537: [ptile(4, 20, "strategic")],
        },
        cities=[fx.city_info(), fx.city_info(city_id=65537, name="Beta", x=30, y=20)],
    )
    run(game)
    tiles = [a for a in game.actions if a[0] == "tile"]
    assert 0 < len(tiles) <= MAX_TILES
    assert tiles[0][2] == 3


def test_plain_tile_needs_budget_to_spare():
    reserve = reserve_for(2, 5.0)
    tight = GoldGame(
        gold_amount=reserve + 30, production=[], units=[], tiles={65536: [ptile(1, 40)]}
    )
    run(tight)
    assert tight.actions == []
    roomy = GoldGame(
        gold_amount=reserve + 45, production=[], units=[], tiles={65536: [ptile(1, 40)]}
    )
    run(roomy)
    assert kinds(roomy) == ["tile"]


def test_nothing_bought_above_reserve_is_logged_with_a_reason(caplog):
    caplog.set_level(logging.INFO, logger=gold.log.name)
    game = GoldGame(production=[], units=[])
    run(game)
    assert game.actions == []
    assert "above the reserve" in caplog.text and "nothing bought" in caplog.text


def test_every_spend_is_logged(caplog):
    caplog.set_level(logging.INFO, logger=gold.log.name)
    game = GoldGame(
        units=[upgradable(1)],
        production=[option("BUILDING_LIBRARY", 50)],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    for phrase in ("Upgraded", "Bought BUILDING_LIBRARY", "Bought tile"):
        assert phrase in caplog.text


def test_a_turn_with_nothing_to_spend_reads_only_the_overview():
    reserve = reserve_for(2, 5.0)
    game = GoldGame(gold_amount=reserve)
    run(game)
    assert game.calls == ["get_game_overview"]


def test_a_turn_with_gold_to_spend_still_reads_the_full_empire():
    game = GoldGame(gold_amount=reserve_for(2, 5.0) + 100)
    run(game)
    assert {"get_policies", "get_governors", "get_trade_routes"} <= set(game.calls)


def test_the_spent_gold_total_is_logged_whether_or_not_anything_was_bought(caplog):
    caplog.set_level(logging.INFO, logger=gold.log.name)
    reserve = reserve_for(2, 5.0)
    run(GoldGame(gold_amount=reserve + 100, units=[], production=[], tiles={}))
    assert "gold_spent turn total: 0 of 100 to spend" in caplog.text
    caplog.clear()
    run(GoldGame(gold_amount=reserve + 100, units=[upgradable(1, 60)], production=[], tiles={}))
    assert "gold_spent turn total: 60 of 100 to spend" in caplog.text


def test_upgrade_reply_without_ok_prefix_is_a_success(caplog):
    # Real reply, from the logs: the game sends `UPGRADED|UNIT_WARRIOR -> UNIT_SWORDSMAN` bare.
    caplog.set_level(logging.INFO, logger=gold.log.name)
    game = GoldGame(
        replies={"upgrade": "UPGRADED|UNIT_WARRIOR -> UNIT_SWORDSMAN"},
        units=[upgradable(1)],
        production=[],
    )
    run(game)
    assert "Upgraded UNIT_WARRIOR" in caplog.text
    assert "refused" not in caplog.text


# ----- a lower reserve and saving (issue 87) -----


def test_income_lowers_the_reserve_but_not_below_the_minimum():
    assert reserve_for(8, 20.0) < reserve_for(8, 0.0)
    assert reserve_for(8, 1000.0) == gold.RESERVE_MIN
    assert reserve_for(1, 1000.0) == gold.RESERVE_MIN
    assert reserve_for(8) <= RESERVE_CAP
    assert reserve_for(8) < 60 + 20 * 8  # Lower than the old 60 + 20 a city.


def test_gold_below_one_price_is_saved_for_the_upgrade_not_spent_on_tiles(caplog):
    caplog.set_level(logging.INFO, logger=gold.log.name)
    reserve = reserve_for(2, 20.0)
    game = GoldGame(
        gold_amount=reserve + 60,
        gold_per_turn=20.0,
        units=[upgradable(1, 100)],
        production=[],
        tiles={65536: [ptile(1, 50, "luxury")]},
    )
    run(game)
    assert game.actions == []
    assert "saving toward UNIT_" in caplog.text  # About 2 turns at 20 a turn.


def test_saving_for_a_building_holds_back_tiles_but_the_building_is_bought_when_reached():
    reserve = reserve_for(2, 20.0)
    kwargs = dict(
        gold_per_turn=20.0,
        units=[],
        production=[option("BUILDING_LIBRARY", 100)],
        tiles={65536: [ptile(1, 50, "luxury")]},
    )
    saving = GoldGame(gold_amount=reserve + 60, **kwargs)
    run(saving)
    assert saving.actions == []
    reached = GoldGame(gold_amount=reserve + 100, **kwargs)
    run(reached)
    assert kinds(reached)[0] == "buy"


def test_a_target_too_far_to_reach_does_not_stop_other_spending():
    reserve = reserve_for(2, 5.0)
    game = GoldGame(
        gold_amount=reserve + 60,
        gold_per_turn=5.0,
        units=[upgradable(1, 400)],  # 68 turns away at 5 a turn.
        production=[],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    assert kinds(game) == ["tile"]


def test_no_saving_without_income():
    reserve = reserve_for(2, 0.0)
    game = GoldGame(
        gold_amount=reserve + 60,
        gold_per_turn=0.0,
        units=[upgradable(1, 100)],
        production=[],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    assert kinds(game) == ["tile"]


def test_a_threatened_city_still_buys_defence_while_gold_is_saved_for_an_upgrade():
    from civ6_ai.policies.defence import CityThreat, DefenceState

    defence = DefenceState()
    defence.cities = {65536: CityThreat.__new__(CityThreat)}
    game = GoldGame(
        gold_amount=300,
        gold_per_turn=60.0,
        units=[upgradable(1, 400)],  # Reachable in 2 turns, so it is the saving target.
        production=[option("UNIT_ARCHER", 40, category="UNIT")],
    )
    policy = GoldPolicy(defence=defence)
    asyncio.run(policy.run(game))
    assert ("buy", 65536, "UNIT", "UNIT_ARCHER") in game.actions
    assert ("upgrade", 1) not in game.actions


def test_a_saving_target_stops_lesser_purchases_in_the_same_city():
    reserve = reserve_for(2, 20.0)
    game = GoldGame(
        gold_amount=reserve + 60,
        gold_per_turn=20.0,
        units=[],
        production=[option("BUILDING_UNIVERSITY", 100), option("BUILDING_MONUMENT", 55)],
    )
    run(game)
    assert [a for a in game.actions if a[0] == "buy"] == []


# ----- saving leaves room for cheap buys (Run 9: nothing bought on 78% of turns above reserve) -


def test_a_cheap_buy_within_the_slack_goes_ahead_of_the_saving_target():
    reserve = reserve_for(2, 20.0)
    game = GoldGame(
        gold_amount=reserve + 60,
        gold_per_turn=20.0,
        units=[],
        production=[option("BUILDING_UNIVERSITY", 100), option("BUILDING_LIBRARY", 30)],
    )
    run(game)
    assert ("buy", 65536, "BUILDING", "BUILDING_LIBRARY") in game.actions


def test_the_slack_is_for_the_whole_wait_so_saving_still_ends():
    reserve = reserve_for(2, 20.0)
    policy = GoldPolicy()
    spent = 0
    for _ in range(3):  # Same target each turn, 40 gold of slack in all.
        game = GoldGame(
            gold_amount=reserve + 60,
            gold_per_turn=20.0,
            units=[],
            production=[option("BUILDING_UNIVERSITY", 100), option("BUILDING_LIBRARY", 30)],
        )
        asyncio.run(policy.run(game))
        spent += 30 * len([a for a in game.actions if a[3] == "BUILDING_LIBRARY"])
    assert spent == 30  # The second and third turns have 10 of slack left, which is too little.


def test_a_tile_within_the_slack_is_bought_while_saving():
    reserve = reserve_for(2, 20.0)
    game = GoldGame(
        gold_amount=reserve + 60,
        gold_per_turn=20.0,
        units=[upgradable(1, 100)],
        production=[],
        tiles={65536: [ptile(1, 30, "luxury")]},
    )
    run(game)
    assert kinds(game) == ["tile"]


# ----- builders and stacking (Run 9) -----


def test_no_builder_is_bought_while_a_builder_has_nothing_to_do():
    from civ6_ai.policies.builder import BuilderDemand

    builder = fx.unit_info(unit_id=9, unit_index=9, unit_type="UNIT_BUILDER", combat_strength=0)
    kwargs = dict(units=[builder], production=[option("UNIT_BUILDER", 40, category="UNIT")])
    idle = GoldGame(**kwargs)
    asyncio.run(GoldPolicy(builder_demand=BuilderDemand(idle=1, spare_jobs=3)).run(idle))
    assert [a for a in idle.actions if a[0] == "buy"] == []
    busy = GoldGame(**kwargs)
    asyncio.run(GoldPolicy(builder_demand=BuilderDemand(idle=0, spare_jobs=3)).run(busy))
    assert any(a[3] == "UNIT_BUILDER" for a in busy.actions)


STACKED = (
    "Error: STACKING_CONFLICT|Cannot purchase UNIT_MILITARY_ENGINEER - "
    "UNIT_MILITARY_ENGINEER (unit_id=77) is on the city tile. Move it first."
)


def test_a_stacking_refusal_is_not_retried_while_the_same_unit_stands_there():
    engineer = fx.unit_info(
        unit_id=77, unit_index=77, unit_type="UNIT_MILITARY_ENGINEER", x=10, y=10
    )
    policy = GoldPolicy()
    game = GoldGame(
        units=[engineer],
        production=[option("UNIT_MILITARY_ENGINEER", 50, category="UNIT")],
        replies={"buy": STACKED},
    )
    asyncio.run(policy.run(game))
    first = len([a for a in game.actions if a[0] == "buy"])
    assert first >= 1
    game.actions.clear()
    asyncio.run(policy.run(game))
    assert [a for a in game.actions if a[1] == 65536] == []  # Not retried where it is blocked.
    assert [a for a in game.actions if a[1] == 65537]  # A city whose tile is free is tried.
    # The unit leaves the tile: the item is eligible again (it is not forced).
    game.data["units"] = [
        fx.unit_info(unit_id=77, unit_index=77, unit_type="UNIT_MILITARY_ENGINEER", x=14, y=14)
    ]
    game.actions.clear()
    asyncio.run(policy.run(game))
    assert any(a[1] == 65536 for a in game.actions)


# ----- military readiness (Run 10) -----


def _tile(x, y, **changes):
    base = dict(units=None, own_units=None, resource=None)
    return fx.tile(x=x, y=y, **{**base, **changes})


class WarGame(GoldGame):
    """A threatened city 65536 at (10,10), with the area around it and recorded moves."""

    def __init__(self, units, production, move_reply=None, tiles=None, **data):
        area = tiles if tiles is not None else [
            _tile(10, 10), _tile(11, 10), _tile(9, 10), _tile(10, 11), _tile(10, 9),
        ]  # fmt: skip
        super().__init__(
            gold_amount=data.pop("gold_amount", 1200),
            units=units,
            production=production,
            **data,
        )
        self.data["tiles"] = {(10, 10): area}
        self.move_reply = move_reply
        self.moves: list[tuple] = []

    async def purchase_item(self, city_id, item_type, item_name, yield_type="YIELD_GOLD"):
        for unit in self.data["units"]:
            if city_id == 65536 and (unit.x, unit.y) == (10, 10) and unit.combat_strength:
                self.actions.append(("refused", city_id, item_name))
                return (
                    f"Error: STACKING_CONFLICT|Cannot purchase {item_name} - "
                    f"{unit.unit_type} (unit_id={unit.unit_id}) is on the city tile."
                )
        return await super().purchase_item(city_id, item_type, item_name, yield_type)

    async def move_unit(self, unit_index, x, y):
        self.moves.append((unit_index, x, y))
        if self.move_reply is not None:
            return self.move_reply
        self.data["units"] = [
            u if u.unit_index != unit_index else _moved(u, x, y) for u in self.data["units"]
        ]
        return f"MOVING_TO|{x},{y}|from:10,10|now_at:{x},{y}|(moved)"


def _moved(unit, x, y):
    from dataclasses import replace

    return replace(unit, x=x, y=y)


def _defence(city_id=65536, hostile=3):
    from civ6_ai.policies.defence import CityThreat, DefenceState

    state = DefenceState()
    state.cities = {city_id: CityThreat(city_id, "Alpha", 10, 10, ("war:X",), hostile, 1, 0)}
    return state


def _holder(unit_id, unit_type="UNIT_CROSSBOWMAN", moves=2.0, x=10, y=10):
    return fx.unit_info(
        unit_id=unit_id, unit_index=unit_id, unit_type=unit_type, x=x, y=y,
        moves_remaining=moves, combat_strength=30, ranged_strength=40,
    )  # fmt: skip


def _run_war(game):
    policy = GoldPolicy(defence=_defence())
    asyncio.run(policy.run(game))
    return policy


def test_a_blocked_defender_purchase_moves_the_occupant_aside_and_buys():
    game = WarGame(
        units=[_holder(5)], production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")]
    )
    _run_war(game)
    assert len(game.moves) == 1 and game.moves[0][0] == 5
    assert game.moves[0][1:] != (10, 10)
    assert ("buy", 65536, "UNIT", "UNIT_FIELD_CANNON") in game.actions
    assert game.actions.index(("buy", 65536, "UNIT", "UNIT_FIELD_CANNON")) >= 0


def test_the_occupant_goes_to_the_owned_tile_farthest_from_the_enemy():
    game = WarGame(
        units=[_holder(5)],
        production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")],
        threats=[fx.threat_info(x=12, y=10)],  # A barbarian, hostile.
    )
    _run_war(game)
    assert game.moves and game.moves[0][1:] == (9, 10)  # West, away from the unit at (12,10).


def test_no_room_is_made_outside_a_threatened_city():
    game = WarGame(
        units=[_holder(5)], production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")]
    )
    asyncio.run(GoldPolicy().run(game))  # No defence state: nothing marks the city.
    assert game.moves == []


def test_a_unit_with_no_moves_is_left_and_the_purchase_goes_elsewhere():
    game = WarGame(
        units=[_holder(5, moves=0.0)],
        production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")],
    )
    _run_war(game)
    assert game.moves == []
    assert not any(a[0] == "buy" and a[1] == 65536 for a in game.actions)


def test_a_refused_move_does_not_buy_and_is_not_repeated_this_turn():
    game = WarGame(
        units=[_holder(5)],
        production=[
            option("UNIT_FIELD_CANNON", 880, category="UNIT"),
            option("UNIT_CAVALRY", 880, category="UNIT"),
        ],
        move_reply="Error: PATH_BLOCKED|no path",
    )
    _run_war(game)
    assert len(game.moves) == 1


def test_tiles_that_are_foreign_water_or_taken_are_not_chosen():
    game = WarGame(
        units=[_holder(5), _holder(6, x=11, y=10)],
        production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")],
        tiles=[
            _tile(10, 10),
            _tile(11, 10),  # Another land unit stands here.
            _tile(9, 10, terrain="TERRAIN_COAST"),
            _tile(10, 11, owner_id=99),
            _tile(10, 9, units=["Rome WARRIOR"]),
        ],
    )
    _run_war(game)
    assert game.moves == []  # No safe tile, so nothing moves and nothing is forced.


def test_a_second_occupant_is_also_moved_and_more_than_two_are_not():
    one_each = WarGame(
        units=[_holder(5), _holder(6)],
        production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")],
    )
    _run_war(one_each)
    assert {m[0] for m in one_each.moves} == {5, 6}
    three = WarGame(
        units=[_holder(5), _holder(6), _holder(7)],
        production=[option("UNIT_FIELD_CANNON", 880, category="UNIT")],
    )
    _run_war(three)
    assert three.moves == []


def test_a_threatened_city_buys_the_strongest_unit_not_the_fastest():
    from civ6_ai.policies.gold import strongest_first
    from civ6_ai.state.reader import build_production

    options = build_production(
        [
            option("UNIT_CROSSBOWMAN", 300, category="UNIT", turns=3),
            option("BUILDING_WALLS", 200),
            option("UNIT_PIKE_AND_SHOT", 400, category="UNIT", turns=6),
            option("UNIT_AT_CREW", 5000, category="UNIT", turns=9),
        ]
    )
    order = [o.item for o in strongest_first(options, budget=1000)]
    assert order == ["UNIT_PIKE_AND_SHOT", "BUILDING_WALLS", "UNIT_CROSSBOWMAN", "UNIT_AT_CREW"]
    # The strongest the budget covers wins; the unaffordable one goes last of the units.
    assert [o.item for o in strongest_first(options, budget=10_000)][0] == "UNIT_AT_CREW"


def test_the_upgrade_note_says_how_many_units_had_no_moves(caplog):
    caplog.set_level(logging.INFO, logger="civ6_ai.policies.gold")
    game = GoldGame(
        units=[_holder(5, moves=0.0, x=30, y=30)],
        production=[],
    )
    asyncio.run(GoldPolicy().run(game))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "no unit can upgrade (1 units with an upgrade path, 1 with no moves left" in text
