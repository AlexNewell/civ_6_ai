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
    assert reserve_for(2, -10) == reserve_for(2) + 10 * gold.DEFICIT_TURNS
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
        gold_amount=reserve + 50, production=[], units=[], tiles={65536: [ptile(1, 40)]}
    )
    run(tight)
    assert tight.actions == []
    roomy = GoldGame(
        gold_amount=reserve + 80, production=[], units=[], tiles={65536: [ptile(1, 40)]}
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
