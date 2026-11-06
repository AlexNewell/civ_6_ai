import asyncio
import dataclasses
import json
from dataclasses import replace

import pytest

from civ6_ai import state
from civ6_ai.state import fog, models, reader, serialise
from fixtures import civ_state as fx


def run(coro):
    return asyncio.run(coro)


def protocol_methods() -> set[str]:
    return {n for n in vars(reader.GameReads) if not n.startswith("_") and n != "__doc__"}


def test_every_game_call_is_audited_and_none_are_banned():
    methods = protocol_methods()
    assert methods
    assert methods <= set(fog.READ_AUDIT)
    assert not {n for n in methods if fog.READ_AUDIT[n].verdict is fog.Verdict.BANNED}
    for name in ("get_rival_snapshot", "get_diary_snapshot", "execute_lua"):
        assert fog.READ_AUDIT[name].verdict is fog.Verdict.BANNED
        assert name not in methods


def test_fake_game_implements_the_protocol():
    assert protocol_methods() <= {n for n in dir(fx.FakeGame) if not n.startswith("_")}


def test_protocol_matches_the_real_game_state():
    from civ_mcp.game_state import GameState

    assert protocol_methods() <= set(dir(GameState))


def test_views_are_immutable():
    city = reader.build_city(fx.city_info(), (), set())
    with pytest.raises(dataclasses.FrozenInstanceError):
        city.pop = 99  # type: ignore[misc]


def test_models_carry_no_omniscient_field():
    names = {
        field.name
        for item in vars(models).values()
        if dataclasses.is_dataclass(item)
        for field in dataclasses.fields(item)
    }
    omniscient = {
        "attack_targets",
        "needs_promotion",
        "religions_founded",
        "religions_max",
        "total_land",
        "num_cities_seen",
        "defensive_pacts",
        "demographics",
        "gold_yield",
        "loyalty_pressure",
    }
    assert not names & omniscient


# Threats


def test_threat_hostility_follows_war_and_barbarians():
    civs = [fx.civ_info(is_at_war=True), fx.unmet_civ()]
    scan = [
        fx.threat_info(distance=3),
        fx.threat_info(owner_id=fx.ROME, owner_name="Rome", distance=1),
        fx.threat_info(owner_id=fx.UNMET, owner_name="Egypt", distance=2),
    ]
    threats = reader.build_threats(scan, civs)
    assert [t.distance for t in threats] == [1, 2, 3]
    assert [t.hostile for t in threats] == [True, False, True]


def test_peaceful_rival_unit_is_not_hostile():
    (threat,) = reader.build_threats(
        [fx.threat_info(owner_id=fx.ROME, owner_name="Rome")], [fx.civ_info()]
    )
    assert not threat.hostile


def test_distant_stealth_units_are_removed():
    scan = [
        fx.threat_info(unit_type="UNIT_SUBMARINE", distance=5),
        fx.threat_info(unit_type="UNIT_SUBMARINE", distance=1),
    ]
    assert [t.distance for t in reader.build_threats(scan, [])] == [1]


# Cities


def test_city_fields_and_omniscient_targets_are_dropped():
    city = reader.build_city(fx.city_info(), (), {("IRON", 12, 10)})
    assert city.is_capital
    assert city.building == "BUILDING_MONUMENT"
    assert [d.district_type for d in city.districts] == [
        "DISTRICT_CITY_CENTER",
        "DISTRICT_CAMPUS",
    ]
    assert not hasattr(city, "attack_targets")


def test_idle_city_has_no_build():
    for idle in ("", "nothing", "NONE"):
        city = reader.build_city(fx.city_info(currently_building=idle), (), set())
        assert city.is_idle


def test_unimproved_resources_need_visibility():
    city = reader.build_city(fx.city_info(), (), {("IRON", 12, 10)})
    assert [r.name for r in city.unimproved_resources] == ["IRON"]
    assert reader.build_city(fx.city_info(), (), set()).unimproved_resources == ()


def test_city_threats_are_hostile_and_near_only():
    scan = [
        fx.threat_info(x=13, y=10, distance=3),
        fx.threat_info(x=11, y=10, distance=1),
        fx.threat_info(x=40, y=40, distance=20),
        fx.threat_info(owner_id=fx.ROME, owner_name="Rome", x=11, y=11),
    ]
    threats = reader.build_threats(scan, [fx.civ_info()])
    city = reader.build_city(fx.city_info(), threats, set())
    assert [(t.x, t.y) for t in city.threats] == [(11, 10), (13, 10)]
    assert city.is_threatened


def test_hex_distance_wraps_the_map_edge():
    assert reader.hex_distance(0, 4, 73, 4) == 1
    assert reader.hex_distance(0, 4, 73, 4, map_width=None) == 73


def test_pillaged_district_is_flagged():
    info = fx.city_info(pillaged_districts=["DISTRICT_CAMPUS"])
    city = reader.build_city(info, (), set())
    assert [d.pillaged for d in city.districts] == [False, True]


# Units


def test_unit_targets_parse_and_hide_distant_stealth():
    info = fx.unit_info(targets=["UNIT_ARCHER@11,11(80hp)", "UNIT_SUBMARINE@20,11(50hp)", "junk"])
    unit = reader.build_unit(info)
    assert [(t.unit_type, t.hp) for t in unit.targets] == [("UNIT_ARCHER", 80)]
    assert unit.is_combat
    assert unit.can_move
    assert not unit.is_hurt


# Research and empire


def test_research_reads_options_and_idle_slots():
    research = reader.build_research(fx.research_status())
    assert (research.current_tech, research.current_tech_turns) == ("Bronze Working", 5)
    assert (research.current_civic, research.current_civic_turns) == (None, None)
    assert research.techs[1].prereqs == ("Pottery", "Mining")
    assert research.civics[0].item_type == "CIVIC_CODE_OF_LAWS"
    assert research.locked_techs[0].item_type == "TECH_CURRENCY"
    assert research.techs_done == 10


def _empire():
    return reader.build_empire(
        fx.overview(),
        fx.victory(),
        fx.government(),
        fx.governors(),
        fx.trade(),
        fx.resources(),
        fx.pantheon(),
        fx.envoys(),
    )


def test_empire_summary():
    empire = _empire()
    assert (empire.turn, empire.civ, empire.score) == (42, "America", 60)
    assert [s.index for s in empire.empty_policy_slots] == [1]
    assert empire.free_trade_routes == 1
    assert empire.luxuries == (("Silk", 1),)
    assert empire.governors[0].city_id is None
    assert [c.suzerain for c in empire.city_states] == [None, "Rome"]
    assert empire.pantheon is None
    assert empire.science_vp == 0


# Rivals


def test_unmet_civs_never_appear():
    rivals = reader.build_rivals([fx.civ_info(), fx.unmet_civ()], fx.victory(), {(20, 10): True})
    assert [r.civ for r in rivals] == ["Rome"]


def test_rival_uses_visible_cities_agendas_and_rankings():
    (rival,) = reader.build_rivals([fx.civ_info()], fx.victory(), {(20, 10): True, (30, 30): False})
    assert [c.name for c in rival.visible_cities] == ["Roma"]
    assert rival.agendas == ("Builder of Roads",)
    assert rival.score == 55
    assert rival.military_strength == 44
    assert rival.majority_religion == "Judaism"
    assert not rival.holds_capital
    assert not hasattr(rival, "num_cities")
    assert not hasattr(rival, "defensive_pacts")


# Map


def test_fogged_tile_blanks_live_values():
    tile = reader.build_tile(fx.tile(visibility="revealed"))
    assert not tile.visible
    assert tile.owner is None
    assert tile.improvement is None
    assert tile.district is None
    assert tile.yields is None
    assert tile.foreign_units == ()


def test_visible_tile_keeps_values_but_drops_stealth_units():
    tile = reader.build_tile(fx.tile())
    assert tile.improvement == "IMPROVEMENT_MINE"
    assert tile.foreign_units == ("Rome WARRIOR",)


def test_builder_jobs_drop_unconfirmed_resources_and_unwrap_repairs():
    tasks = [
        fx.lq.BuilderTask("high", 12, 10, "IMPROVEMENT_MINE", "IRON", "strategic", "Alpha"),
        fx.lq.BuilderTask("high", 5, 5, "IMPROVEMENT_MINE", "IRON", "strategic", "Alpha"),
        fx.lq.BuilderTask("urgent", 6, 6, "REPAIR", "FARM", "", "Alpha"),
    ]
    jobs = reader.build_builder_jobs(tasks, {("IRON", 12, 10)})
    assert [(j.x, j.improvement, j.is_repair) for j in jobs] == [
        (12, "IMPROVEMENT_MINE", False),
        (6, "IMPROVEMENT_FARM", True),
    ]
    assert jobs[0].nearest_builder_id is None


def test_production_choices():
    (choice,) = reader.build_production([fx.lq.ProductionOption("BUILDING", "Library", 90, 9, -1)])
    assert choice.gold_cost is None


# Reading through the protocol


def test_snapshot_needs_no_running_game():
    game = fx.FakeGame()
    snap = run(reader.read_snapshot(game))
    assert [c.name for c in snap.cities] == ["Alpha", "Beta"]
    assert snap.cities[0].threats[0].owner == "Barbarians"
    assert [r.civ for r in snap.rivals] == ["Rome"]
    assert snap.empire.num_cities == 2
    assert set(game.calls) <= set(fog.READ_AUDIT)


def test_rival_city_in_the_fog_is_left_out():
    game = fx.FakeGame(tiles={(20, 10): [fx.tile()], (30, 30): [fx.tile(visibility="revealed")]})
    (rival,) = run(reader.read_rivals(game))
    assert [c.name for c in rival.visible_cities] == ["Roma"]


def test_narrow_reads_make_only_their_own_calls():
    game = fx.FakeGame()
    run(reader.read_units(game))
    run(reader.read_research(game))
    run(reader.read_production(game, 65536))
    assert game.calls == ["get_units", "get_tech_civics", "list_city_production"]


def test_exploration_view():
    strategic = fx.lq.StrategicMapData(
        [fx.lq.FogBoundary("Alpha", 10, 10, [3, -1, 4, 5, 6, 7])],
        [fx.lq.UnclaimedResource("IRON", 14, 9, "strategic")],
    )
    sites = [fx.lq.SettleCandidate(15, 15, 9.5, 8, 6, "coast", ["WHEAT"], 2, 0, 1, 0.5)]
    view = run(reader.read_exploration(fx.FakeGame(strategic=strategic, settle=sites)))
    assert view.fog_edges[0].distances[1] == -1
    assert view.settle_sites[0].water == "coast"
    assert not hasattr(view.settle_sites[0], "score")
    assert not hasattr(view.settle_sites[0], "loyalty_pressure")


# Serialising


def _busy_city():
    """A late city with a long list of everything, to show the limit holds."""
    resources = [f"RESOURCE_{i}@{20 + i},3" for i in range(10)]
    info = fx.city_info(
        buildings=[f"BUILDING_LONG_NAME_NUMBER_{i}" for i in range(40)],
        districts=[f"DISTRICT_NUMBER_{i}@{10 + i},10" for i in range(12)],
        pillaged_districts=["DISTRICT_NUMBER_1"],
        pillaged_buildings=["BUILDING_LONG_NAME_NUMBER_2"],
        pillaged_improvements=[f"FARM@{i},{i}" for i in range(6)],
        unimproved_resources=resources,
        loyalty=60.0,
        loyalty_per_turn=-3.5,
        turns_to_loyalty_flip=7,
    )
    scan = [
        fx.threat_info(unit_type="UNIT_HORSEMAN", x=11 + i % 3, y=10, distance=1 + i % 3)
        for i in range(12)
    ]
    visible = set()
    for item in resources:
        name, xy = item.split("@")
        x, y = xy.split(",")
        visible.add((name, int(x), int(y)))
    return reader.build_city(info, reader.build_threats(scan, []), visible)


def test_ordinary_city_is_small_and_complete():
    city = reader.build_city(fx.city_info(), (), {("IRON", 12, 10)})
    text = serialise.serialise_city(city)
    assert serialise.estimate_tokens(text) < 150
    data = json.loads(text)
    assert data["build"] == ["MONUMENT", 4]
    assert data["cap"] == 1
    assert data["res"] == ["IRON@12,10"]
    assert "loy" not in data
    assert "cut" not in data


def test_worst_case_city_stays_under_the_limit():
    text = serialise.serialise_city(_busy_city())
    assert serialise.estimate_tokens(text) <= serialise.CITY_TOKEN_LIMIT
    assert "cut" in json.loads(text)


def test_uncut_worst_case_would_have_broken_the_limit():
    payload = serialise.city_payload(_busy_city())
    assert serialise.estimate_tokens(serialise._dumps(payload)) > serialise.CITY_TOKEN_LIMIT


def test_cut_removes_the_least_valuable_lists_first():
    data = json.loads(serialise.serialise_city(_busy_city(), limit=300))
    assert data["cut"][0] == "bld"


def test_long_names_are_truncated():
    city = replace(reader.build_city(fx.city_info(), (), set()), name="N" * 100)
    assert len(json.loads(serialise.serialise_city(city))["name"]) == serialise.MAX_NAME_LENGTH


def test_token_estimate_is_documented_bytes_over_three():
    assert serialise.estimate_tokens("abc") == 1
    assert serialise.estimate_tokens("abcd") == 2
    assert serialise.estimate_tokens("é") == 1


def test_public_names_are_exported():
    for name in state.__all__:
        assert hasattr(state, name)


def test_visible_neighbour_does_not_reveal_a_fogged_rival_city():
    neighbour = fx.tile(x=31, y=30)
    game = fx.FakeGame(tiles={(20, 10): [fx.tile()], (30, 30): [neighbour]})
    (rival,) = run(reader.read_rivals(game))
    assert [c.name for c in rival.visible_cities] == ["Roma"]


def test_building_names_share_one_form():
    info = fx.city_info(
        buildings=["PALACE", "BUILDING_MONUMENT", "GRANARY"],
        pillaged_buildings=["BUILDING_MONUMENT"],
    )
    city = reader.build_city(info, (), set())
    assert city.buildings == ("PALACE", "MONUMENT", "GRANARY")
    assert set(city.pillaged_buildings) <= set(city.buildings)
    assert city.is_capital
