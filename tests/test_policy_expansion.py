import asyncio
import logging

from civ6_ai.policies.expansion import (
    ExpansionLog,
    ExpansionState,
    expansion_line,
    expected_cities,
)
from fixtures import civ_state as fx


def test_schedule_is_a_straight_line_to_six_by_turn_150():
    turns = (0, 29, 30, 60, 90, 120, 149, 150, 300)
    assert [expected_cities(t) for t in turns] == [1, 1, 2, 3, 4, 5, 5, 6, 6]


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
        "target=6@150",
        "expected=3",
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
