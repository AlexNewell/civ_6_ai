"""Answer a falling-loyalty warning (roadmap item 1.37).

Run 7 lost 5 of 7 cities after a `city_loyalty_warning`, and earlier runs lost 4 more; nothing
acted on the warning. This handler subscribes to the warning events and, each turn while a warned
city's loyalty keeps falling, takes what the bot has:

- a governor: Victor (`DEFENDER`) or Amani (`AMBASSADOR`) already appointed is sent to the city, or
  appointed and sent when a point is free, or else any appointed governor still waiting for a city
  goes;
- a garrison: the nearest spare combat unit moves in when no unit holds the city;
- a policy card with loyalty or amenities in its name, in a slot that is empty.

Production's own amenity scoring is not touched here. Every response is logged and published as a
`city_loyalty_response` event. Each city is answered at most once every `COOLDOWN_TURNS`, and a
governor is sent to one city at most `MAX_SENDS` times, because moving restarts its establishing.
With no warned city the handler makes no game call.
"""

import logging
from collections import Counter
from typing import Any

from civ6_ai.blockers import action_succeeded, move_succeeded
from civ6_ai.monitors import CityLoyaltyResponse, CityLoyaltyWarning, Event, EventBus
from civ6_ai.policies.government import card_fits
from civ6_ai.policies.settling import (
    SCOUT,
    find_escort,
    unit_at,
    units_reserved_for_settling,
)
from civ6_ai.state import City, read_cities, read_government, read_units

log = logging.getLogger(__name__)

COOLDOWN_TURNS = 3
MAX_SENDS = 2
GARRISON_RADIUS = 8
PREFERRED_GOVERNORS = ("GOVERNOR_THE_DEFENDER", "GOVERNOR_THE_AMBASSADOR")  # Victor, Amani.
LOYALTY_CARD_WORDS = ("LOYAL", "AMENIT")


class LoyaltyResponse:
    """Turn handler. Build it with the bus so it hears the warnings."""

    name = "loyalty_response"

    def __init__(self, bus: EventBus | None = None) -> None:
        self._emit = bus.publish if bus is not None else None
        self.warned: dict[int, int] = {}  # City id to the turn of its warning.
        self._last_response: dict[int, int] = {}
        self._sends: Counter[tuple[str, int]] = Counter()
        if bus is not None:
            bus.subscribe(self._on_warning, CityLoyaltyWarning)

    def _on_warning(self, event: Event) -> None:
        if isinstance(event, CityLoyaltyWarning):
            self.warned.setdefault(event.city_id, event.turn)

    async def run(self, game: Any) -> None:
        if not self.warned:
            return
        turn = (await game.get_game_overview()).turn
        cities = {c.city_id: c for c in await read_cities(game)}
        for city_id in list(self.warned):
            city = cities.get(city_id)
            if city is None or city.loyalty_per_turn >= 0:
                del self.warned[city_id]  # Lost, or the loyalty stopped falling.
                self._last_response.pop(city_id, None)
        for city_id in sorted(self.warned):
            if turn - self._last_response.get(city_id, -COOLDOWN_TURNS) < COOLDOWN_TURNS:
                continue
            self._last_response[city_id] = turn
            city = cities[city_id]
            actions: list[str] = []
            for step in (self._governor, self._garrison, self._card):
                try:
                    actions.extend(await step(game, city))
                except ConnectionError:
                    raise
                except Exception:
                    log.exception(
                        "Loyalty response step %s failed for %s", step.__name__, city.name
                    )
            line = (
                f"loyalty_response turn={turn} city={city.name} loyalty={city.loyalty:.0f} "
                f"per_turn={city.loyalty_per_turn:.1f} actions={actions or ['none available']}"
            )
            log.info("%s", line)
            if self._emit is not None:
                self._emit(
                    CityLoyaltyResponse(
                        turn,
                        city.city_id,
                        city.name,
                        city.loyalty,
                        city.loyalty_per_turn,
                        tuple(actions),
                    )
                )

    async def _governor(self, game: Any, city: City) -> list[str]:
        government = await read_government(game)
        placed = {g.governor_type: g for g in government.governors}
        for kind in PREFERRED_GOVERNORS:
            if kind in placed and placed[kind].city_id == city.city_id:
                return [f"governor {kind} already here"]
        # Never one that holds another city that is falling too.
        busy = {cid for cid in self.warned if cid != city.city_id}
        for kind in PREFERRED_GOVERNORS:
            governor = placed.get(kind)
            if (
                governor is not None
                and governor.city_id not in busy
                and await self._send(game, kind, city)
            ):
                return [f"governor {kind} sent"]
        if government.can_appoint_governor and government.governor_points > 0:
            for kind in PREFERRED_GOVERNORS:
                if kind in government.appointable_governors and kind not in placed:
                    result = await game.appoint_governor(kind)
                    if action_succeeded(result) and "APPOINT" in result:
                        sent = await self._send(game, kind, city)
                        return [f"governor {kind} appointed" + (" and sent" if sent else "")]
        for governor in government.governors:
            if governor.city_id is None and await self._send(game, governor.governor_type, city):
                return [f"governor {governor.governor_type} sent"]
        return []

    async def _send(self, game: Any, kind: str, city: City) -> bool:
        if self._sends[(kind, city.city_id)] >= MAX_SENDS:
            return False
        result = await game.assign_governor(kind, city.city_id)
        if action_succeeded(result) and "ASSIGNED" in result:
            self._sends[(kind, city.city_id)] += 1
            return True
        log.warning("The game refused %s in %s: %s", kind, city.name, result)
        return False

    async def _garrison(self, game: Any, city: City) -> list[str]:
        if city.garrison_unit is not None:
            return []
        units = list(await read_units(game))
        tiles = [(c.x, c.y) for c in await read_cities(game)]
        reserved = units_reserved_for_settling(units, tiles)
        pick = find_escort(unit_at(city.x, city.y), units, tiles, reserved, GARRISON_RADIUS)
        if pick is None or pick.unit_type == SCOUT:
            return ["garrison: no spare unit"]
        if move_succeeded(await game.move_unit(pick.unit_index, city.x, city.y)):
            return [f"garrison {pick.unit_type} sent"]
        return [f"garrison {pick.unit_type} move refused"]

    async def _card(self, game: Any, city: City) -> list[str]:
        government = await read_government(game)
        slotted = {s.policy for s in government.policy_slots if s.policy is not None}
        for slot in government.empty_policy_slots:
            for card in government.unlocked_policies:
                if (
                    card.policy_type not in slotted
                    and card_fits(slot, card)
                    and any(w in card.policy_type for w in LOYALTY_CARD_WORDS)
                ):
                    result = await game.set_policies({slot.index: card.policy_type})
                    if action_succeeded(result) and "POLICIES_SET" in result:
                        return [f"card {card.policy_type} set"]
        return []
