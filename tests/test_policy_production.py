import asyncio
import random
from dataclasses import replace

import pytest
from civ_mcp import lua as lq

from civ6_ai.blockers import BlockerResolver, BlockerType
from civ6_ai.orchestrator import Orchestrator, StopReason, TurnLog
from civ6_ai.policies.production import (
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    drop_dominated,
    rank_production,
    score,
)
from civ6_ai.state import ProductionChoice, Threat
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx


def city(threats=(), **changes):
    return build_city(fx.city_info(**changes), threats, set())


def option(category, item, turns=5, cost=50, **changes):
    return ProductionChoice(category, item, cost, turns, **changes)


def threat(**changes):
    base = dict(
        owner_id=fx.BARBARIANS,
        owner="Barbarians",
        unit_type="UNIT_WARRIOR",
        x=12,
        y=11,
        hp=100,
        max_hp=100,
        combat_strength=8,
        ranged_strength=0,
        distance=2,
        hostile=True,
    )
    return Threat(**{**base, **changes})


EARLY = ProductionContext(turn=3, num_cities=1)
MID = ProductionContext(turn=120, num_cities=6, builders=6, recon=1, army=9)

MENU = [
    option("UNIT", "UNIT_WARRIOR", 4),
    option("UNIT", "UNIT_SETTLER", 8),
    option("UNIT", "UNIT_BUILDER", 5),
    option("UNIT", "UNIT_SCOUT", 3),
    option("BUILDING", "BUILDING_MONUMENT", 5),
    option("BUILDING", "BUILDING_LIBRARY", 9),
    option("BUILDING", "BUILDING_WALLS", 10),
]


def score_of(item, city_, ctx=MID):
    return score(next(o for o in MENU if o.item == item), city_, ctx, ProductionWeights())


def best(city_, options, ctx, weights=None):
    return rank_production(city_, options, ctx, weights)[0].item


def test_early_city_without_a_garrison_builds_a_defender_first():
    c = city(garrison_unit="", population=1, buildings=["PALACE"])
    assert best(c, MENU, EARLY) == "UNIT_WARRIOR"


def test_early_garrisoned_city_expands_once_it_can():
    small = city(population=1, buildings=["PALACE"])
    grown = city(population=3, buildings=["PALACE"])
    assert best(small, MENU, EARLY) != "UNIT_SETTLER"
    assert best(grown, MENU, EARLY) == "UNIT_SETTLER"


def test_expansion_stops_at_target_and_while_a_settler_is_already_out():
    grown = city(population=4)
    capped = ProductionContext(turn=40, num_cities=6, builders=6, recon=1, army=9)
    assert best(grown, MENU, capped) != "UNIT_SETTLER"
    out = ProductionContext(turn=40, num_cities=2, settlers=1, builders=2, recon=1, army=3)
    assert best(grown, MENU, out) != "UNIT_SETTLER"


def test_mid_game_city_builds_infrastructure_by_weight():
    c = city(buildings=["PALACE", "MONUMENT"])
    menu = [m for m in MENU if m.item != "BUILDING_MONUMENT"]
    assert best(c, menu, MID) == "BUILDING_LIBRARY"
    culture = [
        option("BUILDING", "BUILDING_MONUMENT", 5),
        option("BUILDING", "BUILDING_LIBRARY", 5),
    ]
    c = city(buildings=["PALACE"])
    assert best(c, culture, MID) == "BUILDING_LIBRARY"
    leaning = ProductionWeights(culture=5.0, science=0.2)
    assert best(c, culture, MID, leaning) == "BUILDING_MONUMENT"


def test_zero_weight_turns_a_kind_off_but_never_leaves_nothing():
    c = city()
    menu = [option("BUILDING", "BUILDING_LIBRARY", 9), option("BUILDING", "BUILDING_MARKET", 9)]
    ranked = rank_production(c, menu, MID, ProductionWeights(science=0.0, gold=0.0))
    assert [r.item for r in ranked] == ["BUILDING_LIBRARY", "BUILDING_MARKET"]  # Tied, by name.


def test_threatened_city_defends_before_anything_else():
    c = city(threats=[threat()], population=4)
    assert c.is_threatened
    first = rank_production(c, MENU, MID)[0]
    assert first.item in {"UNIT_WARRIOR", "BUILDING_WALLS"}
    ranked = [r.item for r in rank_production(c, MENU, MID)]
    assert ranked.index("UNIT_SETTLER") > ranked.index("BUILDING_LIBRARY") or (
        ranked.index("UNIT_SETTLER") > 1
    )
    walled = city(threats=[threat()], wall_hp=100, wall_max_hp=100)
    assert best(walled, MENU, MID) == "UNIT_WARRIOR"


def test_threat_prefers_ranged_defenders_and_the_best_of_a_line():
    c = city(threats=[threat()])
    menu = [
        option("UNIT", "UNIT_WARRIOR", 4),
        option("UNIT", "UNIT_SWORDSMAN", 6),
        option("UNIT", "UNIT_ARCHER", 5),
        option("BUILDING", "BUILDING_LIBRARY", 5),
    ]
    assert best(c, menu, MID) == "UNIT_ARCHER"
    kept, _ = drop_dominated(menu, c)
    assert "UNIT_WARRIOR" not in [k.item for k in kept]


def test_unthreatened_walls_are_a_last_resort():
    c = city()
    ranked = [r.item for r in rank_production(c, MENU, MID)]
    assert ranked[0] == "BUILDING_LIBRARY"
    assert score_of("BUILDING_WALLS", c) == 0.0
    assert ranked.index("BUILDING_LIBRARY") < ranked.index("BUILDING_WALLS")


def test_dominated_options_are_dropped():
    c = city(buildings=["PALACE", "MONUMENT"], districts=["DISTRICT_CITY_CENTER@10,10"])
    menu = [
        option("BUILDING", "BUILDING_MONUMENT"),  # Already built.
        option("BUILDING", "BUILDING_LIBRARY"),
        option("BUILDING", "BUILDING_LIBRARY"),  # Duplicate.
        option("DISTRICT", "DISTRICT_CITY_CENTER"),  # Already there.
        option("DISTRICT", "DISTRICT_SPACEPORT"),
        option("PROJECT", "PROJECT_BUILD_ROCKET"),
        option("UNIT", "UNIT_TRADER"),
        option("UNIT", "UNIT_SLINGER"),
        option("UNIT", "UNIT_ARCHER"),
        option("BUILDING", "BUILDING_X", is_repair=True),  # No coordinates to repair at.
        option("DISTRICT", "DISTRICT_CAMPUS", is_repair=True, repair_x=11, repair_y=10),
    ]
    kept, repairs = drop_dominated(menu, c)
    assert [k.item for k in kept] == ["BUILDING_LIBRARY", "UNIT_ARCHER"]
    assert [r.item for r in repairs] == ["DISTRICT_CAMPUS"]


def test_ranking_ignores_input_order():
    c = city()
    shuffled = list(MENU)
    random.Random(7).shuffle(shuffled)
    assert rank_production(c, MENU, MID) == rank_production(c, shuffled, MID)


def test_weights_are_validated():
    with pytest.raises(ValueError):
        ProductionWeights(science=-1.0)
    with pytest.raises(ValueError):
        ProductionWeights(gold=float("nan"))


# A scripted game for the handler itself.
class ScriptedGame(fx.FakeGame):
    def __init__(self, refuse=(), **overrides):
        overrides.setdefault("overview", fx.overview(num_cities=1))
        overrides.setdefault("threats", [])
        super().__init__(**overrides)
        self.refuse = dict(refuse)
        self.set_calls: list[tuple] = []
        self.districts = [
            lq.DistrictPlacement(5, 5, {}, 1, "Plains"),
            lq.DistrictPlacement(6, 6, {}, 3, "Hills"),
        ]
        self.wonders = [
            lq.WonderPlacement(9, 9, "TERRAIN_GRASS", "none", False, False, "none", "none", 4),
            lq.WonderPlacement(8, 8, "TERRAIN_GRASS", "none", False, False, "none", "none", 0),
        ]

    async def set_city_production(
        self, city_id, item_type, item_name, target_x=None, target_y=None
    ):
        self.set_calls.append((city_id, item_type, item_name, target_x, target_y))
        if item_name in self.refuse:
            return self.refuse[item_name]
        return f"PRODUCING|{item_name}|5"

    async def get_district_advisor(self, city_id, district_type):
        return self.districts

    async def get_wonder_advisor(self, city_id, wonder_name):
        return self.wonders


def idle_cities():
    return [fx.city_info(currently_building="nothing", buildings=["PALACE", "MONUMENT"])]


def run(coro):
    return asyncio.run(coro)


def test_handler_places_a_district_on_the_best_tile():
    options = [lq.ProductionOption("DISTRICT", "DISTRICT_THEATER", 54, 5, -1)]
    game = ScriptedGame(cities=idle_cities(), production=options)
    run(CityProductionPolicy().run(game))
    assert game.set_calls == [(65536, "DISTRICT", "DISTRICT_THEATER", 6, 6)]


def test_handler_finds_a_wonder_tile_after_the_game_asks_for_one():
    options = [lq.ProductionOption("BUILDING", "BUILDING_PYRAMIDS", 200, 9, -1)]
    refuse = {"BUILDING_PYRAMIDS": "ERR:MISSING_COORDS|BUILDING_PYRAMIDS is a wonder"}
    game = ScriptedGame(refuse=refuse, cities=idle_cities(), production=options)
    # The first call is refused; the retry carries coordinates, which the fake accepts.
    original = game.set_city_production

    async def accept_with_coords(city_id, item_type, item_name, x=None, y=None):
        if x is not None:
            game.set_calls.append((city_id, item_type, item_name, x, y))
            return "PRODUCING|BUILDING_PYRAMIDS|9"
        return await original(city_id, item_type, item_name, x, y)

    game.set_city_production = accept_with_coords
    run(CityProductionPolicy().run(game))
    assert game.set_calls[-1] == (65536, "BUILDING", "BUILDING_PYRAMIDS", 8, 8)


def test_handler_falls_through_refusals_to_the_next_option():
    options = [
        lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 9, -1),
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
    ]
    refuse = {"BUILDING_LIBRARY": "ERR:CANNOT_PRODUCE|BUILDING_LIBRARY"}
    game = ScriptedGame(refuse=refuse, cities=idle_cities(), production=options)
    unfinished = run(CityProductionPolicy().assign(game))
    assert unfinished == []
    assert [c[2] for c in game.set_calls] == ["BUILDING_LIBRARY", "UNIT_WARRIOR"]


def test_handler_reports_a_city_it_could_not_start_and_leaves_idle_ones_alone():
    options = [lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1)]
    game = ScriptedGame(
        refuse={"UNIT_WARRIOR": "Error: nope"}, cities=idle_cities(), production=options
    )
    assert run(CityProductionPolicy().assign(game)) == ["Alpha"]
    busy = ScriptedGame(production=options)
    run(CityProductionPolicy().run(busy))
    assert busy.set_calls == []  # Both fixture cities are already building.


def test_policy_takes_over_the_production_blocker_with_the_default_as_fallback():
    policy = CityProductionPolicy()
    resolver = BlockerResolver(policy.blocker_policies())
    assert BlockerType.PRODUCTION in resolver._policies
    options = [lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1)]
    game = ScriptedGame(cities=idle_cities(), production=options)
    assert run(policy.resolve(game, None)) is True


def test_weights_can_be_replaced_between_turns():
    options = [
        lq.ProductionOption("BUILDING", "BUILDING_MONUMENT", 40, 5, -1),
        lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 40, 5, -1),
    ]
    infos = [fx.city_info(currently_building="nothing", buildings=["PALACE"])]
    policy = CityProductionPolicy()
    first = ScriptedGame(cities=infos, production=options)
    run(policy.run(first))
    policy.weights = replace(policy.weights, culture=10.0)
    second = ScriptedGame(cities=infos, production=options)
    run(policy.run(second))
    assert first.set_calls[0][2] == "BUILDING_LIBRARY"
    assert second.set_calls[0][2] == "BUILDING_MONUMENT"


# A fake game that really builds things, for the 100-turn test.
CATALOGUE = {
    ("UNIT", "UNIT_WARRIOR"): 30,
    ("UNIT", "UNIT_SCOUT"): 20,
    ("UNIT", "UNIT_BUILDER"): 50,
    ("UNIT", "UNIT_SETTLER"): 80,
    ("BUILDING", "BUILDING_MONUMENT"): 40,
    ("BUILDING", "BUILDING_GRANARY"): 40,
    ("BUILDING", "BUILDING_LIBRARY"): 60,
    ("BUILDING", "BUILDING_WALLS"): 60,
    ("BUILDING", "BUILDING_MARKET"): 80,
    ("DISTRICT", "DISTRICT_CAMPUS"): 60,
}


class SimGame(ScriptedGame):
    """Cities make 8 production a turn. Finished buildings are removed from the menu."""

    def __init__(self):
        super().__init__()
        self.turn = 1
        self.cities = {1: self._new_city(1, "A"), 2: self._new_city(2, "B")}
        self.units = [("UNIT_WARRIOR", 1), ("UNIT_WARRIOR", 2)]
        self.next_id = 3
        self.idle_after_policy = 0
        self.never_idle_checks = 0

    @staticmethod
    def _new_city(city_id, name):
        return dict(id=city_id, name=name, building=None, progress=0, buildings=["PALACE"])

    async def check_game_over(self):
        return None

    async def get_game_overview(self):
        return fx.overview(turn=self.turn, num_cities=len(self.cities))

    async def get_cities(self):
        out = []
        for c in self.cities.values():
            out.append(
                fx.city_info(
                    city_id=c["id"],
                    name=c["name"],
                    x=10 + 5 * c["id"],
                    population=3,
                    currently_building=c["building"] or "nothing",
                    buildings=c["buildings"],
                    garrison_unit="UNIT_WARRIOR",
                    unimproved_resources=[],
                )
            )
        return out, []

    async def get_units(self):
        return [
            fx.unit_info(unit_id=i, unit_index=i, unit_type=t, name=t)
            for i, (t, _) in enumerate(self.units, 1)
        ]

    async def get_threat_scan(self):
        # A barbarian shows up next to city 1 on turns 40-50.
        return [fx.threat_info(x=15, y=11)] if 40 <= self.turn <= 50 else []

    async def list_city_production(self, city_id):
        have = self.cities[city_id]["buildings"]
        return [
            lq.ProductionOption(cat, name, cost, cost // 8, -1)
            for (cat, name), cost in CATALOGUE.items()
            if not (cat == "BUILDING" and name.removeprefix("BUILDING_") in have)
        ]

    async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
        if item_type == "DISTRICT" and x is None:
            return "ERR:MISSING_COORDS|district"
        self.cities[city_id]["building"] = item_name
        self.cities[city_id]["progress"] = 0
        self.set_calls.append((city_id, item_type, item_name, x, y))
        return f"PRODUCING|{item_name}|5"

    async def end_turn(self):
        for c in list(self.cities.values()):
            if c["building"] is None:
                continue
            c["progress"] += 8
            kind = "BUILDING" if c["building"].startswith("BUILDING_") else "UNIT"
            if c["building"].startswith("DISTRICT_"):
                kind = "DISTRICT"
            if c["progress"] >= CATALOGUE[(kind, c["building"])]:
                self._finish(c, kind)
        self.turn += 1
        return f"Turn {self.turn - 1} -> {self.turn}"

    def _finish(self, c, kind):
        name = c["building"]
        c["building"] = None
        if kind == "UNIT":
            self.units.append((name, c["id"]))
            if name == "UNIT_SETTLER":
                self.units.remove((name, c["id"]))
                self.cities[self.next_id] = self._new_city(self.next_id, f"C{self.next_id}")
                self.next_id += 1
        else:
            c["buildings"].append(name.removeprefix("BUILDING_").removeprefix("DISTRICT_"))


class IdleAudit:
    name = "idle_audit"

    def __init__(self, game):
        self.game = game
        self.idle_seen = 0
        self.cities_seen = 0

    async def run(self, game):
        cities, _ = await game.get_cities()
        self.cities_seen += len(cities)
        self.idle_seen += sum(c.currently_building == "nothing" for c in cities)


def test_no_city_sits_idle_in_a_100_turn_game(tmp_path):
    game = SimGame()
    audit = IdleAudit(game)
    policy = CityProductionPolicy()
    orchestrator = Orchestrator(game, [policy, audit], TurnLog(tmp_path / "turns.jsonl"))
    assert asyncio.run(orchestrator.play(100)) == StopReason.TURN_LIMIT
    assert audit.idle_seen == 0
    assert audit.cities_seen >= 200
    assert len(game.cities) > 2  # It expanded.
    assert all(not r.handler_errors for r in orchestrator._log.records)
    # The barbarian turns brought a defender or walls to city 1 at least once.
    assert any(
        call[0] == 1 and call[2] in {"UNIT_WARRIOR", "BUILDING_WALLS"} and call[2] is not None
        for call in game.set_calls
    )


def test_the_same_game_gives_the_same_choices():
    def play():
        game = SimGame()
        policy = CityProductionPolicy()
        for _ in range(60):
            asyncio.run(policy.run(game))
            asyncio.run(game.end_turn())
        return game.set_calls

    assert play() == play()
