import asyncio
import json
from pathlib import Path

from civ6_ai.state.models import Threat
from civ6_ai.state.reader import build_tile
from civ6_ai.threat_map import (
    CAMP,
    UNIT,
    ThreatMap,
    ThreatMapHandler,
    ThreatMapSettings,
)
from fixtures.civ_state import FakeGame, city_info, overview, tile

CAMP_TILE = "IMPROVEMENT_BARBARIAN_CAMP"
SRC = Path(__file__).resolve().parents[1] / "src" / "civ6_ai"


def camp(x=67, y=13, visibility="visible", improvement=CAMP_TILE):
    return build_tile(tile(x=x, y=y, improvement=improvement, visibility=visibility, units=[]))


def hostile(kind="UNIT_WARRIOR", x=10, y=10, cs=8, owner_id=63, hostile=True):
    return Threat(owner_id, "Barbarians", kind, x, y, 100, 100, cs, 0, 3, hostile)


def test_a_fogged_camp_is_a_source():
    m = ThreatMap()
    m.see_tiles(52, [camp(visibility="revealed")], baseline=True)
    (src,) = m.camps()
    assert (src.x, src.y) == (67, 13)
    assert src.last_seen is None  # Remembered by the map, never confirmed on a visible tile.
    assert src.first_seen == 52


def test_a_camp_stays_while_fogged_and_goes_when_a_visible_tile_shows_it_gone():
    m = ThreatMap()
    m.see_tiles(52, [camp()])
    m.see_tiles(
        55, [camp(visibility="revealed", improvement=None)]
    )  # Fogged: can't be believed gone.
    assert len(m.camps()) == 1
    m.see_tiles(58, [camp(improvement=None)])  # Visible and no camp.
    assert m.camps() == []


def test_a_visible_sighting_refreshes_last_seen_and_a_fogged_read_does_not():
    m = ThreatMap()
    m.see_tiles(52, [camp()])
    m.see_tiles(60, [camp(visibility="revealed")])
    assert m.camps()[0].age(60) == 8  # A fogged read doesn't refresh it.
    m.see_tiles(61, [camp()])
    assert m.camps()[0].age(61) == 0


def test_hostile_units_are_sources_and_peaceful_ones_are_not():
    m = ThreatMap()
    m.see_threats(10, [hostile(), hostile(owner_id=2, hostile=False, x=20)])
    assert [(s.kind, s.x) for s in m.sources] == [(UNIT, 10)]


def test_a_unit_is_forgotten_after_its_memory_and_replaced_by_a_nearby_resighting():
    m = ThreatMap(ThreatMapSettings(unit_memory_turns=5))
    m.see_threats(10, [hostile(x=10)])
    m.see_threats(12, [hostile(x=11)])  # Same kind, close: the old record is replaced.
    assert [s.x for s in m.sources] == [11]
    m.see_threats(14, [])
    assert len(m.sources) == 1
    m.see_threats(17, [])
    assert m.sources == []


def test_a_camp_is_not_dropped_by_unit_updates():
    m = ThreatMap()
    m.see_tiles(1, [camp()])
    m.see_threats(30, [])
    assert [s.kind for s in m.sources] == [CAMP]


def test_threat_fades_with_distance_and_unit_threat_fades_with_age():
    m = ThreatMap()
    m.see_tiles(1, [camp()])
    assert m.threat_at(67, 13) > m.threat_at(67, 15) > 0
    assert m.threat_at(67, 25) == 0
    m.see_threats(10, [hostile(x=30, y=30)])
    fresh = m.threat_at(30, 30, 10)
    assert m.threat_at(30, 30, 14) < fresh


def test_danger_near_sees_a_camp_from_across_its_radius():
    m = ThreatMap()
    m.see_tiles(1, [camp()])
    assert m.danger_near(67, 20, 8) > 0
    assert m.danger_near(67, 30, 4) == 0


def test_the_record_lists_each_camp_with_its_distance_to_our_nearest_city():
    from civ6_ai.state.reader import build_city

    m = ThreatMap()
    m.see_tiles(52, [camp()])
    city = build_city(city_info(x=64, y=16), (), set(), 74)
    rec = m.record(52, [city])
    assert rec["camps_near_city"] == 1
    assert rec["sources"][0]["city_distance"] == 5
    assert rec["heat"][0][2] > 0


def test_the_handler_finds_a_fogged_camp_and_writes_the_dump(tmp_path):
    fogged = tile(x=67, y=13, improvement=CAMP_TILE, visibility="revealed", units=[])
    game = FakeGame(
        units=[],
        threats=[],
        cities=[city_info(x=64, y=16)],
        overview=overview(turn=52, num_cities=1),
        civs=[],
    )

    async def area(cx, cy, radius=2):
        return [fogged]

    game.get_map_area = area
    dump = tmp_path / "map.jsonl"
    handler = ThreatMapHandler(dump=dump)
    asyncio.run(handler.run(game))
    line = json.loads(dump.read_text().splitlines()[0])
    assert line["turn"] == 52
    assert line["sources"][0]["kind"] == CAMP
    assert (line["sources"][0]["x"], line["sources"][0]["y"]) == (67, 13)
    assert line["camps_near_city"] == 1


def test_the_handler_scans_on_its_interval_not_every_turn():
    game = FakeGame(
        units=[],
        threats=[],
        cities=[city_info()],
        overview=overview(turn=10, num_cities=1),
        civs=[],
    )
    reads = []

    async def area(cx, cy, radius=2):
        reads.append(radius)
        return []

    game.get_map_area = area
    handler = ThreatMapHandler(ThreatMapSettings(scan_turns=3))
    asyncio.run(handler.run(game))
    asyncio.run(handler.run(game))  # Same turn: due_for_scan is false after the first scan.
    assert len(reads) == 1


def test_nothing_in_the_policies_reads_the_map_yet():
    for path in (SRC / "policies").glob("*.py"):
        assert "threat_map" not in path.read_text(encoding="utf-8"), path.name


def test_the_record_carries_our_cities_and_combat_units_for_the_picture():
    from civ6_ai.state.reader import build_city, build_unit
    from fixtures.civ_state import unit_info

    m = ThreatMap()
    city = build_city(city_info(x=64, y=16), (), set(), 74)
    warrior = build_unit(unit_info(x=65, y=16, unit_type="UNIT_WARRIOR", combat_strength=8), 74)
    rec = m.record(5, [city], [warrior])
    assert rec["cities"] == [[64, 16, city.name]]
    assert rec["units"] == [[65, 16, "UNIT_WARRIOR"]]


def test_a_camp_first_seen_fogged_after_the_baseline_is_not_believed():
    """It may have spawned out of sight, which the player's map would not show."""
    m = ThreatMap()
    m.see_tiles(52, [], baseline=True)
    m.see_tiles(73, [camp(x=69, y=27, visibility="revealed")])
    assert m.camps() == []
    m.see_tiles(76, [camp(x=69, y=27)])  # Seen: now it is real.
    assert [(c.x, c.y) for c in m.camps()] == [(69, 27)]


def _own(x, y, kind="UNIT_WARRIOR", cs=20):
    from civ6_ai.state.reader import build_unit
    from fixtures.civ_state import unit_info

    return build_unit(unit_info(x=x, y=y, unit_type=kind, combat_strength=cs), 74)


def test_a_remembered_unit_is_dropped_when_one_of_ours_stands_on_its_tile():
    m = ThreatMap(ThreatMapSettings(map_width=None))
    m.see_threats(57, [hostile("UNIT_SCOUT", x=65, y=22, cs=10)])
    m.see_threats(58, [])  # Not seen now: remembered.
    m.see_own(58, [_own(60, 20)], [])
    assert len(m.sources) == 1
    m.see_own(61, [_own(65, 22)], [])  # Ours stands there: it is gone.
    assert m.sources == []


def test_a_unit_seen_this_turn_is_not_dropped_by_ours_beside_it():
    m = ThreatMap(ThreatMapSettings(map_width=None))
    m.see_threats(60, [hostile(x=10, y=10)])
    m.see_own(60, [_own(10, 11)], [])
    assert len(m.sources) == 1


def test_our_cities_and_units_give_cover_and_it_falls_off_with_distance():
    from civ6_ai.state.reader import build_city

    m = ThreatMap(ThreatMapSettings(map_width=None))
    city = build_city(city_info(x=20, y=20), (), set(), 74)
    m.see_own(1, [_own(22, 20)], [city])
    assert m.cover_at(20, 20) > m.cover_at(22, 20) * 0 and m.cover_at(21, 20) > 0
    assert m.cover_at(21, 20) > m.cover_at(23, 20) > 0
    assert m.cover_at(30, 20) == 0


def test_net_threat_is_threat_less_cover_and_never_negative():
    from civ6_ai.state.reader import build_city

    m = ThreatMap(ThreatMapSettings(map_width=None))
    m.see_threats(1, [hostile(x=10, y=10, cs=10)])
    far = m.net_at(10, 10, 1)
    city = build_city(city_info(x=10, y=11), (), set(), 74)
    m.see_own(1, [], [city])
    assert m.net_at(10, 10, 1) < far
    assert m.net_at(30, 30, 1) == 0


def test_fog_is_a_mild_threat_beside_unseen_ground_near_our_cities():
    from civ6_ai.state.reader import build_city
    from civ6_ai.terrain import TerrainStore

    terrain = TerrainStore(map_width=None)
    terrain.learn([build_tile(tile(x=x, y=10, improvement=None, units=[])) for x in range(0, 8)])
    m = ThreatMap(ThreatMapSettings(map_width=None), terrain=terrain)
    city = build_city(city_info(x=3, y=10), (), set(), 74)
    m.see_own(1, [], [city])
    assert 0 < m.fog_at(3, 10) < m.settings.fog_penalty  # Row 10 is known; rows around are not.
    assert m.fog_at(40, 40) == 0  # Far from any city of ours.


def test_a_stronger_unit_gives_more_cover_than_a_weaker_one():
    m = ThreatMap(ThreatMapSettings(map_width=None))
    m.see_own(1, [_own(10, 10, "UNIT_WARRIOR", cs=8)], [])
    warrior = m.cover_at(10, 10)
    m.see_own(1, [_own(10, 10, "UNIT_MODERN_ARMOR", cs=80)], [])
    assert m.cover_at(10, 10) == warrior * 10  # Strength 80 against 8, with no cap.


def test_a_citys_cover_is_its_defence_strength_and_falls_with_its_damage():
    from civ6_ai.state.reader import build_city

    m = ThreatMap(ThreatMapSettings(map_width=None))
    whole = build_city(city_info(x=5, y=5, defense_strength=12, garrison_hp=200), (), set(), 74)
    m.see_own(1, [], [whole])
    full = m.cover_at(5, 5)
    assert full == 12
    hurt = build_city(city_info(x=5, y=5, defense_strength=12, garrison_hp=100), (), set(), 74)
    m.see_own(1, [], [hurt])
    assert m.cover_at(5, 5) < full


def test_a_camps_strength_is_the_strongest_barbarian_land_unit_seen():
    m = ThreatMap(ThreatMapSettings(map_width=None, camp_strength=15.0))
    m.see_tiles(1, [camp()])
    assert m.strength_of(m.camps()[0]) == 15.0  # The floor, before any barbarian is seen.
    m.see_threats(2, [hostile("UNIT_SWORDSMAN", x=40, y=40, cs=36)])
    assert m.strength_of(m.camps()[0]) == 36.0
    m.see_threats(3, [hostile("UNIT_GALLEY", x=50, y=50, cs=60)])  # A ship doesn't count.
    assert m.strength_of(m.camps()[0]) == 36.0
