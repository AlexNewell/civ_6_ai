# ruff: noqa: E501  (the Lua has long lines)
"""The game's Historic Moments, read once per game from `GameInfo.Moments` (issue 311).

The first live check of the era push (STD_T1_03, era score 1 at turns 40 and 50) showed that
guessing which moments pay was too narrow: a district moment that needs a +3 adjacency tile is
rare in a 40-turn era. The game states each moment's era score and the eras it can happen in in
its own table, so the bot reads that and aims its nudges at the cheap kinds it can reach.

Not read: the moment's trigger conditions (they sit in requirement sets), and whether this player
has already earned it. The kind of a moment is taken from its type name (`classify`); a name that
matches no kind is `other` and gets no nudge. The reply ends with its own `MODONE` line, so a
partial read is dropped (civ6-mcp's `execute_lua` strips `---END---`).
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger(__name__)

READ_TRIES = 2  # A failed read is tried once more, then there are no moment nudges this game.

MOMENTS_LUA = f"""
pcall(function()
    for row in GameInfo.Moments() do
        local score = row.EraScore or row.Score or 0
        print("MO|" .. tostring(row.MomentType) .. "|" .. tostring(score) .. "|" .. tostring(row.MinimumGameEra or "") .. "|" .. tostring(row.MaximumGameEra or ""))
    end
end)
print("MODONE")
print("{SENTINEL}")
"""

# Eras in order, to tell whether a moment can happen in the current era.
ERAS = (
    "ERA_ANCIENT",
    "ERA_CLASSICAL",
    "ERA_MEDIEVAL",
    "ERA_RENAISSANCE",
    "ERA_INDUSTRIAL",
    "ERA_MODERN",
    "ERA_ATOMIC",
    "ERA_INFORMATION",
    "ERA_FUTURE",
)

# Kinds the bot can steer toward, by words in the moment type. The first match wins.
_KINDS = (
    ("pantheon", ("PANTHEON", "BELIEF")),
    ("trade", ("TRADE_ROUTE", "TRADING", "TRADE")),  # TRADING_POST_... is a real moment name.
    ("wonder", ("WONDER",)),
    ("district", ("DISTRICT",)),
    ("meeting", ("MEET", "MET_", "FIRST_CONTACT", "CITY_STATE")),
    ("camp", ("BARBARIAN", "CAMP", "TRIBAL")),
)


@dataclass(frozen=True)
class Moment:
    moment: str
    score: int
    kind: str
    first_era: str = ""
    last_era: str = ""


def classify(moment_type: str) -> str:
    name = moment_type.upper()
    for kind, words in _KINDS:
        if any(word in name for word in words):
            return kind
    return "other"


def parse_moments(output: str) -> tuple[Moment, ...]:
    """Parse the read. Raises ValueError unless its own `MODONE` line is present."""
    if "MODONE" not in output.split():
        raise ValueError("moment read incomplete: no end marker")
    out = []
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5 or parts[0] != "MO" or not parts[1]:
            continue
        try:
            points = int(float(parts[2]))
        except ValueError:
            continue
        out.append(Moment(parts[1], points, classify(parts[1]), parts[3], parts[4]))
    return tuple(out)


async def read_moments(game) -> tuple[Moment, ...]:
    """The moments table in one call. `game` needs `execute_lua`."""
    reply = await game.execute_lua(MOMENTS_LUA, "ingame")
    return parse_moments(reply if isinstance(reply, str) else "\n".join(reply))


def _era_index(name: str) -> int | None:
    """The era's place in `ERAS`. Takes `ERA_ANCIENT` (the table) or `Ancient` (the overview)."""
    key = name.strip().upper().removesuffix(" ERA").removeprefix("ERA_")
    for index, era in enumerate(ERAS):
        if era.removeprefix("ERA_") == key:
            return index
    return None


def in_era(moment: Moment, era: str) -> bool:
    """Whether the moment can happen in this era. An unknown era or bound does not exclude it."""
    now = _era_index(era)
    first, last = _era_index(moment.first_era), _era_index(moment.last_era)
    if now is None:
        return True
    if first is not None and now < first:
        return False
    return not (last is not None and now > last)


def cheapest_by_kind(moments: tuple[Moment, ...], era: str) -> dict[str, int]:
    """Per steerable kind, the era score of the cheapest moment (the fewest points that kind can
    give, so the nudge never counts on more than the least) in this era. Kinds with no moment in this era are left out, and `other` is never in."""
    best: dict[str, int] = {}
    for moment in moments:
        if moment.kind == "other" or moment.score <= 0 or not in_era(moment, era):
            continue
        if moment.kind not in best or moment.score < best[moment.kind]:
            best[moment.kind] = moment.score
    return best


def log_moments(moments: tuple[Moment, ...], era: str) -> None:
    """One `era_moments` line when the table is read: every steerable moment for this era."""
    rows = [asdict(m) for m in moments if m.kind != "other" and in_era(m, era)]
    log.info("era_moments %s", json.dumps({"era": era, "total": len(moments), "moments": rows}))


def moment_nudge(
    kind: str, points: Mapping[str, int], per_point: float, cap: float, fits: bool = True
) -> float:
    """A bounded score nudge for something that earns a moment of this kind: `per_point` for each
    era score point the kind gives, at most `cap`. Zero when the kind has no moment in this era,
    or the build cannot finish before the boundary (`fits` False)."""
    score = points.get(kind, 0)
    if not fits or score <= 0 or per_point <= 0:
        return 0.0
    return min(cap, per_point * score)
