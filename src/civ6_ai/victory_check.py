"""Fix civ6-mcp's per-turn victory-threat check failing on a decimal (issue 151).

`end_turn._check_victory_proximity` reads the Lua reply with `int(parts[3])`, but the game can
answer a fractional number (`SCI_THREAT|Civ|2|33.5`), so it raised `ValueError: invalid literal
for int() with base 10: '33.5'` and `end_turn` logged a warning with a traceback, about once per
turn once a rival had a space-race point. The caller catches it, so the cost was the lost
victory-threat events, not the turn.

The fix keeps civ6-mcp's function and rounds the decimals down in the Lua reply it reads.
"""

import contextlib
import inspect
import types

# For each reply line kind, the fields that civ6-mcp reads with int().
INT_FIELDS = {"DIPLO_THREAT": (2,), "SCI_THREAT": (2, 3)}

# The line in civ6-mcp that raised, and the Lua it reads.
_EXPECTED = ("int(parts[2]), int(parts[3])", "build_victory_proximity_query", "execute_write")


def whole_numbers(line: str) -> str:
    """Round the decimal numbers in the int-read fields of one reply line down."""
    parts = line.split("|")
    for index in INT_FIELDS.get(parts[0], ()):
        if index < len(parts) and "." in parts[index]:
            # Not a number: leave it for civ6-mcp to report.
            with contextlib.suppress(ValueError):
                parts[index] = str(int(float(parts[index])))
    return "|".join(parts)


class _RoundedConn:
    """Wraps a connection so `execute_write` replies have whole numbers."""

    def __init__(self, real) -> None:
        self._real = real

    async def execute_write(self, *args, **kwargs):
        lines = await self._real.execute_write(*args, **kwargs)
        return [whole_numbers(line) for line in lines]

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _RoundedGame:
    """Wraps a `GameState` so its `conn` is a `_RoundedConn`."""

    def __init__(self, real) -> None:
        self._real = real
        self.conn = _RoundedConn(real.conn)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def fix_victory_check(check: bool = True) -> None:
    """Make `_check_victory_proximity` cope with decimals in its reply.

    Raises RuntimeError if civ6-mcp's check no longer has the shape this relies on (unless
    `check` is False, which only tests use).
    """
    from civ_mcp import end_turn

    original = end_turn._check_victory_proximity
    if getattr(original, "_whole_numbers", False):
        return
    if check:
        source = inspect.getsource(original)
        missing = [text for text in _EXPECTED if text not in source]
        if missing:
            raise RuntimeError(f"civ6-mcp's _check_victory_proximity no longer has {missing}")
        if not isinstance(end_turn, types.ModuleType):
            raise RuntimeError("civ6-mcp end_turn is not a module")

    async def _check_victory_proximity(gs):
        return await original(_RoundedGame(gs))

    _check_victory_proximity._whole_numbers = True  # type: ignore[attr-defined]
    end_turn._check_victory_proximity = _check_victory_proximity
