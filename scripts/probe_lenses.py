"""Probe the data behind the game's map lenses (issue 243). Read only; changes nothing in the game.

Runs one or more probes from `civ6_ai/lens_probe.py` in the InGame context and prints what each
returns. Run it by hand on a game you own. Not part of the bot. See docs/research/map-lenses.md for
which runs to do and what to record.

    uv run python scripts/probe_lenses.py [--lens NAME ...] [--json FILE]
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from civ_mcp.connection import GameConnection

from civ6_ai.lens_probe import PROBES, parse_lens_probe, summarise


async def run(args: argparse.Namespace) -> int:
    names = args.lens or list(PROBES)
    connection = GameConnection()
    await connection.connect()
    failed = False
    results = {}
    try:
        if connection.ingame_index is None:
            print("FAILED: no game is open")
            return 1
        for name in names:
            lines = await connection.execute_write(PROBES[name], timeout=15.0)
            probe = parse_lens_probe("\n".join(lines))
            print(summarise(probe) + "\n")
            results[name] = asdict(probe)
            failed = failed or bool(probe.error)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2)
        return 1 if failed else 0
    finally:
        await connection.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lens", action="append", choices=sorted(PROBES), help="probe to run (default all)"
    )
    parser.add_argument("--json", help="also write the full results here")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
