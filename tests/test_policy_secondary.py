import asyncio
from types import SimpleNamespace

import pytest
from civ_mcp import lua as lq

from civ6_ai.blockers import BlockerResolver, BlockerType
from civ6_ai.policies.production import ProductionContext, ProductionWeights, score
from civ6_ai.policies.secondary import (
    GP_STUCK_TURNS,
    MAX_DESTINATIONS,
    SecondaryPolicy,
    SecondaryWeights,
    parse_yields,
    rank_beliefs,
    rank_destinations,
    rank_recruits,
    score_destination,
)
from civ6_ai.state import ProductionChoice
from fixtures.civ_state import FakeGame, overview, unit_info

W = SecondaryWeights()
MET = frozenset({"Rome", "Kabul"})


def dest(name="Roma", owner="Rome", x=20, y=10, domestic=False, **changes):
    base = dict(
        city_name=name,
        owner_name="Domestic" if domestic else owner,
        x=x,
        y=y,
        is_domestic=domestic,
        origin_yields="Gold:3",
    )
    return lq.TradeDestination(**{**base, **changes})


def trader(uid=5, x=10, y=10, **changes):
    return lq.TraderInfo(**{**dict(unit_id=uid, x=x, y=y, has_moves=True), **changes})


def person(pid=1, cls="Great Scientist", name="Hypatia", **changes):
    base = dict(
        class_name=cls,
        individual_name=name,
        era_name="Classical",
        cost=100,
        claimant="Unclaimed",
        player_points=120,
        can_recruit=True,
        individual_id=pid,
    )
    return lq.GreatPersonInfo(**{**base, **changes})


def gp_city(x=12, y=10, distance=2, can=False, **changes):
    base = dict(
        city_name="Alpha",
        city_id=65536,
        district_x=x,
        district_y=y,
        can_activate=can,
        distance=distance,
    )
    return lq.GPAdvisorCity(**{**base, **changes})


def beliefs(*types):
    return [lq.BeliefInfo(t, t, "") for t in types]


class SecondaryGame(FakeGame):
    def __init__(self, **overrides):
        defaults = dict(
            trade=lq.TradeRouteStatus(capacity=2, active_count=0, traders=[]),
            destinations={},
            pantheon_status=[lq.PantheonStatus(True, "B", "B", 30.0, [])],
            religion=lq.ReligionFoundingStatus(False, None, None, -1, 50.0),
            notifications=[],
            people=[],
            advisor=None,
        )
        super().__init__(**{**defaults, **overrides})
        self.actions: list[tuple] = []
        self.replies: dict[str, list[str]] = {}
        self.recruited: set[int] = set()
        self.chosen = 0  # Pantheon statuses move on after each belief is chosen.

    def reply(self, action, default):
        queue = self.replies.get(action)
        return queue.pop(0) if queue else default

    # Reads
    async def get_trade_routes(self):
        self.calls.append("get_trade_routes")
        return self.data["trade"]

    async def get_trade_destinations(self, unit_index):
        self.calls.append("get_trade_destinations")
        return self.data["destinations"].get(unit_index, [])

    async def get_pantheon_status(self):
        self.calls.append("get_pantheon_status")
        statuses = self.data["pantheon_status"]
        return statuses[min(self.chosen, len(statuses) - 1)]

    async def get_religion_founding_status(self):
        self.calls.append("get_religion_founding_status")
        return self.data["religion"]

    async def get_notifications(self):
        return self.data["notifications"]

    async def get_great_people(self):
        self.calls.append("get_great_people")
        return [p for p in self.data["people"] if p.individual_id not in self.recruited]

    async def get_gp_advisor(self, unit_index):
        self.calls.append("get_gp_advisor")
        return self.data["advisor"]

    # Actions
    async def make_trade_route(self, unit_index, target_x, target_y):
        self.actions.append(("route", unit_index, target_x, target_y))
        return self.reply("route", f"TRADE_ROUTE_STARTED|to X at ({target_x},{target_y})")

    async def choose_pantheon(self, belief_type):
        self.actions.append(("pantheon", belief_type))
        self.chosen += 1
        return self.reply("pantheon", "PANTHEON_FOUNDED|x")

    async def found_religion(self, religion_type, follower, founder):
        self.actions.append(("religion", religion_type, follower, founder))
        return self.reply("religion", "RELIGION_FOUNDED|x")

    async def recruit_great_person(self, individual_id):
        self.actions.append(("recruit", individual_id))
        result = self.reply("recruit", f"OK:RECRUITED|{individual_id}")
        if "RECRUITED" in result:
            self.recruited.add(individual_id)
        return result

    async def activate_great_person(self, unit_index):
        self.actions.append(("activate", unit_index))
        return self.reply("activate", "GP_ACTIVATED|x")

    async def move_unit(self, unit_index, target_x, target_y):
        self.actions.append(("move", unit_index, target_x, target_y))
        return self.reply("move", f"MOVING_TO|{target_x},{target_y}")


def make_game(turn=50, **data):
    game = SecondaryGame(overview=overview(turn=turn), **data)
    return game


def run(policy, game):
    asyncio.run(policy.run(game))


def acted(game, kind):
    return [a for a in game.actions if a[0] == kind]


# ----- trade scoring -----


def test_yields_parse_and_ignore_junk():
    assert parse_yields("Food:3 Prod:2 Gold:4.5") == {"food": 3, "production": 2, "gold": 4.5}
    assert parse_yields("nonsense") == {}


def test_destinations_rank_by_our_yields_and_weights():
    rich = dest("Rich", origin_yields="Gold:6")
    poor = dest("Poor", x=21, origin_yields="Gold:2")
    assert [d.city_name for d in rank_destinations([poor, rich], W, MET)] == ["Rich", "Poor"]
    food = SecondaryWeights(food=5.0, gold=0.1)
    farm = dest("Farm", origin_yields="Food:2", x=22)
    assert rank_destinations([rich, farm], food, MET)[0].city_name == "Farm"


def test_destination_yields_of_a_foreign_city_are_not_counted():
    foreign = dest(dest_yields="Gold:50 Food:50")
    assert score_destination(foreign, W) == score_destination(dest(), W)
    home = dest(domestic=True, dest_yields="Food:2 Prod:1 Gold:50")
    assert score_destination(home, W) == 3.0 + 2.0 + 1.0


def test_city_state_quest_and_new_trading_post_add_value():
    base = dest("Kabul", owner="Kabul", is_city_state=True, has_trading_post=True)
    quest = dest("Kabul", owner="Kabul", is_city_state=True, has_trading_post=True, has_quest=True)
    assert score_destination(quest, W) == score_destination(base, W) + W.quest
    fresh = dest("Kabul", owner="Kabul", has_trading_post=False)
    assert score_destination(fresh, W) > score_destination(dest(has_trading_post=True), W)


def test_unmet_owners_are_never_eligible_but_domestic_always_is():
    unmet = dest("Memphis", owner="Egypt", origin_yields="Gold:99")
    home = dest("Beta", domestic=True, origin_yields="Gold:1")
    assert rank_destinations([unmet, home], W, MET) == [home]


def test_rival_religion_is_not_a_score_input():
    spy = dest(religion_in="Islam", pressure_in=99.0)
    assert score_destination(spy, W) == score_destination(dest(), W)


def test_ranking_ignores_input_order_and_breaks_ties_by_name():
    a, b = dest("Bravo", x=21), dest("Alpha", x=22)
    assert rank_destinations([a, b], W, MET) == rank_destinations([b, a], W, MET)
    assert rank_destinations([a, b], W, MET)[0].city_name == "Alpha"


def test_weights_are_validated():
    with pytest.raises(ValueError):
        SecondaryWeights(gold=-1.0)
    with pytest.raises(ValueError):
        SecondaryWeights(quest=float("inf"))


# ----- trade turn -----


def trade_game(traders, capacity=2, active=0, dests=None, turn=50):
    return make_game(
        turn=turn,
        trade=lq.TradeRouteStatus(capacity=capacity, active_count=active, traders=traders),
        destinations=dests or {},
    )


def test_idle_trader_takes_the_best_route():
    game = trade_game(
        [trader()],
        dests={
            5: [dest("Poor", origin_yields="Gold:1"), dest("Rich", x=21, origin_yields="Gold:9")]
        },
    )
    run(SecondaryPolicy(), game)
    assert acted(game, "route") == [("route", 5, 21, 10)]


def test_refused_route_does_not_strand_the_next_and_failure_text_is_not_success():
    game = trade_game(
        [trader()], dests={5: [dest("A", x=20), dest("B", x=21, origin_yields="Gold:1")]}
    )
    game.replies["route"] = ["Error: CANNOT_START_ROUTE|no"]
    run(SecondaryPolicy(), game)
    assert [a[2] for a in acted(game, "route")] == [20, 21]


def test_routes_to_the_same_city_are_not_repeated_from_one_origin():
    game = trade_game(
        [trader(5), trader(6)],
        dests={5: [dest("A", x=20), dest("B", x=21, origin_yields="Gold:1")]},
    )
    game.data["destinations"][6] = game.data["destinations"][5]
    run(SecondaryPolicy(), game)
    assert [(a[1], a[2]) for a in acted(game, "route")] == [(5, 20), (6, 21)]


def test_full_capacity_makes_no_calls_and_counts_down_as_routes_start():
    full = trade_game([trader()], capacity=1, active=1, dests={5: [dest()]})
    run(SecondaryPolicy(), full)
    assert not full.actions and "get_trade_destinations" not in full.calls
    one = trade_game([trader(5), trader(6)], capacity=1, dests={5: [dest()], 6: [dest("B", x=21)]})
    run(SecondaryPolicy(), one)
    assert len(acted(one, "route")) == 1


def test_capacity_full_reply_stops_trying():
    game = trade_game([trader()], dests={5: [dest(x=20), dest("B", x=21)]})
    game.replies["route"] = ["Error: CAPACITY_FULL|x"]
    run(SecondaryPolicy(), game)
    assert len(acted(game, "route")) == 1


def test_attempts_per_trader_are_bounded_and_a_stuck_trader_waits():
    many = [dest(f"C{i}", x=20 + i) for i in range(10)]
    game = trade_game([trader()], dests={5: many})
    game.replies["route"] = ["Error: no"] * 20
    policy = SecondaryPolicy()
    run(policy, game)
    assert len(acted(game, "route")) == MAX_DESTINATIONS
    before = len(game.actions)
    game.data["overview"] = overview(turn=51)
    run(policy, game)
    assert len(game.actions) == before  # Still waiting.
    game.data["overview"] = overview(turn=60)
    run(policy, game)
    assert len(game.actions) > before


def test_trade_wait_is_dropped_when_the_trader_is_gone():
    policy = SecondaryPolicy()
    game = trade_game([trader()], dests={5: []})
    run(policy, game)
    assert 5 in policy._trade_wait
    game.data["trade"] = lq.TradeRouteStatus(capacity=2, active_count=0, traders=[])
    run(policy, game)
    assert policy._trade_wait == {}


def test_a_trader_on_a_route_is_left_alone():
    game = trade_game([trader(on_route=True, has_moves=False)], active=1, dests={5: [dest()]})
    run(SecondaryPolicy(), game)
    assert not acted(game, "route")


def test_unmet_destination_is_never_tried():
    game = trade_game([trader()], dests={5: [dest("Memphis", owner="Egypt")]})
    run(SecondaryPolicy(), game)
    assert not acted(game, "route")


# ----- production builds the traders -----


def trader_option():
    return ProductionChoice("UNIT", "UNIT_TRADER", 40, 4)


def test_trader_scores_only_while_capacity_is_unfilled():
    from civ6_ai.state.reader import build_city
    from fixtures import civ_state as fx

    city = build_city(fx.city_info(), (), set())
    open_slots = ProductionContext(turn=120, num_cities=3, trade_capacity=2, traders=0)
    filled = ProductionContext(turn=120, num_cities=3, trade_capacity=2, traders=2)
    assert score(trader_option(), city, open_slots, ProductionWeights()) > 0
    assert score(trader_option(), city, filled, ProductionWeights()) == 0
    assert score(trader_option(), city, open_slots, ProductionWeights(trade=0.0)) == 0
    built = open_slots.after_choosing("UNIT_TRADER")
    assert built.traders == 1


# ----- pantheon -----


def pantheon_game(faith=30.0, taken=True):
    after = lq.PantheonStatus(taken, "B", "B", faith, [])
    before = lq.PantheonStatus(
        False,
        None,
        None,
        faith,
        beliefs("BELIEF_ORAL_TRADITION", "BELIEF_GOD_OF_THE_FORGE", "BELIEF_FERTILITY_RITES"),
    )
    return make_game(pantheon_status=[before, after])


def test_pantheon_is_chosen_when_available_and_confirmed():
    game = pantheon_game()
    run(SecondaryPolicy(), game)
    assert acted(game, "pantheon") == [("pantheon", "BELIEF_FERTILITY_RITES")]


def test_pantheon_waits_for_faith_but_the_blocker_does_not():
    game = pantheon_game(faith=2.0)
    policy = SecondaryPolicy()
    run(policy, game)
    assert not acted(game, "pantheon")
    assert asyncio.run(policy.resolve_pantheon(game, None))
    assert acted(game, "pantheon")


def test_refused_pantheon_belief_tries_the_next():
    game = pantheon_game()
    game.replies["pantheon"] = ["Error: BELIEF_NOT_FOUND|x"]
    run(SecondaryPolicy(), game)
    assert [a[1] for a in acted(game, "pantheon")][:2] == [
        "BELIEF_FERTILITY_RITES",
        "BELIEF_GOD_OF_THE_FORGE",
    ]


def test_pantheon_that_did_not_apply_is_not_success_and_backs_off():
    game = pantheon_game(taken=False)
    policy = SecondaryPolicy()
    run(policy, game)
    assert len(acted(game, "pantheon")) == 1  # Accepted, not applied: no point trying others.
    assert not asyncio.run(policy.resolve_pantheon(game, None))
    n = len(acted(game, "pantheon"))
    game.data["pantheon_status"] = [game.data["pantheon_status"][0]]
    run(policy, game)  # Still inside the backoff.
    assert len(acted(game, "pantheon")) == n


def test_belief_ranking_is_deterministic_and_unknown_beliefs_are_kept():
    options = beliefs("BELIEF_ZZZ_NEW", "BELIEF_ORAL_TRADITION", "BELIEF_AAA_NEW")
    ranked = [b.belief_type for b in rank_beliefs(options, W)]
    assert ranked == ["BELIEF_ORAL_TRADITION", "BELIEF_AAA_NEW", "BELIEF_ZZZ_NEW"]
    assert ranked == [b.belief_type for b in rank_beliefs(reversed(options), W)]
    science = SecondaryWeights(culture=0.0, faith=9.0)
    assert (
        rank_beliefs(beliefs("BELIEF_ORAL_TRADITION", "BELIEF_RELIGIOUS_IDOLS"), science)[
            0
        ].belief_type
        == "BELIEF_RELIGIOUS_IDOLS"
    )


def test_blocker_policies_cover_pantheon_and_great_people():
    policies = SecondaryPolicy().blocker_policies()
    assert set(policies) == {BlockerType.PANTHEON, BlockerType.GREAT_PERSON_CLAIM}
    BlockerResolver(policies)


# ----- religion -----


def religion_status(**changes):
    base = dict(
        has_religion=False,
        religion_type=None,
        religion_name=None,
        pantheon_index=1,
        faith_balance=200.0,
        available_religions=[("RELIGION_JUDAISM", "Judaism"), ("RELIGION_BUDDHISM", "Buddhism")],
        beliefs_by_class={
            "BELIEF_CLASS_FOLLOWER": [
                lq.ReligionBeliefOption("BELIEF_CLASS_FOLLOWER", "BELIEF_WORK_ETHIC", "", ""),
                lq.ReligionBeliefOption("BELIEF_CLASS_FOLLOWER", "BELIEF_CHORAL_MUSIC", "", ""),
            ],
            "BELIEF_CLASS_FOUNDER": [
                lq.ReligionBeliefOption("BELIEF_CLASS_FOUNDER", "BELIEF_TITHE", "", ""),
                lq.ReligionBeliefOption("BELIEF_CLASS_FOUNDER", "BELIEF_CHURCH_PROPERTY", "", ""),
            ],
        },
    )
    return lq.ReligionFoundingStatus(**{**base, **changes})


def note(name):
    return SimpleNamespace(type_name=name)


def test_religion_is_not_founded_without_a_prophet_signal():
    game = make_game(religion=religion_status())
    run(SecondaryPolicy(), game)
    assert not acted(game, "religion")


def test_religion_is_founded_when_the_game_asks():
    game = make_game(
        religion=religion_status(), notifications=[note("NOTIFICATION_CHOOSE_RELIGION")]
    )
    run(SecondaryPolicy(), game)
    assert acted(game, "religion") == [
        ("religion", "RELIGION_BUDDHISM", "BELIEF_WORK_ETHIC", "BELIEF_CHURCH_PROPERTY")
    ]


def test_founding_attempts_rotate_and_are_bounded():
    game = make_game(religion=religion_status(), notifications=[note("NOTIFICATION_CHOOSE_BELIEF")])
    policy = SecondaryPolicy()
    for _ in range(12):
        run(policy, game)
    tried = acted(game, "religion")
    assert len(tried) == 6
    assert tried[0] != tried[1]


def test_nothing_is_founded_once_we_have_a_religion_or_none_is_left():
    for status in (religion_status(has_religion=True), religion_status(available_religions=[])):
        game = make_game(religion=status, notifications=[note("NOTIFICATION_CHOOSE_RELIGION")])
        run(SecondaryPolicy(), game)
        assert not acted(game, "religion")


# ----- great people -----


def test_recruits_best_class_and_reads_again_between_recruits():
    game = make_game(
        people=[
            person(1, "Great Writer", "Homer"),
            person(2, "Great Scientist", "Hypatia"),
            person(3, "Great Scientist", "Euclid", claimant="Rome"),
            person(4, "Great Engineer", "Imhotep", can_recruit=False),
        ]
    )
    run(SecondaryPolicy(), game)
    assert [a[1] for a in acted(game, "recruit")] == [2, 1]
    assert game.calls.count("get_great_people") >= 3


def test_refused_recruit_does_not_strand_the_next():
    game = make_game(people=[person(1), person(2, "Great Writer", "Homer")])
    game.replies["recruit"] = ["Error: CANNOT_RECRUIT"]
    run(SecondaryPolicy(), game)
    assert [a[1] for a in acted(game, "recruit")] == [1, 2]


def test_a_zero_weight_class_is_not_recruited():
    people = [person(1, "Great General", "Sun Tzu")]
    assert rank_recruits(people, SecondaryWeights(gp_general=0.0)) == []
    assert len(rank_recruits(people, W)) == 1
    assert rank_recruits(people, W, frozenset({1})) == []


def test_blocker_recruiting_returns_true_even_if_nothing_recruits():
    game = make_game(people=[person(1)])
    game.replies["recruit"] = ["Error: no"]
    assert asyncio.run(SecondaryPolicy().resolve_great_people(game, None))


def gp_unit(kind="UNIT_GREAT_SCIENTIST", uid=40, **changes):
    return unit_info(
        unit_id=uid,
        unit_index=uid,
        unit_type=kind,
        name="Hypatia",
        combat_strength=0,
        x=10,
        y=10,
        **changes,
    )


def advisor(*cities):
    return lq.GPAdvisorResult(
        "Hypatia", "GREAT_PERSON_CLASS_SCIENTIST", "DISTRICT_CAMPUS", 10, 10, 1, list(cities)
    )


def test_great_person_walks_to_the_district_then_activates():
    game = make_game(units=[gp_unit()], advisor=advisor(gp_city(distance=2)))
    policy = SecondaryPolicy()
    run(policy, game)
    assert acted(game, "move") == [("move", 40, 12, 10)]
    assert not acted(game, "activate")
    game.data["advisor"] = advisor(gp_city(distance=0, can=True))
    run(policy, game)
    assert acted(game, "activate") == [("activate", 40)]
    assert 40 not in policy._great


def test_a_blocked_move_result_is_a_failure():
    game = make_game(
        units=[gp_unit()], advisor=advisor(gp_city(distance=2), gp_city(13, 10, 3, city_id=2))
    )
    game.replies["move"] = ["MOVING_TO|12,10|BLOCKED (enemy)"]
    run(SecondaryPolicy(), game)
    assert [a[2] for a in acted(game, "move")] == [12, 13]


def test_a_great_person_that_never_gets_closer_is_set_aside_then_retried():
    game = make_game(units=[gp_unit()], advisor=advisor(gp_city(distance=3)))
    policy = SecondaryPolicy()
    for turn in range(10, 10 + GP_STUCK_TURNS + 2):
        game.data["overview"] = overview(turn=turn)
        run(policy, game)
    moves = len(acted(game, "move"))
    assert moves == GP_STUCK_TURNS + 1  # The first move has nothing to compare with.
    game.data["overview"] = overview(turn=10 + GP_STUCK_TURNS + 3)
    run(policy, game)
    assert len(acted(game, "move")) == moves  # Set aside.
    game.data["overview"] = overview(turn=100)
    run(policy, game)
    assert len(acted(game, "move")) == moves + 1


def test_failed_activation_is_counted_and_not_hidden():
    game = make_game(units=[gp_unit()], advisor=advisor(gp_city(distance=0, can=True)))
    game.replies["activate"] = ["Error: CANNOT_ACTIVATE|x"] * 10
    policy = SecondaryPolicy()
    run(policy, game)
    assert policy._great[40].stuck == 1


def test_no_district_yet_waits_without_acting():
    game = make_game(units=[gp_unit()], advisor=advisor())
    policy = SecondaryPolicy()
    run(policy, game)
    run(policy, game)
    assert game.calls.count("get_gp_advisor") == 1
    assert not game.actions


def test_state_is_dropped_when_the_unit_dies():
    game = make_game(units=[gp_unit()], advisor=advisor())
    policy = SecondaryPolicy()
    run(policy, game)
    assert 40 in policy._great
    game.data["units"] = []
    run(policy, game)
    assert policy._great == {}


def test_prophet_waits_if_no_religion_can_be_founded_and_founding_follows_activation():
    unit = gp_unit("UNIT_GREAT_PROPHET")
    taken = make_game(
        units=[unit],
        advisor=advisor(gp_city(distance=0)),
        religion=religion_status(has_religion=True),
    )
    run(SecondaryPolicy(), taken)
    assert not taken.actions
    game = make_game(
        units=[unit], advisor=advisor(gp_city(distance=0, can=True)), religion=religion_status()
    )
    policy = SecondaryPolicy()
    run(policy, game)
    assert acted(game, "activate")
    game.data["overview"] = overview(turn=51)
    run(policy, game)
    assert acted(game, "religion")  # Founded the turn after, with no notification needed.


def test_non_great_people_and_units_without_moves_are_ignored():
    game = make_game(
        units=[unit_info(), gp_unit(uid=41, moves_remaining=0.0)], advisor=advisor(gp_city())
    )
    run(SecondaryPolicy(), game)
    assert "get_gp_advisor" not in game.calls


# ----- the whole handler -----


def test_one_failing_part_does_not_strand_the_others():
    game = pantheon_game()
    game.data["people"] = [person(1)]

    async def boom():
        raise RuntimeError("tuner down")

    game.get_trade_routes = boom
    with pytest.raises(RuntimeError):
        run(SecondaryPolicy(), game)
    assert acted(game, "pantheon") and acted(game, "recruit")


def test_run_is_deterministic():
    def play():
        game = trade_game(
            [trader(5), trader(6)],
            dests={5: [dest("A", x=20), dest("B", x=21)], 6: [dest("A", x=20), dest("B", x=21)]},
        )
        run(SecondaryPolicy(), game)
        return game.actions

    assert play() == play()


def test_progress_is_measured_against_the_district_actually_moved_to():
    near, far = gp_city(12, 10, 2, city_id=1), gp_city(13, 10, 5, city_id=2)
    game = make_game(units=[gp_unit()], advisor=advisor(near, far))
    game.replies["move"] = ["MOVING_TO|12,10|BLOCKED (x)"]  # Falls back to the far district.
    policy = SecondaryPolicy()
    run(policy, game)
    state = policy._great[40]
    assert (state.last_target, state.last_distance) == ((13, 10), 5)
    game.data["advisor"] = advisor(gp_city(12, 10, 2, city_id=1), gp_city(13, 10, 4, city_id=2))
    game.replies["move"] = ["MOVING_TO|12,10|BLOCKED (x)"]
    run(policy, game)
    assert policy._great[40].stuck == 0  # Closer to the far district, so it is progress.


def test_set_aside_clears_the_comparison():
    game = make_game(units=[gp_unit()], advisor=advisor(gp_city(distance=3)))
    policy = SecondaryPolicy()
    for turn in range(10, 10 + GP_STUCK_TURNS + 2):
        game.data["overview"] = overview(turn=turn)
        run(policy, game)
    state = policy._great[40]
    assert state.last_distance is None and state.last_target is None and state.skip_until > 0


def test_prophets_are_not_spent_once_founding_is_given_up():
    unit = gp_unit("UNIT_GREAT_PROPHET")
    game = make_game(
        units=[unit],
        advisor=advisor(gp_city(distance=0, can=True)),
        religion=religion_status(),
        notifications=[note("NOTIFICATION_CHOOSE_RELIGION")],
    )
    policy = SecondaryPolicy()
    for _ in range(8):
        run(policy, game)
    assert len(acted(game, "religion")) == 6
    assert not acted(game, "activate") or len(acted(game, "activate")) <= 6
    policy._great.clear()
    before = len(acted(game, "activate"))
    run(policy, game)
    assert len(acted(game, "activate")) == before
