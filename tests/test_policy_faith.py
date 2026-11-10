import asyncio
import logging

import pytest
from civ_mcp import lua as lq

from civ6_ai.policies.faith import (
    MAX_ATTEMPTS,
    PROPHET_ACT_AFTER,
    FaithPolicy,
    FaithSettings,
)
from civ6_ai.policies.secondary import SecondaryWeights
from fixtures.civ_state import FakeGame, city_info, overview, unit_info

RELIGION_OPEN = lq.ReligionFoundingStatus(
    False, None, None, -1, 0.0, available_religions=[("RELIGION_X", "X")]
)
RELIGION_HELD = lq.ReligionFoundingStatus(True, "RELIGION_Y", "Y", 1, 0.0)
PANTHEON = lq.PantheonStatus(True, "B", "B", 0.0, [])


def person(pid=1, cls="Great Scientist", name="Hypatia", faith=100, **changes):
    base = dict(
        class_name=cls,
        individual_name=name,
        era_name="Classical",
        cost=100,
        claimant="Unclaimed",
        player_points=0,
        faith_cost=faith,
        individual_id=pid,
    )
    return lq.GreatPersonInfo(**{**base, **changes})


def prophet(x=10, y=10, **changes):
    return unit_info(
        unit_id=7,
        unit_index=7,
        unit_type="UNIT_GREAT_PROPHET",
        name="Prophet",
        x=x,
        y=y,
        combat_strength=0,
        **changes,
    )


class Game(FakeGame):
    def __init__(self, **overrides):
        defaults = dict(
            overview=overview(turn=100, faith=300.0),
            pantheon_status=PANTHEON,
            religion=RELIGION_OPEN,
            people=[],
            units=[],
            production=[lq.ProductionOption("BUILDING", "BUILDING_SHRINE", 60, 6, 120)],
            cities=[city_info()],
        )
        super().__init__(**{**defaults, **overrides})
        self.actions: list[tuple] = []
        self.replies: dict[str, list[str]] = {}

    def reply(self, action, default):
        queue = self.replies.get(action)
        return queue.pop(0) if queue else default

    async def get_pantheon_status(self):
        return self.data["pantheon_status"]

    async def get_religion_founding_status(self):
        return self.data["religion"]

    async def get_great_people(self):
        return self.data["people"]

    async def patronize_great_person(self, individual_id, yield_type="YIELD_GOLD"):
        self.actions.append(("patronize", individual_id, yield_type))
        return self.reply("patronize", "OK:PATRONIZED|x|cost:1 faith")

    async def purchase_item(self, city_id, item_type, item_name, yield_type="YIELD_GOLD"):
        self.actions.append(("buy", city_id, item_name, yield_type))
        return self.reply("buy", f"PURCHASED|{item_name}|cost=70f (had 300f)")

    async def activate_great_person(self, unit_index):
        self.actions.append(("activate", unit_index))
        return self.reply("activate", "Error: CANNOT_ACTIVATE|no Holy Site")

    async def get_gp_advisor(self, unit_index):
        return self.data.get("advisor")

    async def move_unit(self, unit_index, x, y):
        self.actions.append(("move", unit_index, x, y))
        return self.reply("move", f"MOVING_TO|{x},{y}")

    async def delete_unit(self, unit_index):
        self.actions.append(("delete", unit_index))
        result = self.reply("delete", "OK:DELETED|UNIT_GREAT_PROPHET at 10,10")
        if result.startswith("OK"):
            self.data["units"] = []
        return result

    async def spread_religion(self, unit_index):
        self.actions.append(("spread", unit_index))
        return self.reply("spread", "Error: CANNOT_SPREAD|nothing here")


def run(policy, game):
    asyncio.run(policy.run(game))


def set_overview(game, turn=100, faith=None):
    faith = game.data["overview"].faith if faith is None else faith
    game.data["overview"] = overview(turn=turn, faith=faith)


def holy_site(distance=0, city_id=1, x=12, y=10, **changes):
    base = dict(
        city_name="Rome",
        city_id=city_id,
        district_x=x,
        district_y=y,
        can_activate=distance == 0,
        distance=distance,
    )
    return lq.GPAdvisorCity(**{**base, **changes})


def advisor(*cities):
    return lq.GPAdvisorResult(
        "Prophet", "GREAT_PERSON_CLASS_PROPHET", "DISTRICT_HOLY_SITE", 10, 10, 1, list(cities)
    )


def acted(game, kind):
    return [a for a in game.actions if a[0] == kind]


# ----- reserve -----


def test_nothing_is_spent_at_or_below_the_reserve():
    game = Game(overview=overview(turn=100, faith=60.0), people=[person(faith=10)])
    run(FaithPolicy(), game)
    assert game.actions == []


def test_a_purchase_never_dips_below_the_reserve():
    game = Game(overview=overview(turn=100, faith=150.0), people=[person(faith=100)])
    run(FaithPolicy(), game)  # 90 above the reserve of 60, the price is 100.
    assert acted(game, "patronize") == []
    set_overview(game, faith=160.0)
    run(FaithPolicy(), game)
    assert acted(game, "patronize") == [("patronize", 1, "YIELD_FAITH")]


def test_faith_is_kept_for_a_pantheon_that_can_still_be_chosen():
    open_pantheon = lq.PantheonStatus(False, None, None, 300.0, [object()])
    game = Game(pantheon_status=open_pantheon, people=[person(faith=10)])
    run(FaithPolicy(), game)
    assert game.actions == []
    game.data["pantheon_status"] = lq.PantheonStatus(False, None, None, 300.0, [])
    run(FaithPolicy(), game)  # None left to choose, so the faith is free.
    assert acted(game, "patronize")


def test_reserve_and_purchase_bound_are_settings():
    game = Game(people=[person(1, faith=10), person(2, name="B", faith=10), person(3, faith=10)])
    run(FaithPolicy(FaithSettings(reserve=100.0, max_purchases=1)), game)
    assert len(acted(game, "patronize")) == 1
    with pytest.raises(ValueError):
        FaithSettings(reserve=-1)


# ----- priority -----


def test_great_people_come_before_buildings_and_rank_by_class_weight():
    game = Game(
        people=[
            person(1, "Great Writer", "Homer", faith=50),
            person(2, "Great Scientist", "Hypatia", faith=120),
        ]
    )
    run(FaithPolicy(FaithSettings(max_purchases=2)), game)
    assert [a[1] for a in acted(game, "patronize")] == [2, 1]
    assert acted(game, "buy") == []  # Two purchases is the bound.


def test_buildings_are_bought_when_no_great_person_is_affordable():
    game = Game(people=[person(faith=900)])
    run(FaithPolicy(), game)
    assert acted(game, "patronize") == []
    assert acted(game, "buy") == [("buy", 65536, "BUILDING_SHRINE", "YIELD_FAITH")]


def test_a_prophet_is_bought_only_while_a_religion_can_be_founded():
    people = [person(1, "Great Prophet", "Moses", faith=80)]
    game = Game(people=people, religion=RELIGION_HELD)
    run(FaithPolicy(), game)
    assert acted(game, "patronize") == []
    game.data["religion"] = RELIGION_OPEN
    run(FaithPolicy(), game)
    assert [a[1] for a in acted(game, "patronize")] == [1]


def test_claimed_people_and_zero_weight_classes_are_not_bought():
    game = Game(people=[person(1, claimant="Rome"), person(2, "Great Artist", faith=40)])
    run(FaithPolicy(weights=SecondaryWeights(gp_artist=0.0)), game)
    assert acted(game, "patronize") == []


def test_building_price_is_learned_from_the_first_purchase():
    policy = FaithPolicy()
    game = Game(overview=overview(turn=100, faith=170.0))
    run(policy, game)  # Estimate 100 fits in 110 above the reserve. Real price 70.
    assert len(acted(game, "buy")) == 1
    set_overview(game, turn=110, faith=135.0)
    run(policy, game)  # 75 above the reserve covers the learned 70, not the estimate.
    assert len(acted(game, "buy")) == 2


def test_the_per_turn_call_bound_holds_when_everything_is_refused():
    people = [person(i, name=f"P{i}", faith=10) for i in range(1, 20)]
    game = Game(people=people)
    game.replies["patronize"] = ["ERR:CANNOT_PATRONIZE|no"] * 30
    run(FaithPolicy(), game)
    assert len(acted(game, "patronize")) <= MAX_ATTEMPTS


# ----- error strings -----


@pytest.mark.parametrize(
    "result",
    ["ERR:CANNOT_PATRONIZE|Cannot buy X (cost: 10 faith)", "No response", "Error: whatever"],
)
def test_patronise_failures_are_not_counted_and_are_not_retried_at_once(result, caplog):
    game = Game(people=[person(faith=10)])
    game.replies["patronize"] = [result]
    policy = FaithPolicy()
    with caplog.at_level(logging.INFO):
        run(policy, game)
        run(policy, game)  # Still inside the refused wait.
    assert len(acted(game, "patronize")) == 1
    assert "Can't buy" in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        "Error: CANNOT_PURCHASE|costs 150f but you only have 90f",
        "Error: STACKING_CONFLICT|a unit is on the city tile",
        "Error: ITEM_NOT_FOUND|BUILDING_SHRINE",
    ],
)
def test_purchase_failures_are_logged_and_wait(result, caplog):
    game = Game()
    game.replies["buy"] = [result]
    policy = FaithPolicy()
    with caplog.at_level(logging.INFO):
        run(policy, game)
        run(policy, game)
    assert len(acted(game, "buy")) == 1
    assert "nothing bought" in caplog.text
    assert "refused" in caplog.text


def test_a_refused_price_quote_is_remembered():
    game = Game(overview=overview(turn=100, faith=200.0))
    game.replies["buy"] = ["Error: CANNOT_PURCHASE|costs 150f but you only have 90f"]
    policy = FaithPolicy()
    run(policy, game)
    assert policy._costs["BUILDING_SHRINE"] == 150


def test_above_reserve_with_nothing_offered_is_logged_with_a_reason(caplog):
    game = Game(production=[])
    with caplog.at_level(logging.INFO):
        run(FaithPolicy(), game)
    assert "nothing bought" in caplog.text
    assert "no great person" in caplog.text


def test_a_failing_call_does_not_stop_the_prophet_part():
    game = Game(units=[prophet()])

    async def broken():
        raise RuntimeError("boom")

    game.get_great_people = broken
    policy = FaithPolicy()
    with pytest.raises(RuntimeError):
        run(policy, game)
    assert policy._prophets  # The prophet part ran before the failure.


# ----- prophets -----


def play(policy, game, turns, start=100):
    for turn in range(start, start + turns):
        set_overview(game, turn=turn, faith=0.0)
        run(policy, game)


def test_a_prophet_that_keeps_moving_is_left_alone():
    game = Game(units=[prophet(10, 10)])
    policy = FaithPolicy()
    for turn in range(100, 106):
        game.data["units"] = [prophet(10 + turn - 100, 10)]
        set_overview(game, turn=turn, faith=0.0)
        run(policy, game)
    assert game.actions == []


def test_an_idle_prophet_is_acted_on_within_the_bound_when_it_can_found():
    game = Game(units=[prophet()], advisor=advisor(holy_site(0)))
    policy = FaithPolicy()
    play(policy, game, PROPHET_ACT_AFTER + 1)  # Sighting, then PROPHET_ACT_AFTER idle turns.
    assert PROPHET_ACT_AFTER <= 5
    assert acted(game, "activate") == [("activate", 7)]
    assert acted(game, "spread") == []  # A religion can be founded, so no spreading.


def test_activation_uses_the_prophet_up_and_forgets_it():
    game = Game(units=[prophet()], advisor=advisor(holy_site(0)))
    game.replies["activate"] = ["GP_ACTIVATED|Prophet"]
    policy = FaithPolicy()
    play(policy, game, PROPHET_ACT_AFTER + 1)
    assert policy._prophets == {}


def test_with_no_religion_to_found_the_prophet_tries_to_spread():
    game = Game(units=[prophet()], religion=RELIGION_HELD)
    game.replies["spread"] = ["RELIGION_SPREAD|Prophet spread religion at 10,10"]
    play(FaithPolicy(), game, PROPHET_ACT_AFTER + 1)
    assert acted(game, "spread") == [("spread", 7)]
    assert acted(game, "activate") == []


def test_a_refused_fallback_is_retried_and_logged_not_looped(caplog):
    game = Game(units=[prophet()], religion=RELIGION_HELD)
    with caplog.at_level(logging.INFO):
        play(FaithPolicy(), game, 14)
    assert 2 <= len(acted(game, "spread")) <= 6  # It backs off between tries.
    assert "cannot spread here" in caplog.text
    assert "still idle" in caplog.text  # One warning once past 5 turns.


def test_other_units_are_left_alone():
    other = unit_info(unit_id=9, unit_index=9, unit_type="UNIT_WARRIOR")
    game = Game(units=[other], religion=RELIGION_HELD)
    play(FaithPolicy(), game, 6)
    assert game.actions == []


def test_a_prophet_that_is_gone_is_forgotten():
    game = Game(units=[prophet()])
    policy = FaithPolicy()
    play(policy, game, 2)
    assert policy._prophets
    game.data["units"] = []
    run(policy, game)
    assert policy._prophets == {}


# ----- prophet and the Holy Site -----


def test_a_prophet_off_the_site_walks_to_the_nearest_holy_site():
    game = Game(units=[prophet()], advisor=advisor(holy_site(6, 2, 20, 10), holy_site(3, 1)))
    play(FaithPolicy(), game, PROPHET_ACT_AFTER + 1)
    assert acted(game, "move") == [("move", 7, 12, 10)]
    assert acted(game, "activate") == []


def test_a_refused_move_tries_the_next_site_up_to_the_bound():
    sites = [holy_site(d, d, 10 + d, 10) for d in (1, 2, 3, 4, 5)]
    game = Game(units=[prophet()], advisor=advisor(*sites))
    game.replies["move"] = ["Error: NO_PATH", "MOVING_TO|x|BLOCKED (enemy)", "MOVING_TO|13,10"]
    play(FaithPolicy(), game, PROPHET_ACT_AFTER + 1)
    assert [a[2] for a in acted(game, "move")] == [11, 12, 13]


def test_every_move_refused_logs_and_backs_off(caplog):
    game = Game(units=[prophet()], advisor=advisor(holy_site(2)))
    game.replies["move"] = ["Error: NO_PATH"] * 20
    with caplog.at_level(logging.INFO):
        play(FaithPolicy(), game, 12)
    assert 1 <= len(acted(game, "move")) <= 6
    assert "every move toward a Holy Site was refused" in caplog.text


def test_a_prophet_that_arrives_activates_without_extra_orders():
    game = Game(units=[prophet(10, 10)], advisor=advisor(holy_site(2)))
    policy = FaithPolicy()
    play(policy, game, PROPHET_ACT_AFTER + 1)
    assert len(acted(game, "move")) == 1
    game.data["units"] = [prophet(12, 10)]  # Arrived.
    game.data["advisor"] = advisor(holy_site(0))
    game.replies["activate"] = ["GP_ACTIVATED|Prophet"]
    play(policy, game, PROPHET_ACT_AFTER + 1, start=110)
    assert acted(game, "activate") == [("activate", 7)]
    assert policy._prophets == {}


def test_walking_orders_are_bounded():
    from civ6_ai.policies.faith import PROPHET_MAX_MOVES

    game = Game(units=[prophet()], advisor=advisor(holy_site(5)))
    play(FaithPolicy(), game, 80)  # It never gets anywhere.
    assert len(acted(game, "move")) == PROPHET_MAX_MOVES


def test_no_holy_site_is_logged_once_and_asked_for_rarely(caplog):
    game = Game(units=[prophet()], advisor=advisor())
    with caplog.at_level(logging.INFO):
        play(FaithPolicy(), game, 20)
    assert caplog.text.count("no city has a finished Holy Site") == 1
    assert acted(game, "move") == []
    assert acted(game, "activate") == []
    assert acted(game, "delete") == []  # A religion can still be founded later.


def test_an_unreadable_advisor_counts_as_no_site(caplog):
    game = Game(units=[prophet()], advisor=None)
    with caplog.at_level(logging.INFO):
        play(FaithPolicy(), game, 5)
    assert "no city has a finished Holy Site" in caplog.text


def test_a_prophet_with_no_use_is_deleted_after_repeated_refusals(caplog):
    from civ6_ai.policies.faith import SPREAD_GIVE_UP

    game = Game(units=[prophet()], religion=RELIGION_HELD)
    policy = FaithPolicy()
    with caplog.at_level(logging.INFO):
        play(policy, game, 60)
    assert acted(game, "delete") == [("delete", 7)]
    assert len(acted(game, "spread")) == SPREAD_GIVE_UP
    assert "deleted" in caplog.text
    assert policy._prophets == {}


def test_a_failed_delete_is_logged_and_retried_later():
    game = Game(units=[prophet()], religion=RELIGION_HELD)
    game.replies["delete"] = ["Error: CANNOT_DELETE|no"]
    play(FaithPolicy(), game, 60)
    assert len(acted(game, "delete")) == 2


def test_a_successful_spread_resets_the_refusal_count():
    game = Game(units=[prophet()], religion=RELIGION_HELD)
    play(FaithPolicy(), game, 14)  # A few refusals.
    game.replies["spread"] = ["RELIGION_SPREAD|ok"]
    play(FaithPolicy(), game, 3, start=200)
    assert acted(game, "delete") == []
