import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from civ6_ai import observe
from civ6_ai.observe import Observer, build_turn_record, new_events


@dataclass
class Player:
    pid: int
    score: int
    cities: int = 1
    pop: int = 3
    era: str = "ERA_ANCIENT"
    era_score: int = 0
    techs: list[str] = field(default_factory=list)
    civics: list[str] = field(default_factory=list)


@dataclass
class City:
    pid: int
    city_id: int
    city: str


@dataclass
class Diary:
    players: list[Player]
    cities: list[City]


@dataclass
class Overview:
    turn: int
    player_id: int = 0
    civ_name: str = "Maya"
    leader_name: str = "Leader"


@dataclass
class Unit:
    unit_id: int
    unit_type: str
    x: int
    y: int
    moves_remaining: float
    max_moves: float
    health: int
    max_health: int


@dataclass
class CityInfo:
    city_id: int
    name: str
    x: int
    y: int
    population: int
    districts: list[str]
    currently_building: str = "NONE"


@dataclass
class Note:
    type_name: str
    message: str
    turn: int
    x: int
    y: int


class FakeGame:
    def __init__(self):
        self.turn = 1
        self.techs: list[str] = []
        self.cities = [City(0, 1, "Capital")]

    async def get_game_overview(self):
        return Overview(self.turn)

    async def get_diary_snapshot(self):
        return Diary(
            [Player(0, 10 + self.turn, len(self.cities), techs=list(self.techs)), Player(1, 15)],
            list(self.cities),
        )

    async def get_tech_civics(self):
        return {"current": "MINING"}

    async def get_trade_routes(self):
        return {"capacity": 0}

    async def get_units(self):
        return [Unit(7, "UNIT_SETTLER", 3, 4, 2.0, 2.0, 100, 100)]

    async def get_cities(self):
        return [CityInfo(1, "Capital", 3, 4, 2, ["DISTRICT_CITY_CENTER"])], []

    async def get_notifications(self):
        return [Note("NOTIFICATION_PRODUCTION", "Pick production", self.turn, 3, 4)]


def test_turn_record_has_standing_and_rank():
    diary = Diary([Player(0, 10), Player(1, 15), Player(2, 5)], [])
    record = build_turn_record(Overview(7), diary)
    assert (record.turn, record.score, record.rank, record.leader_score) == (7, 10, 2, 15)
    assert record.rival_scores == {"1": 15, "2": 5}
    assert record.all_scores == [15, 5]


def test_new_events_finds_cities_and_techs():
    game = FakeGame()
    before = observe.build_observation(
        Overview(1), asyncio.run(game.get_diary_snapshot()), None, None
    )
    game.techs = ["TECH_POTTERY"]
    game.cities.append(City(0, 2, "Second"))
    after = observe.build_observation(
        Overview(2), asyncio.run(game.get_diary_snapshot()), None, None
    )
    kinds = {(e["kind"], e.get("city") or e.get("name")) for e in new_events(before, after)}
    assert kinds == {("city_gained", "Second"), ("tech_complete", "TECH_POTTERY")}
    turns = {e["kind"]: e["turn"] for e in new_events(before, after)}
    assert turns == {"city_gained": 1, "tech_complete": 2}  # Founded on T1; tech lands at T2 start.
    assert new_events(None, after) == []


def test_observer_writes_one_record_per_new_turn(tmp_path: Path):
    game = FakeGame()
    observer = Observer(game, tmp_path, "human_test")

    async def run():
        assert await observer.observe_once()
        assert not await observer.observe_once()  # same turn: nothing written
        game.turn = 2
        game.cities.append(City(0, 2, "Second"))
        assert await observer.observe_once()

    asyncio.run(run())
    turns = [json.loads(line) for line in (tmp_path / "human_test.jsonl").read_text().splitlines()]
    assert [t["turn"] for t in turns] == [1, 2]
    observed = (tmp_path / "human_test_observed.jsonl").read_text().splitlines()
    assert len(observed) == 2
    events = (tmp_path / "human_test_events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["kind"] == "city_gained"


def test_watch_retries_a_garbled_read_and_keeps_going(tmp_path: Path):
    game = FakeGame()
    real = game.get_game_overview
    failed = {"done": False}

    async def flaky():
        # The first read after turn 1 is recorded comes back garbled, once.
        if observer.last_turn == 1 and not failed["done"]:
            failed["done"] = True
            raise ValueError("Overview response has 1 fields: PlotInfo::OnCitySelectionChanged")
        return await real()

    game.get_game_overview = flaky
    observer = Observer(game, tmp_path, "human_flaky")
    logs: list[str] = []

    async def sleep(_):
        if observer.last_turn == 1:
            game.turn = 2  # The turn changes while the next read is about to fail.

    asyncio.run(observer.watch(until_turn=2, sleep=sleep, log=logs.append))
    turns = [
        json.loads(x)["turn"] for x in (tmp_path / "human_flaky.jsonl").read_text().split("\n") if x
    ]
    assert turns == [1, 2]
    assert any("will retry" in line for line in logs)


def test_watch_gives_up_after_many_failures_in_a_row(tmp_path: Path):
    game = FakeGame()

    async def broken():
        raise ValueError("garbled")

    game.get_game_overview = broken
    observer = Observer(game, tmp_path, "human_broken")

    async def sleep(_):
        pass

    try:
        asyncio.run(observer.watch(sleep=sleep, log=lambda _: None))
    except ValueError:
        pass
    else:
        raise AssertionError("should have given up")


def test_resume_carries_on_without_rewriting_the_last_turn(tmp_path: Path):
    game = FakeGame()
    first = Observer(game, tmp_path, "human_resume")
    asyncio.run(first.observe_once())

    again = Observer(game, tmp_path, "human_resume")
    assert again.last_turn == 1
    assert not asyncio.run(again.observe_once())  # Same turn: nothing written.
    game.turn = 2
    assert asyncio.run(again.observe_once())
    lines = (tmp_path / "human_resume.jsonl").read_text().splitlines()
    assert [json.loads(x)["turn"] for x in lines] == [1, 2]


def test_observer_source_calls_nothing_that_changes_the_game():
    names = (
        "end_turn|dismiss_popup|load_game_save|press_escape|move_unit|set_research|found_city"
        "|save_checkpoint"  # The start save; allowed once, in the script, and checked below.
    )
    forbidden = rf"\b({names})\("
    for path in (Path(observe.__file__), Path(__file__).parents[1] / "scripts" / "observe_game.py"):
        code = re.sub(r'""".*?"""', "", path.read_text(encoding="utf-8"), flags=re.S)
        code = re.sub(r"#.*", "", code)
        if path.name == "observe_game.py":
            assert code.count("save_checkpoint(") == 1  # Only the start save.
            code = code.replace("save_checkpoint(", "")
        assert not re.search(forbidden, code), path.name


def test_observed_at_is_taken_when_the_turn_is_first_seen(tmp_path: Path):
    from datetime import UTC, datetime, timedelta

    game = FakeGame()
    clock = {"t": datetime(2026, 1, 1, tzinfo=UTC)}

    async def slow_diary():
        clock["t"] += timedelta(seconds=30)  # The slower reads take time after the overview.
        return Diary([Player(0, 10), Player(1, 15)], list(game.cities))

    game.get_diary_snapshot = slow_diary
    observer = Observer(game, tmp_path, "human_clock", now=lambda: clock["t"])
    asyncio.run(observer.observe_once())
    row = json.loads((tmp_path / "human_clock_observed.jsonl").read_text().splitlines()[0])
    assert row["observed_at"] == "2026-01-01T00:00:00+00:00"


def test_a_turn_that_moves_during_the_reads_is_not_recorded(tmp_path: Path):
    game = FakeGame()
    real_routes = game.get_trade_routes

    async def routes_then_end_turn():
        result = await real_routes()
        game.turn += 1  # The player ends the turn while the observer is reading.
        return result

    game.get_trade_routes = routes_then_end_turn
    observer = Observer(game, tmp_path, "human_moved")
    assert not asyncio.run(observer.observe_once())
    assert not (tmp_path / "human_moved.jsonl").exists()
    game.get_trade_routes = real_routes
    assert asyncio.run(observer.observe_once())  # The next poll records turn 2 whole.
    row = json.loads((tmp_path / "human_moved.jsonl").read_text().splitlines()[0])
    assert row["turn"] == 2


def test_row_has_units_cities_and_notifications(tmp_path: Path):
    observer = Observer(FakeGame(), tmp_path, "human_detail")
    asyncio.run(observer.observe_once())
    row = json.loads((tmp_path / "human_detail_observed.jsonl").read_text().splitlines()[0])
    assert row["units"][0]["type"] == "UNIT_SETTLER"
    assert (row["units"][0]["x"], row["units"][0]["moves"], row["units"][0]["health"]) == (
        3,
        2.0,
        100,
    )
    assert row["city_sites"][0]["districts"] == ["DISTRICT_CITY_CENTER"]
    assert (row["city_sites"][0]["x"], row["city_sites"][0]["y"]) == (3, 4)
    assert row["notifications"][0]["text"] == "Pick production"


def test_screenshots_only_on_the_set_turns(tmp_path: Path):
    game = FakeGame()
    taken: list[int] = []
    observer = Observer(game, tmp_path, "human_shots", shots=taken.append)
    for turn in (1, 25, 26, 50):
        game.turn = turn
        asyncio.run(observer.observe_once())
    assert taken == [25, 50]


def test_header_has_seeds_save_civ_and_start_time(tmp_path: Path):
    from datetime import UTC, datetime

    sent: list[str] = []

    async def execute(lua: str) -> list[str]:
        sent.append(lua)
        return ["OBSERVE|game_seed|123", "OBSERVE|map_seed|456", "other"]

    class Game(FakeGame):
        async def get_game_overview(self):
            return Overview(5, civ_name="Rome", leader_name="Trajan")

    path = tmp_path / "human_x_header.json"
    started = datetime(2026, 1, 1, tzinfo=UTC)
    asyncio.run(observe.write_header(path, Game(), execute, "HUMAN_x_T5", started))
    header = json.loads(path.read_text())
    assert header == {
        "save": "HUMAN_x_T5",
        "game_seed": "123",
        "map_seed": "456",
        "civ": "Rome",
        "leader": "Trajan",
        "turn": 5,
        "started_at": "2026-01-01T00:00:00+00:00",
    }
    assert "Network.SaveGame" not in sent[0]  # The seed read only reads.
