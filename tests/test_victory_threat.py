import asyncio
import logging
from types import SimpleNamespace

from civ6_ai.victory_threat import (
    DIPLOMATIC,
    RELIGIOUS,
    SCIENCE,
    VictoryThreatLog,
    VictoryWatch,
    assess,
    format_line,
)

US = 0


def p(pid, name, vp=0, needed=33, dip=0, techs=20, spaceports=0, sci=10.0):
    return SimpleNamespace(
        player_id=pid,
        name=name,
        science_vp=vp,
        science_vp_needed=needed,
        diplomatic_vp=dip,
        techs_researched=techs,
        spaceports=spaceports,
        science_yield=sci,
    )


def rows_for(players):
    rows = []
    for q in players:
        if q.player_id == US:
            continue
        rows.append((SCIENCE, q.player_id, q.name, q.science_vp, q.science_vp_needed))
        rows.append((DIPLOMATIC, q.player_id, q.name, q.diplomatic_vp, 20))
    return rows


def run_assess(players, turn=100):
    return assess(turn, players, rows_for(players), US)


def by_path(assessment):
    return {s.path: s for s in assessment.standings}


def test_a_quiet_game_flags_nothing():
    a = run_assess([p(0, "Us"), p(1, "Rome"), p(2, "Egypt")])
    assert a.threats == ()
    assert a.threat_ids == ()
    assert len(a.standings) == 2


def test_diplomatic_points_are_near_at_half_and_name_the_margin():
    a = run_assess([p(0, "Us", dip=2), p(1, "Rome", dip=10), p(2, "Egypt", dip=6)])
    s = by_path(a)[DIPLOMATIC]
    assert (s.rival, s.value, s.margin, s.flag) == ("Rome", 10, 4, "near+lead")
    assert "diplomatic:Rome 10/20 margin +4 near+lead" in format_line(100, a)


def test_a_clear_lead_flags_before_the_end_is_near():
    a = run_assess([p(0, "Us"), p(1, "Rome", dip=6), p(2, "Egypt", dip=1)])
    assert by_path(a)[DIPLOMATIC].flag == "lead"


def test_a_small_lead_does_not_flag():
    a = run_assess([p(0, "Us"), p(1, "Rome", dip=6), p(2, "Egypt", dip=5)])
    assert by_path(a)[DIPLOMATIC].flag == ""


def test_our_own_points_count_against_the_margin():
    a = run_assess([p(0, "Us", dip=6), p(1, "Rome", dip=6), p(2, "Egypt", dip=1)])
    assert by_path(a)[DIPLOMATIC].flag == ""


def test_science_flags_on_techs_well_ahead_of_everyone_including_us():
    a = run_assess([p(0, "Us", techs=48), p(1, "Rome", techs=60), p(2, "Egypt", techs=52)])
    s = by_path(a)[SCIENCE]
    assert (s.rival, s.flag) == ("Rome", "lead")
    assert "techs 60 (+8)" in s.describe()
    assert a.threat_ids == (1,)


def test_science_techs_below_the_floor_never_flag():
    a = run_assess([p(0, "Us", techs=5), p(1, "Rome", techs=25), p(2, "Egypt", techs=10)])
    assert by_path(a)[SCIENCE].flag == ""


def test_a_spaceport_is_near_before_any_points():
    a = run_assess([p(0, "Us", techs=50), p(1, "Rome", techs=52, spaceports=1)])
    assert by_path(a)[SCIENCE].flag == "near"


def test_space_race_points_are_near_at_a_quarter():
    a = run_assess([p(0, "Us"), p(1, "Rome", vp=9, techs=50), p(2, "Egypt", techs=50)])
    assert by_path(a)[SCIENCE].flag == "near+lead"
    a = run_assess([p(0, "Us"), p(1, "Rome", vp=8, techs=50), p(2, "Egypt", techs=50)])
    assert by_path(a)[SCIENCE].flag == ""


def test_the_mapuche_run_reading_is_flagged():
    # Turn 319 of the run that ended on turn 327: 17 of 33 points for the winner.
    a = run_assess([p(0, "Us", techs=48), p(4, "Mapuche", vp=17, techs=66, spaceports=4)], 319)
    s = by_path(a)[SCIENCE]
    assert s.flag == "near+lead"
    assert a.threat_ids == (4,)


def test_disabled_paths_are_not_assessed():
    players = [p(0, "Us"), p(1, "Rome", dip=12)]
    a = assess(1, players, [r for r in rows_for(players) if r[0] != DIPLOMATIC], US)
    assert DIPLOMATIC not in by_path(a)


def test_a_religion_row_is_a_standing_when_it_names_a_rival():
    players = [p(0, "Us"), p(1, "Rome")]
    rows = [(RELIGIOUS, 1, "RELIGION_SHINTO", 3, 5), (RELIGIOUS, None, "RELIGION_X", 4, 5)]
    s = by_path(assess(1, players, rows, US))[RELIGIOUS]
    assert (s.rival, s.flag, s.detail) == ("Rome", "near", "RELIGION_SHINTO")


def test_science_rank_counts_us_among_the_met_civs():
    players = [
        p(0, "Us", sci=24, techs=30),
        p(1, "Rome", sci=82, techs=50),
        p(2, "Egypt", sci=60, techs=40),
        p(3, "Maya", sci=30, techs=20),
    ]
    rank = run_assess(players).science_rank
    assert (rank.rank, rank.civs, rank.leader, rank.techs_rank) == (4, 4, "Rome", 3)
    assert round(rank.share, 2) == 0.29
    assert "science_rank=4/4 yield=24.0 top=Rome 82.0 (29%) techs_rank=3/4" in format_line(
        9, run_assess(players)
    )


def test_the_line_says_how_old_the_reading_is():
    a = run_assess([p(0, "Us"), p(1, "Rome")], turn=100)
    assert "turn=103 read=100 age=3" in format_line(103, a)
    assert format_line(4, None) == "victory_threat: turn=4 no reading yet"


class Game:
    def __init__(self):
        self.turn = 1

    async def get_game_overview(self):
        return SimpleNamespace(turn=self.turn)


def test_the_handler_logs_every_turn_from_the_last_reading(caplog):
    watch = VictoryWatch()
    handler = VictoryThreatLog(watch)
    game = Game()
    with caplog.at_level(logging.INFO):
        asyncio.run(handler.run(game))
        watch.update(run_assess([p(0, "Us", dip=1), p(1, "Rome", dip=11)], turn=2))
        for turn in (2, 3, 4):
            game.turn = turn
            asyncio.run(handler.run(game))
    lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("victory_threat: turn=")
    ]
    assert lines[0].endswith("no reading yet")
    assert [line.split(" flagged")[0] for line in lines[1:4]] == [
        "victory_threat: turn=2 read=2 age=0",
        "victory_threat: turn=3 read=2 age=1",
        "victory_threat: turn=4 read=2 age=2",
    ]
    assert sum("now flagged rivals" in r.getMessage() for r in caplog.records) == 1


def test_loading_an_earlier_save_forgets_the_reading():
    watch = VictoryWatch()
    handler = VictoryThreatLog(watch)
    game = Game()
    game.turn = 50
    watch.update(run_assess([p(0, "Us"), p(1, "Rome", dip=11)]))
    asyncio.run(handler.run(game))
    game.turn = 20
    asyncio.run(handler.run(game))
    assert watch.last is None
    assert watch.threat_ids() == ()


def test_our_own_religion_counts_against_a_rivals_religious_lead():
    players = [p(0, "Us"), p(1, "Rome")]
    rows = [(RELIGIOUS, 1, "RELIGION_SHINTO", 3, 8)]
    assert by_path(assess(1, players, rows, US, our_religion=0))[RELIGIOUS].flag == "lead"
    assert by_path(assess(1, players, rows, US, our_religion=5))[RELIGIOUS].flag == ""


def test_an_unidentified_religion_still_counts_against_a_rivals_lead():
    players = [p(0, "Us"), p(1, "Rome")]
    rows = [(RELIGIOUS, 1, "RELIGION_SHINTO", 3, 8), (RELIGIOUS, None, "RELIGION_X", 5, 8)]
    assert by_path(assess(1, players, rows, US))[RELIGIOUS].flag == ""
