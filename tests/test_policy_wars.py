"""War tracking and peace-making tests, with a fake game."""

import asyncio
import logging

import pytest
from civ_mcp import lua as lq

from civ6_ai.policies.wars import WarPolicy, WarSettings
from fixtures import civ_state as fx

ME = fx.ME
SUMERIA = 1
KANDY = 20
GERMANY = 2
LOGGER = "civ6_ai.policies.wars"


def run(coro):
    return asyncio.run(coro)


def civ(pid=SUMERIA, war=False, name="Sumeria", met=True):
    return fx.civ_info(player_id=pid, civ_name=name, is_at_war=war, has_met=met)


def state(pid=KANDY, suzerain=SUMERIA, name="Kandy"):
    return lq.CityStateInfo(pid, name, "Trade", 1, suzerain, str(suzerain), True)


class Game:
    def __init__(self, civs, states=(), answers=None, turn=20):
        self.civs = civs
        self.states = list(states)
        self.answers = dict(answers or {})
        self.turn = turn
        self.calls = []
        self.city_state_error = None

    async def get_game_overview(self):
        return fx.overview(turn=self.turn, player_id=ME)

    async def get_diplomacy(self):
        return self.civs

    async def get_city_states(self):
        if self.city_state_error:
            raise self.city_state_error
        return lq.EnvoyStatus(0, self.states)

    async def propose_peace(self, pid):
        self.calls.append((self.turn, pid))
        answer = self.answers.get(pid, "REJECTED|No")
        if isinstance(answer, Exception):
            raise answer
        return answer


def play(policy, game, turn):
    game.turn = turn
    run(policy.run(game))


def texts(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_war_start_and_end_are_logged_once(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy(WarSettings(min_war_age=99))
    game = Game([civ(war=False)])
    play(policy, game, 10)
    game.civs = [civ(war=True)]
    play(policy, game, 11)
    play(policy, game, 12)
    game.civs = [civ(war=False)]
    play(policy, game, 13)
    lines = texts(caplog)
    assert sum("war with civ Sumeria (1)" in t and "first seen turn 11" in t for t in lines) == 1
    assert sum("ended, lasted 2 turns" in t for t in lines) == 1
    assert sum("at war with Sumeria" in t for t in lines) == 1  # The summary only on change.
    assert policy.at_war_with(SUMERIA) is False


def test_war_found_on_first_read_has_unknown_start(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy()
    game = Game([civ(war=True)])
    play(policy, game, 50)
    assert policy.wars()[SUMERIA].started is None
    assert any("start unknown" in t for t in texts(caplog))
    assert game.calls == [(50, SUMERIA)]  # An unknown start skips the war age rule.


def test_city_state_inherits_suzerain_war_and_logs_changes(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy(WarSettings(min_war_age=99))
    game = Game([civ(war=False), civ(GERMANY, False, "Germany")], [state()])
    play(policy, game, 10)
    game.civs = [civ(war=True), civ(GERMANY, False, "Germany")]
    play(policy, game, 11)
    assert policy.at_war_with(KANDY)
    assert policy.wars()[KANDY].via == SUMERIA
    # Germany, a rival at peace with us, takes Kandy: early warning, and Kandy leaves the war.
    game.states = [state(suzerain=GERMANY)]
    play(policy, game, 12)
    lines = texts(caplog)
    assert any("war with city-state Kandy" in t and "via suzerain Sumeria" in t for t in lines)
    assert any("changed from Sumeria (1) to Germany (2). Early warning" in t for t in lines)
    assert any("war with Kandy (20) ended" in t for t in lines)
    assert not policy.at_war_with(KANDY)


def test_rival_at_war_becoming_suzerain_is_flagged_strongly(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy(WarSettings(min_war_age=99))
    game = Game([civ(war=True)], [state(suzerain=-1)])
    play(policy, game, 10)
    game.states = [state(suzerain=SUMERIA)]
    play(policy, game, 11)
    assert any("rival at war with us is now its suzerain" in t for t in texts(caplog))
    assert policy.at_war_with(KANDY)


def test_no_offer_before_min_war_age_then_one_per_five_turns():
    policy = WarPolicy()
    game = Game([civ(war=False)])
    play(policy, game, 10)
    game.civs = [civ(war=True)]
    for turn in range(11, 36):
        play(policy, game, turn)
    assert game.calls == [(21, SUMERIA), (26, SUMERIA), (31, SUMERIA)]


def test_refused_offer_is_logged_and_not_repeated_next_turn(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy()
    game = Game([civ(war=True)], answers={SUMERIA: "REJECTED|Sumeria rejected your peace offer"})
    play(policy, game, 10)
    play(policy, game, 11)
    assert game.calls == [(10, SUMERIA)]
    lines = texts(caplog)
    assert any("peace offer to Sumeria" in t for t in lines)
    assert any("refused peace, next offer in 5 turns" in t for t in lines)
    assert policy.at_war_with(SUMERIA)


def test_accepted_peace_ends_war_on_next_read(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy()
    game = Game([civ(war=True)], answers={SUMERIA: "ACCEPTED|Peace established with Sumeria"})
    play(policy, game, 10)
    assert any("accepted peace" in t for t in texts(caplog))
    game.civs = [civ(war=False)]
    play(policy, game, 11)
    assert not policy.at_war_with(SUMERIA)


def test_error_strings_and_exceptions_do_not_crash(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    for answer in (
        "Error: CANNOT_MAKE_PEACE|10-turn war cooldown or other restriction",
        "Error: NO_SESSION|Failed to open peace deal session",
        "Error: NOT_AT_WAR|Not at war with player 1",
        "something odd",
        RuntimeError("boom"),
    ):
        policy = WarPolicy()
        game = Game([civ(war=True)], answers={SUMERIA: answer})
        play(policy, game, 10)
        play(policy, game, 11)
        assert game.calls == [(10, SUMERIA)]  # Errors count against the five-turn bound.
    lines = texts(caplog)
    assert any("not possible: Error: CANNOT_MAKE_PEACE" in t for t in lines)
    assert any("unrecognised peace answer" in t for t in lines)
    assert any("raised" in t for t in lines)
    assert any("is not at war with us" in t for t in lines)


def test_connection_error_is_not_swallowed():
    policy = WarPolicy()
    game = Game([civ(war=True)], answers={SUMERIA: ConnectionError("gone")})
    with pytest.raises(ConnectionError):
        play(policy, game, 10)


def test_peace_with_suzerain_is_checked_for_its_city_state_not_assumed(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy()
    game = Game(
        [civ(war=True)],
        [state()],
        answers={SUMERIA: "ACCEPTED|Peace", KANDY: "REJECTED|Kandy rejected"},
    )
    play(policy, game, 10)
    assert policy.at_war_with(KANDY)
    game.civs = [civ(war=False)]
    play(policy, game, 11)
    assert (11, KANDY) in game.calls
    assert any("peace check to Kandy" in t for t in texts(caplog))
    play(policy, game, 12)
    assert (12, KANDY) not in game.calls  # Bounded by the five-turn interval.


def test_city_state_check_not_at_war_is_logged(caplog):
    caplog.set_level(logging.INFO, LOGGER)
    policy = WarPolicy()
    game = Game(
        [civ(war=True)],
        [state()],
        answers={SUMERIA: "ACCEPTED|Peace", KANDY: "Error: NOT_AT_WAR|Not at war with player 20"},
    )
    play(policy, game, 10)
    game.civs = [civ(war=False)]
    play(policy, game, 11)
    assert any("Kandy (20) is not at war" in t or "Kandy is not at war" in t for t in texts(caplog))


def test_failed_city_state_read_keeps_civ_wars():
    policy = WarPolicy()
    game = Game([civ(war=True)], [state()])
    play(policy, game, 10)
    game.city_state_error = RuntimeError("timeout")
    play(policy, game, 11)
    assert policy.at_war_with(SUMERIA)


def test_a_restart_keeps_the_turn_a_war_was_first_seen(tmp_path, caplog):
    path = tmp_path / "wars.json"
    first = WarPolicy(state_path=path)
    play(first, Game([civ(war=False)]), 20)
    play(first, Game([civ(war=True)]), 23)
    assert first.wars()[SUMERIA].started == 23

    restarted = WarPolicy(state_path=path)  # A new process: nothing in memory.
    caplog.set_level(logging.INFO, logger=LOGGER)
    play(restarted, Game([civ(war=True)]), 40)
    assert restarted.wars()[SUMERIA].started == 23
    assert not any("start unknown" in t for t in texts(caplog))
    assert any("read back from turn 23" in t for t in texts(caplog))


def test_a_war_that_began_while_the_bot_was_down_is_first_seen_after_the_restart(tmp_path, caplog):
    path = tmp_path / "wars.json"
    play(WarPolicy(state_path=path), Game([civ(war=False)]), 20)

    caplog.set_level(logging.INFO, logger=LOGGER)
    restarted = WarPolicy(state_path=path)
    play(restarted, Game([civ(war=True)]), 30)
    assert restarted.wars()[SUMERIA].started == 30
    assert any("first seen turn 30" in t for t in texts(caplog))


def test_a_war_that_ended_while_the_bot_was_down_is_logged_as_ended(tmp_path, caplog):
    path = tmp_path / "wars.json"
    first = WarPolicy(state_path=path)
    play(first, Game([civ(war=False)]), 18)
    play(first, Game([civ(war=True)]), 20)

    caplog.set_level(logging.INFO, logger=LOGGER)
    restarted = WarPolicy(state_path=path)
    play(restarted, Game([civ(war=False)]), 35)
    assert restarted.wars() == {}
    assert any("ended, lasted 15 turns" in t for t in texts(caplog))


def test_saved_wars_from_a_later_turn_are_not_used(tmp_path):
    path = tmp_path / "wars.json"
    play(WarPolicy(state_path=path), Game([civ(war=True)]), 90)

    other_game = WarPolicy(state_path=path)  # A save from turn 40 loaded, or a new game.
    play(other_game, Game([civ(war=True)]), 40)
    assert other_game.wars()[SUMERIA].started is None


def test_a_missing_or_broken_file_means_start_unknown(tmp_path):
    path = tmp_path / "wars.json"
    missing = WarPolicy(state_path=path)
    play(missing, Game([civ(war=True)]), 40)
    assert missing.wars()[SUMERIA].started is None

    path.write_text("{not json", encoding="utf-8")
    broken = WarPolicy(state_path=path)
    play(broken, Game([civ(war=True)]), 41)
    assert broken.wars()[SUMERIA].started is None


def test_a_restart_keeps_a_city_states_war_and_its_suzerain(tmp_path):
    path = tmp_path / "wars.json"
    first = WarPolicy(state_path=path)
    play(first, Game([civ(war=False)], [state()]), 20)
    play(first, Game([civ(war=True)], [state()]), 25)

    restarted = WarPolicy(state_path=path)
    play(restarted, Game([civ(war=True)], [state()]), 33)
    wars = restarted.wars()
    assert wars[KANDY].started == 25 and wars[KANDY].via == SUMERIA


def test_without_a_path_nothing_is_written(tmp_path):
    play(WarPolicy(), Game([civ(war=True)]), 20)
    assert list(tmp_path.iterdir()) == []
