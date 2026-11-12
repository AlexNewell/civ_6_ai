import asyncio

import pytest
from civ_mcp.game_state import GameState

from civ6_ai import gp_advisor


def test_query_finds_the_class_by_unit_type_and_accepts_unique_districts():
    lua = gp_advisor.build_gp_advisor_query(3)
    # civ6-mcp read Units.GreatPersonClass, which is nil in this build (Run 5).
    assert "GreatPersonClass " not in lua.replace("GreatPersonClasses", "")
    assert "GameInfo.GreatPersonClasses()" in lua
    assert "GameInfo.DistrictReplaces()" in lua  # The Maya Observatory stands in for a Campus.
    assert "---END---" in lua


class FakeConn:
    async def execute_write(self, lua):
        return [
            "GP_INFO|Aryabhata|GREAT_PERSON_CLASS_SCIENTIST|DISTRICT_CAMPUS|66|25|1",
            "GP_CITY|Wak Kab'nal|1|65|25|true|1|10|-1|-1",
        ]


def test_replacement_is_read_by_civ6_mcp_parser():
    game = GameState.__new__(GameState)
    game.conn = FakeConn()
    result = asyncio.run(gp_advisor.get_gp_advisor(game, 8))
    assert [(c.city_name, c.district_x, c.district_y) for c in result.cities] == [
        ("Wak Kab'nal", 65, 25)
    ]


def test_fix_replaces_the_method(monkeypatch):
    monkeypatch.setattr(GameState, "get_gp_advisor", None, raising=False)
    with pytest.raises(RuntimeError):
        gp_advisor.fix_gp_advisor()


def test_an_engineer_is_offered_only_the_tiles_the_game_highlights():
    lua = gp_advisor.build_gp_advisor_query(3)
    # A Great Engineer needs a wonder being built ("Must be on a wonder..."), Run 5 turns 265-315.
    engineer_branch = lua.split('gpClass == "GREAT_PERSON_CLASS_ENGINEER"', 1)[1].split(
        "local yieldIdx"
    )[0]
    assert "for plotIdx, _ in pairs(validPlotSet)" in engineer_branch
    assert "do return end" in engineer_branch  # A bare return before more code is a Lua error.
    assert "GetPlotPurchaseCity(x, y)" in engineer_branch
