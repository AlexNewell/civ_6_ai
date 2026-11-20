"""What the tile-purchase rules would buy, city by city (issues 87, 338). Read only.

`scripts/tile_report.py` prints this for the open game. The rows come from the same
`valuation.tile_facts` and `value_tile_purchase` the gold policy uses, so the report shows what
the rule sees. The yields are the valuation's estimate from the terrain name, not the game's.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from civ6_ai.policies import valuation
from civ6_ai.policies.production import ProductionWeights
from civ6_ai.state import City
from civ6_ai.state.reader import STANDARD_MAP_WIDTH


@dataclass(frozen=True)
class TileRow:
    x: int
    y: int
    cost: int
    terrain: str
    resource: str | None
    yields: tuple[int, int, int]  # Estimated food, production, gold.
    value: float | None  # Valuation over the turns left, None when no rule applies.
    roi: float | None
    priority: int | None
    reason: str


def tile_rows(
    city: City,
    tiles: Iterable[Any],
    weights: ProductionWeights,
    now: int,
    last: int,
    open_techs: frozenset[str] | None = None,
    has_builder: bool = False,
    occupied: frozenset[tuple[int, int]] = frozenset(),
    reserved: frozenset[tuple[int, int]] = frozenset(),
    foreign_cities: Sequence[tuple[int, int]] = (),
    boost_values: dict[tuple[int, int], float] | None = None,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> list[TileRow]:
    """One row per purchasable tile of the city, best first: valued tiles by priority then ROI,
    then those no rule applies to, cheapest first."""
    rows: list[TileRow] = []
    for tile in tiles:
        if tile.cost <= 0:
            continue
        spot = (tile.x, tile.y)
        facts = valuation.tile_facts(
            tile,
            open_techs,
            has_builder,
            occupied=spot in occupied,
            reserved=spot in reserved,
            foreign_cities=foreign_cities,
            boost_value=(boost_values or {}).get(spot, 0.0),
            map_width=map_width,
        )
        yields = valuation.estimate_tile_yields(tile.terrain)
        worth = valuation.value_tile_purchase(facts, city, weights)
        if worth is None:
            note = (
                f"no rule applies ({facts.resource} cannot be improved)"
                if tile.resource
                else ("no rule applies")
            )
            rows.append(TileRow(*_head(tile), yields, None, None, None, note))
            continue
        priced = valuation.price(city.name, tile.cost, worth.valuation, now, last)
        rows.append(
            TileRow(*_head(tile), yields, priced.value, priced.roi, worth.priority, worth.reason)
        )
    rows.sort(
        key=lambda r: (
            r.priority is None,
            r.priority if r.priority is not None else 0,
            -(r.roi or 0.0),
            r.cost,
            r.x,
            r.y,
        )
    )
    return rows


def _head(tile: Any) -> tuple[int, int, int, str, str | None]:
    return tile.x, tile.y, tile.cost, tile.terrain, tile.resource


def format_rows(city: City, rows: Sequence[TileRow]) -> str:
    """A text table for one city."""
    lines = [f"{city.name} ({city.x}, {city.y}): {len(rows)} purchasable tiles"]
    for r in rows:
        food, prod, gold = r.yields
        res = r.resource or "-"
        worth = "-" if r.value is None else f"{r.value:.0f} (roi {r.roi:.2f}, p{r.priority})"
        lines.append(
            f"  ({r.x:>3},{r.y:>3}) {r.cost:>4}g {r.terrain:<22} {res:<12}"
            f" ~{food}/{prod}/{gold}  {worth}  {r.reason}"
        )
    return "\n".join(lines)


async def report_game(
    game: Any,
    foreign_cities: Sequence[tuple[int, int]] = (),
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> str:
    """The report for every city of the open game. `game` is civ6-mcp's `GameState` or a fake.

    `foreign_cities` are the rival and city-state cities to treat as contested-land sources (the
    bot takes them from its settling memory, which a standalone read does not have).
    """
    from civ6_ai.placement import last_turn_for
    from civ6_ai.policies.builder import is_builder
    from civ6_ai.state import read_area, read_cities, read_empire, read_research, read_units

    empire = await read_empire(game)
    last = last_turn_for(empire.game_speed, empire.max_turns)
    cities = await read_cities(game, map_width)
    has_builder = any(is_builder(u.unit_type) for u in await read_units(game, map_width))
    research = await read_research(game)
    open_techs = frozenset(
        valuation.tech_name(t.item_type) for t in [*research.techs, *research.locked_techs]
    )
    weights = ProductionWeights()
    blocks = []
    for city in cities:
        tiles = await game.get_purchasable_tiles(city.city_id)
        area = await read_area(game, city.x, city.y, 3)
        occupied = frozenset((t.x, t.y) for t in area if t.improvement or t.district)
        rows = tile_rows(
            city,
            tiles if isinstance(tiles, list) else [],
            weights,
            empire.turn,
            last,
            open_techs,
            has_builder,
            occupied,
            foreign_cities=foreign_cities,
            map_width=map_width,
        )
        blocks.append(format_rows(city, rows))
    return "\n\n".join(blocks)
