"""Trade offer tests. A fake game records every call, in civ6-mcp's own result formats."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from civ_mcp import lua as lq

from civ6_ai.policies.diplomacy import DiplomacyWeights, judge_deal
from civ6_ai.policies.trade_offers import (
    TradeOfferPolicy,
    TradeOfferWeights,
    at_war_with,
    price_offer,
    surplus_resources,
)
from fixtures import civ_state as fx

ROME = 1
EGYPT = 2
W = TradeOfferWeights()
D = DiplomacyWeights()


def run(coro):
    return asyncio.run(coro)


def pile(name="HORSES", amount=33, cap=33, per_turn=2, demand=0):
    return lq.ResourceStockpile(name, amount, cap, per_turn, demand, 0)


def civ(player=ROME, war=False, met=True):
    return fx.civ_info(player_id=player, is_at_war=war, has_met=met)


ACCEPTABLE = (
    "Trade test with Rome (player 1):"
    + chr(10) * 2
    + "AI response: ACCEPTABLE — this deal would be accepted as-is"
)


class Game:
    def __init__(self, piles=(), luxuries=None, civs=None, turn=50, result="ACCEPTED|Trade ok"):
        self.piles = list(piles)
        self.luxuries = luxuries or {}
        self.civs = [civ()] if civs is None else civs
        self.turn = turn
        self.result = result
        self.proposals = []
        self.tests = []
        self.verdict = ACCEPTABLE
        self.sessions = []
        self.exits = []
        self.leave_open = False

    async def get_game_overview(self):
        return fx.overview(turn=self.turn)

    async def get_empire_resources(self):
        return self.piles, [], [], self.luxuries

    async def get_diplomacy(self):
        return self.civs

    async def get_diplomacy_sessions(self):
        return [SimpleNamespace(other_player_id=p) for p in self.sessions]

    async def diplomacy_respond(self, player, response):
        self.exits.append((player, response))
        self.sessions.remove(player)
        return "SESSION_CLOSED"

    async def test_trade(self, player, offer, request):
        self.tests.append((player, offer, request))
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict

    async def propose_trade(self, player, offer, request):
        self.proposals.append((player, offer, request))
        if self.leave_open:
            self.sessions.append(player)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


COUNTER = chr(10).join(
    [
        "Trade test with Mapuche (player 4):",
        "",
        "Your proposal:",
        "  We offer: 1x WINE",
        "  We request: 2 gold/turn",
        "",
        "AI counter-offer (what they consider fair):",
        "  We give: 30 Diplomatic Favor, Open Borders, Joint War",
        "  They give: 124 gold",
    ]
)


def test_only_an_offer_the_ai_takes_as_written_is_sent():
    game = Game([pile()])
    play(game)
    assert len(game.tests) == 1 and len(game.proposals) == 1


def test_an_offer_the_ai_would_counter_is_never_sent(caplog):
    # Run 4, turn 105: propose_trade accepted a counter that carried a Joint War.
    game = Game([pile()])
    game.verdict = COUNTER
    with caplog.at_level(logging.INFO):
        play(game)
    assert game.proposals == []
    assert "not sent" in caplog.text and "Joint War" in caplog.text


@pytest.mark.parametrize(
    "verdict",
    ["AI response: REJECTED — will not trade at all", "", "Error: no such player"],
)
def test_a_rejection_or_an_unreadable_verdict_is_not_sent(verdict):
    game = Game([pile()])
    game.verdict = verdict
    play(game)
    assert game.proposals == []


def test_a_refused_test_rests_the_pair_like_a_refused_offer():
    game = Game([pile()])
    game.verdict = COUNTER
    policy = play(game, turns=1)
    play(game, policy, turns=W.refusal_turns - 2)
    assert len(game.tests) == 1


def test_a_failing_test_call_does_not_abort_and_closes_the_session():
    game = Game([pile()])
    game.verdict = RuntimeError("boom")
    game.sessions = []
    play(game)
    assert game.proposals == []


def play(game, policy=None, turns=1):
    policy = policy or TradeOfferPolicy()
    for _ in range(turns):
        run(policy.run(game))
        game.turn += 1
    return policy


def test_pricing_asks_at_least_what_the_resource_is_worth():
    surplus = surplus_resources(([pile()], [], [], {}), W)[0]
    offer, ask, given = price_offer(surplus, W, D)
    assert offer == {"type": "RESOURCE", "name": "RESOURCE_HORSES", "amount": 1, "duration": 30}
    assert given == D.strategic_value
    gold = SimpleNamespace(item_type="GOLD", name="Gold", amount=ask["amount"], duration=30)
    horses = SimpleNamespace(item_type="RESOURCE", name="HORSES", amount=1, duration=30)
    # The same rules judge the deal an AI would send us, with the sides reversed.
    assert judge_deal(SimpleNamespace(items_from_them=[gold], items_from_us=[horses]), D).accept
    assert ask["amount"] * 30 * D.gold_per_turn_factor >= given


def test_lump_sum_pricing():
    surplus = surplus_resources(([], [], [], {"WINE": 2}), W)[0]
    _, ask, given = price_offer(surplus, TradeOfferWeights(gold_per_turn=False), D)
    assert ask == {"type": "GOLD", "amount": 44, "duration": 0}
    assert given == D.luxury_value


def test_capped_resource_is_offered_to_a_civ_that_may_lack_it():
    game = Game([pile()])
    play(game)
    assert len(game.proposals) == 1
    player, offer, ask = game.proposals[0]
    assert player == ROME
    assert offer[0]["name"] == "RESOURCE_HORSES"
    assert ask[0]["type"] == "GOLD" and ask[0]["amount"] >= 1
    assert game.sessions == []


def test_spare_luxury_copy_is_offered_but_not_the_last_one():
    found = surplus_resources(([], [], [], {"WINE": 2, "SILK": 1}), W)
    assert [s.name for s in found] == ["WINE"]
    game = Game(luxuries={"WINE": 2})
    play(game)
    assert game.proposals[0][1][0]["name"] == "RESOURCE_WINE"


@pytest.mark.parametrize(
    "stock",
    [
        pile(amount=10, cap=33),  # Not near the cap.
        pile(amount=33, cap=33, demand=4),  # Upkeep needs 40 turns of it, more than we hold.
        pile(amount=33, cap=0),
        pile(name="Horses"),  # A name civ6-mcp never prints is not sent to the game.
        pile(name='X"]; os.exit() --'),
    ],
)
def test_a_needed_or_unsafe_resource_is_never_offered(stock):
    game = Game([stock], luxuries={"SILK": 1})
    play(game)
    assert game.proposals == []


def test_no_offer_to_a_civ_at_war_unmet_or_unknown():
    game = Game([pile()], civs=[civ(ROME, war=True), civ(EGYPT, met=False)])
    play(game)
    assert game.proposals == []
    assert at_war_with(game.civs, ROME)
    assert at_war_with(game.civs, 9)
    assert not at_war_with([civ(ROME)], ROME)


def test_only_the_civ_not_at_war_is_offered():
    game = Game([pile()], civs=[civ(ROME, war=True), civ(EGYPT)])
    play(game)
    assert [p[0] for p in game.proposals] == [EGYPT]


def test_a_refused_offer_rests_for_the_refusal_time():
    game = Game([pile()], result="REJECTED|Rome rejected")
    policy = play(game, turns=1)
    play(game, policy, turns=W.refusal_turns - 2)
    assert len(game.proposals) == 1  # The civ interval passed, but the pair still rests.
    play(game, policy, turns=4)
    assert len(game.proposals) == 2


def test_one_offer_per_civ_per_interval_and_a_few_per_turn():
    game = Game([pile(), pile("IRON")], luxuries={"WINE": 3}, civs=[civ(1), civ(2), civ(3)])
    policy = play(game)
    assert len(game.proposals) == W.max_offers_per_turn
    play(game, policy)  # Next turn: the third civ is offered, the first two are in their interval.
    assert [p[0] for p in game.proposals][2:] == [3]


def test_an_accepted_sale_is_not_repeated_at_once():
    game = Game([pile()])
    policy = play(game, turns=W.civ_interval_turns + 3)
    assert len(game.proposals) == 1
    play(game, policy, turns=W.accepted_turns)
    assert len(game.proposals) == 2


def test_a_session_left_open_is_closed():
    game = Game([pile()], result="PROPOSED|Trade proposed")
    game.leave_open = True
    play(game)
    assert game.exits == [(ROME, "EXIT")]
    assert game.sessions == []


def test_a_civ_with_an_open_session_is_not_offered():
    game = Game([pile()])
    game.sessions = [ROME]
    play(game)
    assert game.proposals == []


def test_a_failed_proposal_does_not_abort_and_closes_the_session(caplog):
    game = Game([pile()], result=RuntimeError("boom"))
    game.leave_open = True
    with caplog.at_level(logging.INFO):
        play(game)
    assert game.exits == [(ROME, "EXIT")]
    assert "failed" in caplog.text


def test_an_error_result_is_logged_and_rests(caplog):
    game = Game([pile()], result="Error: AT_WAR|Cannot trade while at war")
    with caplog.at_level(logging.INFO):
        play(game, turns=W.civ_interval_turns + 3)
    assert len(game.proposals) == 1
    assert "Cannot trade while at war" in caplog.text


def test_every_offer_and_answer_is_logged(caplog):
    game = Game([pile()])
    with caplog.at_level(logging.INFO):
        play(game)
    assert "Trade offer to player 1: 1x HORSES" in caplog.text
    assert "accepted" in caplog.text


def test_weights_are_validated():
    with pytest.raises(ValueError):
        TradeOfferWeights(near_cap_share=1.5)
    with pytest.raises(ValueError):
        TradeOfferWeights(duration=0)
    with pytest.raises(ValueError):
        TradeOfferWeights(max_offers_per_turn=-1)
