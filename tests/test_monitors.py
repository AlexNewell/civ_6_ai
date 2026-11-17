import asyncio
import dataclasses
import json

import pytest
from civ_mcp.lua.models import (
    CityInfo,
    CivInfo,
    DemographicEntry,
    GameOverview,
    ResourceStockpile,
    ThreatInfo,
    VictoryPlayerProgress,
    VictoryProgress,
    VisibleCity,
)

from civ6_ai.monitors import (
    ABSENT_CHECKS,
    BARBARIAN_ID,
    EVENT_TYPES,
    THREAT_RADIUS,
    CityLost,
    CityLoyaltyWarning,
    EventBus,
    EventLog,
    GoldShortage,
    HostileUnitsNear,
    IncompleteRead,
    MonitorError,
    MonitorGame,
    Monitors,
    ResourceMonitor,
    RivalVictoryWarning,
    StrategicResourceShortage,
    ThreatMonitor,
    VictoryMonitor,
    VictoryPath,
    WarDeclared,
    event_from_json,
)
from civ6_ai.orchestrator import Orchestrator, StopReason, TurnLog
from civ6_ai.victory_threat import VictoryWatch

US = 0
ROME = 1
EGYPT = 2


def city(city_id=1, name="Home", loyalty=100.0, loyalty_per_turn=0.0):
    return CityInfo(
        city_id=city_id,
        name=name,
        x=city_id,
        y=city_id,
        population=1,
        food=0,
        production=0,
        gold=0,
        science=0,
        culture=0,
        faith=0,
        housing=0,
        amenities=0,
        turns_to_grow=0,
        loyalty=loyalty,
        loyalty_per_turn=loyalty_per_turn,
    )


def civ(player_id, name, met=True, at_war=False):
    return CivInfo(player_id, name, "Leader", has_met=met, is_at_war=at_war)


def unit(owner_id=ROME, owner_name="Rome", distance=2, unit_type="UNIT_WARRIOR", city_state=False):
    return ThreatInfo(
        unit_type=unit_type,
        x=1,
        y=1,
        hp=100,
        max_hp=100,
        combat_strength=20,
        ranged_strength=0,
        distance=distance,
        owner_id=owner_id,
        owner_name=owner_name,
        is_city_state=city_state,
    )


def stockpile(name="IRON", amount=5, per_turn=0, demand=1, imported=0):
    return ResourceStockpile(name, amount, 50, per_turn, demand, imported)


def player(player_id, name, science=0, diplomatic=0):
    return VictoryPlayerProgress(
        player_id=player_id,
        name=name,
        score=0,
        science_vp=science,
        science_vp_needed=50,
        diplomatic_vp=diplomatic,
        tourism=0,
        military_strength=0,
        techs_researched=0,
        civics_completed=0,
        religion_cities=0,
    )


class FakeGame:
    """Serves only what's set on it, and records which reads were made on which turn."""

    def __init__(self, script=None):
        self.turn = 1
        self.script = script or {}
        self.gold = 100.0
        self.gold_per_turn = 5.0
        self.cities = [city(1, "Home"), city(2, "Colony")]
        self.reported_cities = None  # Set to differ from what a read returns.
        self.civs = [civ(ROME, "Rome"), civ(EGYPT, "Egypt"), civ(3, "Unmet", met=False)]
        self.threats = []
        self.stockpiles = []
        self.victory = VictoryProgress(
            players=[player(US, "Us"), player(ROME, "Rome"), player(EGYPT, "Egypt")]
        )
        self.reads = []
        self.failing = set()

    def _read(self, name):
        self.reads.append((self.turn, name))
        if name in self.failing:
            raise RuntimeError(f"{name} failed")

    async def check_game_over(self):
        return None

    async def get_game_overview(self):
        self.reads.append((self.turn, "get_game_overview"))
        return GameOverview(
            turn=self.turn,
            player_id=US,
            civ_name="Us",
            leader_name="Leader",
            gold=self.gold,
            gold_per_turn=self.gold_per_turn,
            science_yield=0,
            culture_yield=0,
            faith=0,
            current_research="None",
            current_civic="None",
            num_cities=len(self.cities if self.reported_cities is None else self.reported_cities),
            num_units=0,
        )

    async def get_cities(self):
        self._read("get_cities")
        return self.cities, []

    async def get_diplomacy(self):
        self._read("get_diplomacy")
        return self.civs

    async def get_threat_scan(self):
        self._read("get_threat_scan")
        return self.threats

    async def get_empire_resources(self):
        self._read("get_empire_resources")
        return self.stockpiles, [], [], {}

    async def get_victory_progress(self):
        self._read("get_victory_progress")
        return self.victory

    async def end_turn(self):
        self.turn += 1
        if self.turn in self.script:
            self.script[self.turn](self)
        return f"Turn {self.turn - 1} -> {self.turn}"

    def read_turns(self, name):
        return [turn for turn, read in self.reads if read == name]


def check(monitor, game):
    """Run one check, then move to the next turn."""
    overview = asyncio.run(game.get_game_overview())
    events = asyncio.run(monitor.check(game, overview))
    game.turn += 1
    return events


def collect(bus):
    events = []
    bus.subscribe(events.append)
    return events


def set_at_war(game, at_war=True):
    game.civs[0].is_at_war = at_war


def test_fifty_turns_through_the_turn_loop_run_on_schedule_and_raise_each_event(tmp_path):
    def war(game):
        game.civs[0].is_at_war = True

    def invade(game):
        game.threats = [unit() for _ in range(3)]

    def unrest(game):
        game.cities[1].loyalty_per_turn = -20.0

    def lose_city(game):
        del game.cities[1]

    def run_out_of_iron(game):
        game.stockpiles = [stockpile(amount=5, demand=2)]

    def overspend(game):
        game.gold_per_turn = -50.0

    def rome_gains_points(game):
        game.victory.players[1].diplomatic_vp = 10

    game = FakeGame(
        {
            7: war,
            12: invade,
            15: unrest,
            20: lose_city,
            23: run_out_of_iron,
            33: overspend,
            43: rome_gains_points,
        }
    )
    bus = EventBus()
    events = collect(bus)
    turn_log = TurnLog(tmp_path / "game.jsonl")

    reason = asyncio.run(Orchestrator(game, [Monitors(bus)], turn_log).play(50))

    assert reason is StopReason.TURN_LIMIT
    assert len(turn_log.records) == 50
    assert all(not record.handler_errors for record in turn_log.records)
    assert all("monitors" in record.handler_seconds for record in turn_log.records)

    every_fifth_turn = list(range(1, 51, 5))
    assert game.read_turns("get_threat_scan") == list(range(1, 51))
    assert game.read_turns("get_cities") == list(range(1, 51))
    assert game.read_turns("get_empire_resources") == every_fifth_turn
    assert game.read_turns("get_victory_progress") == every_fifth_turn

    assert [(type(event), event.turn) for event in events] == [
        (WarDeclared, 7),
        (HostileUnitsNear, 12),
        (CityLoyaltyWarning, 15),
        (CityLost, 20),
        (StrategicResourceShortage, 26),
        (GoldShortage, 36),
        (RivalVictoryWarning, 46),
    ]
    assert {type(event) for event in events} == set(EVENT_TYPES)


def test_war_declared_fires_once_on_the_change():
    game = FakeGame()
    monitor = ThreatMonitor()
    assert check(monitor, game) == []
    set_at_war(game)
    assert check(monitor, game) == [WarDeclared(2, ROME, "Rome")]
    assert check(monitor, game) == []


def test_a_war_already_running_at_the_first_check_is_not_news():
    game = FakeGame()
    set_at_war(game)
    assert check(ThreatMonitor(), game) == []


def test_a_short_diplomacy_read_cant_fake_a_new_war():
    game = FakeGame()
    set_at_war(game)
    monitor = ThreatMonitor()
    check(monitor, game)
    civs, game.civs = game.civs, []
    assert check(monitor, game) == []
    game.civs = civs
    assert check(monitor, game) == []


def test_first_contact_while_at_war_fires():
    game = FakeGame()
    monitor = ThreatMonitor()
    check(monitor, game)
    game.civs[2].has_met = True
    game.civs[2].is_at_war = True
    assert check(monitor, game) == [WarDeclared(2, 3, "Unmet")]
    assert check(monitor, game) == []


def test_wars_already_running_on_the_first_read_are_a_baseline():
    game = FakeGame()
    set_at_war(game)
    monitor = ThreatMonitor()
    assert check(monitor, game) == []
    assert check(monitor, game) == []


def test_loading_a_save_mid_war_doesnt_fire_for_the_running_war():
    game = FakeGame()
    monitors = Monitors(EventBus())
    events = collect(monitors._bus)
    game.turn = 20
    asyncio.run(monitors.run(game))
    set_at_war(game)
    game.turn = 10
    asyncio.run(monitors.run(game))
    assert not [event for event in events if isinstance(event, WarDeclared)]


def test_unmet_civs_are_ignored():
    game = FakeGame()
    monitor = ThreatMonitor()
    check(monitor, game)
    game.civs[2].is_at_war = True
    assert check(monitor, game) == []


def test_city_lost_fires_once_and_names_the_city():
    game = FakeGame()
    monitor = ThreatMonitor()
    assert check(monitor, game) == []
    del game.cities[1]
    assert check(monitor, game) == [CityLost(2, 2, "Colony", 2, 2)]
    assert check(monitor, game) == []


def test_a_timed_out_city_read_is_a_failure_and_not_a_lost_empire():
    game = FakeGame()
    monitor = ThreatMonitor()
    check(monitor, game)

    game.reported_cities, game.cities = game.cities, []
    with pytest.raises(IncompleteRead):
        check(monitor, game)

    game.cities, game.reported_cities = game.reported_cities, None
    assert check(monitor, game) == []


def test_loyalty_warning_fires_once_until_the_city_recovers():
    game = FakeGame()
    monitor = ThreatMonitor()
    assert check(monitor, game) == []

    game.cities[0].loyalty, game.cities[0].loyalty_per_turn = 40.0, -5.0
    assert check(monitor, game) == [CityLoyaltyWarning(2, 1, "Home", 40.0, -5.0)]
    assert check(monitor, game) == []

    game.cities[0].loyalty_per_turn = 1.0
    assert check(monitor, game) == []
    game.cities[0].loyalty_per_turn = -5.0
    assert len(check(monitor, game)) == 1


def test_slow_loyalty_loss_is_not_a_warning():
    game = FakeGame()
    game.cities[0].loyalty, game.cities[0].loyalty_per_turn = 80.0, -2.0
    assert check(ThreatMonitor(), game) == []


def test_hostile_units_warn_once_per_owner_until_they_leave():
    game = FakeGame()
    set_at_war(game)
    monitor = ThreatMonitor()
    check(monitor, game)

    game.threats = [unit(distance=2), unit(distance=1), unit(distance=4)]
    assert check(monitor, game) == [HostileUnitsNear(2, ROME, "Rome", 3, 1)]
    game.threats = game.threats[:1]
    assert check(monitor, game) == []  # Thinning out isn't clearing out.
    game.threats = game.threats * 3
    assert check(monitor, game) == []

    game.threats = []
    for _ in range(ABSENT_CHECKS):
        assert check(monitor, game) == []
    game.threats = [unit() for _ in range(3)]
    assert len(check(monitor, game)) == 1


def test_one_empty_threat_scan_isnt_a_retreat():
    """civ6-mcp returns the lines it has when a read times out, so a short scan looks empty."""
    game = FakeGame()
    set_at_war(game)
    monitor = ThreatMonitor()
    game.threats = [unit() for _ in range(3)]
    assert len(check(monitor, game)) == 1

    full, game.threats = game.threats, []
    for _ in range(ABSENT_CHECKS - 1):
        assert check(monitor, game) == []
    game.threats = full
    assert check(monitor, game) == []
    game.threats = []  # The count of missed scans starts again after the army is seen.
    for _ in range(ABSENT_CHECKS - 1):
        assert check(monitor, game) == []
    game.threats = full
    assert check(monitor, game) == []


@pytest.mark.parametrize(
    "owner",
    [
        {"owner_id": BARBARIAN_ID, "owner_name": "Barbarian"},
        {"owner_id": 20, "owner_name": "Geneva", "city_state": True},
    ],
)
def test_barbarians_and_city_states_at_war_count_as_hostile(owner):
    game = FakeGame()
    game.threats = [unit(**owner) for _ in range(3)]
    assert [event.owner_id for event in check(ThreatMonitor(), game)] == [owner["owner_id"]]


@pytest.mark.parametrize(
    "threats",
    [
        pytest.param([unit() for _ in range(3)], id="at peace"),
        pytest.param([unit(owner_id=3, owner_name="Unmet") for _ in range(3)], id="unmet"),
        pytest.param([unit(distance=THREAT_RADIUS + 1) for _ in range(3)], id="far"),
        pytest.param([unit(), unit()], id="too few"),
    ],
)
def test_units_that_are_not_an_alert(threats):
    game = FakeGame()
    game.threats = threats
    assert check(ThreatMonitor(), game) == []


@pytest.mark.parametrize(
    "hidden", ["UNIT_SUBMARINE", "UNIT_PRIVATEER", "UNIT_OTTOMAN_BARBARY_CORSAIR"]
)
def test_stealth_units_on_visible_tiles_are_not_seen(hidden):
    game = FakeGame()
    set_at_war(game)
    game.threats = [unit(), unit(), unit(unit_type=hidden)]
    assert check(ThreatMonitor(), game) == []


def test_resource_shortage_fires_once_until_the_stockpile_recovers():
    game = FakeGame()
    game.stockpiles = [stockpile(amount=10, per_turn=1, demand=3, imported=1)]
    monitor = ResourceMonitor()

    assert check(monitor, game) == []  # Ten units at -1 a turn last exactly ten turns.
    game.stockpiles = [stockpile(amount=5, per_turn=1, demand=3)]
    assert check(monitor, game) == [StrategicResourceShortage(2, "IRON", 5, -2)]
    assert check(monitor, game) == []

    game.stockpiles = [stockpile(amount=5, per_turn=3, demand=3)]
    assert check(monitor, game) == []
    game.stockpiles = [stockpile(amount=5, per_turn=1, demand=3)]
    assert len(check(monitor, game)) == 1


def test_a_big_stockpile_falling_slowly_is_not_a_shortage():
    game = FakeGame()
    game.stockpiles = [stockpile(amount=40, demand=2)]
    assert check(ResourceMonitor(), game) == []


def test_a_resource_missing_from_a_short_read_doesnt_warn_again():
    game = FakeGame()
    game.stockpiles = [stockpile(amount=5, demand=2)]
    monitor = ResourceMonitor()
    assert len(check(monitor, game)) == 1
    stockpiles, game.stockpiles = game.stockpiles, []
    check(monitor, game)
    game.stockpiles = stockpiles
    assert check(monitor, game) == []


def test_gold_shortage_fires_once_until_income_recovers():
    game = FakeGame()
    monitor = ResourceMonitor()
    assert check(monitor, game) == []

    game.gold, game.gold_per_turn = 100.0, -20.0
    assert check(monitor, game) == [GoldShortage(2, 100.0, -20.0)]
    assert check(monitor, game) == []

    game.gold_per_turn = 1.0
    assert check(monitor, game) == []
    game.gold_per_turn = -20.0
    assert len(check(monitor, game)) == 1


def test_rival_warnings_fire_once_per_share_passed():
    game = FakeGame()
    monitor = VictoryMonitor()
    rome = game.victory.players[1]
    warnings = []
    for points in [9, 10, 11, 15, 17, 18, 19]:
        rome.diplomatic_vp = points
        warnings.extend(event.progress for event in check(monitor, game))
    assert warnings == [10, 15, 18]


def test_science_warning_uses_the_points_the_game_says_are_needed():
    game = FakeGame()
    game.victory.players[2].science_vp = 25
    assert check(VictoryMonitor(), game) == [
        RivalVictoryWarning(1, VictoryPath.SCIENCE, EGYPT, "Egypt", 25, 50)
    ]


def test_a_rival_that_falls_back_can_warn_again():
    game = FakeGame()
    monitor = VictoryMonitor()
    rome = game.victory.players[1]
    rome.diplomatic_vp = 10
    assert len(check(monitor, game)) == 1
    rome.diplomatic_vp = 8
    assert check(monitor, game) == []
    rome.diplomatic_vp = 10
    assert len(check(monitor, game)) == 1


def test_our_own_progress_is_not_a_rival_warning():
    game = FakeGame()
    game.victory.players[0].diplomatic_vp = 19
    game.victory.players[0].science_vp = 49
    assert check(VictoryMonitor(), game) == []


def test_a_disabled_victory_type_never_warns():
    game = FakeGame()
    game.victory.players[1].diplomatic_vp = 19
    game.victory.enabled_victories = {"VICTORY_TECHNOLOGY"}
    assert check(VictoryMonitor(), game) == []


def test_religion_warning_counts_living_civs_including_unmet_ones():
    game = FakeGame()  # Four majors, of which three are in the read.
    game.victory.religion_founded_names = {"Rome": "Islam"}
    game.victory.religion_majority = {
        "Us": "RELIGION_ISLAM",
        "Rome": "RELIGION_ISLAM",
        "Egypt": "none",
    }
    assert check(VictoryMonitor(), game) == [
        RivalVictoryWarning(1, VictoryPath.RELIGIOUS, ROME, "RELIGION_ISLAM", 2, 4)
    ]


def test_our_own_religion_spreading_is_not_a_warning():
    game = FakeGame()
    game.victory.religion_founded_names = {"Us": "Islam"}
    game.victory.religion_majority = dict.fromkeys(["Us", "Rome", "Egypt"], "RELIGION_ISLAM")
    assert check(VictoryMonitor(), game) == []


def test_a_rival_religion_converting_us_still_warns():
    game = FakeGame()
    game.victory.religion_founded_names = {"Us": "Islam", "Rome": "Buddhism"}
    game.victory.religion_majority = dict.fromkeys(["Us", "Rome", "Egypt"], "RELIGION_BUDDHISM")
    assert [event.rival_id for event in check(VictoryMonitor(), game)] == [ROME]


def test_a_religion_with_an_unmatched_name_warns_without_a_rival():
    game = FakeGame()
    game.victory.religion_founded_names = {"Rome": "Renamed Faith"}
    game.victory.religion_majority = dict.fromkeys(["Rome", "Egypt"], "RELIGION_ISLAM")
    assert [event.rival_id for event in check(VictoryMonitor(), game)] == [None]


def test_conquest_warning_counts_captured_capitals_and_eliminated_civs():
    game = FakeGame()  # Four majors, so a conqueror needs three capitals.
    monitor = VictoryMonitor()
    assert check(monitor, game) == []

    game.victory.capitals_held = {"Us": True, "Rome": True, "Egypt": False}
    assert check(monitor, game) == []

    # The game only lists living civs in both reads, so an eliminated civ is in neither.
    del game.civs[0]
    del game.victory.capitals_held["Rome"]
    assert check(monitor, game) == []  # A drop must repeat before it's believed.
    assert check(monitor, game) == [
        RivalVictoryWarning(4, VictoryPath.CONQUEST, None, "original capitals", 2, 3)
    ]


def test_a_short_diplomacy_read_isnt_taken_for_eliminated_civs():
    game = FakeGame()
    monitor = VictoryMonitor()
    game.victory.capitals_held = {"Us": True, "Rome": True, "Egypt": True}
    assert check(monitor, game) == []

    full, game.civs = game.civs, game.civs[:1]
    assert check(monitor, game) == []
    game.civs = full
    assert check(monitor, game) == []
    game.civs = full[:1]
    assert check(monitor, game) == []


def test_an_empty_victory_read_is_a_failure():
    game = FakeGame()
    game.civs = []
    with pytest.raises(IncompleteRead):
        check(VictoryMonitor(), game)


def test_no_city_yet_is_not_a_failed_victory_read():
    """Live: before the first city the game lists no players at all, not even us."""
    game = FakeGame()
    game.cities = []
    game.victory = VictoryProgress(players=[])
    assert check(VictoryMonitor(), game) == []


def test_omniscient_fields_never_reach_an_event():
    """Sets every field that ignores fog of war to a marker, then triggers every event."""
    marker = 7654321
    game = FakeGame()
    rome = game.civs[0]
    rome.num_cities = rome.military_strength = marker
    rome.visible_cities = [VisibleCity(str(marker), 1, 1, marker)]
    rome.defensive_pacts = [marker]
    game.cities[0].attack_targets = [f"UNIT_SUBMARINE@1,1({marker}hp)"]
    for row in game.victory.players:
        row.spaceports = row.gold_yield = row.science_yield = row.culture_yield = marker
        row.military_strength = row.techs_researched = row.tourism = marker
    game.victory.demographics = {
        "Soldiers": DemographicEntry(1, marker, marker, marker, marker),
    }

    bus = EventBus()
    events = collect(bus)
    monitors = Monitors(bus)
    asyncio.run(monitors.run(game))
    game.turn = 6
    set_at_war(game)
    game.threats = [unit() for _ in range(3)]
    game.cities[1].loyalty_per_turn = -20.0
    game.victory.players[1].science_vp = 45
    game.victory.players[1].diplomatic_vp = 19
    game.stockpiles = [stockpile(amount=1, demand=5)]
    game.gold_per_turn = -50.0
    asyncio.run(monitors.run(game))
    game.turn = 7
    del game.cities[1]
    asyncio.run(monitors.run(game))

    assert {type(event) for event in events} == set(EVENT_TYPES)
    assert not any(str(marker) in event.to_json() for event in events)


def test_monitors_use_only_the_reads_in_the_narrow_protocol(tmp_path):
    game = FakeGame()
    bus = EventBus()
    asyncio.run(Orchestrator(game, [Monitors(bus)], TurnLog(tmp_path / "game.jsonl")).play(10))

    allowed = {name for name in vars(MonitorGame) if name.startswith("get_")}
    assert {read for _, read in game.reads} == allowed
    assert "get_rival_snapshot" not in allowed


def test_a_failed_monitor_retries_next_turn_without_stopping_the_others():
    game = FakeGame()
    bus = EventBus()
    events = collect(bus)
    monitors = Monitors(bus)
    game.failing = {"get_threat_scan"}
    game.victory.players[1].diplomatic_vp = 10

    with pytest.raises(MonitorError, match="threat"):
        asyncio.run(monitors.run(game))

    assert game.read_turns("get_empire_resources") == [1]
    assert len(events) == 1

    game.failing = set()
    game.turn = 2
    asyncio.run(monitors.run(game))
    assert game.read_turns("get_threat_scan") == [1, 2]
    assert game.read_turns("get_empire_resources") == [1]


def test_a_failed_slow_monitor_is_retried_at_once_not_a_period_later():
    game = FakeGame()
    monitors = Monitors(EventBus())
    asyncio.run(monitors.run(game))

    game.turn = 6
    game.failing = {"get_empire_resources"}
    with pytest.raises(MonitorError):
        asyncio.run(monitors.run(game))

    game.turn, game.failing = 7, set()
    asyncio.run(monitors.run(game))
    assert game.read_turns("get_empire_resources") == [1, 6, 7]
    assert game.read_turns("get_victory_progress") == [1, 6]


def test_a_failed_monitor_is_recorded_in_the_turn_log(tmp_path):
    game = FakeGame()
    game.failing = {"get_threat_scan"}
    turn_log = TurnLog(tmp_path / "game.jsonl")
    asyncio.run(Orchestrator(game, [Monitors(EventBus())], turn_log).play(1))
    assert "threat" in turn_log.records[0].handler_errors["monitors"]


def test_a_lost_connection_is_not_treated_as_a_failed_check():
    class GoneGame(FakeGame):
        async def get_diplomacy(self):
            raise ConnectionError("game closed")

    with pytest.raises(ConnectionError):
        asyncio.run(Monitors(EventBus()).run(GoneGame()))


def test_skipped_turns_dont_postpone_a_check_to_the_next_multiple():
    game = FakeGame()
    monitors = Monitors(EventBus())
    asyncio.run(monitors.run(game))
    game.turn = 8
    asyncio.run(monitors.run(game))
    game.turn = 12
    asyncio.run(monitors.run(game))
    assert game.read_turns("get_victory_progress") == [1, 8]


def test_a_second_run_on_the_same_turn_checks_nothing_new():
    game = FakeGame()
    monitors = Monitors(EventBus())
    asyncio.run(monitors.run(game))
    asyncio.run(monitors.run(game))
    assert game.read_turns("get_threat_scan") == [1]


def test_loading_an_earlier_save_makes_monitors_due():
    game = FakeGame()
    monitors = Monitors(EventBus())
    game.turn = 20
    asyncio.run(monitors.run(game))
    game.turn = 18
    asyncio.run(monitors.run(game))
    assert game.read_turns("get_victory_progress") == [20, 18]


def test_loading_an_earlier_save_forgets_what_the_later_one_had():
    game = FakeGame()
    set_at_war(game, False)
    bus = EventBus()
    events = collect(bus)
    monitors = Monitors(bus)
    game.turn = 20
    game.gold, game.gold_per_turn = 100.0, -20.0
    game.threats = [unit(), unit(), unit()]
    asyncio.run(monitors.run(game))

    game.turn = 10  # An earlier save: one city, and none of the later trouble.
    game.cities = game.cities[:1]
    game.gold, game.gold_per_turn = 100.0, 5.0
    game.threats = []
    asyncio.run(monitors.run(game))
    assert not [event for event in events if isinstance(event, CityLost)]

    game.turn = 15  # Then the same troubles come back: they're new events, not repeats.
    game.gold_per_turn = -20.0
    asyncio.run(monitors.run(game))
    assert [event.kind for event in events].count("gold_shortage") == 2


def test_subscribers_get_only_the_types_they_asked_for():
    bus = EventBus()
    wars, everything = [], []
    bus.subscribe(wars.append, WarDeclared)
    bus.subscribe(everything.append)
    war, city_lost = WarDeclared(1, ROME, "Rome"), CityLost(1, 1, "Home", 0, 0)
    bus.publish(war)
    bus.publish(city_lost)
    assert wars == [war]
    assert everything == [war, city_lost]


def test_a_failing_subscriber_doesnt_cost_the_others_the_event():
    def broken(event):
        raise ValueError("bad subscriber")

    bus = EventBus()
    received = []
    bus.subscribe(broken)
    bus.subscribe(received.append)
    bus.publish(WarDeclared(1, ROME, "Rome"))
    assert len(received) == 1


SAMPLES = [
    WarDeclared(5, ROME, "Rome"),
    CityLost(5, 1, "Home", 3, 4),
    HostileUnitsNear(5, BARBARIAN_ID, "Barbarian", 3, 1),
    CityLoyaltyWarning(5, 1, "Home", 40.0, -5.5),
    RivalVictoryWarning(5, VictoryPath.CONQUEST, None, "original capitals", 3, 5),
    StrategicResourceShortage(5, "IRON", 5, -2),
    GoldShortage(5, 10.5, -3.5),
]


def test_samples_cover_every_event_type():
    assert {type(event) for event in SAMPLES} == set(EVENT_TYPES)


@pytest.mark.parametrize("event", SAMPLES, ids=lambda event: event.kind)
def test_events_survive_json(event):
    restored = event_from_json(event.to_json())
    assert restored == event
    assert type(restored) is type(event)
    if isinstance(event, RivalVictoryWarning):
        assert isinstance(restored.path, VictoryPath)


@pytest.mark.parametrize("event", SAMPLES, ids=lambda event: event.kind)
def test_events_are_immutable(event):
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.turn = 6


def test_event_log_appends_one_json_line_per_event(tmp_path):
    path = tmp_path / "runs" / "events.jsonl"
    bus = EventBus()
    log = EventLog(path)
    bus.subscribe(log.write)
    bus.publish(SAMPLES[0])
    bus.publish(SAMPLES[1])
    assert not path.exists()  # Buffered until the turn's flush.
    log.flush()
    log.flush()  # Nothing more to write, and no repeat.
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["war_declared", "city_lost"]


# ----- who took a lost city, and why -----
# Values below are from Run 5 and Run 6 event logs (city ids, tiles, turns, loyalty figures, the
# Sumerian war of turn 129). The diplomacy reply is built in the civ6-mcp line format and parsed by
# its own parser, so a change to the `ECITY` line shows up here.

SUMERIA = 5
CAPITAL = (65536, "Wak Kab\u2019nal", 66, 25)  # Run 6, lost on turn 135.
PALENQUE = (393221, "Palenque", 66, 15)  # Run 5, warned on turn 126 at 80 and -20 per turn.


def real_city(spec, loyalty=100.0, loyalty_per_turn=0.0):
    city_id, name, x, y = spec
    found = city(city_id, name, loyalty, loyalty_per_turn)
    found.x, found.y = x, y
    return found


def diplomacy_reply(at_war, ecity_lines=()):
    """Parsed `get_diplomacy` lines: Sumeria (met, `at_war`), with the given `ECITY` lines."""
    from civ_mcp.lua import parse_diplomacy_response

    war = "1" if at_war else "0"
    lines = [f"CIV|{SUMERIA}|Sumeria|Gilgamesh|1|{war}|NEUTRAL|0|2|0|0|0|0", *ecity_lines]
    return parse_diplomacy_response(lines)


def lose(game, monitor, spec, civs, threats=()):
    """The monitor sees the city on one turn and not on the next, as in the logs."""
    game.cities = [real_city(spec, 100.0, 0.0)]
    game.civs = civs
    check(monitor, game)
    game.cities = []
    game.threats = list(threats)
    return check(monitor, game)


def test_a_captured_city_names_the_civ_that_now_holds_it_and_says_capture():
    game = FakeGame()
    monitor = ThreatMonitor()
    game.turn = 134
    ecity = f"ECITY|{SUMERIA}|Wak Kab\u2019nal|66,25|3|45.0|-2.0|0|18"
    game.cities = [real_city(CAPITAL)]
    game.civs = diplomacy_reply(True)
    check(monitor, game)
    game.cities = []
    game.civs = diplomacy_reply(True, [ecity])
    (event,) = check(monitor, game)
    assert event == CityLost(
        135,
        65536,
        "Wak Kab\u2019nal",
        66,
        25,
        taker_id=SUMERIA,
        taker_name="Sumeria",
        cause="capture",
    )


def test_a_lost_city_with_nothing_beside_it_and_no_war_is_unknown():
    game = FakeGame()
    (event,) = lose(game, ThreatMonitor(), CAPITAL, diplomacy_reply(False))
    assert (event.taker_id, event.taker_name, event.cause) == (None, "", "unknown")


def test_a_razed_city_takes_its_taker_from_the_enemy_unit_beside_it():
    # Run 6 Uxmal: no city left on the tile, a Sumerian War Cart one tile away, at war.
    game = FakeGame()
    spec = (131073, "Uxmal", 64, 22)
    cart = unit(SUMERIA, "Sumeria", distance=1, unit_type="UNIT_SUMERIAN_WAR_CART")
    cart.x, cart.y = 65, 22
    monitor = ThreatMonitor()
    game.cities = [real_city(spec)]
    game.civs = diplomacy_reply(False)
    check(monitor, game)
    game.civs = diplomacy_reply(True)
    check(monitor, game)
    game.cities, game.threats = [], [cart]
    (event,) = check(monitor, game)
    assert (event.taker_id, event.taker_name, event.cause) == (SUMERIA, "Sumeria", "capture")


def test_a_unit_far_from_the_city_does_not_name_a_taker():
    game = FakeGame()
    far = unit(SUMERIA, "Sumeria", distance=1)
    far.x, far.y = 66, 30  # Five rows from the capital.
    (event,) = lose(game, ThreatMonitor(), CAPITAL, diplomacy_reply(True), [far])
    assert event.taker_id is None


def test_a_loyalty_flip_is_a_loyalty_loss_even_with_an_enemy_unit_near():
    # Run 5 Palenque: warned at 80, -20 a turn on turn 126, lost on turn 133. The last read, a
    # turn before, had 20 left at -20.
    game = FakeGame()
    monitor = ThreatMonitor()
    game.turn = 132
    game.cities = [real_city(PALENQUE, 20.0, -20.0)]
    game.civs = diplomacy_reply(True)
    check(monitor, game)
    game.cities = []
    ecity = f"ECITY|{SUMERIA}|Palenque|66,15|2|60.0|10.0|0|12"
    game.civs = diplomacy_reply(True, [ecity])
    (event,) = check(monitor, game)
    assert (event.turn, event.city_id, event.taker_id, event.cause) == (
        133,
        393221,
        SUMERIA,
        "loyalty",
    )


def test_a_city_that_was_falling_slowly_is_not_called_a_loyalty_loss():
    # Run 6 Yaxchilan: -9.3 a turn at 89.7 on turn 153; it held, and was captured on turn 273.
    game = FakeGame()
    spec = (327680, "Yaxchilan", 63, 31)
    monitor = ThreatMonitor()
    game.cities = [real_city(spec, 89.7, -9.3)]
    game.civs = diplomacy_reply(True)
    check(monitor, game)
    game.cities = []
    ecity = f"ECITY|{SUMERIA}|Yaxchilan|63,31|3|60.0|0.0|0|18"
    game.civs = diplomacy_reply(True, [ecity])
    (event,) = check(monitor, game)
    assert event.cause == "capture"


def test_the_event_round_trips_through_the_log_and_old_lines_still_read():
    from civ6_ai.monitors import event_from_json

    event = CityLost(135, 65536, "Wak Kab\u2019nal", 66, 25, SUMERIA, "Sumeria", "capture")
    assert event_from_json(event.to_json()) == event
    old = '{"kind": "city_lost", "turn": 135, "city_id": 65536, "city_name": "X", "x": 66, "y": 25}'
    assert event_from_json(old) == CityLost(135, 65536, "X", 66, 25)


# ----- the city set across a restart -----


def test_a_city_lost_while_the_bot_was_down_is_reported_on_the_next_session(tmp_path):
    path = tmp_path / "threat.json"
    game = FakeGame()
    game.turn = 50
    first = ThreatMonitor(path)
    assert check(first, game) == []
    assert path.exists()

    # A new process: a new monitor, the colony gone, and some turns passed.
    del game.cities[1]
    game.turn = 53
    events = check(ThreatMonitor(path), game)
    assert [(e.kind, e.city_id, e.turn) for e in events] == [("city_lost", 2, 53)]


def test_without_a_file_the_first_read_after_a_restart_is_only_a_baseline(tmp_path):
    game = FakeGame()
    del game.cities[1]
    assert check(ThreatMonitor(tmp_path / "none.json"), game) == []


def test_a_saved_set_from_a_later_turn_is_ignored(tmp_path):
    path = tmp_path / "threat.json"
    game = FakeGame()
    game.turn = 80
    check(ThreatMonitor(path), game)
    del game.cities[1]
    game.turn = 40  # An earlier save was loaded.
    assert check(ThreatMonitor(path), game) == []


def test_a_damaged_state_file_is_ignored(tmp_path):
    path = tmp_path / "threat.json"
    path.write_text("{not json", encoding="utf-8")
    game = FakeGame()
    monitor = ThreatMonitor(path)
    assert check(monitor, game) == []
    assert json.loads(path.read_text(encoding="utf-8"))["cities"]  # Rewritten whole.


def test_the_restart_carries_the_loyalty_the_loss_is_judged_by(tmp_path):
    path = tmp_path / "threat.json"
    game = FakeGame()
    game.turn = 130
    game.cities = [real_city(PALENQUE, 40.0, -20.0)]
    check(ThreatMonitor(path), game)
    game.cities = []
    game.turn = 133
    (event,) = check(ThreatMonitor(path), game)
    assert event.cause == "loyalty"


def test_a_hidden_submarine_beside_a_lost_city_is_not_its_taker():
    game = FakeGame()
    sub = unit(SUMERIA, "Sumeria", distance=1, unit_type="UNIT_SUBMARINE")
    sub.x, sub.y = 66, 24
    (event,) = lose(game, ThreatMonitor(), CAPITAL, diplomacy_reply(False), [sub])
    assert (event.taker_id, event.cause) == (None, "unknown")


def test_the_victory_monitor_feeds_the_watch_without_extra_reads():
    game = FakeGame()
    game.victory.players[1].diplomatic_vp = 12
    watch = VictoryWatch()
    monitor = VictoryMonitor(watch)
    before = len(game.reads)
    check(monitor, game)
    assert watch.threat_ids() == (ROME,)
    assert watch.last.turn == 1
    assert {name for _, name in game.reads[before:]} <= {
        "get_game_overview",
        "get_victory_progress",
        "get_diplomacy",
    }
    monitor.reset()
    assert watch.last is None


def test_the_watch_counts_the_majorities_of_our_own_religion():
    game = FakeGame()
    game.victory.religion_majority = {"Us": "RELIGION_BUDDHISM", "Rome": "RELIGION_BUDDHISM"}
    game.victory.religion_founded_names = {"Us": "Buddhism"}
    assert VictoryMonitor._our_religion(game.victory, "Us") == 2
