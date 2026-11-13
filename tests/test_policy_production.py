import asyncio
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest
from civ_mcp import lua as lq

from civ6_ai.blockers import BlockerResolver, BlockerType
from civ6_ai.orchestrator import Orchestrator, StopReason, TurnLog
from civ6_ai.policies.production import (
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    build_context,
    drop_dominated,
    rank_production,
    score,
)
from civ6_ai.state import ProductionChoice, Threat, read_empire
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
    scouted = replace(EARLY, recon=1)
    assert best(small, MENU, scouted) != "UNIT_SETTLER"
    assert best(grown, MENU, scouted) == "UNIT_SETTLER"


def test_the_first_scout_comes_before_a_settler():
    # Live: with nothing exploring, a settler found no site on the 30-tile revealed map.
    grown = city(population=3, buildings=["PALACE"])
    assert best(grown, MENU, EARLY) == "UNIT_SCOUT"


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


def test_threat_picks_the_strongest_defender_by_the_games_numbers():
    c = city(threats=[threat()])
    menu = [
        option("UNIT", "UNIT_WARRIOR", 4),
        option("UNIT", "UNIT_SWORDSMAN", 6),
        option("UNIT", "UNIT_ARCHER", 5),
        option("BUILDING", "BUILDING_LIBRARY", 5),
    ]
    assert best(c, menu, MID) == "UNIT_SWORDSMAN"
    kept, _ = drop_dominated(menu, c)
    assert "UNIT_WARRIOR" not in [k.item for k in kept]  # Its upgrade is offered.


def test_threat_never_picks_siege_over_a_real_defender_by_name():
    c = city(threats=[threat()])
    menu = [
        option("UNIT", "UNIT_CATAPULT", 5),
        option("UNIT", "UNIT_ARCHER", 5),
        option("UNIT", "UNIT_BOMBARD", 5),
        option("UNIT", "UNIT_CROSSBOWMAN", 5),
    ]
    assert best(c, menu, MID) == "UNIT_CROSSBOWMAN"  # The archer upgrades to it.
    assert score(menu[0], c, MID, ProductionWeights()) == 0.0


def test_late_ranged_units_count_as_defenders():
    c = city(threats=[threat()])
    menu = [option("UNIT", "UNIT_FIELD_CANNON", 6), option("UNIT", "UNIT_SCOUT", 3)]
    assert best(c, menu, MID) == "UNIT_FIELD_CANNON"


def test_unique_and_unlisted_units_are_kept_and_scored():
    c = city(garrison_unit="")
    legion = option("UNIT", "UNIT_ROMAN_LEGION", 6)
    kept, _ = drop_dominated([legion], c)
    assert [k.item for k in kept] == ["UNIT_ROMAN_LEGION"]
    assert best(c, [legion, option("BUILDING", "BUILDING_X")], MID) == "UNIT_ROMAN_LEGION"
    extras = [option("UNIT", "UNIT_NEW_THING"), option("UNIT", "UNIT_MISSIONARY")]
    kept, _ = drop_dominated(extras, city())
    assert [k.item for k in kept] == ["UNIT_NEW_THING"]


def test_a_unique_unit_does_not_remove_its_neighbours():
    menu = [option("UNIT", "UNIT_JAPANESE_SAMURAI", 8), option("UNIT", "UNIT_MAN_AT_ARMS", 8)]
    kept, _ = drop_dominated(menu, city())
    assert len(kept) == 2


def test_wonders_use_the_wonders_weight():
    c = city()
    menu = [
        option("BUILDING", "BUILDING_PYRAMIDS", 10),
        option("BUILDING", "BUILDING_LIBRARY", 10),
    ]
    assert best(c, menu, MID, ProductionWeights(wonders=0.0)) == "BUILDING_LIBRARY"
    assert best(c, menu, MID, ProductionWeights(wonders=5.0)) == "BUILDING_PYRAMIDS"
    threatened = city(threats=[threat()])
    assert score(menu[0], threatened, MID, ProductionWeights(wonders=5.0)) == 0.0


def test_context_counts_what_busy_cities_are_building():
    empire = run(read_empire(fx.FakeGame()))
    ctx = build_context(empire, [], ["UNIT_SETTLER", "UNIT_BUILDER"])
    assert (ctx.settlers, ctx.builders) == (1, 1)
    grown = city(population=4)
    assert score(option("UNIT", "UNIT_SETTLER", 8), grown, ctx, ProductionWeights()) == 0.0


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
        option("UNIT", "UNIT_MISSIONARY"),
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
        count = len(overrides["cities"]) if "cities" in overrides else 2  # The fixture's two.
        overrides.setdefault("overview", fx.overview(num_cities=count))
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


@pytest.mark.parametrize(
    "reply",
    [
        "ERR:MISSING_COORDS|BUILDING_PYRAMIDS is a wonder",
        # The wording civ6-mcp really returns, seen live in Run 4 on every wonder.
        "Error: MISSING_COORDS|BUILDING_PYRAMIDS is a wonder and requires target_x/target_y",
    ],
)
def test_handler_finds_a_wonder_tile_after_the_game_asks_for_one(reply):
    options = [lq.ProductionOption("BUILDING", "BUILDING_PYRAMIDS", 200, 9, -1)]
    refuse = {"BUILDING_PYRAMIDS": reply}
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


class BlockerGame(ScriptedGame):
    """Adds the reads `BlockerResolver` makes. A production blocker is open while a city is idle."""

    async def execute_lua(self, code, context="gamecore"):
        cities, _ = await self.get_cities()
        idle = any(c.currently_building == "nothing" for c in cities)
        return f"BLOCKING|{BlockerType.PRODUCTION}|choose" if idle else "NONE"

    async def get_notifications(self):
        return []

    async def get_diplomacy_sessions(self):
        return []

    async def get_pending_deals(self):
        return []

    async def get_world_congress(self):
        return SimpleNamespace(turns_until_next=-1, is_in_session=False, resolutions=[])

    async def set_city_production(self, *args, **kwargs):
        result = await super().set_city_production(*args, **kwargs)
        if not result.startswith(("ERR", "Error")):
            infos, _ = await self.get_cities()
            self.data["cities"] = [replace(c, currently_building=args[2]) for c in infos]
        return result


def test_a_real_resolver_hands_the_production_blocker_to_the_policy():
    options = [lq.ProductionOption("DISTRICT", "DISTRICT_THEATER", 54, 5, -1)]
    game = BlockerGame(cities=idle_cities(), production=options)
    resolver = BlockerResolver(CityProductionPolicy().blocker_policies())
    assert run(resolver.resolve(game)) == []
    # The default skips districts, so only the policy could have placed this one.
    assert game.set_calls == [(65536, "DISTRICT", "DISTRICT_THEATER", 6, 6)]


def test_a_real_resolver_falls_back_to_the_default_when_the_policy_fails():
    options = [
        lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 9, -1),
        lq.ProductionOption("DISTRICT", "DISTRICT_THEATER", 54, 5, -1),
    ]
    game = BlockerGame(cities=idle_cities(), production=options)

    async def broken(*args):
        raise RuntimeError("advisor down")

    game.get_district_advisor = broken
    resolver = BlockerResolver(CityProductionPolicy().blocker_policies())
    assert run(resolver.resolve(game)) == []
    assert game.set_calls[-1][2] == "BUILDING_LIBRARY"


def threatened_game(building, turns_left=6):
    info = fx.city_info(
        currently_building=building,
        production_turns_left=turns_left,
        buildings=["PALACE", "MONUMENT"],
    )
    options = [
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
        lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 9, -1),
    ]
    return ScriptedGame(cities=[info], production=options, threats=[fx.threat_info(x=12, y=11)])


def test_a_threatened_city_switches_from_a_library_to_a_defender():
    game = threatened_game("BUILDING_LIBRARY")
    run(CityProductionPolicy().run(game))
    assert [c[2] for c in game.set_calls] == ["UNIT_WARRIOR"]


def test_a_threatened_city_does_not_switch_when_nearly_done_or_already_defending():
    near_done = threatened_game("BUILDING_LIBRARY", turns_left=1)
    run(CityProductionPolicy().run(near_done))
    assert near_done.set_calls == []
    for building in ("UNIT_WARRIOR", "BUILDING_WALLS"):
        game = threatened_game(building)
        run(CityProductionPolicy().run(game))
        assert game.set_calls == []


def test_an_unthreatened_busy_city_is_never_switched():
    game = ScriptedGame(
        cities=[fx.city_info(currently_building="BUILDING_LIBRARY")],
        production=[lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1)],
    )
    run(CityProductionPolicy().run(game))
    assert game.set_calls == []


def test_two_cities_do_not_both_start_a_settler():
    infos = [
        fx.city_info(city_id=1, name="A", currently_building="UNIT_SETTLER", population=4),
        fx.city_info(city_id=2, name="B", currently_building="nothing", population=4),
    ]
    options = [
        lq.ProductionOption("UNIT", "UNIT_SETTLER", 80, 8, -1),
        lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 9, -1),
    ]
    game = ScriptedGame(cities=infos, production=options)
    run(CityProductionPolicy().run(game))
    assert [c[:3] for c in game.set_calls] == [(2, "BUILDING", "BUILDING_LIBRARY")]


def test_a_unique_replacement_counts_as_the_unit_it_replaces():
    empire = run(read_empire(fx.FakeGame(overview=fx.overview(turn=5))))
    ctx = build_context(empire, [], ["UNIT_CREE_OKIHTCITAW"])
    assert (ctx.recon, ctx.army) == (1, 0)
    fresh = ProductionContext(turn=5, num_cities=1)
    c = city()
    unique = option("UNIT", "UNIT_CREE_OKIHTCITAW", 3)
    scout = option("UNIT", "UNIT_SCOUT", 3)
    assert score(unique, c, fresh, ProductionWeights()) == score(
        scout, c, fresh, ProductionWeights()
    )
    assert score(unique, c, fresh, ProductionWeights()) > 0
    assert score(unique, c, ctx, ProductionWeights()) == 0.0


def test_a_dropped_settler_does_not_block_another_city():
    infos = [
        fx.city_info(
            city_id=1,
            name="A",
            currently_building="UNIT_SETTLER",
            production_turns_left=6,
            population=4,
        ),
        fx.city_info(city_id=2, name="B", x=40, y=40, currently_building="nothing", population=4),
    ]
    options = [
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
        lq.ProductionOption("UNIT", "UNIT_SETTLER", 80, 8, -1),
    ]
    # Only city A is threatened, because the barbarian is next to it and far from B.
    game = ScriptedGame(cities=infos, production=options, threats=[fx.threat_info(x=12, y=11)])
    game.data["overview"] = fx.overview(num_cities=2)
    run(CityProductionPolicy().run(game))
    picks = {c[0]: c[2] for c in game.set_calls}
    assert picks[1] == "UNIT_WARRIOR"
    assert picks[2] == "UNIT_SETTLER"


def test_a_refused_switch_keeps_counting_the_build():
    infos = [
        fx.city_info(
            city_id=1,
            name="A",
            currently_building="UNIT_SETTLER",
            production_turns_left=6,
            population=4,
        ),
        fx.city_info(city_id=2, name="B", x=40, y=40, currently_building="nothing", population=4),
    ]
    options = [
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
        lq.ProductionOption("UNIT", "UNIT_SETTLER", 80, 8, -1),
        lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 9, -1),
    ]
    game = ScriptedGame(
        refuse={"UNIT_WARRIOR": "Error: nope"},
        cities=infos,
        production=options,
        threats=[fx.threat_info(x=12, y=11)],
    )
    game.data["overview"] = fx.overview(num_cities=2)
    run(CityProductionPolicy().run(game))
    picks = [c[2] for c in game.set_calls if c[0] == 2]
    assert "UNIT_SETTLER" not in picks
    assert picks[-1] == "BUILDING_LIBRARY"


def test_a_long_siege_by_barbarians_does_not_stop_expansion_once_defended():
    # Live, turns 9 to 25: barbarians camped beside the capital and never attacked. The capital
    # built six defenders and, as threatened, never scored a settler or builder again.
    camp = [threat(unit_type="UNIT_WARRIOR", x=11, y=10, distance=1) for _ in range(4)]
    c = city(threats=camp, population=3)
    assert len(c.threats) == 4
    held = ProductionContext(turn=25, num_cities=1, army=6, recon=1)
    assert best(c, MENU, held) == "UNIT_SETTLER"
    thin = ProductionContext(turn=25, num_cities=1, army=2, recon=1)
    assert best(c, MENU, thin) in {"UNIT_WARRIOR", "BUILDING_WALLS"}


# Replies copied from live bot logs.
NO_PLACEMENT = "ERR:NO_PLACEMENT|DISTRICT_HARBOR: no valid tile"
CANNOT_START = (
    "Error: CANNOT_START|BUILDING_TAJ_MAHAL cannot start. Hint: TAJ_MAHAL may require a "
    "completed district or prerequisite building"
)
SILENT_FAILURE = (
    "Error: SILENT_FAILURE|DISTRICT_THEATER appeared to set but the game engine did not persist it"
)


def attempts(game, item):
    return sum(1 for c in game.set_calls if c[2] == item)


def memory_game(reply, item="BUILDING_TAJ_MAHAL", cities=None):
    options = [
        lq.ProductionOption("BUILDING", item, 200, 9, -1),
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
    ]
    return ScriptedGame(refuse={item: reply}, cities=cities or idle_cities(), production=options)


@pytest.mark.parametrize("reply", [CANNOT_START, SILENT_FAILURE])
def test_a_refused_item_is_not_tried_again_until_the_backoff_ends(reply):
    item = "BUILDING_TAJ_MAHAL" if reply is CANNOT_START else "DISTRICT_THEATER"
    kind = "BUILDING" if reply is CANNOT_START else "DISTRICT"
    options = [
        lq.ProductionOption(kind, item, 200, 9, -1),
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
    ]
    game = ScriptedGame(refuse={item: reply}, cities=idle_cities(), production=options)
    if kind == "DISTRICT":
        game.districts = [lq.DistrictPlacement(5, 5, {}, 1, "Plains")]
    policy = CityProductionPolicy()
    for _ in range(3):
        run(policy.assign(game))
    assert attempts(game, item) == 1


def test_no_placement_is_remembered_and_the_pair_is_not_retried():
    options = [
        lq.ProductionOption("DISTRICT", "DISTRICT_HARBOR", 60, 5, -1),
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
    ]
    game = ScriptedGame(cities=idle_cities(), production=options)
    game.districts = []  # The advisor finds no tile, as in the live harbor case.
    policy = CityProductionPolicy()
    for _ in range(4):
        run(policy.assign(game))
    assert attempts(game, "DISTRICT_HARBOR") == 0  # Never reached set_city_production
    assert [c[2] for c in game.set_calls] == ["UNIT_WARRIOR"] * 4
    assert (65536, "DISTRICT_HARBOR") in policy.refusals._entries


@pytest.mark.parametrize(
    "reply",
    ["Error: Could not read cities: timeout", "Error: LUA_ERROR|nil value", "ERR:GAME_BUSY"],
)
def test_transient_failures_are_not_remembered(reply):
    game = memory_game(reply)
    policy = CityProductionPolicy()
    for _ in range(3):
        run(policy.assign(game))
    assert attempts(game, "BUILDING_TAJ_MAHAL") == 3
    assert policy.refusals._entries == {}


def test_an_accepted_item_clears_its_memory():
    game = memory_game(CANNOT_START)
    policy = CityProductionPolicy()
    run(policy.assign(game))
    game.refuse = {}
    policy.refusals._entries.clear()
    run(policy.assign(game))
    assert policy.refusals._entries == {}


def test_an_advisor_read_error_is_not_remembered_as_a_refusal():
    options = [lq.ProductionOption("DISTRICT", "DISTRICT_HARBOR", 60, 5, -1)]
    game = ScriptedGame(cities=idle_cities(), production=options)
    game.districts = "Error: timed out reading the advisor"
    policy = CityProductionPolicy()
    for _ in range(3):
        run(policy.assign(game))
    assert policy.refusals._entries == {}


# Special districts: each is wanted in a situation that needs it and skipped in one that doesn't.
def _districts_kept(name, c=None, ctx=None, victory="any"):
    from civ6_ai.policies.production import ProductionWeights

    menu = [option("DISTRICT", f"DISTRICT_{name}", 6)]
    kept, _ = drop_dominated(
        menu,
        c or city(),
        ctx or ProductionContext(turn=100, num_cities=4),
        ProductionWeights(victory=victory),
    )
    return bool(kept)


def _capped():
    return city(population=6, housing=6.0)


def test_spaceport_wanted_for_science_even_early_and_skipped_with_no_aim():
    early = ProductionContext(turn=60, num_cities=4)
    assert _districts_kept("SPACEPORT", ctx=early, victory="science")
    assert not _districts_kept("SPACEPORT", ctx=early, victory="any")
    assert not _districts_kept("SPACEPORT", victory="culture")


def test_spaceport_with_no_aim_waits_for_the_late_game_and_is_built_once():
    late = ProductionContext(turn=250, num_cities=4)
    assert _districts_kept("SPACEPORT", ctx=late, victory="any")
    held = ProductionContext(turn=250, num_cities=4, districts=frozenset({"SPACEPORT"}))
    assert not _districts_kept("SPACEPORT", ctx=held, victory="science")


def test_aerodrome_wanted_only_for_domination():
    assert _districts_kept("AERODROME", victory="domination")
    assert not _districts_kept("AERODROME", victory="any")
    assert not _districts_kept("AERODROME", victory="science")


def test_dam_wanted_when_housing_caps_a_grown_city():
    assert _districts_kept("DAM", c=_capped())
    assert not _districts_kept("DAM", c=city(population=6, housing=12.0))
    assert not _districts_kept("DAM", c=city(population=2, housing=2.0))


def test_canal_wanted_in_a_capped_city_but_not_early():
    assert _districts_kept("CANAL", c=_capped())
    assert not _districts_kept("CANAL", c=_capped(), ctx=ProductionContext(turn=30, num_cities=2))
    assert not _districts_kept("CANAL", c=city(population=6, housing=12.0))


def test_stadium_wanted_for_culture_in_an_established_city():
    assert _districts_kept("STADIUM", c=city(population=6), victory="culture")
    assert not _districts_kept("STADIUM", c=city(population=6), victory="any")
    assert not _districts_kept("STADIUM", c=city(population=2), victory="culture")
    early = ProductionContext(turn=30, num_cities=2)
    assert not _districts_kept("STADIUM", c=city(population=6), ctx=early, victory="culture")


def test_wanted_special_district_scores_above_unknown_and_planned_above_base():
    from civ6_ai.policies.production import ProductionWeights

    spaceport = option("DISTRICT", "DISTRICT_SPACEPORT", 6)
    ctx = ProductionContext(turn=100, num_cities=4)
    planned = score(spaceport, city(), ctx, ProductionWeights(victory="science"))
    base = score(spaceport, city(), ctx, ProductionWeights())
    assert planned > base > 0


def test_context_collects_the_districts_every_city_built():
    from civ6_ai.state.models import District

    built = city(population=5)
    built = replace(built, districts=(District("DISTRICT_SPACEPORT", 1, 1),))
    empire = run(read_empire(fx.FakeGame()))
    assert "SPACEPORT" in build_context(empire, [], (), [built]).districts


def test_a_district_being_chosen_counts_toward_the_one_spaceport():
    ctx = ProductionContext(turn=250, num_cities=4).after_choosing("DISTRICT_SPACEPORT")
    assert not _districts_kept("SPACEPORT", ctx=ctx, victory="science")


# ----- expansion target (1.41) -----


def _settler_score(ctx, **city_changes):
    grown = city(population=4, **city_changes)
    return score(option("UNIT", "UNIT_SETTLER"), grown, ctx, ProductionWeights())


def test_settler_priority_rises_when_behind_the_schedule():
    behind = ProductionContext(turn=100, num_cities=2, builders=2, recon=1, army=2)
    on_pace = replace(behind, num_cities=4, army=4, builders=4)
    assert _settler_score(behind) > _settler_score(on_pace) > 0
    assert _settler_score(behind) > 14.0


def test_no_boost_without_safe_sites_a_garrison_or_a_defender_per_city():
    behind = ProductionContext(turn=100, num_cities=2, builders=2, recon=1, army=2)
    boosted = _settler_score(behind)
    for plain in (
        _settler_score(replace(behind, sites_ok=False)),
        _settler_score(behind, garrison_unit=""),
        _settler_score(replace(behind, army=1)),  # A city would be left bare.
    ):
        assert 0 < plain < boosted


def test_a_second_settler_is_queued_only_when_behind():
    behind = ProductionContext(turn=100, num_cities=2, settlers=1, builders=2, recon=1, army=2)
    assert _settler_score(behind) > 0
    on_pace = replace(behind, num_cities=4, army=4, builders=4)
    assert _settler_score(on_pace) == 0
    assert _settler_score(replace(behind, settlers=2)) == 0


def test_boosted_settler_beats_a_builder_but_not_defence_under_threat():
    behind = ProductionContext(turn=100, num_cities=2, builders=0, recon=1, army=2)
    menu = [option("UNIT", "UNIT_SETTLER"), option("UNIT", "UNIT_BUILDER")]
    assert best(city(population=4), menu, behind) == "UNIT_SETTLER"
    pressed = city(population=4, threats=[threat()])
    assert score(option("UNIT", "UNIT_SETTLER"), pressed, behind, ProductionWeights()) == 0


# 1.45: placement precheck, builder cap and starving cities.


class CountingGame(ScriptedGame):
    advisor_calls = 0

    async def get_district_advisor(self, city_id, district_type):
        self.advisor_calls += 1
        return await super().get_district_advisor(city_id, district_type)


def harbor_game(cities=None):
    options = [
        lq.ProductionOption("DISTRICT", "DISTRICT_HARBOR", 60, 5, -1),
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
    ]
    game = CountingGame(cities=cities or idle_cities(), production=options)
    game.districts = []
    return game


def test_a_district_with_no_tile_is_not_asked_for_after_the_city_builds_something_else():
    game = harbor_game()
    policy = CityProductionPolicy()
    run(policy.assign(game))
    assert game.advisor_calls == 1
    # The city gains a building and a district, as it does every few turns. Still no tile.
    grown = fx.city_info(
        currently_building="nothing",
        buildings=["PALACE", "MONUMENT", "BUILDING_LIBRARY"],
        districts=["DISTRICT_CITY_CENTER@10,10", "DISTRICT_CAMPUS@11,10", "DISTRICT_THEATER@9,9"],
    )
    game.data["cities"] = [grown]
    run(policy.assign(game))
    assert game.advisor_calls == 1


def test_a_refused_wonder_is_not_asked_for_again_as_buildings_come_and_go():
    options = [
        lq.ProductionOption("BUILDING", "BUILDING_TAJ_MAHAL", 200, 9, -1),
        lq.ProductionOption("UNIT", "UNIT_WARRIOR", 40, 4, -1),
    ]
    game = ScriptedGame(
        refuse={"BUILDING_TAJ_MAHAL": CANNOT_START}, cities=idle_cities(), production=options
    )
    policy = CityProductionPolicy()
    for n in range(4):
        game.data["cities"] = [
            fx.city_info(
                currently_building="nothing",
                buildings=["PALACE", "MONUMENT", *[f"BUILDING_X{i}" for i in range(n)]],
            )
        ]
        run(policy.assign(game))
    assert attempts(game, "BUILDING_TAJ_MAHAL") == 1


def test_builders_are_not_trained_while_one_is_idle():
    ctx = replace(MID, builders=2, idle_builders=1)
    assert score_of("UNIT_BUILDER", city(), ctx) == 0.0
    assert score_of("UNIT_BUILDER", city(), replace(MID, builders=2)) > 0.0


def test_builders_are_capped_by_the_jobs_nobody_holds():
    short = replace(MID, builders=2, spare_jobs=0)
    assert score_of("UNIT_BUILDER", city(), short) == 0.0
    assert score_of("UNIT_BUILDER", city(), replace(short, spare_jobs=2)) > 0.0
    # With no builders at all, a stale count never stops the first one.
    assert score_of("UNIT_BUILDER", city(), replace(short, builders=0)) > 0.0


def test_the_policy_reads_the_builder_demand():
    from civ6_ai.policies.builder import BuilderDemand

    demand = BuilderDemand(idle=1, spare_jobs=0)
    policy = CityProductionPolicy(builder_demand=demand)
    empire = run(read_empire(fx.FakeGame()))
    ctx = policy._context(empire, [], [], [])
    assert (ctx.idle_builders, ctx.spare_jobs) == (1, 0)
    ctx = CityProductionPolicy()._context(empire, [], [], [])
    assert (ctx.idle_builders, ctx.spare_jobs) == (0, None)


def starving(**changes):
    return city(food_surplus=-0.1, population=4, **changes)


def test_a_starving_city_prefers_a_food_building():
    c, ctx = starving(), MID
    granary, sewer = option("BUILDING", "BUILDING_GRANARY"), option("BUILDING", "BUILDING_SEWER")
    weights = ProductionWeights()
    assert score(granary, c, ctx, weights) > 3 * score(granary, city(), ctx, weights)
    options = [granary, option("BUILDING", "BUILDING_LIBRARY")]
    assert best(c, options, ctx) == "BUILDING_GRANARY"
    assert best(city(), options, ctx) == "BUILDING_LIBRARY"
    # Housing does not feed anyone.
    assert score(sewer, c, ctx, weights) == score(sewer, city(), ctx, weights)


def test_a_starving_city_trains_no_settler():
    ctx = replace(MID, num_cities=2, army=9, turn=40)
    assert score_of("UNIT_SETTLER", city(), ctx) > 0
    assert score_of("UNIT_SETTLER", starving(), ctx) == 0


class FocusGame(ScriptedGame):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.focus_calls: list[tuple] = []

    async def set_city_focus(self, city_id, focus):
        self.focus_calls.append((city_id, focus))
        return f"FOCUS|{focus}"


def test_a_starving_city_gets_a_food_focus_once_and_default_after_it_recovers():
    busy = dict(currently_building="BUILDING_LIBRARY")
    game = FocusGame(cities=[fx.city_info(food_surplus=-0.1, **busy)])
    policy = CityProductionPolicy()
    for _ in range(3):
        run(policy.assign(game))
    assert game.focus_calls == [(65536, "food")]  # One call, not one per turn.
    game.data["cities"] = [fx.city_info(food_surplus=1.0, **busy)]
    run(policy.assign(game))
    assert game.focus_calls == [(65536, "food")]  # Not yet recovered.
    game.data["cities"] = [fx.city_info(food_surplus=2.5, **busy)]
    run(policy.assign(game))
    run(policy.assign(game))
    assert game.focus_calls == [(65536, "food"), (65536, "default")]


def test_a_failed_focus_call_is_retried_and_never_stops_production():
    class Broken(FocusGame):
        async def set_city_focus(self, city_id, focus):
            self.focus_calls.append((city_id, focus))
            raise RuntimeError("no connection")

    game = Broken(cities=[fx.city_info(food_surplus=-1.0, currently_building="nothing")])
    policy = CityProductionPolicy()
    run(policy.assign(game))
    run(policy.assign(game))
    assert len(game.focus_calls) == 2
    assert game.set_calls  # The idle city still got something to build.


def test_each_builder_order_uses_up_an_unclaimed_job():
    ctx = replace(MID, builders=1, spare_jobs=1)
    assert score_of("UNIT_BUILDER", city(), ctx) > 0
    ctx = ctx.after_choosing("UNIT_BUILDER")
    assert ctx.spare_jobs == 0 and score_of("UNIT_BUILDER", city(), ctx) == 0.0
    # Builders that already exist were counted in the job figure, so they take none.
    empire = run(read_empire(fx.FakeGame()))
    built = build_context(empire, [], (), (), None, 0, 1)
    assert built.spare_jobs == 1
    ordering = build_context(empire, [], ["UNIT_BUILDER"], (), None, 0, 1)
    assert ordering.spare_jobs == 0


def test_a_city_that_starves_again_after_the_reset_keeps_its_food_focus():
    busy = dict(currently_building="BUILDING_LIBRARY")
    game = FocusGame(cities=[fx.city_info(food_surplus=-0.1, **busy)])
    policy = CityProductionPolicy()
    for surplus in (-0.1, 2.5, -0.1, 2.5, 2.5):
        game.data["cities"] = [fx.city_info(food_surplus=surplus, **busy)]
        run(policy.assign(game))
    assert game.focus_calls == [(65536, "food"), (65536, "default"), (65536, "food")]
