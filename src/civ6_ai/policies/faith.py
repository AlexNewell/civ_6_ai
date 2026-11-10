"""Spend faith above a reserve and find a use for Great Prophets, with no model calls.

Run 3 ended turn 135 with 224 faith banked and a Great Prophet standing still. Why the prophet
was idle: `SecondaryPolicy` leaves a prophet alone once no religion can be founded (we have one,
or every religion is taken), and while one can be founded but the prophet has no Holy Site to
walk to it asks again only every few turns. Nothing else handles prophets, so it stood.

`FaithPolicy` runs after `SecondaryPolicy` and does two things.

Prophets. It counts the turns each Great Prophet has stayed on the same tile. After
`PROPHET_ACT_AFTER` turns it tries a fallback, so no prophet stands for more than a few turns
before something is attempted, and every try and refusal is logged:

- A religion can still be founded: ask the Great People advisor for own cities with a finished
  Holy Site. On one, activate. Otherwise walk toward the nearest (a few targets are tried when a
  move is refused, and at most `PROPHET_MAX_MOVES` orders are given). With no Holy Site anywhere
  it is logged once for that prophet and asked again every `NO_SITE_WAIT` turns. Production has
  no way to be asked for an item, so this policy does not ask for a Holy Site. The log names
  the gap.
- No religion can be founded: try to spread our religion from the current tile. If the game
  refuses (no charges, no city here with another majority), it is retried every
  `PROPHET_RETRY_TURNS` turns and the reason is logged. After `SPREAD_GIVE_UP` refusals in a row
  nothing is left to do with it (nothing moves it to a better place and no religion can be
  founded), so it is deleted with `delete_unit` rather than left standing for the whole game.

Faith. Faith above `FaithSettings.reserve` is spent, in this order:

1. Great people, bought with faith. The price is read from the Great People screen, so it is
   exact and the reserve can be kept. One person is worth many turns of points, which makes it
   the best use of a lump of faith. Ranked by the same class weights as recruiting. A Great
   Prophet is bought only while a religion can still be founded.
2. Shrine and Temple, offered by the city's production list. These raise faith and, for the
   Temple, amenity and housing. The list gives only gold costs, so the faith price is learned
   from the first purchase and until then an upper estimate is required above the reserve.

Religious units are not bought. Nothing in the bot moves or spreads missionaries yet, so a unit
bought now would stand idle. Add them when a spreading policy exists.

Nothing is spent until a pantheon is chosen, as long as the game still offers one, because
`SecondaryPolicy` needs that faith. Spending is bounded to `FaithSettings.max_purchases` bought
and `MAX_ATTEMPTS` tried per turn. Every purchase is logged, and so is every turn that faith is
above the reserve and nothing was bought, with the reason.

This policy buys with faith only. Gold spending is a separate policy.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from civ6_ai.blockers import action_succeeded, move_succeeded
from civ6_ai.policies.secondary import _GP_WEIGHTS, SecondaryWeights, canonical
from civ6_ai.state import GameReads, Unit, read_units

log = logging.getLogger(__name__)

PROPHET_ACT_AFTER = 2  # Turns on one tile before the fallback runs. Well inside the 5 allowed.
PROPHET_RETRY_TURNS = 2  # Turns between tries when the fallback is refused.
MAX_ATTEMPTS = 6  # Purchase and patronise calls per turn, successful or not.
MAX_CITY_SCANS = 4  # Cities whose production list is read per turn.
NO_SITE_WAIT = 5  # Turns between advisor reads while no city has a Holy Site.
PROPHET_MAX_MOVES = 15  # Move orders given to one prophet before it is left where it is.
MAX_MOVE_TARGETS = 3  # Holy Sites tried, nearest first, when a move is refused.
SPREAD_GIVE_UP = 8  # Refused spread tries in a row before a prophet with no use is deleted.
REFUSED_WAIT = 5  # Turns before a refused person or building is tried again.

_PROPHET = "UNIT_GREAT_PROPHET"
_PROPHET_CLASS = "Great Prophet"
_UNCLAIMED = "Unclaimed"
_FAITH = "YIELD_FAITH"
_UNKNOWN_CLASS = 0.5  # Weight of a great person class the weights don't name.

# Buildings bought with faith, best first, with an upper estimate of the price. The estimate
# gates the first purchase only. After that the price the game reported is used.
_BUILDINGS: tuple[tuple[str, int], ...] = (
    ("BUILDING_SHRINE", 100),
    ("BUILDING_TEMPLE", 250),
)

_PAID = re.compile(r"cost[=:]\s*(\d+)\s*f", re.IGNORECASE)
_QUOTED = re.compile(r"costs\s+(\d+)f", re.IGNORECASE)


@dataclass(frozen=True)
class FaithSettings:
    """How much faith is held back and how much is spent per turn."""

    reserve: float = 60.0
    max_purchases: int = 2

    def __post_init__(self) -> None:
        if self.reserve < 0 or self.max_purchases < 0:
            raise ValueError("Faith reserve and max_purchases must be at least 0")


class FaithGame(GameReads, Protocol):
    """What the policy needs from civ6-mcp's `GameState`."""

    async def get_pantheon_status(self) -> Any: ...
    async def get_religion_founding_status(self) -> Any: ...
    async def get_great_people(self) -> list[Any]: ...
    async def patronize_great_person(self, individual_id: int, yield_type: str = ...) -> str: ...
    async def list_city_production(self, city_id: int) -> list[Any]: ...
    async def purchase_item(
        self, city_id: int, item_type: str, item_name: str, yield_type: str = ...
    ) -> str: ...
    async def activate_great_person(self, unit_index: int) -> str: ...
    async def spread_religion(self, unit_index: int) -> str: ...
    async def get_gp_advisor(self, unit_index: int) -> Any: ...
    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...
    async def delete_unit(self, unit_index: int) -> str: ...


@dataclass
class _Prophet:
    x: int
    y: int
    idle: int = 0
    retry_at: int = -1
    warned: bool = False
    moves: int = 0  # Move orders given. Kept while it walks.
    refusals: int = 0  # Spread tries refused in a row.
    no_site_logged: bool = False


@dataclass
class FaithPolicy:
    """Spends faith and keeps Great Prophets in use. Run it after `SecondaryPolicy`."""

    settings: FaithSettings = field(default_factory=FaithSettings)
    weights: SecondaryWeights = field(default_factory=SecondaryWeights)
    name: str = "faith"
    _prophets: dict[int, _Prophet] = field(default_factory=dict, init=False, repr=False)
    _costs: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _refused: dict[tuple[str, int, str], int] = field(default_factory=dict, init=False, repr=False)

    async def run(self, game: FaithGame) -> None:
        """Run each part so one failing doesn't strand the other, then raise the first error."""
        turn = (await game.get_game_overview()).turn
        errors: list[Exception] = []
        for step in (self._handle_prophets, self._spend):
            try:
                await step(game, turn)
            except Exception as error:  # noqa: BLE001 - each part is independent.
                log.exception("Faith step %s failed", step.__name__)
                errors.append(error)
        if errors:
            raise errors[0]

    # ----- prophets -----

    async def _handle_prophets(self, game: FaithGame, turn: int) -> None:
        units = [u for u in await read_units(game) if canonical(u.unit_type) == _PROPHET]
        self._prophets = {
            k: v for k, v in self._prophets.items() if k in {u.unit_id for u in units}
        }
        can_found: bool | None = None
        for unit in sorted(units, key=lambda u: u.unit_id):
            seen = self._prophets.get(unit.unit_id)
            if seen is None:
                self._prophets[unit.unit_id] = _Prophet(unit.x, unit.y)
                continue
            if (seen.x, seen.y) != (unit.x, unit.y):
                seen.x, seen.y, seen.idle, seen.retry_at = unit.x, unit.y, 0, -1
                continue
            seen.idle += 1
            if seen.idle < PROPHET_ACT_AFTER or seen.retry_at > turn or not unit.can_move:
                continue
            if can_found is None:
                can_found = await self._can_found(game)
            await self._use_prophet(game, unit, seen, turn, can_found)

    @staticmethod
    async def _can_found(game: FaithGame) -> bool:
        status = await game.get_religion_founding_status()
        return not status.has_religion and bool(status.available_religions)

    async def _use_prophet(
        self, game: FaithGame, unit: Unit, seen: _Prophet, turn: int, can_found: bool
    ) -> None:
        log.info("%s has stood %d turns at (%d,%d)", unit.name, seen.idle, unit.x, unit.y)
        if can_found:
            reason = await self._found_or_walk(game, unit, seen, turn)
            if reason is None:
                return
        else:
            result = await game.spread_religion(unit.unit_index)
            if action_succeeded(result) and "RELIGION_SPREAD" in result:
                log.info("%s spread religion: %s", unit.name, result[:120])
                seen.idle = seen.refusals = 0
                return
            seen.refusals += 1
            reason = f"no religion to found and cannot spread here: {result[:120]}"
            if seen.refusals >= SPREAD_GIVE_UP:
                await self._retire(game, unit, seen)
                return
        seen.retry_at = max(seen.retry_at, turn + PROPHET_RETRY_TURNS)
        if seen.idle > 5 and not seen.warned:
            seen.warned = True
            log.warning("%s still idle after %d turns: %s", unit.name, seen.idle, reason)
        else:
            log.info("%s unused: %s", unit.name, reason)

    async def _found_or_walk(
        self, game: FaithGame, unit: Unit, seen: _Prophet, turn: int
    ) -> str | None:
        """Found the religion on a Holy Site, or walk to one. None means it did something."""
        advisor = await game.get_gp_advisor(unit.unit_index)
        sites = list(advisor.cities) if advisor is not None else []
        if not sites:
            seen.retry_at = turn + NO_SITE_WAIT
            if not seen.no_site_logged:
                seen.no_site_logged = True
                log.warning(
                    "%s can found a religion but no city has a finished Holy Site. "
                    "Production does not take requests, so none is asked for",
                    unit.name,
                )
            return "religion open but no city has a Holy Site"
        sites.sort(key=lambda c: (not c.can_activate, c.distance, -c.city_yield, c.city_id))
        if sites[0].distance == 0:
            result = await game.activate_great_person(unit.unit_index)
            if "GP_ACTIVATED" in result:
                log.info(
                    "%s founded a religion in %s: %s", unit.name, sites[0].city_name, result[:120]
                )
                self._prophets.pop(unit.unit_id, None)
                return None
            return f"on the Holy Site of {sites[0].city_name} but refused: {result[:120]}"
        if seen.moves >= PROPHET_MAX_MOVES:
            return f"gave up walking after {seen.moves} orders"
        for site in sites[:MAX_MOVE_TARGETS]:
            seen.moves += 1
            result = await game.move_unit(unit.unit_index, site.district_x, site.district_y)
            if move_succeeded(result):
                log.info(
                    "%s walks to the Holy Site in %s, %d tiles away",
                    unit.name,
                    site.city_name,
                    site.distance,
                )
                return None
            log.info("%s can't move to %s: %s", unit.name, site.city_name, result[:120])
        return "every move toward a Holy Site was refused"

    async def _retire(self, game: FaithGame, unit: Unit, seen: _Prophet) -> None:
        """Delete a prophet that has no use left, so it doesn't stand for the whole game."""
        result = await game.delete_unit(unit.unit_index)
        if action_succeeded(result):
            log.warning(
                "%s deleted: no religion to found and spreading refused %d times",
                unit.name,
                seen.refusals,
            )
            self._prophets.pop(unit.unit_id, None)
        else:
            log.info("%s could not be deleted: %s", unit.name, result[:120])
            seen.refusals = 0  # Count again before the next try.

    # ----- faith -----

    async def _spend(self, game: FaithGame, turn: int) -> None:
        faith = (await game.get_game_overview()).faith
        reserve = self.settings.reserve
        if faith <= reserve or self.settings.max_purchases <= 0:
            return
        pantheon = await game.get_pantheon_status()
        if not pantheon.has_pantheon and pantheon.available_beliefs:
            log.info("Faith %.0f above reserve %.0f but kept for the pantheon", faith, reserve)
            return
        budget = faith - reserve
        bought = 0
        attempts = 0
        notes: list[str] = []
        for step in (self._patronise, self._buildings):
            spent, count, tried = await step(game, turn, budget, attempts, bought, notes)
            budget -= spent
            bought += count
            attempts += tried
            if bought >= self.settings.max_purchases or attempts >= MAX_ATTEMPTS:
                break
        if not bought:
            log.info(
                "Faith %.0f above reserve %.0f but nothing bought: %s",
                faith,
                reserve,
                "; ".join(notes) or "nothing offered",
            )

    # Each step below returns (faith spent, things bought, calls made).

    async def _patronise(
        self,
        game: FaithGame,
        turn: int,
        budget: float,
        attempts: int,
        bought: int,
        notes: list[str],
    ) -> tuple[float, int, int]:
        people = await game.get_great_people()
        religion = await game.get_religion_founding_status()
        can_found = not religion.has_religion and bool(religion.available_religions)
        offered = [p for p in people if p.claimant == _UNCLAIMED and p.faith_cost > 0]
        if not offered:
            notes.append("no great person can be bought with faith")
            return 0.0, 0, 0

        def class_weight(person: Any) -> float:
            attr = _GP_WEIGHTS.get(person.class_name)
            return getattr(self.weights, attr) if attr else _UNKNOWN_CLASS

        wanted = [
            p
            for p in offered
            if class_weight(p) > 0 and (can_found or p.class_name != _PROPHET_CLASS)
        ]
        wanted.sort(
            key=lambda p: (-class_weight(p), p.faith_cost, p.individual_name, p.individual_id)
        )
        spent = 0.0
        tried = 0
        count = 0
        for person in wanted:
            if bought + count >= self.settings.max_purchases or attempts + tried >= MAX_ATTEMPTS:
                break
            key = ("gp", person.individual_id, "")
            if self._refused.get(key, -1) > turn:
                continue
            if person.faith_cost > budget - spent:
                continue
            tried += 1
            result = await game.patronize_great_person(person.individual_id, _FAITH)
            if "PATRONIZED" in result and action_succeeded(result):
                spent += person.faith_cost
                count += 1
                log.info(
                    "Faith bought %s (%s) for %d faith",
                    person.individual_name,
                    person.class_name,
                    person.faith_cost,
                )
            else:
                self._refused[key] = turn + REFUSED_WAIT
                log.info("Can't buy %s with faith: %s", person.individual_name, result[:120])
        if not count:
            cheapest = min(p.faith_cost for p in offered)
            notes.append(
                f"great people: cheapest {cheapest} faith vs {budget:.0f} above reserve"
                if cheapest > budget
                else "great people: none wanted or all refused recently"
            )
        return spent, count, tried

    async def _buildings(
        self,
        game: FaithGame,
        turn: int,
        budget: float,
        attempts: int,
        bought: int,
        notes: list[str],
    ) -> tuple[float, int, int]:
        cities, _ = await game.get_cities()
        ordered = sorted(cities, key=lambda c: c.city_id)
        if ordered:
            start = turn % len(ordered)  # Rotate, so the same cities aren't always read first.
            ordered = ordered[start:] + ordered[:start]
        spent = 0.0
        tried = 0
        count = 0
        for city in ordered[:MAX_CITY_SCANS]:
            if bought + count >= self.settings.max_purchases or attempts + tried >= MAX_ATTEMPTS:
                break
            options = {
                o.item_name: o
                for o in await game.list_city_production(city.city_id)
                if o.category == "BUILDING" and not o.is_repair
            }
            for item, estimate in _BUILDINGS:
                if item not in options or self._refused.get(("b", city.city_id, item), -1) > turn:
                    continue
                price = self._costs.get(item, estimate)
                if price > budget - spent:
                    notes.append(
                        f"{item} in {city.name} needs about {price}, have {budget - spent:.0f}"
                    )
                    continue
                tried += 1
                result = await game.purchase_item(city.city_id, "BUILDING", item, _FAITH)
                if "PURCHASED" in result and action_succeeded(result):
                    paid = _PAID.search(result)
                    cost = int(paid.group(1)) if paid else price
                    self._costs[item] = cost
                    spent += cost
                    count += 1
                    log.info("Faith bought %s in %s for %d faith", item, city.name, cost)
                    break
                quoted = _QUOTED.search(result)
                if quoted:
                    self._costs[item] = int(quoted.group(1))
                self._refused[("b", city.city_id, item)] = turn + REFUSED_WAIT
                log.info("Can't buy %s in %s with faith: %s", item, city.name, result[:120])
                notes.append(f"{item} in {city.name} refused")
        if not count and not notes:
            notes.append("no Shrine or Temple offered in the cities read")
        return spent, count, tried
