import asyncio

import pytest
from civ_mcp import end_turn

from civ6_ai import victory_check


class FakeConn:
    def __init__(self, lines):
        self.lines = lines

    async def execute_write(self, lua):
        return list(self.lines)


class FakeGame:
    def __init__(self, lines):
        self.conn = FakeConn(lines)


@pytest.fixture(autouse=True)
def restore(monkeypatch):
    monkeypatch.setattr(end_turn, "_check_victory_proximity", end_turn._check_victory_proximity)


def test_the_pinned_check_fails_on_a_decimal_and_the_fix_does_not():
    lines = ["SCI_THREAT|Rome|2|33.5", "DIPLO_THREAT|Rome|12.5", "---END---"]
    with pytest.raises(ValueError):
        asyncio.run(end_turn._check_victory_proximity(FakeGame(lines)))
    victory_check.fix_victory_check()
    events = asyncio.run(end_turn._check_victory_proximity(FakeGame(lines)))
    messages = [e.message for e in events]
    assert any("2/33 space race" in m for m in messages)
    assert any("12/20 DVP" in m for m in messages)


def test_whole_numbers_are_left_alone():
    assert victory_check.whole_numbers("SCI_THREAT|Rome|2|50") == "SCI_THREAT|Rome|2|50"
    assert (
        victory_check.whole_numbers("REL_THREAT|Rome|Faith|1.5|4") == "REL_THREAT|Rome|Faith|1.5|4"
    )
    assert victory_check.whole_numbers("SCI_THREAT|Rome|x.y|3") == "SCI_THREAT|Rome|x.y|3"


def test_fixing_twice_does_not_stack():
    victory_check.fix_victory_check()
    once = end_turn._check_victory_proximity
    victory_check.fix_victory_check()
    assert end_turn._check_victory_proximity is once


def test_it_fails_loudly_when_the_pinned_code_changes(monkeypatch):
    async def changed(gs):
        return []

    monkeypatch.setattr(end_turn, "_check_victory_proximity", changed)
    with pytest.raises(RuntimeError):
        victory_check.fix_victory_check()
