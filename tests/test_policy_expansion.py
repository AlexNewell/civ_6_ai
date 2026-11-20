import asyncio
import logging

from civ6_ai.policies.expansion import (
    ExpansionLog,
    ExpansionState,
    expansion_line,
    expected_cities,
    settler_cap,
)
from fixtures import civ_state as fx


def test_schedule_is_front_loaded_and_steps_between_points():
    turns = (0, 24, 25, 44, 45, 60, 89, 90, 120, 149, 150, 300)
    assert [expected_cities(t) for t in turns] == [1, 1, 2, 2, 3, 4, 4, 6, 8, 8, 9, 9]
    assert expected_cities(-5) == 1


def test_the_ceiling_only_lowers_the_schedule():
    assert expected_cities(150, ceiling=4) == 4
    assert expected_cities(150, ceiling=20) == 9
    assert expected_cities(0, ceiling=0) == 1


def test_settler_cap_runs_one_city_ahead_of_the_schedule():
    # Turn 60 expects 4, so five cities may be reached; the cap is held cities plus settlers.
    assert settler_cap(60, 1, sites_ok=False) == 5
    assert settler_cap(60, 4, sites_ok=False) == 5


def test_settler_cap_grows_with_the_cities_while_safe_sites_exist():
    # The old fixed 6 stopped a bot at 6 cities at turn 150; now a known safe site keeps it going.
    assert settler_cap(150, 9, sites_ok=True) == 10
    assert settler_cap(150, 9, sites_ok=False) == 10
    assert settler_cap(60, 7, sites_ok=True) == 8  # Ahead of the schedule, still room.
    assert settler_cap(60, 7, sites_ok=False) == 5  # Ahead and no safe site: stop.


def test_settler_cap_never_passes_the_ceiling():
    assert settler_cap(300, 11, sites_ok=True) == 12
    assert settler_cap(300, 12, sites_ok=True) == 12
    assert settler_cap(300, 3, sites_ok=True, ceiling=3) == 3


def test_unknown_or_stale_survey_counts_as_sites_ok():
    assert ExpansionState().sites_ok(50)
    assert ExpansionState(survey_turn=10, safe_sites=0).sites_ok(40)  # Stale.
    assert not ExpansionState(survey_turn=45, safe_sites=0).sites_ok(50)
    assert ExpansionState(survey_turn=45, safe_sites=2).sites_ok(50)


def test_line_names_every_figure_the_live_check_needs():
    line = expansion_line(72, 4, 1, ExpansionState(70, 3, 213.4, 1))
    for part in (
        "turn=72",
        "cities=4",
        "ceiling=12",
        "expected=4",
        "behind=0",
        "settlers_building=1",
        "settlers_map=1",
        "safe_sites=3",
        "best_site=213",
    ):
        assert part in line


def test_handler_logs_one_line_with_city_and_settler_counts(caplog):
    game = fx.FakeGame(
        cities=[fx.city_info(), fx.city_info(city_id=2, currently_building="UNIT_SETTLER")],
        overview=fx.overview(turn=30, num_cities=2),
    )
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.expansion"):
        asyncio.run(ExpansionLog(ExpansionState()).run(game))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("expansion:")]
    assert len(lines) == 1
    assert "cities=2" in lines[0] and "settlers_building=1" in lines[0]
    assert "best_site=unknown" in lines[0]


def test_state_carries_the_city_target_for_the_survey_gate():
    assert ExpansionState().target_cities == 12
