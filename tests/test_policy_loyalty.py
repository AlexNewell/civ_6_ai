import asyncio
import logging

from civ_mcp import lua as lq

from civ6_ai.monitors import CityLoyaltyResponse, CityLoyaltyWarning, EventBus
from civ6_ai.policies.loyalty import LoyaltyResponse, is_hopeless, needed_rate, turns_left
from civ6_ai.state import read_cities
from fixtures import civ_state as fx
from test_policy_government import ScriptedGame, appointed, slot

CITY_ID = 65540
VICTOR = "GOVERNOR_THE_DEFENDER"
AMANI = "GOVERNOR_THE_AMBASSADOR"


class LoyaltyGame(ScriptedGame):
    def __init__(self, units=(), garrison="", **kwargs):
        falling = fx.city_info(
            city_id=CITY_ID,
            name="Far",
            x=20,
            y=20,
            loyalty=40.0,
            loyalty_per_turn=-8.0,
            garrison_unit=garrison,
        )
        super().__init__(cities=[fx.city_info(), falling], units=list(units), **kwargs)
        self.moves: list[tuple] = []

    async def move_unit(self, unit_index, x, y):
        self.moves.append((unit_index, x, y))
        return f"MOVING_TO|{x},{y}"


def warned(bus, turn=50):
    bus.publish(CityLoyaltyWarning(turn, CITY_ID, "Far", 40.0, -8.0))


def make(**kwargs):
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append, CityLoyaltyResponse)
    return bus, LoyaltyResponse(bus), LoyaltyGame(**kwargs), seen


def run(handler, game):
    asyncio.run(handler.run(game))


def test_no_warning_means_no_game_call():
    _, handler, game, _ = make()
    run(handler, game)
    assert game.calls == []


def test_a_governor_waiting_for_a_city_goes_to_the_warned_city(caplog):
    bus, handler, game, seen = make(points=0)
    game.appointed = [appointed(VICTOR)]
    warned(bus)
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.loyalty"):
        run(handler, game)
    assert ("assign", VICTOR, CITY_ID) in game.actions
    assert seen and "governor" in seen[0].actions[0]
    assert any("loyalty_response" in r.getMessage() for r in caplog.records)


def test_victor_is_appointed_and_sent_when_a_point_is_free():
    bus, handler, game, seen = make(points=1)
    game.to_appoint = [AMANI, VICTOR]
    warned(bus)
    run(handler, game)
    assert game.actions[:2] == [("appoint", VICTOR), ("assign", VICTOR, CITY_ID)]
    assert "appointed and sent" in seen[0].actions[0]


def test_a_governor_in_another_falling_city_is_left_there():
    bus, handler, game, _ = make(points=0)
    other = 65536
    game.data["cities"][0] = fx.city_info(city_id=other, loyalty_per_turn=-5.0)
    game.appointed = [appointed(VICTOR, city_id=other)]
    handler.warned[other] = 49
    warned(bus)
    run(handler, game)
    assert ("assign", VICTOR, CITY_ID) not in game.actions


def test_a_governor_already_in_the_city_is_not_moved():
    bus, handler, game, seen = make(points=0)
    game.appointed = [appointed(AMANI, city_id=CITY_ID)]
    warned(bus)
    run(handler, game)
    assert not [a for a in game.actions if a[0] == "assign"]
    assert "already here" in seen[0].actions[0]


def test_spare_unit_garrisons_an_empty_warned_city():
    spare = fx.unit_info(unit_id=5, unit_index=5, x=18, y=20)
    bus, handler, game, seen = make(
        units=[spare, fx.unit_info(unit_id=6, unit_index=6, x=18, y=21)]
    )
    warned(bus)
    run(handler, game)
    assert game.moves and game.moves[0][1:] == (20, 20)
    assert any("garrison" in a for a in seen[0].actions)


def test_a_garrisoned_city_gets_no_extra_unit():
    spare = fx.unit_info(unit_id=5, unit_index=5, x=18, y=20)
    bus, handler, game, _ = make(units=[spare], garrison="UNIT_WARRIOR")
    warned(bus)
    run(handler, game)
    assert game.moves == []


def test_a_loyalty_card_fills_an_empty_slot():
    bus, handler, game, seen = make(
        slots=[slot(0, "SLOT_ECONOMIC")],
        cards=[lq.PolicyInfo("POLICY_CIVIC_LOYALTY", "x", "", "SLOT_ECONOMIC")],
    )
    warned(bus)
    run(handler, game)
    assert ("policies", {0: "POLICY_CIVIC_LOYALTY"}) in game.actions
    assert any("card" in a for a in seen[0].actions)


def test_cooldown_and_recovery():
    bus, handler, game, _ = make(points=0)
    game.appointed = [appointed(VICTOR)]
    warned(bus, turn=50)
    run(handler, game)
    first = len(game.actions)
    run(handler, game)  # Same turn: inside the cooldown.
    assert len(game.actions) == first
    game.data["cities"][1] = fx.city_info(city_id=CITY_ID, loyalty_per_turn=2.0, x=20, y=20)
    run(handler, game)
    assert handler.warned == {}


def _city(loyalty, per_turn):
    game = LoyaltyGame()
    game.data["cities"][1] = fx.city_info(
        city_id=CITY_ID, name="Far", x=20, y=20, loyalty=loyalty, loyalty_per_turn=per_turn
    )
    return [c for c in asyncio.run(read_cities(game)) if c.city_id == CITY_ID][0]


def test_run9_numbers_chichen_itza_and_copan_were_past_saving_but_adab_was_not():
    chichen = _city(76.0, -24.0)  # Lost 4 turns later.
    assert turns_left(chichen) == 76 / 24 and needed_rate(chichen) == 24.0
    assert is_hopeless(chichen)  # With a governor's +9 it still flips in about 5 turns.
    adab = _city(69.0, -15.6)
    assert not is_hopeless(adab)
    copan = _city(30.0, -14.4)  # A governor took it to -5.6 and it still fell.
    assert is_hopeless(copan)  # Still 5.6 turns even so; the governor is sent regardless.
    assert _city(40.0, 1.0).loyalty_per_turn == 1.0 and turns_left(_city(40.0, 1.0)) is None


def test_a_hopeless_city_gets_no_garrison_and_the_log_says_what_it_needed(caplog):
    bus, handler, game, seen = make(
        points=0, units=[fx.unit_info(unit_id=2, unit_index=2, x=19, y=20)]
    )
    game.data["cities"][1] = fx.city_info(
        city_id=CITY_ID, name="Far", x=20, y=20, loyalty=76.0, loyalty_per_turn=-24.0
    )
    warned(bus)
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.loyalty"):
        run(handler, game)
    assert game.moves == []
    assert any(a.startswith("accept the loss") for a in seen[0].actions)
    message = next(r.getMessage() for r in caplog.records if "loyalty_response" in r.getMessage())
    assert "needs=+24.0/turn" in message and "with_governor=5.1" in message
