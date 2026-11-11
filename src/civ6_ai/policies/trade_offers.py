"""Offer surplus strategic and luxury resources to other civs for gold, with no model calls.

`TradeOfferPolicy` is a turn handler. It runs after `DiplomacyPolicy`, which has answered every
open proposal, and sends a few offers of its own through `GameState.propose_trade`.

What counts as a surplus:

- A strategic resource whose stockpile is at or near its cap, and that stays above what our unit
  upkeep needs (`reserve_turns` of demand) after the offer.
- A luxury of which we hold more than one copy. One copy gives the amenities, so we offer the
  copies beyond it and never the last one.

Who gets an offer: every civ we have met that is not at war with us (`at_war_with`, the one place
that asks), has no open session, was not offered the same resource recently and did not refuse it
recently. Which resources a civ lacks is not readable through civ6-mcp (`get_diplomacy` lists
none), so the proxy is the civ's answer: a civ that has the resource refuses, and that refusal is
remembered. Asking for a resource in return is not done for the same reason.

The price is the one `DiplomacyPolicy` uses for incoming deals (`item_value`): we ask gold worth
at least what we give, as gold per turn over the deal's length or as a lump sum.

Bounds: `max_offers_per_turn`, one offer per civ per `civ_interval_turns`, and a refused or
failed (civ, resource) pair is not tried again for `refusal_turns`. Every offer and answer is
logged. A session left open after an offer is closed at once, because it can hold the game.
"""

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from types import SimpleNamespace
from typing import Any, Protocol

from civ6_ai.blockers import action_succeeded
from civ6_ai.policies.diplomacy import EXIT, DiplomacyWeights, item_value

log = logging.getLogger(__name__)

_TYPE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class TradeOfferWeights:
    """What to offer and every bound. The numbers are not tuned against games."""

    near_cap_share: float = 0.9  # A stockpile at this share of its cap is at or near it.
    reserve_turns: int = 10  # Turns of unit upkeep demand that always stay with us.
    strategic_amount: int = 1  # Units offered per strategic deal.
    luxury_amount: int = 1
    duration: int = 30  # Turns a resource deal lasts.
    ask_margin: float = 1.1  # Gold asked is worth this many times what we give.
    gold_per_turn: bool = True  # Ask gold per turn, or else a lump sum.
    max_offers_per_turn: int = 2
    civ_interval_turns: int = 10  # One offer per civ per this many turns.
    refusal_turns: int = 20  # A refused (civ, resource) pair rests this long.
    accepted_turns: int = 30  # A resource sold to a civ is not offered to it again this long.
    max_remembered: int = 128

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "gold_per_turn":
                if not isinstance(value, bool):
                    raise ValueError("gold_per_turn must be True or False")
            elif isinstance(value, bool):
                raise ValueError(f"{item.name} must be a number")
            elif not math.isfinite(value) or value < 0:
                raise ValueError(f"{item.name} must be a finite number of at least 0")
        if not 0 < self.near_cap_share <= 1:
            raise ValueError("near_cap_share must be above 0 and at most 1")
        if min(self.strategic_amount, self.luxury_amount, self.duration) < 1:
            raise ValueError("amounts and duration must be at least 1")


class TradeGame(Protocol):
    """The part of civ6-mcp's `GameState` that this policy uses."""

    async def get_game_overview(self) -> Any: ...
    async def get_diplomacy(self) -> Any: ...
    async def get_empire_resources(self) -> Any: ...
    async def get_diplomacy_sessions(self) -> Any: ...
    async def diplomacy_respond(self, other_player_id: int, response: str) -> str: ...
    async def propose_trade(
        self, other_player_id: int, offer_items: list[dict], request_items: list[dict]
    ) -> str: ...
    async def test_trade(
        self, other_player_id: int, offer_items: list[dict], request_items: list[dict]
    ) -> str: ...


ACCEPTABLE_AS_IS = "ACCEPTABLE"  # civ6-mcp's `test_trade` text for a deal taken without changes.


def is_acceptable_as_is(verdict: str) -> bool:
    """True only if the AI's answer to `test_trade` says it would take the offer unchanged.

    A counter-offer, a rejection and anything unreadable all count as no.
    """
    return any(line.strip().startswith("AI response: ACCEPTABLE") for line in verdict.splitlines())


@dataclass(frozen=True)
class Surplus:
    name: str  # civ6-mcp's short name, such as HORSES.
    kind: str  # "strategic" or "luxury".
    amount: int  # Units offered in one deal.


def at_war_with(civs: Iterable[Any], player_id: int) -> bool:
    """True if we are at war with this player, or if nothing is known about them.

    The only place trade offers read war status. A war tracker can replace its body later.
    """
    for civ in civs:
        if civ.player_id == player_id:
            return bool(civ.is_at_war)
    return True


def surplus_resources(resources: Any, weights: TradeOfferWeights) -> list[Surplus]:
    """The resources we can spare, strategic first. Pure: it reads only what it is given."""
    stockpiles, _owned, _nearby, luxuries = resources
    found: list[Surplus] = []
    for pile in stockpiles:
        if not _TYPE_NAME.match(pile.name) or pile.cap <= 0:
            continue
        give = weights.strategic_amount
        keeps = pile.demand * weights.reserve_turns
        if pile.amount < pile.cap * weights.near_cap_share or pile.amount - give < keeps:
            continue
        found.append(Surplus(pile.name, "strategic", give))
    for name, count in sorted(luxuries.items()):
        give = min(weights.luxury_amount, count - 1)  # The last copy is never offered.
        if _TYPE_NAME.match(name) and give >= 1:
            found.append(Surplus(name, "luxury", give))
    return found


def price_offer(
    surplus: Surplus, trade: TradeOfferWeights, diplomacy: DiplomacyWeights
) -> tuple[dict, dict, float]:
    """The offer item, the gold we ask, and what we give, priced as `judge_deal` prices a deal.

    The gold is the least whole amount that returns the value given times `ask_margin`, plus
    `min_net_value`.
    """
    gives = SimpleNamespace(
        item_type="RESOURCE", name=surplus.name, amount=surplus.amount, duration=trade.duration
    )
    given = item_value(gives, diplomacy)
    wanted = given * trade.ask_margin + diplomacy.min_net_value
    if trade.gold_per_turn:
        unit = diplomacy.gold_per_turn_factor * diplomacy.gold_value * trade.duration
        gold = max(1, math.ceil(wanted / unit)) if unit > 0 else 0
        duration = trade.duration
    else:
        gold = max(1, math.ceil(wanted / diplomacy.gold_value)) if diplomacy.gold_value > 0 else 0
        duration = 0
    offer = {
        "type": "RESOURCE",
        "name": f"RESOURCE_{surplus.name}",
        "amount": surplus.amount,
        "duration": trade.duration,
    }
    ask = {"type": "GOLD", "amount": gold, "duration": duration}
    return offer, ask, given


def _received_value(ask: dict, diplomacy: DiplomacyWeights) -> float:
    return item_value(
        SimpleNamespace(item_type="GOLD", name="", amount=ask["amount"], duration=ask["duration"]),
        diplomacy,
    )


@dataclass
class _TurnState:
    turn: int = -1
    offers: int = 0


@dataclass
class TradeOfferPolicy:
    """A turn handler that offers surplus resources. Keep one instance for a whole game."""

    diplomacy: DiplomacyWeights = field(default_factory=DiplomacyWeights)
    weights: TradeOfferWeights = field(default_factory=TradeOfferWeights)
    name = "trade_offers"

    def __post_init__(self) -> None:
        self._state = _TurnState()
        self._last_offer: dict[int, int] = {}  # Player -> turn of our last offer to them.
        self._rest_until: dict[tuple[int, str], int] = {}  # (Player, resource) -> turn.

    async def run(self, game: TradeGame) -> None:
        overview = await game.get_game_overview()
        turn = overview.turn
        if turn != self._state.turn:
            self._state = _TurnState(turn=turn)
        self._forget_old(turn)
        surplus = surplus_resources(await game.get_empire_resources(), self.weights)
        if not surplus:
            return
        civs = [civ for civ in await game.get_diplomacy() if civ.has_met]
        open_with = {s.other_player_id for s in await game.get_diplomacy_sessions()}
        for civ in sorted(
            civs, key=lambda c: (self._last_offer.get(c.player_id, -(10**9)), c.player_id)
        ):
            if self._state.offers >= self.weights.max_offers_per_turn:
                log.info("Trade offers: the limit of %d this turn is reached", self._state.offers)
                break
            await self._offer_to(game, civs, civ.player_id, surplus, turn, open_with)

    def _forget_old(self, turn: int) -> None:
        self._rest_until = {k: t for k, t in self._rest_until.items() if t > turn}
        self._last_offer = {
            p: t for p, t in self._last_offer.items() if turn - t < self.weights.civ_interval_turns
        }
        while len(self._rest_until) > self.weights.max_remembered:
            del self._rest_until[min(self._rest_until, key=self._rest_until.__getitem__)]

    def _skip_reason(self, civs: list[Any], player: int, turn: int, open_with: set[int]) -> str:
        if at_war_with(civs, player):
            return "at war or unknown"
        if player in open_with:
            return "a session with them is open"
        if player in self._last_offer:
            return f"offered on turn {self._last_offer[player]}"
        return ""

    async def _offer_to(
        self,
        game: TradeGame,
        civs: list[Any],
        player: int,
        surplus: list[Surplus],
        turn: int,
        open_with: set[int],
    ) -> None:
        if reason := self._skip_reason(civs, player, turn, open_with):
            log.info("Trade offers: none to player %s, %s", player, reason)
            return
        for item in surplus:
            if self._rest_until.get((player, item.name), -1) > turn:
                log.info(
                    "Trade offers: %s to player %s rests, refused or sold lately", item.name, player
                )
                continue
            await self._propose(game, player, item, turn)
            return  # One offer per civ per interval.

    async def _propose(self, game: TradeGame, player: int, item: Surplus, turn: int) -> None:
        offer, ask, given = price_offer(item, self.weights, self.diplomacy)
        received = _received_value(ask, self.diplomacy)
        if received - given < self.diplomacy.min_net_value:
            log.info("Trade offers: %s to player %s not priced above its worth", item.name, player)
            return
        self._state.offers += 1
        self._last_offer[player] = turn
        log.info(
            "Trade offer to player %s: %dx %s (%s) for %s gold%s, worth %g for %g",
            player,
            item.amount,
            item.name,
            item.kind,
            ask["amount"],
            "/turn" if ask["duration"] else "",
            received,
            given,
        )
        key = (player, item.name)
        try:
            # `propose_trade` accepts whatever the AI counters with. In Run 4 (turn 105) that was
            # 30 favor, Open Borders and a Joint War on top of a wine-for-gold offer, and the AI's
            # turn never finished. So the AI is asked first, and only an offer it would accept
            # exactly as written is sent.
            verdict = await game.test_trade(player, [offer], [ask])
            if not is_acceptable_as_is(verdict):
                log.info(
                    "Trade offer to player %s not sent, the AI would not take it as written: %s",
                    player,
                    " ".join(verdict.split())[:200],
                )
                self._rest_until[key] = turn + self.weights.refusal_turns
                await self._close(game, player)
                return
            result = await game.propose_trade(player, [offer], [ask])
        except ConnectionError:
            raise
        except Exception:
            log.exception("Offering %s to player %s failed", item.name, player)
            self._rest_until[key] = turn + self.weights.refusal_turns
            await self._close(game, player)
            return
        if not action_succeeded(result):
            log.info("Trade offer to player %s failed: %s", player, result)
            self._rest_until[key] = turn + self.weights.refusal_turns
        elif result.upper().startswith("ACCEPTED"):
            log.info("Trade offer to player %s accepted: %s", player, result)
            self._rest_until[key] = turn + self.weights.accepted_turns
        else:  # REJECTED, or PROPOSED with no answer yet.
            log.info("Trade offer to player %s not accepted: %s", player, result)
            self._rest_until[key] = turn + self.weights.refusal_turns
        await self._close(game, player)

    async def _close(self, game: TradeGame, player: int) -> None:
        """Exit any session still open with this player."""
        try:
            left = [s for s in await game.get_diplomacy_sessions() if s.other_player_id == player]
            if left:
                log.warning("Session with player %s left open by a trade offer, exiting", player)
                await game.diplomacy_respond(player, EXIT)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Closing the session with player %s failed", player)
