"""Patches that make civ6-mcp's game calls faster, each failing loudly if the pinned code changes.

Two costs are cut:

* The fixed waits. `GameConnection._locked_execute` drains stray messages for 0.1 s before each
  call and 0.2 s after it, so every call costs at least 0.3 s (issue 112). Messages that are
  already waiting are still read at once; only the idle wait is shorter.
* The popup scan. When an "exclusive" popup (first city, era score, disaster...) is on screen,
  `dismiss_popup` closes it by walking every Lua state with "Popup", "Wonder", "Moment", "Era"
  or "Disaster" in its name, about 30 states, including around 20 `InvitePopup` copies that
  never hold one. Each visit took a full call, so a popup turn cost 15 to 30 s. Here only the
  states that can hold an exclusive popup are walked.
"""

import inspect
import types

# What the two idle waits in `_locked_execute` are cut to, by their original length in seconds.
DRAIN_CUTS = {0.1: 0.02, 0.2: 0.05}

# The popups civ6-mcp's `dismiss_popup` pre-check looks for. A visible one is the only reason
# its Phase 2 closes a state, so only the states with these names need a visit.
EXCLUSIVE_POPUPS = (
    "TechCivicCompletedPopup",
    "NaturalWonderPopup",
    "NaturalDisasterPopup",
    "WonderBuiltPopup",
    "EraCompletePopup",
    "HistoricMoments",
    "MomentPopup",
    "ProjectBuiltPopup",
    "RockBandPopup",
    "RockBandMoviePopup",
)


class _ShortDrains:
    """Stands in for civ6-mcp's `tuner_client` inside its connection module."""

    def __init__(self, real: types.ModuleType) -> None:
        self._real = real

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    async def drain_messages(self, reader, timeout: float = 0.5):
        return await self._real.drain_messages(reader, timeout=DRAIN_CUTS.get(timeout, timeout))


def shorten_call_waits() -> None:
    """Cut the idle waits before and after each call. Raises RuntimeError if the code changed."""
    from civ_mcp import connection

    source = inspect.getsource(connection.GameConnection._locked_execute)
    for original in DRAIN_CUTS:
        if f"drain_messages(self._reader, timeout={original})" not in source:
            raise RuntimeError(f"civ6-mcp no longer drains for {original} s in _locked_execute")
    if isinstance(connection.tuner_client, _ShortDrains):
        return
    connection.tuner_client = _ShortDrains(connection.tuner_client)


def narrow_popup_scan(check: bool = True) -> None:
    """Make `dismiss_popup` visit only the states that can hold an exclusive popup.

    Raises RuntimeError if civ6-mcp's popup code no longer has the shape this relies on (unless
    `check` is False, which only tests use).
    """
    from civ_mcp import game_lifecycle

    original = game_lifecycle.dismiss_popup
    if getattr(original, "_narrowed", False):
        return
    if check:
        source = inspect.getsource(original)
        if "conn.lua_states.items()" not in source:
            raise RuntimeError("civ6-mcp's dismiss_popup no longer reads conn.lua_states")
        missing = [name for name in EXCLUSIVE_POPUPS if f'"{name}"' not in source]
        if missing:
            raise RuntimeError(f"civ6-mcp's dismiss_popup no longer lists {missing}")

    async def dismiss_popup(conn) -> str:
        full = conn.lua_states
        conn.lua_states = {i: n for i, n in full.items() if n in EXCLUSIVE_POPUPS}
        try:
            return await original(conn)
        finally:
            conn.lua_states = full

    dismiss_popup._narrowed = True  # type: ignore[attr-defined]
    game_lifecycle.dismiss_popup = dismiss_popup
