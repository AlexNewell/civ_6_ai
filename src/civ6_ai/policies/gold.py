"""Spend banked gold, with no model calls.

Runs after `CityProductionPolicy`. Each turn it keeps a reserve and spends what is above it, in
this order, until the turn's bounds or the gold run out:

1. Upgrade combat units the game says can upgrade now (the game's own check covers tech, gold
   and strategic resources). Units next to a hostile unit go first, then the most outdated.
2. Buy units, buildings and great people by return (`valuation`, added after Run 12, where
   nothing was bought on 71% of turns above the reserve and the saving target was whatever
   production ranked first, a 1,015 gold building valued at 1.0). Every unthreatened city's list
   is read and each option valued: yields per turn (production's own tables, weighted by
   `ProductionWeights`) over the turns of game left, plus time saved when the city is already
   building it. Options are bought in ROI order down to a floor that falls as gold idles. The
   unaffordable option with the best ROI at the turn it would be reached is the saving target,
   and a lesser buy goes first only if buying it costs less than the delay it causes. A great
   person patronised with gold is one more option (the faith policy does the same with faith,
   and leaves Great Prophets to itself). Defence for a threatened city, no Builder while one is
   idle, and one Builder, Settler or Trader a turn are kept from production's ranking. Every
   purchase logs a `valuation` line with its value, cost, ROI, the floor and its basis. When
   gold is below half of the cheapest price seen in the last three turns the lists are not read.
3. Buy tiles, resource tiles first. A tile with no resource is bought only when it costs at most
   half of what is left to spend. With `tile_valuation` on (issue 87) tiles are instead valued by
   the owner's rules (`valuation.value_tile_purchase`: a yield the city lacks, an improvable
   resource, a boost, contested land) and bought in ROI order.

Reserve: `RESERVE_FLOOR` plus `RESERVE_PER_CITY` for each city, lowered by `INCOME_RELIEF` per
gold of income (never below `RESERVE_MIN`), plus ten turns of any deficit, capped at
`RESERVE_CAP`. Run 8 idled with gold above a reserve of 215 on 73% of turns, so the old
60 plus 20 per city was too high. The floor is about one emergency Warrior. A high income
refills a spent reserve quickly. The deficit term keeps a negative income from emptying the
treasury.

Saving: when the best upgrade is out of reach but income closes the gap within `SAVE_TURNS`
turns, it becomes the saving target (the best-return item, step 2, is saved for by value, with no
turn cutoff). While there
is one, tiles and other purchases wait (a threatened city's defence does not), so small buys do
not keep the gold below one price. A target further away is not saved for.

Threatened cities (roadmap 1.35, see `defence.DefenceState`): while any city is threatened the
reserve drops to `THREAT_DEFICIT_TURNS` turns of any deficit (zero with income), so war chests
are spent, not banked. Units in or beside a threatened city upgrade first, a threatened city
buys only defenders and walls, and the bounds below widen to `THREAT_MAX_UPGRADES` and one
purchase per threatened city.

Under threat a purchase is ordered by strength, since gold buys the unit now and build time does
not matter: the strongest defender the game offers and the budget covers comes first. A land unit
cannot be bought while another stands on the city tile (Run 10 left 1226 gold unspent in a
threatened city that way). The policy then moves the occupants to a safe adjacent tile and buys:
see `_make_room`.

Bounds per turn: 3 upgrades, 3 purchases (one per city, since the game allows one per city per
turn), 2 tiles, and 8 calls that the game refuses. A refusal is logged and the next candidate is
tried. Costs come from the game's reads, and each spend is taken off the budget at its listed
cost, so the reserve holds even if a later read is stale.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from civ6_ai.placement import PlanBook, last_turn_for
from civ6_ai.policies import valuation
from civ6_ai.policies.builder import is_builder
from civ6_ai.policies.game_data import COMBAT_UNITS, UPGRADES
from civ6_ai.policies.military import holds_tile
from civ6_ai.policies.production import (
    _CAMP_CITY_REACH,
    ProductionContext,
    ProductionWeights,
    build_context,
    camp_unit_value,
    canonical,
    defence_strength,
    is_defence,
    rank_production,
    score,
)
from civ6_ai.policies.secondary import _GP_WEIGHTS, SecondaryWeights
from civ6_ai.policies.settling import SCOUT
from civ6_ai.stacking import StackBlocks
from civ6_ai.state import City, Unit
from civ6_ai.state import read_area as _read_area
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_empire as _read_empire
from civ6_ai.state import read_production as _read_production
from civ6_ai.state import read_research as _read_research
from civ6_ai.state import read_threats as _read_threats
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.reader import hex_distance

log = logging.getLogger(__name__)

RESERVE_FLOOR = 40
RESERVE_PER_CITY = 10
RESERVE_MIN = 30  # Never lower than about one emergency Warrior, however high the income.
RESERVE_CAP = 150
INCOME_RELIEF = 3  # Reserve gold saved per gold of income: a high income refills a spent reserve.
DEFICIT_TURNS = 10
SAVE_TURNS = (
    8  # Most turns to save for the best purchase or upgrade before spending on lesser ones.
)
SLACK_TURNS = 2  # While saving, lesser buys may delay the target by this many turns of income.
MAX_UPGRADES = 3
MAX_PURCHASES = 3
MAX_TILES = 2
MAX_REFUSALS = 8
THREAT_DEFICIT_TURNS = 2  # The reserve under threat: enough that a deficit cannot disband units.
THREAT_MAX_UPGRADES = 5
MAX_CLEARED_PER_CITY = 2  # Units moved off one city tile to make room for a purchase.
MAX_CLEARED_PER_TURN = 4
SCAN_AGE = 3  # Turns the cheapest price seen holds the scan off while gold is far below it.
SCAN_MARGIN = 0.5  # ... and gold must be below this share of that price (prices only fall slowly).
GP_MIN_BUDGET = 150  # Gold above the reserve before the great people list is first read.
GP_REFRESH = 4  # Turns the great people list is reused while none is in reach.
_PROPHET = "Great Prophet"  # Left to the faith policy: it needs a religion to be founded.
_UNBUYABLE = 2_000_000_000  # The game reports INT_MAX (or near) for a person who can't be bought.
_PURCHASABLE = ("UNIT", "BUILDING")
_ONE_A_TURN = ("UNIT_BUILDER", "UNIT_SETTLER", "UNIT_TRADER")  # Never two in one turn.
_NAME = re.compile(r"[A-Z0-9_]+")
_RESOURCE_RANK = {"luxury": 0, "strategic": 1, "bonus": 2}


def reserve_for(num_cities: int, gold_per_turn: float = 0.0) -> int:
    """Gold never spent. See the module docstring for why each part is there."""
    deficit = DEFICIT_TURNS * max(0.0, -gold_per_turn)
    base = RESERVE_FLOOR + RESERVE_PER_CITY * max(0, num_cities)
    relief = INCOME_RELIEF * max(0.0, gold_per_turn)
    # Income lowers the reserve, but never below the minimum (or the base, when that is lower).
    held = max(min(base, RESERVE_MIN), base - relief)
    return int(min(RESERVE_CAP, held + deficit))


class GoldGame(Protocol):
    """What the policy needs from civ6-mcp's `GameState`, on top of what the readers use."""

    async def upgrade_unit(self, unit_id: int) -> str: ...
    async def purchase_item(
        self, city_id: int, item_type: str, item_name: str, yield_type: str = "YIELD_GOLD"
    ) -> str: ...
    async def patronize_great_person(self, individual_id: int, yield_type: str = ...) -> str: ...
    async def get_great_people(self) -> list[Any]: ...
    async def get_purchasable_tiles(self, city_id: int) -> Any: ...
    async def get_tech_civics(self) -> Any: ...
    async def purchase_tile(self, city_id: int, x: int, y: int) -> str: ...
    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...


@dataclass
class _Option:
    """A valued purchase: an item in a city, or a great person (`person`)."""

    priced: Any  # `valuation.Priced`
    city: City | None
    option: Any
    context: ProductionContext | None
    person: Any = None


def _ok_any(result: str) -> bool:
    return isinstance(result, str) and not result.startswith(("Error", "ERR:"))


def _ok(result: str, marker: str) -> bool:
    """The game replies `OK:...` for some buys and a bare `UPGRADED|...` for an upgrade."""
    return (
        isinstance(result, str)
        and (result.startswith("OK:") or result.startswith(marker))
        and marker in result
    )


def _strength(unit_type: str) -> int:
    stats = COMBAT_UNITS.get(unit_type)
    return max(stats[0], stats[1]) if stats else 0


def threat_reserve(gold_per_turn: float = 0.0) -> int:
    """The reserve while a city is threatened: nothing banked beyond a deficit's next turns."""
    return int(THREAT_DEFICIT_TURNS * max(0.0, -gold_per_turn))


def strongest_first(ranked: list, budget: int) -> list:
    """Reorder a threatened city's defence so the strongest unit the budget covers comes first.

    Production's ranking divides by build time, which a purchase does not wait for. Units trade
    places among themselves (affordable, then strongest, then cheapest) and buildings (walls) keep
    their slots, so the ranking still decides walls against a unit.
    """
    slots = [i for i, o in enumerate(ranked) if o.category == "UNIT"]
    units = sorted(
        (ranked[i] for i in slots),
        key=lambda o: (
            (o.gold_cost or 0) > budget,
            -defence_strength(o.item),
            o.gold_cost or 0,
            o.item,
        ),
    )
    result = list(ranked)
    for slot, unit in zip(slots, units, strict=True):
        result[slot] = unit
    return result


def upgrade_order(
    units: list[Unit],
    hostile: list[tuple[int, int]],
    guarded: list[tuple[int, int]] | None = None,
) -> list[Unit]:
    """Upgradable combat units, those nearest a hostile unit first, then the most outdated.

    Units in or beside a city in `guarded` (the threatened cities' tiles) come before all others.
    """
    guarded = guarded or []

    def key(unit: Unit) -> tuple:
        near = min((hex_distance(unit.x, unit.y, x, y) for x, y in hostile), default=10_000)
        gap = _strength(unit.upgrade_to or "") - _strength(unit.unit_type)
        holds = any(hex_distance(unit.x, unit.y, x, y) <= 1 for x, y in guarded)
        return (not holds, near, -gap, unit.upgrade_cost, unit.unit_id)

    return sorted((u for u in units if u.is_combat and u.can_upgrade and u.upgrade_to), key=key)


@dataclass
class _Turn:
    """What is left to spend this turn, and what has been done."""

    budget: int
    refusals: int = 0
    cleared: int = 0  # Units moved off a city tile to make room for a purchase.
    roomed: set[int] = field(default_factory=set)  # Cities whose tile was tried this turn.
    upgrades: int = 0
    purchases: int = 0
    tiles: int = 0
    income: float = 0.0
    target: str | None = None  # What gold is being saved for: it holds back lesser spending.
    target_defence: bool = False  # The target is defence, so a threatened city may still buy.
    slack: float = 0.0  # Gold lesser buys may still take while saving (see `SLACK_TURNS`).
    key: str | None = None  # Identifies the target across turns, for `delay`.
    delay: dict[str, float] = field(default_factory=dict)  # Gold lesser buys took per target.
    now: int = 0  # The game turn, and the turn the game ends, for the horizon of a valuation.
    last: int = 330
    roi_target: Any = None  # `valuation.Priced` being saved for by return, not by an upgrade.

    def spend(self, cost: int) -> None:
        """Take a spend off the budget, and off the slack when it is made while saving."""
        self.budget -= cost
        if self.target is not None and self.key is not None:
            self.slack -= cost
            self.delay[self.key] = self.delay.get(self.key, 0.0) + cost

    def save_for(self, name: str, cost: int) -> bool:
        """Make `name` the saving target when income closes the gap within `SAVE_TURNS` turns."""
        gap = cost - self.budget
        if (
            self.target is not None
            or self.income <= 0
            or gap <= 0
            or gap / self.income > SAVE_TURNS
        ):
            return False
        self.target = (
            f"{name} (cost {cost}, {gap / self.income:.0f} turns at {self.income:.0f} a turn)"
        )
        self.key = f"{name}:{cost}"
        # The allowance is for the whole wait, not for each turn of it: gold already spent on
        # lesser things while saving for this target counts against it.
        self.slack = max(0.0, SLACK_TURNS * self.income - self.delay.get(self.key, 0.0))
        return True

    def save_by_return(self, item: Any, wait: float) -> None:
        """Make a valued item the saving target. Its worth was checked (`valuation.pick_target`)."""
        self.roi_target = item
        self.target = (
            f"{item.label} (cost {item.cost}, value {item.value:.0f}, roi {item.roi:.2f}, "
            f"{wait:.0f} turns at {self.income:.0f} a turn)"
        )
        self.key = f"{item.label}:{item.cost}"
        self.slack = max(0.0, SLACK_TURNS * self.income - self.delay.get(self.key, 0.0))

    @property
    def refused_out(self) -> bool:
        return self.refusals >= MAX_REFUSALS


class GoldPolicy:
    """A turn handler that spends gold above the reserve."""

    name = "gold"

    def __init__(
        self,
        weights: ProductionWeights | None = None,
        defence: Any = None,
        plans: PlanBook | None = None,
        builder_demand: Any = None,
        expansion: Any = None,
        tile_valuation: bool = False,
    ) -> None:
        # Issue 87: value tile buys by the owner's rules (`valuation.value_tile_purchase`) and
        # buy them in ROI order. Off by default: tiles then follow the old resource-first order.
        self.tile_valuation = tile_valuation
        self.contested_tiles: set[tuple[int, int]] = set()  # Tiles marked contested by hand.
        # Foreign (rival and city-state) city tiles, as a callable so it is read fresh each turn:
        # `SettlingPolicy.foreign_city_tiles` (issue 338). A purchasable tile near one that is
        # worth taking counts as contested (`valuation.is_contested`).
        self.foreign_cities: Callable[[], list[tuple[int, int]]] | None = None
        self.boost_values: dict[tuple[int, int], float] = {}  # Boost work (issue 322) fills this.
        self.builder_demand = builder_demand  # `builder.BuilderDemand`, or None.
        self.expansion = expansion  # `expansion.ExpansionState`, or None (issue 284).
        self._stack = StackBlocks()  # Purchases a unit on the city tile blocks.
        self._delay: dict[str, float] = {}  # Lesser spending while saving, per target.
        self._hostile: list[tuple[int, int]] = []  # Hostile unit tiles, read for the upgrades.
        # Tiles production wants for a district or wonder (see `placement`): bought when they fit
        # the budget, else the saving target.
        self.plans = plans if plans is not None else PlanBook()
        self.weights = weights or ProductionWeights()
        self.defence = defence  # The threat state (`defence.DefenceState`), or None.
        self._cheapest: tuple[int, int] | None = None  # (turn, price) of the cheapest option.
        self._people: tuple[int, list] | None = None  # (turn read, great people).
        self._refused_people: dict[int, int] = {}  # Person id -> gold price when refused.

    async def run(self, game: GoldGame) -> None:
        # The overview alone settles whether there is anything to spend. The full empire read is
        # five more game calls, so it waits until there is (Run 3 profile: most turns have none).
        overview = await game.get_game_overview()
        threatened = self.defence is not None and self.defence.any
        reserve = self._reserve(overview.num_cities, overview.gold_per_turn)
        budget = int(overview.gold) - reserve
        if budget <= 0:
            log.info(
                "Gold %d is at or below the reserve %d, nothing to spend", overview.gold, reserve
            )
            return
        empire = await _read_empire(game)
        reserve = self._reserve(empire.num_cities, empire.gold_per_turn)
        budget = int(empire.gold) - reserve
        turn = _Turn(
            budget,
            income=empire.gold_per_turn,
            delay=self._delay,
            now=empire.turn,
            last=last_turn_for(empire.game_speed, empire.max_turns),
        )
        notes: list[str] = []
        log.info("Gold %d, reserve %d, %d to spend", empire.gold, reserve, budget)
        if threatened:
            log.info(
                "defence: %d threatened cities, so the gold reserve is %d, not %d",
                len(self.defence.cities),
                reserve,
                reserve_for(empire.num_cities, empire.gold_per_turn),
            )
        cities = sorted(await _read_cities(game), key=lambda c: (not self._marked(c), c.city_id))
        units = list(await _read_units(game))
        await self._upgrade(game, turn, units, notes, cities)
        await self._plan_tiles(game, turn, empire.turn, notes)
        await self._buy(game, turn, empire, cities, units, notes)
        await self._tiles(game, turn, cities, notes, units)
        # Forget the allowance of a target no longer being saved for.
        self._delay = {k: v for k, v in self._delay.items() if k == turn.key}
        spent = budget - turn.budget
        log.info("gold_spent turn total: %d of %d to spend", spent, budget)
        if turn.target is not None:
            log.info("Gold %d, reserve %d: saving toward %s", empire.gold, reserve, turn.target)
        if spent == 0:
            log.info(
                "Gold %d is above the reserve %d, nothing bought: %s",
                empire.gold,
                reserve,
                "; ".join(notes) or "nothing worth buying",
            )
        else:
            log.info(
                "Spent %d gold this turn (%d upgrades, %d purchases, %d tiles)",
                spent,
                turn.upgrades,
                turn.purchases,
                turn.tiles,
            )

    def _reserve(self, num_cities: int, gold_per_turn: float) -> int:
        if self.defence is not None and self.defence.any:
            return threat_reserve(gold_per_turn)
        return reserve_for(num_cities, gold_per_turn)

    def _marked(self, city: City) -> bool:
        """True if hostile units are near the city or the threat state marks it."""
        return city.is_threatened or (
            self.defence is not None and self.defence.is_threatened(city.city_id)
        )

    def _max_upgrades(self) -> int:
        marked = self.defence is not None and self.defence.any
        return THREAT_MAX_UPGRADES if marked else MAX_UPGRADES

    async def _upgrade(
        self,
        game: GoldGame,
        turn: _Turn,
        units: list[Unit],
        notes: list[str],
        cities: list[City] | None = None,
    ):
        try:
            hostile = [(t.x, t.y) for t in await _read_threats(game) if t.hostile]
        except ConnectionError:
            raise
        except Exception:
            log.exception("Threat read failed, upgrading without threat order")
            hostile = []
        self._hostile = hostile
        guarded = [(c.x, c.y) for c in cities or [] if self._marked(c)]
        candidates = upgrade_order(units, hostile, guarded if self.defence is not None else None)
        if not candidates:
            notes.append(f"no unit can upgrade{self._upgrade_gap(units)}")
            return
        for unit in candidates:
            if turn.upgrades >= self._max_upgrades() or turn.refused_out:
                break
            if unit.upgrade_cost > turn.budget:
                notes.append(f"{unit.unit_type} upgrade costs {unit.upgrade_cost}")
                turn.save_for(f"{unit.unit_type} upgrade", unit.upgrade_cost)
                continue
            result = await self._call(game.upgrade_unit(unit.unit_id))
            if _ok(result, "UPGRADED"):
                turn.spend(unit.upgrade_cost)
                turn.upgrades += 1
                reason = "next to a hostile unit" if self._near(unit, hostile) else "outdated"
                log.info(
                    "Upgraded %s to %s for %d (%s)",
                    unit.unit_type,
                    unit.upgrade_to,
                    unit.upgrade_cost,
                    reason,
                )
                self._note_upgrade(unit, cities or [], f"{unit.unit_type} to {unit.upgrade_to}")
            else:
                turn.refusals += 1
                log.info("Upgrade of %s refused: %s", unit.unit_type, str(result)[:120])

    @staticmethod
    def _upgrade_gap(units: list[Unit]) -> str:
        """Why nothing upgrades, from what the unit list shows.

        The game's own check (`CanStartCommand` for UPGRADE, in its strict form) covers tech,
        strategic resources, the unit's moves and its tile, not gold. A unit that moved or fought
        earlier in the turn has no moves, and one whose upgrade needs a tech the empire lacks is
        refused. Run 10 logged only "no unit can upgrade" 217 times, which hid which of the two.
        """
        combat = [u for u in units if u.is_combat and u.unit_type in UPGRADES]
        if not combat:
            return ""
        spent = sum(1 for u in combat if u.moves <= 0)
        types = sorted({u.unit_type.removeprefix("UNIT_") for u in combat})
        return f" ({len(combat)} units with an upgrade path, {spent} with no moves left: {types})"

    def _note_upgrade(self, unit: Unit, cities: list[City], text: str) -> None:
        if self.defence is None:
            return
        for city in cities:
            if self.defence.is_threatened(city.city_id) and (
                hex_distance(unit.x, unit.y, city.x, city.y) <= 1
            ):
                self.defence.note(city.city_id, f"gold: upgraded {text}")

    @staticmethod
    def _near(unit: Unit, hostile: list[tuple[int, int]]) -> bool:
        return any(hex_distance(unit.x, unit.y, x, y) <= 4 for x, y in hostile)

    async def _buy(self, game, turn, empire, cities: list[City], units, notes: list[str]):
        """Threatened cities buy defence first (as before). The rest compete on return.

        Every unthreatened city's list is read, each option is valued (`valuation`), and the
        affordable ones are bought in order of ROI down to the floor, which falls as gold idles.
        The unaffordable option with the best ROI at the turn it would be reached becomes the
        saving target; a lesser buy goes first only if `valuation.buy_now_beats_saving` says so.
        """
        building = {c.city_id: c.building for c in cities if c.building}
        self._stack.refresh(cities, units)
        demand = self.builder_demand
        limit = MAX_PURCHASES
        if self.defence is not None:
            limit = max(limit, len(self.defence.cities))
        threatened = [c for c in cities if self._may_buy_while_saving(c)]
        pool: list[_Option] = []
        camp: list[tuple[float, int, City, Any]] = []  # Camp units on offer: value, price, city.
        why: list[str] = []  # Why no camp unit is on offer (issue 279), for the log.
        if not threatened and self._scan_skipped(turn, notes):
            return
        for city in cities:
            if turn.refused_out:
                break
            try:
                options = await _read_production(game, city.city_id)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Production read failed for %s", city.name)
                continue
            others = [item for cid, item in building.items() if cid != city.city_id]
            context = build_context(
                empire,
                units,
                others,
                cities,
                self.defence,
                idle_builders=demand.idle if demand is not None else 0,
                spare_jobs=demand.spare_jobs if demand is not None else None,
            )
            if self.expansion is not None:  # The same two reads production scores a settler by.
                context = replace(
                    context,
                    sites_ok=self.expansion.sites_ok(empire.turn),
                    held_settlers=self.expansion.held_settlers,
                )
            ranked = rank_production(city, options, context, self.weights)
            if context.camp_units > 0:
                if city in threatened:
                    why.append(self._defence_note(city, ranked, turn))
                else:
                    camp += self._camp_candidates(city, ranked, context, turn, why)
            if city in threatened:
                if turn.purchases >= limit or turn.budget <= 0:
                    continue
                # Defenders and walls only. A threatened city does not buy a library.
                ranked = strongest_first([o for o in ranked if is_defence(o)], turn.budget)
                await self._buy_in(game, turn, city, ranked, context, [], notes, units)
                continue
            for option in ranked:
                if score(option, city, context, self.weights) <= 0:
                    break
                if (
                    option.category not in _PURCHASABLE
                    or option.is_repair
                    or option.gold_cost is None
                    or option.gold_cost <= 0
                    or not _NAME.fullmatch(option.item)
                ):
                    continue
                if self._stack.is_blocked(city.city_id, option.item):
                    notes.append(f"{city.name} {option.item} blocked by a unit on the city tile")
                    continue
                worth = valuation.value_option(option, city, context, self.weights)
                if worth is None:
                    continue
                label = f"{city.name} {option.item}"
                priced = valuation.price(label, option.gold_cost, worth, turn.now, turn.last)
                pool.append(_Option(priced, city, option, context))
        await self._buy_camp_unit(game, turn, camp, notes, limit, why)
        pool += await self._great_people(game, turn)
        self._note_scan(turn, pool)
        await self._spend_by_return(game, turn, pool, notes, limit)

    @staticmethod
    def _defence_note(city: City, ranked: list, turn: Any) -> str:
        """Why a threatened city gives the camp force no purchase: it buys defence, and whether
        any defence is affordable (issue 284: 'buys defence' was logged on 46 turns with nothing
        bought)."""
        prices = [
            o.gold_cost
            for o in ranked
            if is_defence(o) and not o.is_repair and o.gold_cost is not None and o.gold_cost > 0
        ]
        if not prices:
            return f"{city.name} is threatened and buys defence (none on offer to buy)"
        cheapest = min(prices)
        if cheapest > turn.budget:
            return (
                f"{city.name} is threatened and buys defence, but the cheapest costs {cheapest} "
                f"and {turn.budget} is to spend, so nothing is affordable"
            )
        return f"{city.name} is threatened and buys defence (cheapest {cheapest} is affordable)"

    def _camp_candidates(
        self,
        city: City,
        ranked: list,
        context: ProductionContext,
        turn: _Turn,
        why: list[str] | None = None,
    ) -> list[tuple[float, int, City, Any]]:
        """Units this city could buy for the camp force, by matchup (issue 275).

        Dear ones are kept in the list (issue 279): iteration 2 had 40 to 140 gold against prices
        of 160 to 210, so nothing was ever affordable, and the log never said so. The reasons a
        city offers nothing go to `why`.
        """
        found = []
        why = [] if why is None else why
        if context.camp_target is not None and (
            hex_distance(city.x, city.y, *context.camp_target) > _CAMP_CITY_REACH
        ):
            why.append(f"{city.name} is too far from the camp to join the force")
            return found
        skipped = 0
        for option in ranked:
            if (
                option.category != "UNIT"
                or option.item not in COMBAT_UNITS
                or option.is_repair
                or option.gold_cost is None
                or option.gold_cost <= 0
                or not _NAME.fullmatch(option.item)
                or self._stack.is_blocked(city.city_id, option.item)
                or score(option, city, context, self.weights) <= 0
            ):
                skipped += 1 if option.category == "UNIT" and option.item in COMBAT_UNITS else 0
                continue
            value = camp_unit_value(
                option.item, COMBAT_UNITS[option.item][4], float(option.cost), context
            )
            if context.camp_wants == "melee" and COMBAT_UNITS[option.item][4] == "RANGED":
                continue  # Only a melee unit takes the tile the force is short of.
            if value is None:
                value = defence_strength(option.item) / 100.0  # No defender seen: by strength.
            if value > 0:
                found.append((value, option.gold_cost, city, option))
        if not found:
            why.append(
                f"{city.name} offers no unit worth buying for the camp "
                f"({skipped} combat units scored 0 or could not be bought)"
            )
        return found

    async def _buy_camp_unit(
        self,
        game: GoldGame,
        turn: _Turn,
        candidates: list[tuple[float, int, City, Any]],
        notes: list[str],
        limit: int,
        why: list[str] | None = None,
    ) -> None:
        """While a camp goal is unmet, buy the best matchup unit gold allows.

        Iteration 1 of the turn-100 test had gold above the reserve on 41 of 45 turns with the camp
        force short and nothing bought: a unit's return on gold was under the floor. The camp
        plan's own shortfall is the case, so one unit a turn is bought, the best value first.
        Iteration 2 bought none either: every camp unit cost more than the gold on hand (160 to
        210 against 24 to 143). Now the reason is logged, and when income closes the gap soon the
        cheapest camp unit becomes the saving target, which holds back lesser buys.
        """
        if turn.purchases >= limit or turn.refused_out:
            why = [*(why or []), "no purchases left this turn"]
        if not candidates or turn.purchases >= limit or turn.refused_out:
            if why is not None and (why or candidates):
                log.info("No camp unit bought: %s", "; ".join(why))
            return
        affordable = [c for c in candidates if c[1] <= turn.budget]
        if not affordable:
            _, price, city, option = min(candidates, key=lambda c: (c[1], c[2].city_id))
            saving = turn.save_for(f"{city.name} {option.item} (camp force)", price)
            log.info(
                "No camp unit bought: the cheapest, %s in %s, costs %d and %d is to spend%s",
                option.item, city.name, price, turn.budget,
                "; saving for it" if saving else "; income is too slow to save for it",
            )  # fmt: skip
            return
        for value, price, city, option in sorted(
            affordable, key=lambda c: (-c[0], c[1], c[2].city_id)
        ):
            result = await self._call(
                game.purchase_item(city.city_id, option.category, option.item)
            )
            if _ok(result, "PURCHASED"):
                turn.spend(price)
                turn.purchases += 1
                log.info(
                    "Bought %s in %s for %d (camp force, matchup %.2f)",
                    option.item, city.name, price, value,
                )  # fmt: skip
                if self.defence is not None:
                    self.defence.note(city.city_id, f"gold: bought {option.item} for the camp")
                return
            turn.refusals += 1
            if self._stack.record(city.city_id, option.item, result):
                notes.append(f"{city.name} {option.item} blocked by a unit on the city tile")
            log.info(
                "Camp purchase of %s in %s refused: %s", option.item, city.name, str(result)[:120]
            )
            return

    def _scan_skipped(self, turn: _Turn, notes: list[str]) -> bool:
        """True when gold is far below the cheapest price seen a moment ago.

        Reading every city's list costs a call each. The price only falls as a city's own
        production progresses, so a short memory with a wide margin is safe; any threat, or gold
        reaching the margin, reads the lists again at once.
        """
        seen = self._cheapest
        if seen is None or turn.now - seen[0] > SCAN_AGE or turn.now < seen[0]:
            return False
        if turn.budget >= SCAN_MARGIN * seen[1]:
            return False
        notes.append(
            f"gold {turn.budget} is below half of the cheapest price seen ({seen[1]}) "
            f"on turn {seen[0]}, lists not read"
        )
        return True

    def _note_scan(self, turn: _Turn, pool: list) -> None:
        prices = [p.priced.cost for p in pool if not p.priced.valuation.always]
        self._cheapest = (turn.now, min(prices)) if prices else None

    async def _great_people(self, game: GoldGame, turn: _Turn) -> list:
        """Great people the game lets us patronize with gold, valued by class and era."""
        read = getattr(game, "get_great_people", None)
        if read is None or (turn.budget < GP_MIN_BUDGET and self._people is None):
            return []
        cached = self._people
        if (
            cached is not None
            and 0 <= turn.now - cached[0] < GP_REFRESH
            and not any(0 < p.gold_cost <= turn.budget for p in cached[1])
        ):
            people = cached[1]
        else:
            try:
                people = list(await read())
            except ConnectionError:
                raise
            except Exception:
                log.exception("Great people read failed")
                return []
            self._people = (turn.now, people)
        out = []
        weights = SecondaryWeights()
        for person in people:
            attr = _GP_WEIGHTS.get(person.class_name)
            if (
                person.claimant != "Unclaimed"
                or not 0 < person.gold_cost < _UNBUYABLE
                or person.class_name == _PROPHET
                or attr is None
                or getattr(weights, attr) <= 0
                or self._refused_people.get(person.individual_id) == person.gold_cost
            ):
                continue
            worth = valuation.great_person_valuation(person, weights, turn.now, turn.last)
            label = f"{person.individual_name} ({person.class_name})"
            priced = valuation.price(label, person.gold_cost, worth, turn.now, turn.last)
            out.append(_Option(priced, None, None, None, person))
        return out

    async def _spend_by_return(
        self, game: GoldGame, turn: _Turn, pool: list, notes: list[str], limit: int
    ) -> None:
        """Buy in ROI order down to the floor, keeping gold for a better item out of reach."""
        if not pool:
            return
        reach = [p.priced for p in pool if p.priced.cost > turn.budget]
        if turn.target is None:
            found = valuation.pick_target(reach, turn.budget, turn.income, turn.now, turn.last)
            if found is not None:
                turn.save_by_return(*found)
        bought_cities: set[int] = set()
        bought_kinds: set[str] = set()
        bought_items: list[str] = []
        skipped: list[str] = []
        for entry in sorted(pool, key=lambda p: (-p.priced.roi, p.priced.cost, p.priced.label)):
            priced = entry.priced
            if turn.purchases >= limit or turn.refused_out or turn.budget <= 0:
                break
            if priced.cost > turn.budget:
                continue
            kind = canonical(entry.option.item) if entry.option is not None else ""
            if (entry.city is not None and entry.city.city_id in bought_cities) or (
                kind in _ONE_A_TURN and kind in bought_kinds
            ):
                continue
            if bought_items and entry.option is not None:
                # What was bought this turn counts: one scout wanted is not bought by each city.
                context = entry.context
                for item in bought_items:
                    context = context.after_choosing(item)
                worth = valuation.value_option(entry.option, entry.city, context, self.weights)
                if worth is None:
                    continue
                priced = valuation.price(priced.label, priced.cost, worth, turn.now, turn.last)
            bar = valuation.floor(turn.budget, turn.income)
            if not priced.valuation.always:
                if priced.roi < bar:
                    skipped.append(f"{priced.label} roi {priced.roi:.2f} < floor {bar:.2f}")
                    continue
                if turn.roi_target is not None:
                    target = turn.roi_target
                    if not valuation.buy_now_beats_saving(
                        priced.value, priced.cost, target.value, target.cost - turn.budget
                    ):
                        skipped.append(
                            f"{priced.label} roi {priced.roi:.2f} waits for {target.label}"
                        )
                        continue
                elif turn.target is not None and priced.cost > turn.slack:
                    skipped.append(f"{priced.label} waits for {turn.target}")
                    continue
            entry.priced = priced
            if await self._purchase(game, turn, entry, bar, notes):
                if entry.city is not None:
                    bought_cities.add(entry.city.city_id)
                bought_kinds.add(kind)
                if entry.option is not None:
                    bought_items.append(entry.option.item)
        notes += skipped[:4]
        if not turn.purchases:
            best = sorted(reach, key=lambda p: -p.roi)[:4]
            notes += [
                f"{p.label} costs {p.cost} (roi {p.roi:.2f}, value {p.value:.0f})" for p in best
            ]
            if len(reach) > len(best):
                notes.append(f"{len(reach) - len(best)} more cost more than {turn.budget}")

    async def _purchase(
        self, game: GoldGame, turn: _Turn, entry: "_Option", bar: float, notes: list[str]
    ) -> bool:
        priced = entry.priced
        audit = (
            f"value={priced.value:.0f} cost={priced.cost} roi={priced.roi:.2f} floor={bar:.2f} "
            f"basis={priced.valuation.basis!r} target={turn.target or 'none'!r}"
        )
        if entry.person is not None:
            person = entry.person
            result = await self._call(
                game.patronize_great_person(person.individual_id, "YIELD_GOLD")
            )
            if "PATRONIZED" in str(result) and _ok_any(result):
                turn.spend(priced.cost)
                turn.purchases += 1
                self._people = None  # Prices change with every great person recruited.
                log.info("Bought %s for %d gold (great person)", priced.label, priced.cost)
                log.info(
                    "valuation turn=%d buy kind=great_person item=%r %s",
                    turn.now,
                    priced.label,
                    audit,
                )
                return True
            turn.refusals += 1
            self._refused_people[person.individual_id] = person.gold_cost
            log.info("Patronage of %s refused: %s", priced.label, str(result)[:120])
            return False
        city, option = entry.city, entry.option
        result = await self._call(game.purchase_item(city.city_id, option.category, option.item))
        if _ok(result, "PURCHASED"):
            turn.spend(priced.cost)
            turn.purchases += 1
            why = "threatened" if priced.valuation.always else "best return affordable"
            if self.defence is not None:
                self.defence.note(city.city_id, f"gold: bought {option.item} for {priced.cost}")
            log.info("Bought %s in %s for %d (%s)", option.item, city.name, priced.cost, why)
            log.info(
                "valuation turn=%d buy kind=%s city=%r item=%s %s",
                turn.now,
                option.category.lower(),
                city.name,
                option.item,
                audit,
            )
            return True
        turn.refusals += 1
        if self._stack.record(city.city_id, option.item, result):
            notes.append(f"{city.name} {option.item} blocked by a unit on the city tile")
        log.info("Purchase of %s in %s refused: %s", option.item, city.name, str(result)[:120])
        return False

    def _may_buy_while_saving(self, city: City) -> bool:
        """Only a threatened city's defence is bought while gold is saved for something else."""
        return self.defence is not None and self.defence.is_threatened(city.city_id)

    async def _buy_in(
        self,
        game: GoldGame,
        turn: _Turn,
        city: City,
        ranked: list,
        context: ProductionContext,
        bought: list[str],
        notes: list[str],
        units: list[Unit] | None = None,
    ) -> bool:
        tried = 0
        for option in ranked:
            if score(option, city, context, self.weights) <= 0:
                break
            if option.category not in _PURCHASABLE or option.is_repair:
                continue
            if option.gold_cost is None or option.gold_cost <= 0:
                continue
            if not _NAME.fullmatch(option.item):
                continue
            if option.gold_cost <= turn.budget and units is not None and self._marked(city):
                # Under threat, a unit on the city tile is moved aside rather than left to
                # block the one purchase that would help.
                await self._make_room(game, turn, city, option, units, notes)
            if self._stack.is_blocked(city.city_id, option.item):
                notes.append(f"{city.name} {option.item} blocked by a unit on the city tile")
                continue
            if option.gold_cost > turn.budget:
                notes.append(f"{city.name} {option.item} costs {option.gold_cost}")
                if tried == 0 and not is_defence(option) and not self._marked(city):
                    # The best item is out of reach: save for it if income gets there soon.
                    turn.save_for(f"{city.name} {option.item}", option.gold_cost)
                continue
            if tried >= 2 or turn.refused_out:
                break
            if turn.target is not None and not is_defence(option) and option.gold_cost > turn.slack:
                continue  # Saving toward a better item: only a buy within the slack goes first.
            tried += 1
            result = await self._call(
                game.purchase_item(city.city_id, option.category, option.item)
            )
            if _ok(result, "PURCHASED"):
                turn.spend(option.gold_cost)
                turn.purchases += 1
                bought.append(option.item)
                why = "threatened" if self._marked(city) else "top-ranked affordable item"
                if self.defence is not None:
                    self.defence.note(
                        city.city_id, f"gold: bought {option.item} for {option.gold_cost}"
                    )
                log.info(
                    "Bought %s in %s for %d (%s)", option.item, city.name, option.gold_cost, why
                )
                return True
            turn.refusals += 1
            if self._stack.record(city.city_id, option.item, result):
                notes.append(f"{city.name} {option.item} blocked by a unit on the city tile")
            log.info("Purchase of %s in %s refused: %s", option.item, city.name, str(result)[:120])
        return False

    async def _make_room(
        self,
        game: GoldGame,
        turn: _Turn,
        city: City,
        option: Any,
        units: list[Unit],
        notes: list[str],
    ) -> None:
        """Move the land units off a threatened city's tile so a land unit can be bought there.

        The game refuses a land unit while another stands on the tile (`CanStartCommand` says
        "Too many units of the same class in this location"), whatever its count. Only a unit
        purchase can be blocked, and only by units that hold a tile. Each occupant goes to the
        adjacent tile the city owns that is land, empty of our land units and of any foreign
        unit, farthest from hostile units and then on defensive ground. The block lifts by the
        usual rule (the units read no longer shows the unit on the tile), so nothing is forced:
        if no occupant can move (no moves left, no tile) the item stays blocked.
        """
        if option.category != "UNIT" or option.item not in COMBAT_UNITS:
            return
        if city.city_id in turn.roomed:
            return  # Tried already this turn, moved or not.
        turn.roomed.add(city.city_id)
        here = (city.x, city.y)
        occupants = [u for u in units if u.is_combat and holds_tile(u) and (u.x, u.y) == here]
        if not occupants:
            return  # Nothing blocks a soldier: a builder or a great person does not.
        if (
            len(occupants) > MAX_CLEARED_PER_CITY
            or turn.cleared + len(occupants) > MAX_CLEARED_PER_TURN
            or any(u.moves <= 0 for u in occupants)
        ):
            notes.append(
                f"{city.name}: {len(occupants)} units on the city tile, "
                "too many or none able to move aside"
            )
            return
        try:
            area = await _read_area(game, city.x, city.y, 1)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Area read failed for %s", city.name)
            return
        plans = self._room_plan(city, occupants, units, area)
        if plans is None:
            notes.append(f"{city.name}: no safe tile beside the city for its defenders")
            return
        for unit, (x, y) in plans:
            result = await self._call(game.move_unit(unit.unit_index, x, y))
            turn.cleared += 1
            left = str(result).startswith(("MOVING_TO", "OK")) and (
                f"now_at:{city.x},{city.y}" not in str(result)
            )
            if not left:
                turn.refusals += 1
                log.info(
                    "Moving %s off %s refused: %s", unit.unit_type, city.name, str(result)[:120]
                )
                return
            units[units.index(unit)] = replace(unit, x=x, y=y, moves=0.0)
            log.info("Moved %s off the city tile of %s to (%d,%d)", unit.unit_type, city.name, x, y)
            if self.defence is not None:
                self.defence.note(city.city_id, f"gold: moved {unit.unit_type} aside to buy")
        self._stack.release(city.city_id)

    def _room_plan(
        self, city: City, occupants: list[Unit], units: list[Unit], area: tuple
    ) -> list[tuple[Unit, tuple[int, int]]] | None:
        """A tile for each occupant, or None when there are not enough safe ones."""
        owner = next((t.owner_id for t in area if (t.x, t.y) == (city.x, city.y)), None)
        # A scout is not ordered by military, but the game still won't stack a land unit on it.
        taken = {(u.x, u.y) for u in units if holds_tile(u) or u.unit_type == SCOUT}
        ground = []
        for tile in area:
            if hex_distance(city.x, city.y, tile.x, tile.y) != 1 or (tile.x, tile.y) in taken:
                continue
            if tile.foreign_units or owner is None or tile.owner_id != owner:
                continue
            terrain = tile.terrain or ""
            if any(word in terrain for word in ("COAST", "OCEAN", "LAKE", "MOUNTAIN")):
                continue
            nearest = min(
                (hex_distance(tile.x, tile.y, x, y) for x, y in self._hostile), default=99
            )
            ground.append((-min(nearest, 3), not tile.hills, tile.x, tile.y))
        ground.sort()
        if len(ground) < len(occupants):
            return None
        weakest_first = sorted(occupants, key=lambda u: (defence_strength(u.unit_type), u.unit_id))
        return [(u, (g[2], g[3])) for u, g in zip(weakest_first, ground, strict=False)]

    async def _plan_tiles(self, game: GoldGame, turn: _Turn, now: int, notes: list[str]) -> None:
        """Buy the tile a district or wonder is waiting for, or make it the saving target."""
        if self.defence is not None and self.defence.any:
            return  # A threatened city's defence comes before any tile.
        if turn.target is not None:
            return  # An earlier saving target (an upgrade) comes first.
        bought_for: set[int] = set()
        for target in self.plans.current(now):
            if turn.refused_out:
                return
            if target.city_id in bought_for:
                continue  # One tile per city per turn.
            if target.cost <= turn.budget:
                result = await self._call(game.purchase_tile(target.city_id, target.x, target.y))
                self.plans.drop(target.city_id, target.item)  # Bought or refused, it is spent.
                if _ok(result, "TILE_PURCHASED"):
                    bought_for.add(target.city_id)
                    turn.spend(target.cost)
                    turn.tiles += 1
                    log.info(
                        "placement_plan city=%s item=%s tile=(%d,%d) action=bought cost=%d",
                        target.city_name,
                        target.item,
                        target.x,
                        target.y,
                        target.cost,
                    )
                else:
                    turn.refusals += 1
                    log.info(
                        "Planned tile (%d,%d) for %s refused: %s",
                        target.x,
                        target.y,
                        target.city_name,
                        str(result)[:120],
                    )
            elif turn.save_for(
                f"tile ({target.x},{target.y}) for {target.item} in {target.city_name}",
                target.cost,
            ):
                notes.append(f"saving for the {target.item} tile in {target.city_name}")

    async def _tiles_by_value(
        self, game: GoldGame, turn: _Turn, cities: list[City], notes: list[str], units=()
    ) -> None:
        """Tile buys by the owner's rules (issue 87): one more candidate list, bought by ROI.

        Runs after upgrades, planned tiles and purchases, from the budget they left (the reserve
        and any threatened city's defence were settled before), and obeys the saving target.
        """
        weights = self.weights
        has_builder = any(is_builder(u.unit_type) for u in units)
        open_techs: frozenset[str] | None = None  # Read once, and only if a resource tile shows.
        read_techs = False
        candidates: list[tuple[tuple, City, Any, valuation.Priced, valuation.TileValue]] = []
        foreign = list(self.foreign_cities()) if self.foreign_cities is not None else []
        reserved = {(t.x, t.y) for t in self.plans.current(turn.now)}  # Planned districts.
        for city in cities:
            if turn.refused_out:
                break
            try:
                tiles = await game.get_purchasable_tiles(city.city_id)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Tile read failed for %s", city.name)
                continue
            if not isinstance(tiles, list):
                continue
            occupied: set[tuple[int, int]] | None = None  # Read once per city, if a resource shows.
            for tile in tiles:
                if tile.cost <= 0:
                    continue
                spot = (tile.x, tile.y)
                if tile.resource and occupied is None:
                    occupied = await self._occupied_tiles(game, city)
                if tile.resource and not read_techs:
                    read_techs = True
                    open_techs = await self._open_techs(game)
                facts = valuation.tile_facts(
                    tile,
                    open_techs,
                    has_builder,
                    occupied=spot in (occupied or ()),
                    reserved=spot in reserved,
                    foreign_cities=foreign,
                    contested=spot in self.contested_tiles,
                    boost_value=self.boost_values.get(spot, 0.0),
                )
                worth = valuation.value_tile_purchase(facts, city, weights)
                if worth is None:
                    continue
                label = f"{city.name} tile ({tile.x},{tile.y})"
                priced = valuation.price(label, tile.cost, worth.valuation, turn.now, turn.last)
                candidates.append(
                    ((worth.priority, -priced.roi, tile.cost), city, tile, priced, worth)
                )
        bought_cities: set[int] = set()
        for _, city, tile, priced, worth in sorted(candidates, key=lambda c: c[0]):
            if turn.tiles >= MAX_TILES or turn.refused_out or turn.budget <= 0:
                break
            if city.city_id in bought_cities:
                continue
            audit = f"value={priced.value:.0f} cost={priced.cost} roi={priced.roi:.2f}"
            bar = valuation.floor(turn.budget, turn.income)
            if priced.cost > turn.budget:
                log.info(
                    "tile_buy %s skipped: costs %d, %d to spend (%s)",
                    priced.label,
                    priced.cost,
                    turn.budget,
                    worth.reason,
                )
                continue
            if priced.roi < bar and worth.priority != 0:
                log.info(
                    "tile_buy %s skipped: roi %.2f < floor %.2f (%s)",
                    priced.label,
                    priced.roi,
                    bar,
                    worth.reason,
                )
                continue
            if turn.target is not None and priced.cost > turn.slack:
                log.info("tile_buy %s waits for %s", priced.label, turn.target)
                continue
            result = await self._call(game.purchase_tile(city.city_id, tile.x, tile.y))
            if _ok(result, "TILE_PURCHASED"):
                turn.spend(priced.cost)
                turn.tiles += 1
                bought_cities.add(city.city_id)
                log.info("tile_buy %s bought %s: %s", priced.label, audit, worth.reason)
            else:
                turn.refusals += 1
                log.info("tile_buy %s refused: %s", priced.label, str(result)[:120])
        if turn.tiles == 0:
            notes.append("no tile worth buying by the tile rules")

    @staticmethod
    async def _occupied_tiles(game: GoldGame, city: City) -> set[tuple[int, int]]:
        """Tiles around the city with an improvement or a district already on them. A failed
        read gives the empty set (no tile known to be occupied), which only costs a refused buy."""
        try:
            area = await _read_area(game, city.x, city.y, 3)
        except ConnectionError:
            raise
        except Exception as error:
            log.warning("Area read failed for %s tile rules: %r", city.name, error)
            return set()
        return {(t.x, t.y) for t in area if t.improvement or t.district}

    @staticmethod
    async def _open_techs(game: GoldGame) -> frozenset[str] | None:
        """Techs not yet researched (available or locked), or None when the read fails."""
        try:
            research = await _read_research(game)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Research read failed for the tile rules")
            return None
        return frozenset(
            [valuation.tech_name(t.item_type) for t in research.techs]
            + [valuation.tech_name(t.item_type) for t in research.locked_techs]
        )

    async def _tiles(
        self, game: GoldGame, turn: _Turn, cities: list[City], notes: list[str], units=()
    ):
        if self.tile_valuation:
            if turn.target is not None and turn.slack <= 0:
                notes.append(f"tiles wait while saving toward {turn.target}")
                return
            await self._tiles_by_value(game, turn, cities, notes, units)
            return
        for city in cities:
            if turn.tiles >= MAX_TILES or turn.refused_out or turn.budget <= 0:
                return
            if turn.target is not None and turn.slack <= 0:
                notes.append(f"tiles wait while saving toward {turn.target}")
                return
            try:
                tiles = await game.get_purchasable_tiles(city.city_id)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Tile read failed for %s", city.name)
                continue
            if not isinstance(tiles, list):
                continue
            ranked = sorted(
                tiles,
                key=lambda t: (_RESOURCE_RANK.get(t.resource_class or "", 3), t.cost, t.x, t.y),
            )
            for tile in ranked:
                if tile.cost <= 0 or tile.cost > turn.budget:
                    continue
                if turn.target is not None and tile.cost > turn.slack:
                    continue
                if not tile.resource_class and tile.cost > turn.budget:
                    continue
                result = await self._call(game.purchase_tile(city.city_id, tile.x, tile.y))
                if _ok(result, "TILE_PURCHASED"):
                    turn.spend(tile.cost)
                    turn.tiles += 1
                    log.info(
                        "Bought tile (%d,%d) for %s for %d (%s)",
                        tile.x,
                        tile.y,
                        city.name,
                        tile.cost,
                        tile.resource_class or "no resource",
                    )
                else:
                    turn.refusals += 1
                    log.info(
                        "Tile (%d,%d) for %s refused: %s",
                        tile.x,
                        tile.y,
                        city.name,
                        str(result)[:120],
                    )
                break
        if turn.tiles == 0:
            notes.append("no affordable tile worth buying")

    @staticmethod
    async def _call(awaitable) -> str:
        """Await a game call. Any failure but a lost connection reads as a refusal."""
        try:
            return await awaitable
        except ConnectionError:
            raise
        except Exception as error:
            log.exception("Gold call raised")
            return f"ERR:EXCEPTION|{error}"
