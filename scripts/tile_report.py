"""Print each city's purchasable tiles with price, estimated yields and the rule's valuation
(issues 87, 338). Read only: it never buys.

    uv run python scripts/tile_report.py [--foreign X,Y ...] [--map-width N]

For every city it lists the tiles the game offers, what the tile-purchase rules value each at
(per the same code the gold policy runs with `tile_valuation` on), and the reason, or why no rule
applies. `--foreign` gives rival and city-state city tiles so contested land shows. Yields are
estimates from the terrain name. Live use is pending: the tests use fakes.
"""

import argparse
import asyncio
import sys

from civ6_ai.state.reader import STANDARD_MAP_WIDTH
from civ6_ai.tile_report import report_game


def parse_xy(text: str) -> tuple[int, int]:
    x, y = text.split(",")
    return int(x), int(y)


async def run(args: argparse.Namespace) -> int:
    from civ_mcp.connection import GameConnection
    from civ_mcp.game_state import GameState

    connection = GameConnection()
    await connection.connect()
    try:
        print(await report_game(GameState(connection), args.foreign, args.map_width))
        return 0
    finally:
        await connection.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foreign", nargs="*", type=parse_xy, default=[], help="city tiles as X,Y")
    parser.add_argument("--map-width", type=int, default=STANDARD_MAP_WIDTH)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
