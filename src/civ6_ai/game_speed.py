"""Read the game speed the way the InGame context allows.

civ6-mcp's overview asks `Game.GetGameSpeedType()`, which does not exist in the InGame Lua
context. The call sits in a `pcall`, so the overview silently reports no speed, and with no
turn limit set (`GAME_MAX_TURNS` is nil) every "turns left" in the bot fell back to Standard's
500 turns. Run 13 was a Quick game, which ends at turn 330: the faith policy saved for a great
person 185 turns away, and valuation horizons and the growth model's last-turns rules were off.
`GameConfiguration.GetGameSpeedType()` returns the speed's hash, which `GameInfo.GameSpeeds`
accepts (checked live: GAMESPEED_QUICK, cost multiplier 67).
"""

from civ_mcp import lua as lq

_BROKEN = "Game.GetGameSpeedType()"
_FIXED = "GameConfiguration.GetGameSpeedType()"

_original = None


def build_overview_query(*args, **kwargs) -> str:
    return _original(*args, **kwargs).replace(_BROKEN, _FIXED)


def fix_overview_speed() -> None:
    """Make the overview read the game speed. Raises if civ6-mcp has changed."""
    global _original
    current = getattr(lq, "build_overview_query", None)
    if not callable(current):
        raise RuntimeError("civ6-mcp has no build_overview_query to patch")
    if current is build_overview_query:
        return
    if _BROKEN not in current():
        raise RuntimeError("civ6-mcp's overview no longer reads Game.GetGameSpeedType()")
    from civ_mcp import game_state

    if game_state.lq is not lq:
        raise RuntimeError("civ6-mcp game_state no longer builds its Lua through civ_mcp.lua")
    _original = current
    lq.build_overview_query = build_overview_query  # type: ignore[attr-defined]
