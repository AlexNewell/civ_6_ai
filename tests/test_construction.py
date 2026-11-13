import asyncio
from types import SimpleNamespace

from civ_mcp import lua as lq

from civ6_ai.construction import (
    CONSTRUCTION_EVENTS,
    BuildingCompleted,
    CityGained,
    ConstructionWatcher,
    DistrictCompleted,
    DistrictLost,
    PlacementPlan,
    ProductionRefused,
    ProductionSet,
)
from civ6_ai.monitors import EventBus, EventLog, event_from_json
from civ6_ai.policies.production import CityProductionPolicy
from fixtures import civ_state as fx
from test_policy_production import ScriptedGame


def watcher():
    seen = []
    bus = EventBus()
    bus.subscribe(seen.append)
    return ConstructionWatcher(bus), seen


def info(city_id=1, districts=(), buildings=(), building="nothing"):
    return SimpleNamespace(
        city_id=city_id,
        name=f"City{city_id}",
        x=10,
        y=10,
        districts=list(districts),
        buildings=list(buildings),
        currently_building=building,
    )


def test_first_read_is_only_a_baseline():
    w, _ = watcher()
    assert (
        w.update(5, [info(districts=["DISTRICT_CAMPUS@11,10"], buildings=["BUILDING_LIBRARY"])])
        == []
    )


def test_completions_come_from_list_changes():
    w, _ = watcher()
    w.update(5, [info(districts=["DISTRICT_CITY_CENTER@10,10"])])
    events = w.update(
        6,
        [
            info(
                districts=["DISTRICT_CITY_CENTER@10,10", "DISTRICT_CAMPUS@11,10"],
                buildings=["BUILDING_LIBRARY"],
            )
        ],
    )
    assert [type(e) for e in events] == [DistrictCompleted, BuildingCompleted]
    assert events[0].district == "DISTRICT_CAMPUS" and (
        events[0].district_x,
        events[0].district_y,
    ) == (11, 10)
    assert events[0].turn == 6 and events[0].city_id == 1 and events[0].at.endswith("+00:00")
    assert events[1].building == "LIBRARY"
    assert (
        w.update(
            7,
            [
                info(
                    districts=["DISTRICT_CITY_CENTER@10,10", "DISTRICT_CAMPUS@11,10"],
                    buildings=["BUILDING_LIBRARY"],
                )
            ],
        )
        == []
    )


def test_a_district_still_being_built_is_not_reported_early():
    w, _ = watcher()
    w.update(5, [info()])
    under_way = info(districts=["DISTRICT_CAMPUS@11,10"], building="DISTRICT_CAMPUS")
    assert w.update(6, [under_way]) == []
    assert w.update(7, [under_way]) == []
    done = info(districts=["DISTRICT_CAMPUS@11,10"], building="BUILDING_LIBRARY")
    assert [type(e) for e in w.update(8, [done])] == [DistrictCompleted]


def test_a_district_under_way_at_the_baseline_is_reported_when_done():
    w, _ = watcher()
    w.update(5, [info(districts=["DISTRICT_CAMPUS@11,10"], building="DISTRICT_CAMPUS")])
    done = info(districts=["DISTRICT_CAMPUS@11,10"])
    assert [type(e) for e in w.update(6, [done])] == [DistrictCompleted]


def test_a_second_district_of_a_type_does_not_hide_the_first():
    w, _ = watcher()
    w.update(5, [info(districts=["DISTRICT_CANAL@9,9"])])
    both = info(districts=["DISTRICT_CANAL@9,9", "DISTRICT_CANAL@8,8"], building="DISTRICT_CANAL")
    assert w.update(6, [both]) == []


def test_district_lost_and_cities_gained_and_regained():
    w, _ = watcher()
    w.update(5, [info(1, districts=["DISTRICT_CAMPUS@11,10"]), info(2)])
    events = w.update(6, [info(1), info(3)])
    assert [type(e) for e in events] == [DistrictLost, CityGained]
    assert events[1].city_id == 3 and not events[1].regained
    back = w.update(7, [info(1), info(2), info(3)])
    assert [(type(e), e.regained) for e in back] == [(CityGained, True)]


def test_a_turn_going_backwards_resets_the_baseline():
    w, _ = watcher()
    w.update(9, [info()])
    w.reset()
    assert w.update(4, [info(buildings=["BUILDING_LIBRARY"])]) == []


def test_handler_skips_a_short_read_and_resets_after_a_reload():
    w, seen = watcher()
    game = fx.FakeGame(overview=fx.overview(turn=5, num_cities=1), cities=[fx.city_info()])
    asyncio.run(w.run(game))
    short = fx.FakeGame(overview=fx.overview(turn=6, num_cities=2), cities=[fx.city_info()])
    asyncio.run(w.run(short))
    assert seen == []
    assert w._seen is not None and w._last_turn == 5


def test_events_round_trip_through_the_events_file(tmp_path):
    path = tmp_path / "game_events.jsonl"
    bus = EventBus()
    log = EventLog(path)
    bus.subscribe(log.write)
    sent = [
        ProductionSet(
            3,
            1,
            "A",
            1,
            2,
            item="DISTRICT_CAMPUS",
            category="DISTRICT",
            tile_x=5,
            tile_y=6,
            placements_offered=2,
        ),
        ProductionRefused(
            3,
            1,
            "A",
            1,
            2,
            item="X",
            category="DISTRICT",
            reply="ERR:NO_PLACEMENT|X",
            is_refusal=True,
            placements_offered=0,
            backoff_turns=5,
        ),
        DistrictCompleted(4, 1, "A", 1, 2, district="DISTRICT_CAMPUS", district_x=5, district_y=6),
        DistrictLost(4, 1, "A", 1, 2, district="DISTRICT_CAMPUS", district_x=5, district_y=6),
        BuildingCompleted(4, 1, "A", 1, 2, building="LIBRARY"),
        CityGained(4, 1, "A", 1, 2, regained=True),
        PlacementPlan(
            4,
            1,
            "A",
            1,
            2,
            item="X",
            category="DISTRICT",
            action="save",
            tile_x=7,
            tile_y=7,
            cost=90,
        ),
    ]
    assert len(sent) == len(CONSTRUCTION_EVENTS)
    for event in sent:
        bus.publish(event)
    log.flush()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [event_from_json(line) for line in lines] == sent


def test_production_publishes_set_and_refused_events():
    options = [
        lq.ProductionOption("DISTRICT", "DISTRICT_THEATER", 54, 5, -1),
        lq.ProductionOption("BUILDING", "BUILDING_TAJ_MAHAL", 200, 9, -1),
    ]
    game = ScriptedGame(
        cities=[fx.city_info(currently_building="nothing", buildings=["PALACE", "MONUMENT"])],
        production=options,
        refuse={"BUILDING_TAJ_MAHAL": "ERR:NOT_AVAILABLE|taj"},
    )
    seen = []
    bus = EventBus()
    bus.subscribe(seen.append)
    asyncio.run(CityProductionPolicy(bus=bus).run(game))
    by_type = {type(e): e for e in seen}
    placed = by_type[ProductionSet]
    assert (placed.item, placed.tile_x, placed.tile_y, placed.placements_offered) == (
        "DISTRICT_THEATER",
        6,
        6,
        2,
    )
    assert placed.city_name and placed.at
    refused = by_type.get(ProductionRefused)
    if refused is not None:
        assert refused.reply.startswith("ERR:") and refused.backoff_turns >= 0


def test_a_paused_district_is_not_reported_until_it_is_finished():
    w, _ = watcher()
    w.update(5, [info()])
    tile = ["DISTRICT_CAMPUS@11,10"]
    assert w.update(6, [info(districts=tile, building="DISTRICT_CAMPUS")]) == []
    # The defence switch replaces it with a unit.
    w._on_production_set(ProductionSet(6, 1, "City1", 10, 10, item="UNIT_ARCHER", category="UNIT"))
    assert w.update(7, [info(districts=tile, building="UNIT_ARCHER")]) == []
    assert w.update(8, [info(districts=tile, building="UNIT_ARCHER")]) == []
    w._on_production_set(
        ProductionSet(8, 1, "City1", 10, 10, item="DISTRICT_CAMPUS", category="DISTRICT")
    )
    assert w.update(9, [info(districts=tile, building="DISTRICT_CAMPUS")]) == []
    assert [type(e) for e in w.update(10, [info(districts=tile)])] == [DistrictCompleted]
