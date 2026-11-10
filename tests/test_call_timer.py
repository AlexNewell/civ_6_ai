import asyncio

import pytest

from civ6_ai.call_timer import CallTimer


class Game:
    flag = "attribute"

    async def get_units(self):
        return ["unit"]

    async def move_unit(self, index, x, y):
        if x < 0:
            raise ValueError("off the map")
        return "ok"

    def helper(self):
        return "not a game call"


def test_calls_are_counted_per_handler_and_method():
    timer = CallTimer(Game())

    async def go():
        timer.begin("settling")
        assert await timer.get_units() == ["unit"]
        assert await timer.get_units() == ["unit"]
        await timer.move_unit(1, 2, 3)
        timer.begin("military")
        await timer.get_units()

    asyncio.run(go())
    taken = timer.take()
    assert taken["settling"]["get_units"][0] == 2
    assert taken["settling"]["move_unit"][0] == 1
    assert taken["military"]["get_units"][0] == 1
    assert taken["settling"]["get_units"][1] >= 0


def test_take_starts_over():
    timer = CallTimer(Game())
    timer.begin("a")
    asyncio.run(timer.get_units())
    assert timer.take()
    assert timer.take() == {}


def test_calls_outside_a_handler_are_not_counted():
    timer = CallTimer(Game())
    asyncio.run(timer.get_units())
    timer.begin("a")
    timer.begin(None)
    asyncio.run(timer.get_units())
    assert timer.take() == {}


def test_a_failing_call_is_counted_and_still_raises():
    timer = CallTimer(Game())
    timer.begin("a")
    with pytest.raises(ValueError):
        asyncio.run(timer.move_unit(1, -1, 0))
    assert timer.take()["a"]["move_unit"][0] == 1


def test_attributes_and_plain_methods_pass_through():
    timer = CallTimer(Game())
    timer.begin("a")
    assert timer.flag == "attribute"
    assert timer.helper() == "not a game call"
    assert timer.take() == {}


def test_seconds_are_the_time_the_call_took():
    class Slow:
        async def get_units(self):
            await asyncio.sleep(0.05)

    timer = CallTimer(Slow())
    timer.begin("a")
    asyncio.run(timer.get_units())
    assert timer.take()["a"]["get_units"][1] >= 0.04
