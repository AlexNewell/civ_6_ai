"""Check that this project can read and control a running Civ VI game.

Reads game state by default. With --act it also moves a unit and ends the turn, which can't be
undone, so only use that on a game you don't mind changing. Civ must be running with the tuner on.
"""

import argparse
import asyncio

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState
from civ_mcp.lua import TileInfo, UnitInfo

END_TURN_TIMEOUT_SECONDS = 300
IMPASSABLE_TERRAIN = ("OCEAN", "COAST", "MOUNTAIN")


def open_neighbours(unit: UnitInfo, tiles: list[TileInfo]) -> list[TileInfo]:
    """Return adjacent land tiles with no visible unit on them."""
    return [
        tile
        for tile in tiles
        if (tile.x, tile.y) != (unit.x, unit.y)
        and not any(terrain in tile.terrain for terrain in IMPASSABLE_TERRAIN)
        and not tile.units
        and not tile.own_units
    ]


async def move_any_unit(game: GameState, units: list[UnitInfo]) -> bool:
    """Move the first unit that accepts a move, and confirm its position changed."""
    for unit in units:
        if unit.moves_remaining <= 0:
            continue
        for tile in open_neighbours(unit, await game.get_map_area(unit.x, unit.y, 1)):
            result = await game.move_unit(unit.unit_index, tile.x, tile.y)
            print(f"Move {unit.name} ({unit.x},{unit.y}) to ({tile.x},{tile.y}): {result}")
            after = {u.unit_id: u for u in await game.get_units()}.get(unit.unit_id)
            if after and (after.x, after.y) != (unit.x, unit.y):
                print(f"Confirmed: {after.name} is now at ({after.x},{after.y})")
                return True
    return False


async def run(act: bool) -> int:
    connection = GameConnection()
    await connection.connect()
    game = GameState(connection)

    try:
        overview = await game.get_game_overview()
    except ConnectionError as error:
        print(f"FAILED: {error}")
        return 1
    units = await game.get_units()
    print(f"Read OK: turn {overview.turn}, {overview.civ_name}, {len(units)} units")
    for unit in units:
        print(f"  {unit.name} at ({unit.x},{unit.y}), {unit.moves_remaining} moves left")
    if not act:
        return 0

    if not await move_any_unit(game, units):
        print("FAILED: no unit could be moved")
        return 1

    result = await asyncio.wait_for(game.end_turn(), END_TURN_TIMEOUT_SECONDS)
    print(f"End turn: {result}")
    after = await game.get_game_overview()
    if after.turn <= overview.turn:
        print(f"FAILED: still on turn {after.turn}")
        return 1
    print(f"Confirmed: turn {overview.turn} became turn {after.turn}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--act", action="store_true", help="move a unit and end the turn")
    return asyncio.run(run(parser.parse_args().act))


if __name__ == "__main__":
    raise SystemExit(main())
