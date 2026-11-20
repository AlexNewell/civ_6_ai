"""Print the quality breakdown of city sites on a loaded map (issue 324). Read only.

    uv run python scripts/site_report.py X,Y [X,Y ...] [--map-width N]

Reads the area around each tile from the open game, adds the centre resource and ring yields, and
prints each component of `score_site_breakdown` (raw and weighted). It also compares the tile
with the survey's own list when the survey has it. Live use is pending: the tests use fakes.

For the human's saved games (STD_T1_04_TURN58, _TURN78, _TURN140): load the save in the game
(or `scripts/check_saves.py` lists them), run this script with the coordinates of each city the
human founded (Ostia, Aquileia, Anshan and later ones, from the observer's run header), and
compare against the bot's own sites printed the same way.
"""

import argparse
import asyncio
import sys
from typing import Any

from civ6_ai.policies.settling import SettlingSettings, score_site_breakdown
from civ6_ai.site_quality import enrich_site, format_breakdown
from civ6_ai.state.models import SettleSite
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, read_area, read_cities, read_exploration


def parse_xy(text: str) -> tuple[int, int]:
    x, y = text.split(",")
    return int(x), int(y)


async def report_site(
    game: Any, x: int, y: int, own_cities: list[tuple[int, int]], map_width: int | None
) -> str:
    """The report for one tile. The survey's totals are used when it lists the tile."""
    exploration = await read_exploration(game)
    listed = next((s for s in exploration.settle_sites if (s.x, s.y) == (x, y)), None)
    tiles = await read_area(game, x, y, 3)
    centre = next((t for t in tiles if (t.x, t.y) == (x, y)), None)
    water = "none"
    if centre is not None and centre.fresh_water:
        water = "fresh"
    elif centre is not None and centre.coastal:
        water = "coast"
    base = listed or SettleSite(x, y, 0, 0, water, (), 0, 0, 0)
    site = enrich_site(base, tiles, map_width)
    capital = own_cities[0] if own_cities else None
    breakdown = score_site_breakdown(
        site, SettlingSettings(map_width=map_width), own_cities, capital
    )
    note = "" if listed else " (not on the survey list: food, production, defence read as 0)"
    return f"site ({x}, {y}){note}\n{format_breakdown(breakdown)}"


async def run(args: argparse.Namespace) -> int:
    from civ_mcp.connection import GameConnection
    from civ_mcp.game_state import GameState

    connection = GameConnection()
    await connection.connect()
    try:
        game = GameState(connection)
        cities = [(c.x, c.y) for c in await read_cities(game, args.map_width)]
        for xy in args.sites:
            print(await report_site(game, *xy, cities, args.map_width))
        return 0
    finally:
        await connection.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sites", nargs="+", type=parse_xy, help="tile coordinates as X,Y")
    parser.add_argument("--map-width", type=int, default=STANDARD_MAP_WIDTH)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
