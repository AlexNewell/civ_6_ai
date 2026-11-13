"""Spend banked gold, with no model calls.

Runs after `CityProductionPolicy`. Each turn it keeps a reserve and spends what is above it, in
this order, until the turn's bounds or the gold run out:

1. Upgrade combat units the game says can upgrade now (the game's own check covers tech, gold
   and strategic resources). Units next to a hostile unit go first, then the most outdated.
2. Buy one unit or building in a city. The choice is `rank_production`'s, the same ranking
   production uses, so a Settler or Builder it wants, defence for a threatened city and the
   high-value buildings come out in its order. A city's own current build does not count against
   it, so the builder production just picked can be bought rather than skipped as "already
   building one".
3. Buy tiles, resource tiles first. A tile with no resource is bought only when it costs at most
   half of what is left to spend.

Reserve: `RESERVE_FLOOR` plus `RESERVE_PER_CITY` for each city, lowered by `INCOME_RELIEF` per
gold of income (never below `RESERVE_MIN`), plus ten turns of any deficit, capped at
`RESERVE_CAP`. Run 8 idled with gold above a reserve of 215 on 73% of turns, so the old
60 plus 20 per city was too high. The floor is about one emergency Warrior. A high income
refills a spent reserve quickly. The deficit term keeps a negative income from emptying the
treasury.

Saving: when the best upgrade (or the top-ranked item of an unthreatened city) is out of reach
but income closes the gap within `SAVE_TURNS` turns, it becomes the saving target. While there
is one, tiles and other purchases wait (a threatened city's defence does not), so small buys do
not keep the gold below one price. A target further away is not saved for.

Threatened cities (roadmap 1.35, see `defence.DefenceState`): while any city is threatened the
reserve drops to `THREAT_DEFICIT_TURNS` turns of any deficit (zero with income), so war chests
are spent, not banked. Units in or beside a threatened city upgrade first, a threatened city
buys only defenders and walls, and the bounds below widen to `THREAT_MAX_UPGRADES` and one
purchase per threatened city.

Bounds per turn: 3 upgrades, 3 purchases (one per city, since the game allows one per city per
turn), 2 tiles, and 8 calls that the game refuses. A refusal is logged and the next candidate is
tried. Costs come from the game's reads, and each spend is taken off the budget at its listed
cost, so the reserve holds even if a later read is stale.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from civ6_ai.placement import PlanBook
from civ6_ai.policies.game_data import COMBAT_UNITS
from civ6_ai.policies.production import (
    ProductionContext,
    ProductionWeights,
    build_context,
    is_defence,
    rank_production,
    score,
)
from civ6_ai.state import City, Unit
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_empire as _read_empire
from civ6_ai.state import read_production as _read_production
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
MAX_UPGRADES = 3
MAX_PURCHASES = 3
MAX_TILES = 2
MAX_REFUSALS = 8
THREAT_DEFICIT_TURNS = 2  # The reserve under threat: enough that a deficit cannot disband units.
THREAT_MAX_UPGRADES = 5
_PURCHASABLE = ("UNIT", "BUILDING")
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
    async def get_purchasable_tiles(self, city_id: int) -> Any: ...
    async def purchase_tile(self, city_id: int, x: int, y: int) -> str: ...


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
    upgrades: int = 0
    purchases: int = 0
    tiles: int = 0
    income: float = 0.0
    target: str | None = None  # What gold is being saved for: it holds back lesser spending.
    target_defence: bool = False  # The target is defence, so a threatened city may still buy.

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
        return True

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
    ) -> None:
        # Tiles production wants for a district or wonder (see `placement`): bought when they fit
        # the budget, else the saving target.
        self.plans = plans if plans is not None else PlanBook()
        self.weights = weights or ProductionWeights()
        self.defence = defence  # The threat state (`defence.DefenceState`), or None.

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
        turn = _Turn(budget, income=empire.gold_per_turn)
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
        await self._tiles(game, turn, cities, notes)
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
        guarded = [(c.x, c.y) for c in cities or [] if self._marked(c)]
        candidates = upgrade_order(units, hostile, guarded if self.defence is not None else None)
        if not candidates:
            notes.append("no unit can upgrade")
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
                turn.budget -= unit.upgrade_cost
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
        building = {c.city_id: c.building for c in cities if c.building}
        bought: list[str] = []
        limit = MAX_PURCHASES
        if self.defence is not None:
            limit = max(limit, len(self.defence.cities))
        for city in cities:
            if turn.purchases >= limit or turn.refused_out:
                break
            if turn.budget <= 0:
                break
            try:
                options = await _read_production(game, city.city_id)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Production read failed for %s", city.name)
                continue
            if turn.target is not None and not self._may_buy_while_saving(city):
                notes.append(f"{city.name}: saving toward {turn.target}")
                continue
            others = [item for cid, item in building.items() if cid != city.city_id]
            context = build_context(empire, units, [*others, *bought], cities, self.defence)
            ranked = rank_production(city, options, context, self.weights)
            if self.defence is not None and self.defence.is_threatened(city.city_id):
                # Defenders and walls only. A threatened city does not buy a library.
                ranked = [o for o in ranked if is_defence(o)]
            await self._buy_in(game, turn, city, ranked, context, bought, notes)

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
            if option.gold_cost > turn.budget:
                notes.append(f"{city.name} {option.item} costs {option.gold_cost}")
                if tried == 0 and not is_defence(option) and not self._marked(city):
                    # The best item is out of reach: save for it if income gets there soon.
                    turn.save_for(f"{city.name} {option.item}", option.gold_cost)
                continue
            if tried >= 2 or turn.refused_out:
                break
            if turn.target is not None and not is_defence(option):
                break  # Saving toward a better item: no lesser one is bought meanwhile.
            tried += 1
            result = await self._call(
                game.purchase_item(city.city_id, option.category, option.item)
            )
            if _ok(result, "PURCHASED"):
                turn.budget -= option.gold_cost
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
            log.info("Purchase of %s in %s refused: %s", option.item, city.name, str(result)[:120])
        return False

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
                    turn.budget -= target.cost
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

    async def _tiles(self, game: GoldGame, turn: _Turn, cities: list[City], notes: list[str]):
        for city in cities:
            if turn.tiles >= MAX_TILES or turn.refused_out or turn.budget <= 0:
                return
            if turn.target is not None:
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
                if not tile.resource_class and tile.cost > turn.budget:
                    continue
                result = await self._call(game.purchase_tile(city.city_id, tile.x, tile.y))
                if _ok(result, "TILE_PURCHASED"):
                    turn.budget -= tile.cost
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
