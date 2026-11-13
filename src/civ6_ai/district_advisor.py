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

The wonder advisor (`build_wonder_advisor_query`) has no such filter and needs no patch.
"""

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


def widen_water_filter(lua: str) -> str:
    """The advisor's Lua with water tiles kept for water districts. Raises if the text differs."""
    if lua.count(_FILTER) != 1 or lua.count(_ANCHOR) != 1:
        raise RuntimeError("civ6-mcp's district advisor Lua no longer has the water filter")
    start = lua.index(_ANCHOR)
    end = lua.index("\n", start)
    return (lua[:end] + "\n" + _WATER_OK + lua[end:]).replace(_FILTER, _NEW_FILTER)


def fix_district_advisor() -> None:
    """Make `GameState.get_district_advisor` keep water tiles for water districts.

    Raises RuntimeError if civ6-mcp's query no longer has the shape this relies on.
    """
    original = getattr(lq, "build_district_advisor_query", None)
    if not callable(original):
        raise RuntimeError("civ6-mcp has no build_district_advisor_query to patch")
    if getattr(original, "_water_ok", False):
        return
    widen_water_filter(original(1, "DISTRICT_HARBOR"))  # Fail now, not mid-game.
    from civ_mcp import game_state

    if game_state.lq is not lq:
        raise RuntimeError("civ6-mcp game_state no longer builds its Lua through civ_mcp.lua")

    def build_district_advisor_query(city_id: int, district_type: str) -> str:
        return widen_water_filter(original(city_id, district_type))

    build_district_advisor_query._water_ok = True  # type: ignore[attr-defined]
    lq.build_district_advisor_query = build_district_advisor_query  # type: ignore[attr-defined]
