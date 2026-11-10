"""Diplomacy policy tests. Response lines are in civ6-mcp's own formats, parsed by its parsers.

The text of the leaders' words and of the resolutions is invented, because no game has recorded it.
"""

import asyncio
import re
from dataclasses import replace

import pytest
from civ_mcp import lua as lq
from civ_mcp.lua import diplomacy as lqd

from civ6_ai.blockers import Blocker, BlockerResolver, BlockerType
from civ6_ai.policies import diplomacy as dp
from civ6_ai.policies.diplomacy import (
    DiplomacyPolicy,
    DiplomacyWeights,
    Vote,
    VoteContext,
    answer_talk,
    blocker_policies,
    build_voter_lua,
    choose_vote,
    classify_talk,
    judge_deal,
)
from fixtures import civ_state as fx

SENTINEL = lq.SENTINEL
ME = fx.ME
ROME = 1
EGYPT = 2
DV_HASH = dp.DIPLOMATIC_VICTORY_HASH
GOLD_100 = "THEM|GOLD|Gold (lump sum)|100|0"
GIFT_100 = "US|GOLD|Gold (lump sum)|100|0"
W = DiplomacyWeights()


def run(coro):
    return asyncio.run(coro)


def deal_from(*lines, player=ROME):
    """A pending deal parsed from `THEM or US|type|name|amount|duration` item lines."""
    header = f"DEAL|{player}|Rome|Trajan"
    items = [f"ITEM|{player}|{line}" for line in lines]
    return lqd.parse_pending_deals_response([header, *items, SENTINEL])[0]


def session_from(player=ROME, said="", reason="", buttons="", at_war=False, deal_lines=()):
    fields = f"{player}|Rome|Trajan|{said}|{reason}|{buttons}|{'1' if at_war else '0'}"
    lines = [f"SESSION|7|{fields}"]
    lines += [f"DEAL_ITEM|{player}|{line}" for line in deal_lines]
    return lqd.parse_diplomacy_sessions([*lines, SENTINEL])[0]


def offer(*lines, player=ROME):
    """A session and its pending deal for the same offer, as the game shows them together."""
    return session_from(player, deal_lines=lines), deal_from(*lines, player=player)


def civ(player=ROME, state="NEUTRAL", war=False, met=True):
    return fx.civ_info(player_id=player, diplomatic_state=state, is_at_war=war, has_met=met)


class Game:
    """Sessions and deals that close when answered, and a record of every call."""

    def __init__(self, sessions=(), deals=(), civs=None, turn=10):
        self.sessions = list(sessions)
        self.deals = list(deals)
        self.civs = [civ()] if civs is None else civs
        self.turn = turn
        self.calls = []
        self.lua = []
        self.respond_text = {}  # Player -> results to return in order, instead of closing.
        self.fail = {}  # Method name -> exception to raise.
        self.victory = fx.victory()
        self.congress = None
        self.hidden = {}  # Player -> items the parsed deal does not show.
        self.raw_fail = False
        self.lua_result = f"OK:WC_VOTER_REGISTERED\n{SENTINEL}"

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail[name]

    async def get_game_overview(self):
        return fx.overview(turn=self.turn)

    async def get_diplomacy(self):
        self.calls.append(("get_diplomacy",))
        return self.civs

    async def get_victory_progress(self):
        self.calls.append(("get_victory_progress",))
        return self.victory

    async def get_diplomacy_sessions(self):
        self.calls.append(("get_diplomacy_sessions",))
        self._maybe_fail("get_diplomacy_sessions")
        return list(self.sessions)

    async def get_pending_deals(self):
        self.calls.append(("get_pending_deals",))
        self._maybe_fail("get_pending_deals")
        return list(self.deals)

    def _close(self, player):
        self.sessions = [s for s in self.sessions if s.other_player_id != player]
        self.deals = [d for d in self.deals if d.other_player_id != player]

    async def diplomacy_respond(self, player, response):
        self.calls.append(("respond", player, response))
        self._maybe_fail("diplomacy_respond")
        if response == "EXIT":
            self._close(player)
            return "OK:SESSION_CLOSED"
        queued = self.respond_text.get(player)
        if queued:
            result = queued.pop(0)
            if "SESSION_CLOSED" in result:
                self._close(player)
            return result
        self._close(player)
        return f"OK:RESPONDED|{response}|SESSION_CLOSED"

    async def respond_to_deal(self, player, accept):
        self.calls.append(("deal", player, accept))
        self._maybe_fail("respond_to_deal")
        self._close(player)
        return f"OK:DEAL_{'ACCEPTED' if accept else 'REJECTED'}|Rome"

    async def get_world_congress(self):
        return self.congress

    async def vote_world_congress(self, resolution_hash, option, target_index, num_votes):
        self.calls.append(("vote", resolution_hash, option, target_index, num_votes))
        self._maybe_fail("vote_world_congress")
        return "OK:VOTED"

    async def execute_lua(self, code, context="gamecore"):
        if "RAWDEAL" in code:
            return self._raw_deal(code)
        self.lua.append((code, context))
        return self.lua_result

    def _raw_deal(self, code):
        self.calls.append(("raw_deal",))
        if self.raw_fail:
            raise ValueError("lua failed")
        player = int(re.search(r"local i = (\d+)", code).group(1))
        deal = next(d for d in self.deals if d.other_player_id == player)
        count = len(deal.items_from_them) + len(deal.items_from_us) + self.hidden.get(player, 0)
        lines = [f"RAWDEAL|{player}|{count}"]
        lines += [f"RAWITEM|{player}|{player}|1|0|-1|0|0"] * count
        return "\n".join([*lines, SENTINEL])

    def answers(self):
        return [c for c in self.calls if c[0] in ("respond", "deal")]


def wc_status(resolutions, in_session=True, favor=40):
    return lq.WorldCongressStatus(
        is_in_session=in_session,
        turns_until_next=0,
        favor=favor,
        max_votes=5,
        favor_costs=[0, 10, 30, 60, 100, 150],
        resolutions=resolutions,
        proposals=[],
    )


def parse_resolution(line):
    # Built by civ6-mcp's parser from a WC_RES line, so the field order is its own.
    status = lq.parse_world_congress_response(["WC_STATUS|true|0|40|5|0,10,30", line, SENTINEL])
    return status.resolutions[0]


def dv_resolution(
    targets=("0:America", "1:Rome", "2:Egypt"),
    effect_a="+2 Diplomatic Victory Points for the chosen civ",
    effect_b="The chosen civ loses 1 Diplomatic Victory Point",
):
    fields = [
        "WC_RES",
        str(DV_HASH),
        dp.DIPLOMATIC_VICTORY_TYPE,
        "Diplomatic Victory",
        "PLAYER",
        effect_a,
        effect_b,
        "0",
        "-1",
        "",
        "~".join(targets),
    ]
    return parse_resolution("|".join(fields))


def other_resolution(kind="YIELD", targets=("0:Gold", "1:Faith")):
    line = f"WC_RES|555|WC_RES_MERCENARY_COMPANIES|Mercenaries|{kind}|Cheap|Dear|0|-1||"
    return parse_resolution(line + "~".join(targets))


def victory_with(**points):
    players = [fx._progress(pid, name, diplomatic_vp=vp) for pid, name, vp in points["rows"]]
    return lq.VictoryProgress(players=players, capitals_held={}, religion_majority={})


# --- Weights ------------------------------------------------------------------------------------


def test_weights_reject_bad_numbers():
    for bad in ({"gold_value": -1.0}, {"strip_votes": float("nan")}, {"urgent_vp": True}):
        with pytest.raises(ValueError):
            DiplomacyWeights(**bad)
    with pytest.raises(ValueError):
        DiplomacyWeights(accept_unreadable_deals=1)
    with pytest.raises(ValueError):
        DiplomacyWeights(max_session_rounds=0)
    with pytest.raises(ValueError):
        DiplomacyWeights(resolution_rules=(("bad name; os.exit()", 1, 1),))
    with pytest.raises(ValueError):
        DiplomacyWeights(resolution_rules=(("MERCENARY_COMPANIES", 3, 1),))


# --- Deals --------------------------------------------------------------------------------------


def test_free_gold_is_accepted_and_gold_given_for_nothing_is_not():
    assert judge_deal(deal_from(GOLD_100), W).accept
    assert not judge_deal(deal_from(GIFT_100), W).accept


def test_a_swap_of_gold_for_a_luxury_is_accepted_only_if_fair():
    fair = deal_from("US|GOLD|Gold (lump sum)|30|0", "THEM|RESOURCE|Tobacco|1|0")
    poor = deal_from("US|GOLD|Gold (lump sum)|300|0", "THEM|RESOURCE|Tobacco|1|0")
    assert judge_deal(fair, W).accept
    assert not judge_deal(poor, W).accept


def test_per_turn_gold_is_priced_over_its_duration():
    dear = deal_from("US|GOLD|Gold per turn|10|30", "THEM|RESOURCE|Tobacco|1|0")
    assert not judge_deal(dear, W).accept


def test_a_strategic_resource_is_never_given_for_nothing():
    assert not judge_deal(deal_from("US|RESOURCE|Iron|2|0"), W).accept
    paid = deal_from("US|RESOURCE|Iron|2|0", "THEM|GOLD|Gold (lump sum)|200|0")
    assert judge_deal(paid, W).accept
    cheap = deal_from("US|RESOURCE|Iron|2|0", "THEM|GOLD|Gold (lump sum)|20|0")
    assert not judge_deal(cheap, W).accept


def test_a_strategic_resource_is_recognised_by_its_type_name_too():
    assert dp._is_strategic("RESOURCE_HORSES", W)
    assert not dp._is_strategic("Tobacco", W)


@pytest.mark.parametrize(
    "line",
    [
        "THEM|AGREEMENT|Research Alliance|0|10",
        "US|AGREEMENT|Military Alliance|0|10",
        "THEM|AGREEMENT|Joint War|0|0",
        "US|CITY|City|0|0",
    ],
)
def test_alliances_joint_wars_and_our_cities_are_never_accepted(line):
    sweetener = "THEM|GOLD|Gold (lump sum)|5000|0"
    assert not judge_deal(deal_from(line, sweetener), W).accept


def test_a_city_they_give_is_accepted():
    assert judge_deal(deal_from("THEM|CITY|City|0|0"), W).accept


def test_open_borders_both_ways_is_accepted_and_one_way_from_us_is_not():
    both = deal_from("THEM|AGREEMENT|Open Borders|0|0", "US|AGREEMENT|Open Borders|0|0")
    ours = deal_from("US|AGREEMENT|Open Borders|0|0")
    assert judge_deal(both, W).accept
    assert not judge_deal(ours, W).accept


def test_a_deal_of_unreadable_items_follows_the_flag_which_is_off_by_default():
    # civ6-mcp leaves out items it cannot name, such as delegations, so none are listed.
    empty = deal_from()
    assert not judge_deal(empty, W).accept
    assert judge_deal(empty, replace(W, accept_unreadable_deals=True)).accept


def test_the_signature_is_the_same_for_the_same_offer_in_any_order():
    one = deal_from(GOLD_100, "US|RESOURCE|Iron|1|0")
    two = deal_from("US|RESOURCE|Iron|1|0", GOLD_100)
    other = deal_from("THEM|GOLD|Gold (lump sum)|101|0", "US|RESOURCE|Iron|1|0")
    assert dp.deal_signature(one) == dp.deal_signature(two) != dp.deal_signature(other)


# --- Conversations ------------------------------------------------------------------------------


def test_classifying_what_a_leader_says():
    assert classify_talk("Let us declare friendship!") == "friendship"
    assert classify_talk("We should form an alliance") == "alliance"
    assert classify_talk("Stop settling near my borders") == "demand"
    assert classify_talk("I denounce you") == "war"
    assert classify_talk("Greetings, stranger") == "greeting"
    assert classify_talk("") == "greeting"


def test_friendship_is_accepted_unless_at_war_denounced_or_unknown():
    ok = dp.Counterpart(ROME, False, "NEUTRAL")
    assert answer_talk("friendship", ok, W) == "POSITIVE"
    assert answer_talk("friendship", dp.Counterpart(ROME, True, "WAR"), W) == "NEGATIVE"
    assert answer_talk("friendship", dp.Counterpart(ROME, False, "DENOUNCED"), W) == "NEGATIVE"
    assert answer_talk("friendship", dp.Counterpart(ROME, False, "NEUTRAL", True), W) == "NEGATIVE"
    assert answer_talk("friendship", None, W) == "NEGATIVE"
    assert answer_talk("alliance", ok, W) == "NEGATIVE"
    assert answer_talk("demand", ok, W) == "NEGATIVE"
    assert answer_talk("greeting", None, W) == "POSITIVE"


# --- Proposals through the policy ---------------------------------------------------------------


def test_a_good_deal_is_accepted_and_a_bad_one_refused_in_the_same_pass():
    good_session, good = offer(GOLD_100, player=ROME)
    bad_session, bad = offer(GIFT_100, player=EGYPT)
    game = Game([good_session, bad_session], [good, bad], civs=[civ(ROME), civ(EGYPT)])
    run(DiplomacyPolicy().run(game))
    assert game.answers() == [("deal", ROME, True), ("deal", EGYPT, False)]
    assert game.sessions == [] and game.deals == []


def test_a_war_declaration_is_exited_not_answered():
    game = Game(sessions=[session_from(ROME, at_war=True)])
    run(DiplomacyPolicy().run(game))
    assert game.answers() == [("respond", ROME, "EXIT")]


def test_a_session_showing_a_deal_the_query_missed_is_refused():
    game = Game(sessions=[session_from(ROME, deal_lines=[GOLD_100])])
    run(DiplomacyPolicy().run(game))
    assert game.answers() == [("deal", ROME, False)]


def test_a_greeting_gets_a_friendly_answer_until_it_closes():
    game = Game(sessions=[session_from(ROME, said="Greetings")])
    game.respond_text[ROME] = [
        'OK:RESPONDED|POSITIVE|SESSION_CONTINUES\nLeader says: "Welcome"',
        "OK:RESPONDED|POSITIVE|SESSION_CLOSED",
    ]
    run(DiplomacyPolicy().run(game))
    assert game.answers() == [("respond", ROME, "POSITIVE"), ("respond", ROME, "POSITIVE")]


def test_friendship_is_accepted_for_a_neutral_civ_and_refused_for_a_denounced_one():
    for state, expected in (("NEUTRAL", "POSITIVE"), ("DENOUNCED", "NEGATIVE")):
        game = Game(
            sessions=[session_from(ROME, said="Declare friendship?")], civs=[civ(state=state)]
        )
        run(DiplomacyPolicy().run(game))
        assert game.answers()[0] == ("respond", ROME, expected), state


def test_a_denounce_is_remembered_after_the_state_changes():
    policy = DiplomacyPolicy()
    run(policy.run(Game(civs=[civ(state="DENOUNCED")], turn=10)))
    ask = [session_from(ROME, said="Declare friendship?")]
    soon = Game(sessions=ask, turn=15)
    run(policy.run(soon))
    assert soon.answers()[0] == ("respond", ROME, "NEGATIVE")
    late = Game(sessions=ask, turn=30)
    run(policy.run(late))
    assert late.answers()[0] == ("respond", ROME, "POSITIVE")


def test_a_leader_who_moves_on_to_a_demand_is_refused_then_the_session_is_closed():
    game = Game(sessions=[session_from(ROME, said="Hello")])
    game.respond_text[ROME] = [
        'OK:RESPONDED|POSITIVE|SESSION_CONTINUES\nLeader says: "Stop settling"',
        "OK:RESPONDED|NEGATIVE|SESSION_CONTINUES",
    ]
    run(DiplomacyPolicy().run(game))
    assert [a[2] for a in game.answers()] == ["POSITIVE", "NEGATIVE", "EXIT"]
    assert game.sessions == []


def test_a_conversation_that_never_ends_is_exited_after_the_round_bound():
    game = Game(sessions=[session_from(ROME, said="Hello")])
    game.respond_text[ROME] = ["OK:RESPONDED|POSITIVE|SESSION_CONTINUES"] * 10
    run(DiplomacyPolicy().run(game))
    answers = [a[2] for a in game.answers()]
    assert answers == ["POSITIVE"] * W.max_session_rounds + ["EXIT"]
    assert game.sessions == []


def test_an_error_from_the_game_closes_the_session_instead_of_leaving_it_open():
    game = Game(sessions=[session_from(ROME, said="Hello")])
    game.respond_text[ROME] = ["Error: no session"]
    run(DiplomacyPolicy().run(game))
    assert game.answers()[-1] == ("respond", ROME, "EXIT")


def test_a_failure_on_one_proposal_closes_it_and_the_next_is_still_answered():
    class Flaky(Game):
        async def respond_to_deal(self, player, accept):
            if player == ROME:
                raise ValueError("boom")
            return await super().respond_to_deal(player, accept)

    first, first_deal = offer(GOLD_100, player=ROME)
    second, second_deal = offer(GOLD_100, player=EGYPT)
    game = Flaky([first, second], [first_deal, second_deal], civs=[civ(ROME), civ(EGYPT)])
    run(DiplomacyPolicy().run(game))
    assert ("respond", ROME, "EXIT") in game.calls
    assert ("deal", EGYPT, True) in game.calls
    assert game.sessions == []


def test_a_session_that_survives_its_answer_is_exited_by_the_leftover_check():
    class Lingers(Game):
        async def respond_to_deal(self, player, accept):
            self.calls.append(("deal", player, accept))
            return "OK:DEAL_REJECTED|Rome"  # Leaves the session open.

    session, deal = offer(GIFT_100)
    game = Lingers([session], [deal])
    assert run(DiplomacyPolicy().respond(game)) is True
    assert game.calls.count(("respond", ROME, "EXIT")) == 1
    assert game.sessions == []


def test_run_raises_and_stops_trying_if_a_session_cannot_be_closed():
    class Unclosable(Game):
        async def diplomacy_respond(self, player, response):
            self.calls.append(("respond", player, response))
            if response == "EXIT":
                return "Error: NO_SESSION"
            return "OK:RESPONDED|POSITIVE|SESSION_CONTINUES"

    game = Unclosable(sessions=[session_from(ROME, said="Hello")])
    with pytest.raises(RuntimeError):
        run(DiplomacyPolicy().run(game))
    exits = [c for c in game.calls if c == ("respond", ROME, "EXIT")]
    assert len(exits) == 2  # One closing the conversation, one in the leftover check. No more.


def test_pending_deals_that_cannot_be_read_do_not_stop_sessions_being_answered():
    game = Game(sessions=[session_from(ROME, said="Hello")])
    game.fail["get_pending_deals"] = ValueError("bad")
    run(DiplomacyPolicy().run(game))
    assert game.answers() == [("respond", ROME, "POSITIVE")]


def test_sessions_that_cannot_be_read_raise():
    game = Game()
    game.fail["get_diplomacy_sessions"] = ValueError("bad")
    with pytest.raises(ValueError):
        run(DiplomacyPolicy().respond(game))


def test_a_lost_connection_is_not_swallowed():
    game = Game(sessions=[session_from(ROME, said="Hello")])
    game.fail["diplomacy_respond"] = ConnectionError("gone")
    with pytest.raises(ConnectionError):
        run(DiplomacyPolicy().run(game))


# --- Bounds and memory --------------------------------------------------------------------------


def test_a_refused_offer_is_remembered_and_stays_refused_even_if_it_would_now_pass(monkeypatch):
    policy = DiplomacyPolicy()
    session, deal = offer(GIFT_100)
    run(policy.run(Game([session], [deal], turn=10)))
    assert len(policy._refused) == 1
    monkeypatch.setattr(dp, "judge_deal", lambda d, w: dp.Judgement(True, "pretend it is good"))
    game = Game([session], [deal], turn=11)
    run(policy.run(game))
    assert game.answers() == [("deal", ROME, False)]


def test_refusals_are_forgotten_after_the_memory_window():
    policy = DiplomacyPolicy(DiplomacyWeights(refusal_memory_turns=5))
    session, deal = offer(GIFT_100)
    run(policy.run(Game([session], [deal], turn=10)))
    assert policy._refused
    run(policy.run(Game(turn=16)))
    assert not policy._refused


def test_the_memory_of_refusals_is_bounded():
    policy = DiplomacyPolicy(DiplomacyWeights(max_remembered_refusals=3))
    policy._begin_turn(5)
    for n in range(10):
        policy._refused[("deal", n)] = 5
    policy._begin_turn(6)
    assert len(policy._refused) == 3


def test_the_same_proposal_is_answered_at_most_twice_a_turn_then_refused_unpriced():
    policy = DiplomacyPolicy()
    policy._begin_turn(10)
    session, deal = offer(GOLD_100)
    outcomes = []
    for _ in range(4):
        game = Game([session], [deal])
        run(policy.respond(game))
        outcomes.append(game.answers()[0][2])
    assert outcomes == [True, True, False, False]


def test_a_new_turn_starts_the_bounds_again():
    policy = DiplomacyPolicy()
    session, deal = offer(GOLD_100)
    for turn in (10, 10, 10, 11):
        game = Game([session], [deal], turn=turn)
        run(policy.run(game))
    assert game.answers()[0] == ("deal", ROME, True)


def test_the_per_turn_response_cap_closes_further_proposals_unanswered():
    policy = DiplomacyPolicy(
        DiplomacyWeights(max_responses_per_turn=2, max_attempts_per_proposal=9)
    )
    policy._begin_turn(10)
    results = []
    for _ in range(4):
        game = Game(sessions=[session_from(ROME, said="Hello")])
        run(policy.respond(game))
        results.append(game.answers())
    assert results[0] == [("respond", ROME, "POSITIVE")]
    assert results[3] == [("respond", ROME, "EXIT")]


# --- Fog of war ---------------------------------------------------------------------------------


def test_unmet_civs_are_never_counted():
    policy = DiplomacyPolicy()
    run(policy.run(Game(civs=[civ(ROME), civ(3, met=False)])))
    assert set(policy._counterparts) == {ROME}


# --- World Congress -----------------------------------------------------------------------------


def ctx(strip=(), my_id=ME, favor=40):
    return VoteContext(my_id, favor, (0, 10, 30, 60, 100, 150), strip)


def test_a_rival_with_points_is_voted_down_with_option_b():
    vote = choose_vote(dv_resolution(), ctx(strip=((ROME, 6),)), W)
    assert vote == Vote(2, 1, W.strip_votes)


def test_a_rival_near_twenty_gets_the_urgent_votes_within_the_favor():
    vote = choose_vote(dv_resolution(), ctx(strip=((EGYPT, 15),), favor=40), W)
    assert vote.option == 2 and vote.target_index == 2
    assert vote.num_votes == 3  # Four votes cost 60 favor and only 40 is held.


def test_the_favor_reserve_is_kept():
    weights = replace(W, favor_reserve=35)
    vote = choose_vote(dv_resolution(), ctx(strip=((ROME, 6),), favor=40), weights)
    assert vote.num_votes == 1


def test_with_no_rival_to_strip_we_vote_points_to_ourselves_only():
    assert choose_vote(dv_resolution(), ctx(), W) == Vote(1, 0, 1)


def test_option_a_is_never_cast_for_a_rival():
    no_us = dv_resolution(targets=("1:Rome", "2:Egypt"))
    assert choose_vote(no_us, ctx(), W) is None
    below = ctx(strip=((ROME, W.strip_from_vp - 1),))
    assert choose_vote(dv_resolution(targets=("1:Rome", "2:Egypt")), below, W) is None


def test_if_the_text_is_not_understood_nothing_is_voted():
    odd = dv_resolution(effect_a="Something", effect_b="Something else")
    assert choose_vote(odd, ctx(strip=((ROME, 9),)), W) is None


def test_if_option_b_adds_points_the_rival_is_not_named():
    flipped = dv_resolution(
        effect_a="The chosen civ loses 1 Diplomatic Victory Point",
        effect_b="+2 Diplomatic Victory Points for the chosen civ",
    )
    vote = choose_vote(flipped, ctx(strip=((ROME, 9),)), W)
    assert vote is None or vote.target_index == 0  # At most a vote on ourselves, never Rome.


def test_the_resolution_is_found_by_its_hash_or_its_type():
    by_hash = replace(dv_resolution(), resolution_type="WC_RES_SOMETHING")
    by_type = replace(dv_resolution(), resolution_hash=1)
    assert dp.is_diplomatic_victory(by_hash) and dp.is_diplomatic_victory(by_type)
    assert not dp.is_diplomatic_victory(other_resolution())


def test_other_resolutions_are_left_unvoted_unless_a_rule_names_them():
    resolution = other_resolution()
    assert choose_vote(resolution, ctx(), W) is None
    weights = replace(W, resolution_rules=(("MERCENARY_COMPANIES", 2, 3),))
    assert choose_vote(resolution, ctx(), weights) == Vote(2, 0, 3)


def test_a_rule_never_applies_to_a_resolution_that_targets_a_player():
    resolution = other_resolution(kind="PLAYER", targets=("1:Rome", "2:Egypt"))
    weights = replace(W, resolution_rules=(("MERCENARY_COMPANIES", 1, 1),))
    assert choose_vote(resolution, ctx(), weights) is None


def test_an_open_session_is_voted_then_submitted():
    game = Game()
    game.congress = wc_status([dv_resolution(), other_resolution()])
    game.victory = victory_with(rows=[(ME, "America", 0), (ROME, "Rome", 7)])
    policy = DiplomacyPolicy()
    assert run(policy.vote_in_open_session(game, Blocker(BlockerType.WORLD_CONGRESS_SESSION)))
    votes = [c for c in game.calls if c[0] == "vote"]
    assert votes == [("vote", DV_HASH, 2, 1, 2)]  # Only the Diplomatic Victory resolution.
    assert game.lua[-1][1] == "ingame" and "WORLD_CONGRESS_SUBMIT_TURN" in game.lua[-1][0]


def test_an_open_session_registers_the_voter_end_turn_requires_before_submitting():
    game = Game()
    game.congress = wc_status([dv_resolution()])
    assert run(
        DiplomacyPolicy().vote_in_open_session(game, Blocker(BlockerType.WORLD_CONGRESS_SESSION))
    )
    codes = [code for code, _ in game.lua]
    assert "__civmcp_wc_handler = handler" in codes[0]  # The voter's own code submits too.
    assert "WORLD_CONGRESS_SUBMIT_TURN" in codes[-1]
    assert "__civmcp_wc_handler" not in codes[-1]


def test_an_open_session_still_votes_and_submits_if_the_voter_will_not_register():
    game = Game()
    game.congress = wc_status([dv_resolution()])
    game.lua_result = "ERR:something"
    policy = DiplomacyPolicy()
    assert not run(policy.queue_voter(game, Blocker(BlockerType.WORLD_CONGRESS_VOTE)))
    assert any(c[0] == "vote" for c in game.calls)
    assert "WORLD_CONGRESS_SUBMIT_TURN" in game.lua[-1][0]


def test_an_open_session_still_votes_and_submits_if_registering_raises():
    class RegisterFails(Game):
        async def execute_lua(self, code, context="gamecore"):
            if "__civmcp_wc_handler = handler" in code:
                raise ValueError("lua failed")
            return await super().execute_lua(code, context)

    game = RegisterFails()
    game.congress = wc_status([dv_resolution()])
    policy = DiplomacyPolicy()
    assert not run(policy.queue_voter(game, Blocker(BlockerType.WORLD_CONGRESS_VOTE)))
    assert any(c[0] == "vote" for c in game.calls)
    assert "WORLD_CONGRESS_SUBMIT_TURN" in game.lua[-1][0]


def test_an_unmet_rival_is_never_voted_on():
    game = Game()
    game.congress = wc_status([dv_resolution()])
    game.victory = victory_with(rows=[(ME, "America", 0), (EGYPT, "Egypt", 18)])  # Egypt unmet.
    run(DiplomacyPolicy().vote_in_open_session(game, Blocker(BlockerType.WORLD_CONGRESS_SESSION)))
    assert [c for c in game.calls if c[0] == "vote"] == [("vote", DV_HASH, 1, 0, 1)]


def test_a_failed_vote_still_submits_so_the_session_closes():
    game = Game()
    game.congress = wc_status([dv_resolution()])
    game.fail["vote_world_congress"] = ValueError("boom")
    assert run(
        DiplomacyPolicy().vote_in_open_session(game, Blocker(BlockerType.WORLD_CONGRESS_SESSION))
    )
    assert "WORLD_CONGRESS_SUBMIT_TURN" in game.lua[-1][0]


def test_a_lost_connection_while_voting_is_raised_after_the_submit_is_tried():
    game = Game()
    game.congress = wc_status([dv_resolution()])
    game.fail["vote_world_congress"] = ConnectionError("gone")
    with pytest.raises(ConnectionError):
        run(
            DiplomacyPolicy().vote_in_open_session(
                game, Blocker(BlockerType.WORLD_CONGRESS_SESSION)
            )
        )
    assert "WORLD_CONGRESS_SUBMIT_TURN" in game.lua[-1][0]


def test_before_a_session_the_voter_is_registered_with_the_rivals_to_strip():
    game = Game()
    game.congress = wc_status([], in_session=False)
    game.victory = victory_with(
        rows=[(ME, "America", 9), (ROME, "Rome", 13), (3, "Egypt", 19)]  # Egypt is not met.
    )
    assert run(DiplomacyPolicy().queue_voter(game, Blocker(BlockerType.WORLD_CONGRESS_VOTE)))
    code, context = game.lua[0]
    assert context == "ingame"
    assert f"local STRIP = {{{{id={ROME}, v={W.urgent_votes}}}}}" in code
    assert "id=3" not in code and "id=0," not in code


def test_queueing_with_a_session_already_open_votes_in_it():
    game = Game()
    game.congress = wc_status([dv_resolution()])
    run(DiplomacyPolicy().queue_voter(game, Blocker(BlockerType.WORLD_CONGRESS_VOTE)))
    assert any(c[0] == "vote" for c in game.calls)


def test_if_the_voter_does_not_register_the_policy_reports_failure_so_the_default_runs():
    game = Game()
    game.congress = wc_status([], in_session=False)
    game.lua_result = "ERR:something"
    assert not run(DiplomacyPolicy().queue_voter(game, Blocker(BlockerType.WORLD_CONGRESS_VOTE)))


def test_the_voter_lua_registers_the_handler_civ6_mcp_looks_for():
    weights = replace(W, resolution_rules=(("MERCENARY_COMPANIES", 2, 3),))
    code = build_voter_lua(((ROME, 2),), weights)
    assert "__civmcp_wc_handler = handler" in code
    assert "Events.WorldCongressStage1.Add(handler)" in code
    assert "WORLD_CONGRESS_SUBMIT_TURN" in code
    assert '["WC_RES_MERCENARY_COMPANIES"] = {o=2, v=3}' in code
    assert "local STRIP = {{id=1, v=2}}" in code
    assert f"local DV_HASH = {DV_HASH}" in code
    assert code.rstrip().endswith(f'print("{SENTINEL}")')
    assert "@" not in code  # Every placeholder was filled.
    assert code.count("pcall(function()") >= 2


def test_the_voter_lua_with_no_rules_or_rivals_is_still_complete():
    code = build_voter_lua((), W)
    assert "local STRIP = {}" in code and "local RULES = {}" in code


# --- Blockers -----------------------------------------------------------------------------------


def test_the_blocker_policies_cover_the_four_types():
    policies = blocker_policies(DiplomacyPolicy())
    expected = {
        BlockerType.DIPLOMACY_SESSION,
        BlockerType.TRADE_DEAL,
        BlockerType.WORLD_CONGRESS_VOTE,
        BlockerType.WORLD_CONGRESS_SESSION,
    }
    assert set(policies) == expected
    assert set(blocker_policies()) == expected


def test_the_resolver_uses_the_policy_in_the_diplomacy_only_pass_without_other_reads():
    session, deal = offer(GOLD_100)
    game = Game([session], [deal])
    policy = DiplomacyPolicy()
    resolver = BlockerResolver(blocker_policies(policy))
    assert run(resolver.resolve(game, diplomacy_only=True)) == []
    assert game.answers() == [("deal", ROME, True)]  # Once: the second blocker found nothing.
    assert {c[0] for c in game.calls} <= {
        "get_diplomacy_sessions",
        "get_pending_deals",
        "deal",
        "respond",
        "raw_deal",  # The one extra read, of the deal itself.
    }


def test_the_resolver_default_still_runs_if_the_policy_cannot_read_sessions():
    class Broken(Game):
        async def get_diplomacy_sessions(self):
            self.calls.append(("get_diplomacy_sessions",))
            if self.fail.get("sessions"):
                raise ValueError("bad")
            return list(self.sessions)

    game = Broken(sessions=[session_from(ROME, said="Hello")])
    game.fail["sessions"] = True
    resolver = BlockerResolver(blocker_policies())
    run(resolver.resolve(game, diplomacy_only=True))  # No exception: failures are logged.
    assert game.answers() == []


def test_without_a_turn_handler_the_bounds_still_start_again_each_call():
    policy = DiplomacyPolicy(DiplomacyWeights(max_responses_per_turn=1))
    for _ in range(3):
        game = Game(sessions=[session_from(ROME, said="Hello")])
        run(policy.respond(game))
        assert game.answers() == [("respond", ROME, "POSITIVE")]


def test_the_vote_paths_cannot_call_each_other_forever():
    class Flipping(Game):
        async def get_world_congress(self):
            self.reads = getattr(self, "reads", 0) + 1
            return wc_status([], in_session=self.reads % 2 == 0)

    game = Flipping()
    run(DiplomacyPolicy().queue_voter(game, Blocker(BlockerType.WORLD_CONGRESS_VOTE)))
    run(DiplomacyPolicy().vote_in_open_session(game, Blocker(BlockerType.WORLD_CONGRESS_SESSION)))
    assert game.reads == 2


# --- Hidden items -------------------------------------------------------------------------------


def priced_offer():
    return offer(GOLD_100)


def test_a_priced_deal_with_one_hidden_item_is_refused_and_the_raw_items_are_logged(caplog):
    session, deal = priced_offer()
    game = Game([session], [deal])
    game.hidden[ROME] = 1
    with caplog.at_level("INFO"):
        run(DiplomacyPolicy().run(game))
    assert game.answers() == [("deal", ROME, False)]
    assert "Raw items of the deal from player 1" in caplog.text


def test_a_failed_raw_read_refuses_the_deal_and_does_not_remember_it():
    session, deal = priced_offer()
    game = Game([session], [deal])
    game.raw_fail = True
    policy = DiplomacyPolicy()
    run(policy.run(game))
    assert game.answers() == [("deal", ROME, False)]
    assert not policy._refused


def test_a_failed_raw_read_refuses_even_with_the_flag():
    session, deal = offer()
    game = Game([session], [deal])
    game.raw_fail = True
    run(DiplomacyPolicy(DiplomacyWeights(accept_unreadable_deals=True)).run(game))
    assert game.answers() == [("deal", ROME, False)]


def test_a_fully_readable_deal_is_unchanged():
    session, deal = priced_offer()
    game = Game([session], [deal])
    run(DiplomacyPolicy().run(game))
    assert game.answers() == [("deal", ROME, True)]
    assert ("raw_deal",) in game.calls


def test_a_deal_with_no_parsed_item_needs_the_flag_even_though_the_raw_items_exist():
    for flag, expected in ((False, False), (True, True)):
        session, deal = offer()
        game = Game([session], [deal])
        game.hidden[ROME] = 2
        run(DiplomacyPolicy(DiplomacyWeights(accept_unreadable_deals=flag)).run(game))
        assert game.answers() == [("deal", ROME, expected)], flag


def test_a_hidden_item_beside_parsed_ones_is_refused_even_with_the_flag():
    session, deal = priced_offer()
    game = Game([session], [deal])
    game.hidden[ROME] = 1
    run(DiplomacyPolicy(DiplomacyWeights(accept_unreadable_deals=True)).run(game))
    assert game.answers() == [("deal", ROME, False)]


def test_the_raw_read_parses_real_line_shapes_and_rejects_a_short_one():
    head = "RAWDEAL|1|2"
    one, two = "RAWITEM|1|1|2|3|4|5|6", "RAWITEM|1|0|2|3|4|5|6"
    ok = "\n".join([head, one, two, SENTINEL])
    assert len(dp.parse_raw_deal(ok, 1)) == 2
    assert dp.parse_raw_deal("\n".join([head, one]), 1) is None
    assert dp.parse_raw_deal(SENTINEL, 1) is None  # No session or no deal.
    assert dp.parse_raw_deal(ok, 2) is None


def test_the_raw_read_uses_only_calls_the_pending_deals_query_makes():
    code = dp.build_raw_deal_lua(3)
    query = lqd.build_pending_deals_query()
    assert "local i = 3" in code
    for call in (
        "FindOpenSessionID",
        "GetWorkingDeal(DealDirection.INCOMING",
        "GetFromPlayerID",
        "GetSubType",
        "GetValueType",
        "GetAmount",
        "GetDuration",
        "deal:Items()",
    ):
        assert call in query and call in code
    assert "DealAgreementTypes" not in code
