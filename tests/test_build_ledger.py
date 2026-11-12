import asyncio
import json
import time
from types import SimpleNamespace

from civ6_ai.build_ledger import (
    BuildLedger,
    BuildTracker,
    EventFlush,
    earlier_event_files,
    find_gaps,
    gap_line,
)
from civ6_ai.construction import (
    BuildingCompleted,
    DistrictCompleted,
    DistrictLost,
    ProductionRefused,
    ProductionSet,
)
from civ6_ai.monitors import CityLost, EventBus, EventLog


def city(city_id=1, districts=(), buildings=(), pop=6, housing=7, building="nothing"):
    return SimpleNamespace(
        city_id=city_id,
        name=f"City{city_id}",
        x=10,
        y=10,
        districts=list(districts),
        buildings=list(buildings),
        currently_building=building,
        population=pop,
        housing=housing,
    )


def completed(turn, district="DISTRICT_CAMPUS", city_id=1):
    return DistrictCompleted(
        turn, city_id, f"City{city_id}", 10, 10, district=district, district_x=11, district_y=10
    )


def refused(turn, city_id, item="DISTRICT_CAMPUS", is_refusal=True):
    return ProductionRefused(
        turn,
        city_id,
        f"City{city_id}",
        10,
        10,
        item=item,
        category="DISTRICT",
        reply="ERR:X",
        is_refusal=is_refusal,
    )


def started(turn, city_id, item="DISTRICT_CAMPUS"):
    return ProductionSet(turn, city_id, f"City{city_id}", 10, 10, item=item, category="DISTRICT")


def test_ledger_folds_districts_buildings_and_losses():
    ledger = BuildLedger()
    ledger.apply(completed(5))
    ledger.apply(BuildingCompleted(6, 1, "City1", 10, 10, building="LIBRARY"))
    assert ledger.district_types() == {"CAMPUS"}
    assert ledger.buildings[1] == {"LIBRARY"}
    ledger.apply(
        DistrictLost(
            7, 1, "City1", 10, 10, district="DISTRICT_CAMPUS", district_x=11, district_y=10
        )
    )
    assert ledger.district_types() == set()
    ledger.apply(completed(8))
    ledger.apply(CityLost(9, 1, "City1", 10, 10))
    assert ledger.district_types() == set() and 1 not in ledger.buildings


def test_gap_no_wanted_district_anywhere():
    ledger = BuildLedger()
    gaps = find_gaps(ledger, 60, "any", [city()])
    assert {g.subject for g in gaps} == {
        "no CAMPUS",
        "no COMMERCIAL_HUB",
        "no HOLY_SITE",
        "no THEATER",
    }
    assert all("never started" in g.detail for g in gaps)
    ledger.apply(started(50, 1))
    campus = next(g for g in find_gaps(ledger, 60, "any", [city()]) if "CAMPUS" in g.subject)
    assert campus.detail == "(started)"
    assert find_gaps(ledger, 10, "any", [city()]) == []  # Too early to expect any.


def test_victory_aim_adds_planned_special_districts():
    cities = [city()]
    science = {g.subject for g in find_gaps(BuildLedger(), 150, "science", cities)}
    assert "no SPACEPORT" in science
    assert "no SPACEPORT" not in {g.subject for g in find_gaps(BuildLedger(), 150, "any", cities)}
    ledger = BuildLedger()
    ledger.apply(completed(100, "DISTRICT_SPACEPORT"))
    assert "no SPACEPORT" not in {g.subject for g in find_gaps(ledger, 150, "science", cities)}


def test_gap_district_with_none_of_its_buildings():
    ledger = BuildLedger()
    ledger.apply(completed(10))
    ledger.apply(completed(10, "DISTRICT_CITY_CENTER"))  # Has no table entry, never a gap.
    assert [g for g in find_gaps(ledger, 15, "any", []) if g.kind == "empty_district"] == []
    gaps = [g for g in find_gaps(ledger, 30, "any", []) if g.kind == "empty_district"]
    assert [g.text() for g in gaps] == ["empty CAMPUS in City1 (20t)"]
    ledger.apply(BuildingCompleted(31, 1, "City1", 10, 10, building="BUILDING_LIBRARY"))
    assert [g for g in find_gaps(ledger, 40, "any", []) if g.kind == "empty_district"] == []


def test_gap_refused_in_several_cities_and_never_started():
    ledger = BuildLedger()
    for c in (1, 2):
        ledger.apply(refused(20, c, "DISTRICT_THEATER"))
    ledger.apply(refused(20, 3, "DISTRICT_THEATER", is_refusal=False))  # A failed call, not a no.
    assert find_gaps(ledger, 20, "any", []) == []
    ledger.apply(refused(21, 3, "DISTRICT_THEATER"))
    assert [g.text() for g in find_gaps(ledger, 21, "any", [])] == ["THEATER refused in 3 cities"]
    ledger.apply(started(22, 4, "DISTRICT_THEATER"))
    assert find_gaps(ledger, 22, "any", []) == []


def test_gap_line_is_short():
    assert gap_line([]) == "build gaps: none"
    line = gap_line(find_gaps(BuildLedger(), 60, "science", [city()]))
    assert line.startswith("build gaps: no CAMPUS") and "more)" not in line
    long = gap_line(find_gaps(BuildLedger(), 150, "science", [city()]))
    assert "(+" in long and len(long) < 200


def test_observe_adds_what_no_event_told_but_skips_a_district_in_progress():
    ledger = BuildLedger()
    ledger.observe(
        30,
        [
            city(
                districts=["DISTRICT_CAMPUS@11,10", "DISTRICT_THEATER@12,10"],
                buildings=["BUILDING_LIBRARY"],
                building="DISTRICT_THEATER",
            )
        ],
    )
    assert ledger.district_types() == {"CAMPUS"}
    assert ledger.buildings[1] == {"LIBRARY"}


def test_restart_restores_history_from_earlier_event_files(tmp_path):
    first = tmp_path / "save_20260101T000000Z_events.jsonl"
    bus = EventBus()
    log = EventLog(first)
    bus.subscribe(log.write)
    for event in (completed(5), started(4, 1), refused(6, 2, "DISTRICT_THEATER")):
        bus.publish(event)
    log.flush()
    second = tmp_path / "save_20260102T000000Z_events.jsonl"
    second.write_text(
        "not json\n" + completed(9, "DISTRICT_THEATER", 2).to_json() + "\n", encoding="utf-8"
    )
    ledger = BuildLedger()
    assert ledger.restore([first, second]) == 4
    assert ledger.district_types() == {"CAMPUS", "THEATER"}
    assert "CAMPUS" in ledger.started and ledger.refused["THEATER"] == {2}


def test_restore_is_size_bounded_and_keeps_the_newest(tmp_path):
    path = tmp_path / "e.jsonl"
    lines = [completed(t, city_id=t).to_json() for t in range(1, 201)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ledger = BuildLedger()
    applied = ledger.restore([path], max_bytes=len(lines[0]) * 20)
    assert 15 <= applied <= 20
    assert 200 in ledger.districts and 1 not in ledger.districts


def test_earlier_event_files_picks_this_games_sessions(tmp_path):
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (tmp_path / f"s_{stamp}_events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "other_20260101T000000Z_events.jsonl").write_text("", encoding="utf-8")
    ids = {"s_20260101T000000Z": "g1", "s_20260102T000000Z": "g2", "s_20260103T000000Z": "g2"}
    found = earlier_event_files(
        tmp_path, "s", "g2", "s_20260103T000000Z", lambda p: ids.get(p.stem)
    )
    assert [p.name for p in found] == ["s_20260102T000000Z_events.jsonl"]


class FakeGame:
    def __init__(self, num_cities=None):
        self.turn = 1
        self.cities = [city()]
        self.num_cities = num_cities

    async def get_game_overview(self):
        count = len(self.cities) if self.num_cities is None else self.num_cities
        return SimpleNamespace(turn=self.turn, num_cities=count)

    async def get_cities(self):
        return self.cities, []


def test_tracker_logs_a_gap_line_each_turn(caplog):
    game = FakeGame()
    game.turn = 60
    tracker = BuildTracker(BuildLedger(), victory=lambda: "science")
    with caplog.at_level("INFO", logger="civ6_ai.build_ledger"):
        asyncio.run(tracker.run(game))
    assert any(r.message.startswith("build gaps: no CAMPUS") for r in caplog.records)


def test_tracker_skips_a_short_city_read(caplog):
    game = FakeGame(num_cities=5)  # The overview counts more cities than were read.
    game.turn = 60
    with caplog.at_level("INFO", logger="civ6_ai.build_ledger"):
        asyncio.run(BuildTracker(BuildLedger()).run(game))
    assert not caplog.records


def test_flush_handler_writes_the_turns_events(tmp_path):
    log = EventLog(tmp_path / "e.jsonl")
    log.write(completed(1))
    assert not (tmp_path / "e.jsonl").exists()
    asyncio.run(EventFlush(log).run(None))
    line = (tmp_path / "e.jsonl").read_text(encoding="utf-8")
    assert json.loads(line)["kind"] == "district_completed"


def test_tracker_and_buffered_log_stay_within_the_per_turn_budget(tmp_path):
    """Added cost per turn with 20 cities and a busy turn of events, over 300 turns."""
    bus = EventBus()
    log = EventLog(tmp_path / "e.jsonl")
    bus.subscribe(log.write)
    ledger = BuildLedger()
    ledger.subscribe(bus)
    tracker = BuildTracker(ledger, victory=lambda: "science")
    flush = EventFlush(log)
    game = FakeGame()
    game.cities = [
        city(
            i,
            districts=[f"DISTRICT_CAMPUS@{i},1", f"DISTRICT_THEATER@{i},2"],
            buildings=["BUILDING_LIBRARY"],
        )
        for i in range(1, 21)
    ]

    async def play():
        spent = 0.0
        for turn in range(1, 301):
            game.turn = turn
            began = time.perf_counter()
            for i in range(1, 21):
                bus.publish(started(turn, i))
                bus.publish(refused(turn, i, "DISTRICT_X"))
            await tracker.run(game)
            await flush.run(game)
            spent += time.perf_counter() - began
        return spent / 300

    average = asyncio.run(play())
    print(f"added per turn: {average * 1000:.2f} ms")
    assert average < 0.050
