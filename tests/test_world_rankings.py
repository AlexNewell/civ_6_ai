import asyncio
import json
import logging
from types import SimpleNamespace

from civ6_ai.metrics.civbench_gap import gaps
from civ6_ai.metrics.history import points
from civ6_ai.orchestrator import DoNothing, Orchestrator, TurnLog
from civ6_ai.victory_threat import VictoryThreatLog, VictoryWatch, format_line
from civ6_ai.world_rankings import (
    UNMET,
    RankingsHolder,
    WorldRankingsLog,
    parse_world_rankings,
    read_world_rankings,
)

# The Run 16 turn-151 screen: two unmet civs at 590 and 497, two met civs, and us fifth of five.
REPLY = """WR_ME|0
WR_SCORE|-1||590
WR_SCORE|-1||497
WR_SCORE|5|Sumeria|421
WR_SCORE|4|Mapuche|390
WR_SCORE|0|Maya|384
WR_LEAD|VICTORY_TECHNOLOGY|-1|12
WR_LEAD|VICTORY_CULTURE|-1|130.5
WR_LEAD|VICTORY_RELIGIOUS|5|2
WR_DONE
---END---"""


def test_unmet_civs_count_toward_rank_gap_and_leader():
    r = parse_world_rankings(REPLY)
    assert (r.alive, r.unmet, r.rank) == (5, 2, 5)
    assert (r.leader_score, r.gap) == (590, 384 - 590)
    assert [c.score for c in r.civs] == [590, 497, 421, 390, 384]
    assert r.fields() == {
        "players_alive": 5,
        "all_scores": [590, 497, 421, 390],
        "rank": 5,
        "leader_score": 590,
    }


def test_unmet_leaders_are_anonymous_and_met_leaders_are_named():
    r = parse_world_rankings(REPLY)
    assert [(x.short, x.player_id) for x in r.leaders] == [
        ("science", UNMET),
        ("culture", UNMET),
        ("religious", 5),
    ]
    assert [x.short for x in r.unmet_leaders()] == ["science", "culture"]
    text = r.describe()
    assert "rank=5/5" in text and "gap=-206" in text
    assert "religious:Sumeria 2" in text and "science:unmet 12" in text


def test_we_lead_when_we_have_the_best_score():
    r = parse_world_rankings("WR_ME|1\nWR_SCORE|1|Us|300\nWR_SCORE|-1||250\nWR_DONE")
    assert (r.rank, r.gap, r.leader_score) == (1, 50, 300)


def test_bad_replies_are_not_half_used():
    assert parse_world_rankings("(no output)") is None
    assert parse_world_rankings("WR_SCORE|-1||5\n") is None  # No id of ours.
    assert parse_world_rankings("WR_ME|0\nWR_SCORE|0|x|notanumber") is None
    assert parse_world_rankings("WR_ME|0\nWR_SCORE|0|Us|-1\nWR_DONE") is None  # Unreadable.
    # Cut off before the end marker: one score must not pass for the whole table.
    assert parse_world_rankings("WR_ME|0\nWR_SCORE|0|Us|384\n") is None
    unreadable = "WR_ME|0\nWR_SCORE|0|Us|384\nWR_SCORE|-1||-1\nWR_DONE"
    assert parse_world_rankings(unreadable) is None


class LuaGame:
    def __init__(self, ingame, gamecore=None):
        self.answers = {"ingame": ingame, "gamecore": gamecore}
        self.asked = []

    async def execute_lua(self, code, context="gamecore"):
        self.asked.append(context)
        answer = self.answers[context]
        if isinstance(answer, Exception):
            raise answer
        return answer or "(no output)"


def test_read_falls_back_to_gamecore_and_is_one_call_when_ingame_works():
    game = LuaGame(REPLY)
    assert asyncio.run(read_world_rankings(game)).alive == 5
    assert game.asked == ["ingame"]
    game = LuaGame(RuntimeError("no UI"), REPLY)
    assert asyncio.run(read_world_rankings(game)).alive == 5
    assert game.asked == ["ingame", "gamecore"]
    assert asyncio.run(read_world_rankings(LuaGame(None, None))) is None


def test_lua_never_reads_unmet_names_or_ids():
    from civ6_ai.world_rankings import _WORLD_RANKINGS_LUA as lua

    assert "if metOrMe(i) then" in lua  # The name is read only inside this branch.
    assert lua.count("GetCivilizationShortDescription") == 1
    assert "GetCities" not in lua and "GetUnits" not in lua and "GetX" not in lua


class Game:
    async def get_game_overview(self):
        return SimpleNamespace(turn=151)


def test_handler_logs_one_line_and_holds_the_reading(caplog):
    holder = RankingsHolder()
    handler = WorldRankingsLog(LuaGame(REPLY), holder)
    with caplog.at_level(logging.INFO):
        asyncio.run(handler.run(Game()))
    assert holder.last.turn == 151 and holder.current(151).alive == 5
    assert holder.current(152) is None
    assert "world_rankings: turn=151 rank=5/5 alive=5 unmet=2 leader_score=590 gap=-206" in (
        caplog.text
    )


def test_failed_read_clears_the_holder():
    holder = RankingsHolder()
    asyncio.run(WorldRankingsLog(LuaGame(REPLY), holder).run(Game()))
    asyncio.run(WorldRankingsLog(LuaGame(None, None), holder).run(Game()))
    assert holder.last is None


def test_victory_threat_line_names_unmet_leaders():
    r = parse_world_rankings(REPLY)
    line = format_line(151, None, r)
    assert line.endswith("no reading yet unmet_leaders=[science 12; culture 130.5]")
    assert format_line(151, None).endswith("no reading yet")
    holder = RankingsHolder()
    holder.last = r
    assert VictoryThreatLog(VictoryWatch(), holder)._rankings is holder


def test_turn_record_carries_true_rank_and_history_uses_it(tmp_path):
    class FullGame:
        turn = 150

        async def check_game_over(self):
            return None

        async def get_game_overview(self):
            return SimpleNamespace(
                turn=self.turn,
                score=384,
                num_cities=5,
                total_population=30,
                rankings=[SimpleNamespace(player_id=5, score=421)],
            )

        async def end_turn(self):
            self.turn += 1
            return f"Turn {self.turn - 1} -> {self.turn}"

    holder = RankingsHolder()
    log_path = tmp_path / "runs" / "game.jsonl"
    turn_log = TurnLog(log_path)
    handlers = [WorldRankingsLog(LuaGame(REPLY), holder), DoNothing()]
    asyncio.run(Orchestrator(FullGame(), handlers, turn_log, rankings=holder).play(1))
    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["rival_scores"] == {"5": 421}  # Met only, as before.
    assert (record["players_alive"], record["rank"], record["leader_score"]) == (5, 5, 590)
    point = points([record])[0]
    assert point.rank == 5 and point.rivals == (590, 497, 421, 390)
    assert gaps([record | {"turn": 150}], {})[0]["to_leader"] == 384 - 590


def test_old_logs_without_all_scores_still_use_met_rivals():
    point = points([{"turn": 5, "score": 10, "rival_scores": {"1": 12, "2": 8}}])[0]
    assert point.rivals == (12, 8) and point.rank == 2
