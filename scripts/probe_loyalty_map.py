"""Probe the game's per-tile loyalty numbers (issue 239). Read only; changes nothing in the game.

Runs `Map.GetContinentPlotsLoyalty()` in the InGame context and prints what it returns. Run it
by hand on a game you own: once with a settler owned (and again with its lens on), once with no
settler, and once on a map with more than one continent. Not part of the bot.

    uv run python scripts/probe_loyalty_map.py [--plots N] [--json FILE]
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from civ_mcp.connection import GameConnection

from civ6_ai.loyalty_probe import LOYALTY_PROBE_LUA, parse_loyalty_probe, summarise


async def run(args: argparse.Namespace) -> int:
    connection = GameConnection()
    await connection.connect()
    try:
        if connection.ingame_index is None:
            print("FAILED: no game is open")
            return 1
        lines = await connection.execute_write(LOYALTY_PROBE_LUA, timeout=15.0)
        probe = parse_loyalty_probe("\n".join(lines))
        print(summarise(probe))
        for x, y, value, revealed in probe.plots[: args.plots]:
            print(f"  ({x}, {y}) {value:+g}{'' if revealed else '  not revealed'}")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(asdict(probe), fh, indent=2)
        return 1 if probe.error else 0
    finally:
        await connection.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plots", type=int, default=20, help="plots to print (default 20)")
    parser.add_argument("--json", help="also write the full result here")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
