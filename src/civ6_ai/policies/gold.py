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

Reserve: `RESERVE_FLOOR` plus `RESERVE_PER_CITY` for each city, plus ten turns of any deficit,
capped at `RESERVE_CAP`. The floor is about one emergency Warrior or Builder. Each city adds
enough to buy a defender if it is attacked. The deficit term keeps a negative income from
emptying the treasury. The cap exists because Run 3 banked 1,363 gold by turn 135 and Run 2
held 625 at turn 100, so a reserve that grows without limit is what we are avoiding.

Bounds per turn: 3 upgrades, 3 purchases (one per city, since the game allows one per city per
turn), 2 tiles, and 8 calls that the game refuses. A refusal is logged and the next candidate is
tried. Costs come from the game's reads, and each spend is taken off the budget at its listed
cost, so the reserve holds even if a later read is stale.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from civ6_ai.policies.game_data import COMBAT_UNITS
from civ6_ai.policies.production import (
    ProductionContext,
    ProductionWeights,
    build_context,
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

RESERVE_FLOOR = 60
RESERVE_PER_CITY = 20
RESERVE_CAP = 300
DEFICIT_TURNS = 10
MAX_UPGRADES = 3
MAX_PURCHASES = 3
MAX_TILES = 2
MAX_REFUSALS = 8
_PURCHASABLE = ("UNIT", "BUILDING")
_NAME = re.compile(r"[A-Z0-9_]+")
_RESOURCE_RANK = {"luxury": 0, "strategic": 1, "bonus": 2}


def reserve_for(num_cities: int, gold_per_turn: float = 0.0) -> int:
    """Gold never spent. See the module docstring for why each part is there."""
    deficit = DEFICIT_TURNS * max(0.0, -gold_per_turn)
    return int(min(RESERVE_CAP, RESERVE_FLOOR + RESERVE_PER_CITY * max(0, num_cities) + deficit))


class GoldGame(Protocol):
    """What the policy needs from civ6-mcp's `GameState`, on top of what the readers use."""

    async def upgrade_unit(self, unit_id: int) -> str: ...
    async def purchase_item(
        self, city_id: int, item_type: str, item_name: str, yield_type: str = "YIELD_GOLD"
    ) -> str: ...
    async def get_purchasable_tiles(self, city_id: int) -> Any: ...
    async def purchase_tile(self, city_id: int, x: int, y: int) -> str: ...


def _ok(result: str, marker: str) -> bool:
    return isinstance(result, str) and result.startswith("OK:") and marker in result


def _strength(unit_type: str) -> int:
    stats = COMBAT_UNITS.get(unit_type)
    return max(stats[0], stats[1]) if stats else 0


def upgrade_order(units: list[Unit], hostile: list[tuple[int, int]]) -> list[Unit]:
    """Upgradable combat units, those nearest a hostile unit first, then the most outdated."""

    def key(unit: Unit) -> tuple:
        near = min((hex_distance(unit.x, unit.y, x, y) for x, y in hostile), default=10_000)
        gap = _strength(unit.upgrade_to or "") - _strength(unit.unit_type)
        return (near, -gap, unit.upgrade_cost, unit.unit_id)

    return sorted((u for u in units if u.is_combat and u.can_upgrade and u.upgrade_to), key=key)


@dataclass
class _Turn:
    """What is left to spend this turn, and what has been done."""

    budget: int
    refusals: int = 0
    upgrades: int = 0
    purchases: int = 0
    tiles: int = 0

    @property
    def refused_out(self) -> bool:
        return self.refusals >= MAX_REFUSALS


class GoldPolicy:
    """A turn handler that spends gold above the reserve."""

    name = "gold"

    def __init__(self, weights: ProductionWeights | None = None) -> None:
        self.weights = weights or ProductionWeights()

    async def run(self, game: GoldGame) -> None:
        # The overview alone settles whether there is anything to spend. The full empire read is
        # five more game calls, so it waits until there is (Run 3 profile: most turns have none).
        overview = await game.get_game_overview()
        reserve = reserve_for(overview.num_cities, overview.gold_per_turn)
        budget = int(overview.gold) - reserve
        if budget <= 0:
            log.info(
                "Gold %d is at or below the reserve %d, nothing to spend", overview.gold, reserve
            )
            return
        empire = await _read_empire(game)
        reserve = reserve_for(empire.num_cities, empire.gold_per_turn)
        budget = int(empire.gold) - reserve
        turn = _Turn(budget)
        notes: list[str] = []
        log.info("Gold %d, reserve %d, %d to spend", empire.gold, reserve, budget)
        cities = sorted(await _read_cities(game), key=lambda c: (not c.is_threatened, c.city_id))
        units = list(await _read_units(game))
        await self._upgrade(game, turn, units, notes)
        await self._buy(game, turn, empire, cities, units, notes)
        await self._tiles(game, turn, cities, notes)
        spent = budget - turn.budget
        log.info("gold_spent turn total: %d of %d to spend", spent, budget)
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

    async def _upgrade(self, game: GoldGame, turn: _Turn, units: list[Unit], notes: list[str]):
        try:
            hostile = [(t.x, t.y) for t in await _read_threats(game) if t.hostile]
        except ConnectionError:
            raise
        except Exception:
            log.exception("Threat read failed, upgrading without threat order")
            hostile = []
        candidates = upgrade_order(units, hostile)
        if not candidates:
            notes.append("no unit can upgrade")
            return
        for unit in candidates:
            if turn.upgrades >= MAX_UPGRADES or turn.refused_out:
                break
            if unit.upgrade_cost > turn.budget:
                notes.append(f"{unit.unit_type} upgrade costs {unit.upgrade_cost}")
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
            else:
                turn.refusals += 1
                log.info("Upgrade of %s refused: %s", unit.unit_type, str(result)[:120])

    @staticmethod
    def _near(unit: Unit, hostile: list[tuple[int, int]]) -> bool:
        return any(hex_distance(unit.x, unit.y, x, y) <= 4 for x, y in hostile)

    async def _buy(self, game, turn, empire, cities: list[City], units, notes: list[str]):
        building = {c.city_id: c.building for c in cities if c.building}
        bought: list[str] = []
        for city in cities:
            if turn.purchases >= MAX_PURCHASES or turn.refused_out:
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
            others = [item for cid, item in building.items() if cid != city.city_id]
            context = build_context(empire, units, [*others, *bought])
            ranked = rank_production(city, options, context, self.weights)
            await self._buy_in(game, turn, city, ranked, context, bought, notes)

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
                continue
            if tried >= 2 or turn.refused_out:
                break
            tried += 1
            result = await self._call(
                game.purchase_item(city.city_id, option.category, option.item)
            )
            if _ok(result, "PURCHASED"):
                turn.budget -= option.gold_cost
                turn.purchases += 1
                bought.append(option.item)
                why = "threatened" if city.is_threatened else "top-ranked affordable item"
                log.info(
                    "Bought %s in %s for %d (%s)", option.item, city.name, option.gold_cost, why
                )
                return True
            turn.refusals += 1
            log.info("Purchase of %s in %s refused: %s", option.item, city.name, str(result)[:120])
        return False

    async def _tiles(self, game: GoldGame, turn: _Turn, cities: list[City], notes: list[str]):
        for city in cities:
            if turn.tiles >= MAX_TILES or turn.refused_out or turn.budget <= 0:
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
                if not tile.resource_class and tile.cost * 2 > turn.budget:
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
