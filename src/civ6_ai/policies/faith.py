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

1. Great people, bought with faith. A price of 2147483647 (the game's maximum integer) means
   the person can't be bought, so it is never the cheapest option. The price is read from the
   Great People screen, so it is exact. Ranked by the class weights used for recruiting. A
   Great Prophet is bought only while a religion can still be founded. When none is affordable
   and the cheapest wanted one costs at most `SAVE_CAP` and the estimated income closes the gap
   within `SAVE_TURNS`, the faith is saved toward it. There is no price cap: Run 9 judged
   1,160 faith at 107 a turn (7 turns) not worth saving for and left up to 4,519 idle. A
   person further than `SAVE_TURNS` away is not saved for, so the faith goes down the list.
   While saving, a lesser buy that costs at most `SLACK_TURNS` turns of income, summed over
   the whole wait, still goes ahead, so the saving does not stall every other outlet.
   When no person is priced, the log counts how many are claimed, priced 0 or the sentinel.
2. Units from the cities' production lists, tried with faith: the best defenders in a
   threatened city (`DefenceState`), then a Builder, but no Builder while one has nothing to do
   or no job is unclaimed (`BuilderDemand`; Run 9 had them idle on 76% of builder-turns). A
   `STACKING_CONFLICT` refusal names the unit on the city tile: that city and item are skipped
   until the units read shows that unit has left (`stacking.StackBlocks`), and other cities are
   tried meanwhile. The game allows faith for units only under
   some policy cards and dedications (Theocracy, Monumentality) and the list shows gold prices
   only, so a unit is tried and a refusal that is not a price quote is remembered per item for
   `RULE_WAIT` turns.
3. Shrine, Temple and the worship buildings (Cathedral, Mosque and the like), offered by the
   production list (they need a Holy Site, and the worship ones a Temple). The faith price
   is learned from the first purchase and until then an upper estimate is required.

Religious units are not bought. Nothing in the bot moves or spreads missionaries yet.

Calls: the great people list is reused for `GP_REFRESH` turns while the cheapest price is out
of reach, the "can a religion be founded" answer for `RELIGION_REFRESH` turns, the pantheon
answer for good once settled, and the cities' lists are skipped for `SINK_WAIT` turns when
nothing could be tried.

Every turn logs one line: faith, reserve and the sink spent on, the saving target, or why
nothing was bought.

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
from civ6_ai.policies.production import defence_strength
from civ6_ai.policies.secondary import _GP_WEIGHTS, SecondaryWeights, canonical
from civ6_ai.stacking import StackBlocks
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
RULE_WAIT = 15  # Turns before an item the game's rules refuse (no card or dedication) is retried.
SINK_WAIT = 3  # Turns the cities' lists are not read again when nothing could be tried.
GP_REFRESH = 4  # Turns the great people list is reused while the cheapest price is out of reach.
RELIGION_REFRESH = 10  # Turns the "can a religion be founded" answer is reused.
SAVE_TURNS = 15  # Most turns of saving worth waiting for a great person (no price cap).
SLACK_TURNS = 2  # While saving, a lesser buy is fine if it delays the target by at most this.

_PROPHET = "UNIT_GREAT_PROPHET"
_PROPHET_CLASS = "Great Prophet"
_UNCLAIMED = "Unclaimed"
_FAITH = "YIELD_FAITH"
_UNBUYABLE = 2**31 - 1  # The game's max int: the price it reports for a person who can't be bought.
_BUILDER = "UNIT_BUILDER"
_UNKNOWN_CLASS = 0.5  # Weight of a great person class the weights don't name.

# Buildings bought with faith, best first, with an upper estimate of the price. The estimate
# gates the first purchase only. After that the price the game reported is used.
_BUILDINGS: tuple[tuple[str, int], ...] = (
    ("BUILDING_SHRINE", 100),
    ("BUILDING_TEMPLE", 250),
    # Worship buildings (one per religion's followers) are offered only when the city has a
    # Temple and our religion, so the production list decides whether any is bought.
    ("BUILDING_CATHEDRAL", 400),
    ("BUILDING_GURDWARA", 400),
    ("BUILDING_MEETING_HOUSE", 400),
    ("BUILDING_MOSQUE", 400),
    ("BUILDING_PAGODA", 400),
    ("BUILDING_STUPA", 400),
    ("BUILDING_SYNAGOGUE", 400),
    ("BUILDING_WAT", 400),
    ("BUILDING_DAR_E_MEHR", 400),
)


def _estimate(option: Any) -> int:
    """An upper guess at a faith price before one is learned: the gold price, else 150."""
    gold = getattr(option, "gold_cost", -1)
    return int(gold) if gold and gold > 0 else 150


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
    _refused: dict[tuple, int] = field(default_factory=dict, init=False, repr=False)
    defence: Any = None  # The threat state (`defence.DefenceState`), or None.
    builder_demand: Any = None  # `builder.BuilderDemand`, or None: what builders left undone.
    _stack: StackBlocks = field(default_factory=StackBlocks, init=False, repr=False)
    _sink_cap: float | None = field(default=None, init=False, repr=False)
    _saving_for: int | None = field(default=None, init=False, repr=False)  # Person's id.
    _delay: tuple[int, float] = field(default=(-1, 0.0), init=False, repr=False)
    _last: tuple[int, float] | None = field(default=None, init=False, repr=False)
    _spent_since: float = field(default=0.0, init=False, repr=False)
    _income: float | None = field(default=None, init=False, repr=False)
    _pantheon_done: bool = field(default=False, init=False, repr=False)
    _gp: tuple | None = field(default=None, init=False, repr=False)
    _can_found_at: tuple[int, bool] | None = field(default=None, init=False, repr=False)
    _sink_gate: tuple[int, float] = field(default=(-1, 0.0), init=False, repr=False)
    _saving: str | None = field(default=None, init=False, repr=False)

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

    def _track_income(self, turn: int, faith: float) -> None:
        """Estimate faith per turn from the balance, adding back what was spent since."""
        if self._last is not None and turn > self._last[0]:
            gained = faith - self._last[1] + self._spent_since
            rate = max(0.0, gained / (turn - self._last[0]))
            self._income = rate if self._income is None else 0.5 * self._income + 0.5 * rate
            self._spent_since = 0.0
        if self._last is None or turn > self._last[0]:
            self._last = (turn, faith)

    async def _spend(self, game: FaithGame, turn: int) -> None:
        faith = (await game.get_game_overview()).faith
        reserve = self.settings.reserve
        self._track_income(turn, faith)
        if faith <= reserve or self.settings.max_purchases <= 0:
            log.info("Faith %.0f, reserve %.0f: at or below the reserve", faith, reserve)
            return
        if not self._pantheon_done:
            pantheon = await game.get_pantheon_status()
            if not pantheon.has_pantheon and pantheon.available_beliefs:
                log.info("Faith %.0f above reserve %.0f but kept for the pantheon", faith, reserve)
                return
            self._pantheon_done = True  # Not asked again once chosen or none is left.
        budget = faith - reserve
        bought = 0
        attempts = 0
        notes: list[str] = []
        sinks: list[str] = []
        self._saving = None
        self._saving_for = None
        self._sink_cap = None
        for step in (self._patronise, self._production_sinks):
            spent, count, tried = await step(game, turn, budget, attempts, bought, notes, sinks)
            budget -= spent
            bought += count
            attempts += tried
            self._spent_since += spent
            if self._saving_for is not None and step == self._production_sinks:
                self._delay = (self._saving_for, self._delay[1] + spent)
            if bought >= self.settings.max_purchases or attempts >= MAX_ATTEMPTS:
                break
            if self._saving is not None:
                # Keep the faith for the great person, but a buy that delays it by at most
                # `SLACK_TURNS` turns of income is still fine.
                # The allowance is for the whole wait, so what lesser buys already took counts.
                used = self._delay[1] if self._delay[0] == self._saving_for else 0.0
                self._sink_cap = max(0.0, SLACK_TURNS * (self._income or 0.0) - used)
        if bought:
            log.info("Faith %.0f, reserve %.0f: spent on %s", faith, reserve, ", ".join(sinks))
        if self._saving is not None:
            log.info("Faith %.0f, reserve %.0f: saving toward %s", faith, reserve, self._saving)
        elif not bought:
            log.info(
                "Faith %.0f above reserve %.0f but nothing bought: %s",
                faith,
                reserve,
                "; ".join(notes) or "nothing offered",
            )

    # Each step below returns (faith spent, things bought, calls made).

    async def _great_people(self, game: FaithGame, turn: int, budget: float) -> tuple[list, bool]:
        """The great people and whether a religion can be founded.

        Reused for `GP_REFRESH` turns while the cheapest known price is out of reach, and the
        religion answer for `RELIGION_REFRESH` turns, which saves up to two calls a turn.
        """
        cached = self._gp
        if cached is not None and turn - cached[0] < GP_REFRESH:
            cheapest = min(
                (p.faith_cost for p in cached[1] if 0 < p.faith_cost < _UNBUYABLE), default=None
            )
            if cheapest is None or cheapest > budget:
                return cached[1], cached[2]
        people = await game.get_great_people()
        if self._can_found_at is None or turn - self._can_found_at[0] >= RELIGION_REFRESH:
            religion = await game.get_religion_founding_status()
            self._can_found_at = (
                turn,
                not religion.has_religion and bool(religion.available_religions),
            )
        self._gp = (turn, people, self._can_found_at[1])
        return people, self._can_found_at[1]

    async def _patronise(
        self,
        game: FaithGame,
        turn: int,
        budget: float,
        attempts: int,
        bought: int,
        notes: list[str],
        sinks: list[str],
    ) -> tuple[float, int, int]:
        people, can_found = await self._great_people(game, turn, budget)
        offered = [p for p in people if p.claimant == _UNCLAIMED and 0 < p.faith_cost < _UNBUYABLE]
        if not offered:
            claimed = sum(1 for p in people if p.claimant != _UNCLAIMED)
            unpriced = sum(1 for p in people if p.claimant == _UNCLAIMED and p.faith_cost <= 0)
            sentinel = sum(
                1 for p in people if p.claimant == _UNCLAIMED and p.faith_cost >= _UNBUYABLE
            )
            notes.append(
                "no great person can be bought with faith "
                f"({len(people)} listed: {claimed} claimed, {unpriced} priced 0, "
                f"{sentinel} not buyable)"
            )
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
                sinks.append(f"{person.individual_name} for {person.faith_cost}")
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
            self._save_for(wanted, turn, budget, notes)
        return spent, count, tried

    def _save_for(self, wanted: list[Any], turn: int, budget: float, notes: list[str]) -> None:
        """Pick the cheapest wanted great person as a saving target when it is reachable.

        Reachable means the income estimate closes the gap within `SAVE_TURNS` turns, whatever
        the price (Run 9: 1,160 faith at 107 a turn was 7 turns off and was refused as too
        dear, while thousands sat idle). Otherwise the faith goes to the units and buildings
        below.
        """
        pool = [p for p in wanted if self._refused.get(("gp", p.individual_id, ""), -1) <= turn]
        if not pool:
            notes.append("great people: none wanted or all refused recently")
            return
        target = min(pool, key=lambda p: (p.faith_cost, p.individual_id))
        gap = target.faith_cost - budget
        income = self._income or 0.0
        if income > 0 and gap / income <= SAVE_TURNS:
            self._saving_for = target.individual_id
            self._saving = (
                f"{target.individual_name} ({target.class_name}) at {target.faith_cost} faith, "
                f"about {gap / income:.0f} turns at {income:.1f} a turn"
            )
            return
        notes.append(
            f"great people: cheapest {target.faith_cost} faith vs {budget:.0f} above reserve, "
            f"not worth saving for at {income:.1f} a turn"
        )

    def _builder_wanted(self) -> bool:
        """False while a builder has nothing to do or no job is unclaimed (Run 9: 76% idle)."""
        demand = self.builder_demand
        if demand is None:
            return True
        return demand.idle <= 0 and demand.spare_jobs != 0

    def _any_threatened(self) -> bool:
        return self.defence is not None and self.defence.any

    def _threatened(self, city: Any) -> bool:
        return self.defence is not None and self.defence.is_threatened(city.city_id)

    def _candidates(self, city: Any, options: dict[str, Any]) -> list[tuple[str, str, int]]:
        """(category, item, price estimate) the city could buy with faith, best first.

        Which units faith can buy depends on policy cards and Golden Age dedications
        (Theocracy: land military, Monumentality: Builders), and the production list gives only
        gold prices. So a unit is tried, and a refusal for a reason that is not the price is
        remembered per item for `RULE_WAIT` turns.
        """
        out: list[tuple[str, str, int]] = []
        if self._threatened(city):
            defenders = [
                o
                for o in options.values()
                if o.category == "UNIT" and defence_strength(o.item_name) > 0
            ]
            defenders.sort(key=lambda o: (-defence_strength(o.item_name), o.item_name))
            out += [("UNIT", o.item_name, _estimate(o)) for o in defenders[:2]]
        builder = options.get(_BUILDER)
        if builder is not None and builder.category == "UNIT" and self._builder_wanted():
            out.append(("UNIT", _BUILDER, _estimate(builder)))
        out += [("BUILDING", item, est) for item, est in _BUILDINGS if item in options]
        return out

    async def _production_sinks(
        self,
        game: FaithGame,
        turn: int,
        budget: float,
        attempts: int,
        bought: int,
        notes: list[str],
        sinks: list[str],
    ) -> tuple[float, int, int]:
        """Units, then Shrine or Temple, from the cities' production lists (one call a city)."""
        until, need = self._sink_gate
        if until > turn and budget < need and not self._any_threatened():
            notes.append(f"no unit or building to buy yet (looked again from turn {until})")
            return 0.0, 0, 0
        cities, _ = await game.get_cities()
        if len(self._stack):
            self._stack.refresh(cities, await read_units(game))
        ordered = sorted(cities, key=lambda c: c.city_id)
        if ordered:
            start = turn % len(ordered)  # Rotate, so the same cities aren't always read first.
            ordered = ordered[start:] + ordered[:start]
        ordered.sort(key=lambda c: not self._threatened(c))  # Threatened cities first.
        spent = 0.0
        tried = 0
        count = 0
        cheapest_short = float("inf")  # The lowest price that was out of reach.
        for city in ordered[:MAX_CITY_SCANS]:
            if bought + count >= self.settings.max_purchases or attempts + tried >= MAX_ATTEMPTS:
                break
            options = {
                o.item_name: o
                for o in await game.list_city_production(city.city_id)
                if o.category in ("BUILDING", "UNIT") and not o.is_repair
            }
            for category, item, estimate in self._candidates(city, options):
                if self._refused.get(("rule", item), -1) > turn:
                    continue
                if self._refused.get((category[0], city.city_id, item), -1) > turn:
                    continue
                if self._stack.is_blocked(city.city_id, item):
                    notes.append(f"{item} in {city.name} blocked by a unit on the city tile")
                    continue
                price = self._costs.get(item, estimate)
                if self._sink_cap is not None and price > self._sink_cap:
                    continue  # Saving for a great person: only a cheap buy may go first.
                if price > budget - spent:
                    cheapest_short = min(cheapest_short, price)
                    notes.append(
                        f"{item} in {city.name} needs about {price}, have {budget - spent:.0f}"
                    )
                    continue
                tried += 1
                result = await game.purchase_item(city.city_id, category, item, _FAITH)
                if "PURCHASED" in result and action_succeeded(result):
                    paid = _PAID.search(result)
                    cost = int(paid.group(1)) if paid else price
                    self._costs[item] = cost
                    spent += cost
                    count += 1
                    sinks.append(f"{item} in {city.name} for {cost}")
                    log.info("Faith bought %s in %s for %d faith", item, city.name, cost)
                    break
                if self._stack.record(city.city_id, item, result):
                    # The unit on the city tile decides, not a timer: see `StackBlocks`.
                    log.info("Can't buy %s in %s with faith: %s", item, city.name, result[:120])
                    notes.append(f"{item} in {city.name} blocked by a unit on the city tile")
                    continue
                quoted = _QUOTED.search(result)
                if quoted:
                    self._costs[item] = int(quoted.group(1))
                else:
                    # The game's rules, not the price, refuse it (no card or dedication yet).
                    self._refused[("rule", item)] = turn + RULE_WAIT
                self._refused[(category[0], city.city_id, item)] = turn + REFUSED_WAIT
                log.info("Can't buy %s in %s with faith: %s", item, city.name, result[:120])
                notes.append(f"{item} in {city.name} refused")
                if attempts + tried >= MAX_ATTEMPTS:
                    break
        if not tried and not any(self._threatened(c) for c in ordered):
            # Nothing to try: skip the lists until the faith covers the cheapest candidate that
            # was short, or a few turns when none was offered at all.
            self._sink_gate = (turn + SINK_WAIT, cheapest_short)
        if not count and not notes:
            notes.append("no unit or Shrine or Temple offered in the cities read")
        return spent, count, tried
