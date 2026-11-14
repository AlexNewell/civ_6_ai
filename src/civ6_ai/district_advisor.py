# ruff: noqa: E501  (the Lua has long lines)
"""Let civ6-mcp's district advisor keep the water tiles of a water district (issue 187).

`build_district_advisor_query` asks the game for the valid tiles with
`CityManager.GetOperationTargets`, then drops every plot with `plot:IsWater()`. For a Harbor,
which is built on water, that removes every tile the game offered, so the advisor answered "no
valid tile" for every Harbor (Run 8: one city asked 16 to 20 times; 298 `NO_PLACEMENT` refusals a
run, most of them Harbors). The game's list is the authority, so the patch only widens the
water test: a district the game data marks as a water district (`Districts.Aquatic` or
`Districts.Coast`, or one of the named water districts) keeps its water tiles. Other districts
are filtered as before, and the mountain and impassable tests are unchanged: the game's list
never holds such a tile for a district, so they remove nothing the game offered.

Both advisors also merge every list `GetOperationTargets` returns, and the game returns more than
the valid tiles: for a wonder it adds `MOUNTAIN_PLOTS`, the mountains around the city, which are
not build sites. A bare mountain displaces nothing, so the wonder advisor ranked it first, the bot
chose it and the game refused (Run 9: 207 wonder refusals, El Mirador's Chichen Itza offered
(62,19), a mountain, before the two real tiles). `plots_only` keeps only
`targets[CityOperationResults.PLOTS]`, the list the game's own placement view uses.
"""

import re

from civ_mcp import lua as lq

# The line that drops water tiles, and the line after which `dist` and `dType` are both set.
_FILTER = (
    "if plot and not plot:IsWater() and not plot:IsImpassable() and not plot:IsMountain() then"
)
_ANCHOR = "local dType = "

# Water districts by name, for a build whose data has neither column.
WATER_DISTRICTS = (
    "DISTRICT_HARBOR",
    "DISTRICT_WATER_ENTERTAINMENT_COMPLEX",
    "DISTRICT_ROYAL_NAVY_DOCKYARD",
    "DISTRICT_COTHON",
)

_WATER_OK = "local waterOk = (dist.Aquatic == true) or (dist.Coast == true)" + "".join(
    f' or dType == "{name}"' for name in WATER_DISTRICTS
)
_NEW_FILTER = (
    "if plot and (waterOk or not plot:IsWater()) and not plot:IsImpassable() "
    "and not plot:IsMountain() then"
)


# The loop both advisors use to merge every target list, with either indentation.
_MERGE = re.compile(
    r"for k, v in pairs\(targets\) do\s+"
    r'if type\(v\) == "table" then\s+'
    r"for _, idx in ipairs\(v\) do table\.insert\(plotIndices, idx\) end\s+"
    r"end\s+end"
)
_PLOTS_ONLY = (
    "for _, idx in ipairs(targets[CityOperationResults.PLOTS] or {}) do "
    "table.insert(plotIndices, idx) end"
)


def plots_only(lua: str) -> str:
    """The advisor's Lua reading only the game's valid tiles. Raises if the loop is not found."""
    if len(_MERGE.findall(lua)) != 1:
        raise RuntimeError("civ6-mcp's advisor Lua no longer merges the target lists as expected")
    return _MERGE.sub(lambda _: _PLOTS_ONLY, lua)


def widen_water_filter(lua: str) -> str:
    """The advisor's Lua with water tiles kept for water districts. Raises if the text differs."""
    if lua.count(_FILTER) != 1 or lua.count(_ANCHOR) != 1:
        raise RuntimeError("civ6-mcp's district advisor Lua no longer has the water filter")
    start = lua.index(_ANCHOR)
    end = lua.index("\n", start)
    return (lua[:end] + "\n" + _WATER_OK + lua[end:]).replace(_FILTER, _NEW_FILTER)


def fix_district_advisor() -> None:
    """Make `GameState.get_district_advisor` keep water tiles for water districts, and read only
    the game's valid tiles (`plots_only`).

    Raises RuntimeError if civ6-mcp's query no longer has the shape this relies on.
    """
    original = getattr(lq, "build_district_advisor_query", None)
    if not callable(original):
        raise RuntimeError("civ6-mcp has no build_district_advisor_query to patch")
    if getattr(original, "_water_ok", False):
        return
    plots_only(widen_water_filter(original(1, "DISTRICT_HARBOR")))  # Fail now, not mid-game.
    from civ_mcp import game_state

    if game_state.lq is not lq:
        raise RuntimeError("civ6-mcp game_state no longer builds its Lua through civ_mcp.lua")

    def build_district_advisor_query(city_id: int, district_type: str) -> str:
        return plots_only(widen_water_filter(original(city_id, district_type)))

    build_district_advisor_query._water_ok = True  # type: ignore[attr-defined]
    lq.build_district_advisor_query = build_district_advisor_query  # type: ignore[attr-defined]


def fix_wonder_advisor() -> None:
    """Make `GameState.get_wonder_advisor` offer only the game's valid tiles (`plots_only`).

    Raises RuntimeError if civ6-mcp's query no longer has the shape this relies on.
    """
    original = getattr(lq, "build_wonder_advisor_query", None)
    if not callable(original):
        raise RuntimeError("civ6-mcp has no build_wonder_advisor_query to patch")
    if getattr(original, "_plots_only", False):
        return
    plots_only(original(1, "BUILDING_PYRAMIDS"))  # Fail now, not mid-game.
    from civ_mcp import game_state

    if game_state.lq is not lq:
        raise RuntimeError("civ6-mcp game_state no longer builds its Lua through civ_mcp.lua")

    def build_wonder_advisor_query(city_id: int, wonder_name: str) -> str:
        return plots_only(original(city_id, wonder_name))

    build_wonder_advisor_query._plots_only = True  # type: ignore[attr-defined]
    lq.build_wonder_advisor_query = build_wonder_advisor_query  # type: ignore[attr-defined]
