from civ6_ai.state.reader import build_tile
from civ6_ai.terrain import TerrainStore
from civ6_ai.threat_map import CAMP_IMPROVEMENT, Source, ThreatMap, ThreatMapSettings
from fixtures.civ_state import tile

W = None  # No map wrap in these small maps.


def ground(x, y, terrain="TERRAIN_GRASS", feature=None, hills=False, river=False, vis="visible"):
    return build_tile(
        tile(
            x=x, y=y, terrain=terrain, feature=feature, is_hills=hills, is_river=river,
            improvement=None, visibility=vis, units=[],
        )
    )  # fmt: skip


def store(rows):
    """rows maps (x, y) to a code: g flat, w water, h hills, f forest, m mountain, r river.
    Every tile in 0..9 by 0..9 not listed is flat land."""
    t = TerrainStore(map_width=None)
    tiles = []
    for y in range(0, 10):
        for x in range(0, 10):
            code = rows.get((x, y), "g")
            if code == "w":
                tiles.append(ground(x, y, terrain="TERRAIN_COAST"))
            elif code == "h":
                tiles.append(ground(x, y, hills=True))
            elif code == "f":
                tiles.append(ground(x, y, feature="FEATURE_FOREST"))
            elif code == "m":
                tiles.append(ground(x, y, terrain="TERRAIN_GRASS_MOUNTAIN"))
            elif code == "r":
                tiles.append(ground(x, y, river=True))
            else:
                tiles.append(ground(x, y))
    t.learn(tiles)
    return t


def test_the_store_remembers_ground_including_fogged_tiles_and_reports_new_tiles_once():
    t = TerrainStore(map_width=None)
    assert t.learn([ground(1, 1), ground(2, 1, terrain="TERRAIN_COAST", vis="revealed")]) == 2
    assert t.learn([ground(1, 1)]) == 0
    assert t.get(2, 1).water
    assert t.take_new() == {(1, 1): "g", (2, 1): "w"}
    assert t.take_new() == {}


def test_step_costs_follow_the_ground():
    t = store({(1, 0): "h", (2, 0): "f", (3, 0): "m", (4, 0): "w", (5, 0): "r"})
    assert t.step_cost(0, 0, naval=False) == 1
    assert t.step_cost(1, 0, naval=False) == 2  # Hills.
    assert t.step_cost(2, 0, naval=False) == 2  # Woods.
    assert t.step_cost(3, 0, naval=False) is None  # Mountains.
    assert t.step_cost(4, 0, naval=False) is None  # A walker can't enter water.
    assert t.step_cost(5, 0, naval=False) == 2  # A river costs one more.
    assert t.step_cost(4, 0, naval=True) == 1
    assert t.step_cost(0, 0, naval=True) is None  # A ship can't enter land.


def test_a_tile_never_read_is_slow_for_a_walker_and_closed_to_a_ship():
    t = TerrainStore(map_width=None)
    assert t.step_cost(5, 5, naval=False) == t.unknown_land_cost
    assert t.step_cost(5, 5, naval=True) is None


def test_hills_slow_a_walker():
    flat = store({}).reach(1, 5, 2, False, 3)
    hilly = store({(x, y): "h" for x in range(2, 5) for y in range(3, 8)}).reach(1, 5, 2, False, 3)
    assert hilly[(4, 5)] > flat[(4, 5)]


def test_a_bay_stops_a_walker():
    sea = {(4, y): "w" for y in range(0, 10)}  # A strait from top to bottom.
    reach = store(sea).reach(2, 5, 2, False, 3)
    assert (3, 5) in reach
    assert not any(x >= 5 for x, _ in reach)


def test_a_ship_reaches_the_water_and_only_the_coast_beside_it():
    water = {(x, y): "w" for x in range(4, 10) for y in range(0, 10)}  # Land to the west.
    reach = store(water).reach(6, 5, 3, True, 3)
    assert (9, 5) in reach  # Along the water.
    assert (3, 5) in reach  # The coast beside the water.
    assert (2, 5) not in reach  # Not further inland.
    assert (0, 5) not in reach


def test_a_ranged_unit_shoots_past_where_it_can_stand():
    near = store({}).reach(5, 5, 2, False, 0.5)  # Can only stand within a tile.
    far = store({}).reach(5, 5, 2, False, 0.5, reach_range=2)
    assert len(far) > len(near)


def test_the_threat_from_a_galley_follows_the_water_not_a_circle():
    water = {(x, y): "w" for x in range(4, 10) for y in range(0, 10)}
    terrain = store(water)
    tm = ThreatMap(ThreatMapSettings(map_width=None), terrain=terrain)
    tm.sources.append(Source("unit", 6, 5, 30.0, 1, 1, unit_type="UNIT_GALLEY", naval=True))
    assert tm.threat_at(8, 5, 1) > 0  # Out to sea.
    assert tm.threat_at(3, 5, 1) > 0  # The coast beside it.
    assert tm.threat_at(1, 5, 1) == 0  # Inland to the west, three tiles from the coast.
    flat = ThreatMap(ThreatMapSettings(map_width=None))
    flat.sources.append(Source("unit", 6, 5, 30.0, 1, 1, unit_type="UNIT_GALLEY", naval=True))
    assert flat.threat_at(3, 5, 1) > 0 and flat.threat_at(4, 5, 1) > 0  # A circle covers land.


def test_a_camp_behind_a_strait_is_no_threat_to_the_far_side():
    sea = {(4, y): "w" for y in range(0, 10)}
    tm = ThreatMap(ThreatMapSettings(map_width=None), terrain=store(sea))
    tm.sources.append(Source("camp", 2, 5, 25.0, 1, 1))
    assert tm.threat_at(3, 5) > 0
    assert tm.threat_at(6, 5) == 0


def test_a_camp_read_is_the_camp_improvement():
    assert CAMP_IMPROVEMENT == "IMPROVEMENT_BARBARIAN_CAMP"


def test_step_toward_goes_around_a_mountain_and_water_and_avoids_refused_tiles():
    t = store({(2, 5): "m", (2, 4): "w", (2, 6): "w"})
    step = t.step_toward((0, 5), (6, 5), 2)
    assert step is not None and step not in {(2, 5), (2, 4), (2, 6)}
    assert t.step_toward((0, 5), (6, 5), 2, avoid={(1, 5)}) != (1, 5)
    assert t.step_toward((0, 5), (6, 5), 2, stop_short=True) is not None
    path = t.path_to((0, 5), (6, 5))
    assert path[-1] == (6, 5) and (2, 5) not in path


def test_step_toward_spends_no_more_than_the_moves():
    t = store({(1, 5): "f", (1, 4): "f", (1, 6): "f", (1, 3): "f", (1, 7): "f"})
    assert t.step_toward((0, 5), (6, 5), 2) is not None
    assert t.path_to((0, 5), (0, 5)) == []


def test_paths_never_enter_rows_beyond_the_map():
    mountains = {(11, 9): "m", (9, 8): "m", (10, 8): "m", (11, 8): "m", (12, 8): "m", (13, 8): "m"}
    t = store(mountains | {(x, 9): "g" for x in (9, 10, 12, 13)})
    path = t.path_to((10, 9), (12, 9))
    assert path is None or all(y <= 9 for _, y in path)
