"""Every living civ's score and the victory leaders, as the game's World Rankings screen shows them.

`get_game_overview` and `get_victory_progress` list met civs only, so the bot's rank and its gap to
the leader were wrong: in Run 16 it reported second of three with a gap of -26, while the screen
showed five civs, us fifth and about 206 behind (issue 256). The screen lists every living major
civ with its score, naming only the met ones ("Unmet Player" for the rest), and its Overall tab
names the leader of each victory type the same way. This module reads exactly that and no more.

It is one fixed Lua string, run once per turn (the shape of `defeat.py`, no model ever supplies or
sees Lua). Per living major civ it prints the score, plus the name and player id only if the civ
has been met (or is us). Per victory type it prints who leads and the lead's progress, again with
the id only when met. An unmet civ is an anonymous slot: its identity and position are never read.
The UI's own functions behind this are `GatherScoreData` and `PopulateOverallInstance`
(WorldRankings.lua, read only); the leader here uses the game's per-team victory progress, without
the UI's tiebreakers, so a tie may name a different civ than the screen.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from civ_mcp.lua import SENTINEL

log = logging.getLogger(__name__)

_SCORE = "WR_SCORE|"
_LEAD = "WR_LEAD|"
_ME = "WR_ME|"
_DONE = "WR_DONE"  # Own end marker: civ6-mcp strips its sentinel from the reply.

# The victory types the Overall tab shows a leader for, with the short names used in logs.
VICTORY_TYPES = {
    "VICTORY_TECHNOLOGY": "science",
    "VICTORY_CULTURE": "culture",
    "VICTORY_RELIGIOUS": "religious",
}
_TYPE_LIST = ", ".join(f'"{key}"' for key in VICTORY_TYPES)

_WORLD_RANKINGS_LUA = f"""
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
print("{_ME}" .. tostring(me))
local function metOrMe(i) return i == me or pDiplo:HasMet(i) end
for i = 0, 62 do
    local p = Players[i]
    if p and p:IsMajor() and p:IsAlive() then
        local id, name = -1, ""
        if metOrMe(i) then
            id = i
            pcall(function()
                name = Locale.Lookup(PlayerConfigurations[i]:GetCivilizationShortDescription())
            end)
            name = string.gsub(tostring(name), "|", "/")
        end
        local score = -1
        pcall(function() score = p:GetScore() end)
        print("{_SCORE}" .. id .. "|" .. name .. "|" .. tostring(score))
    end
end
for _, vt in ipairs({{{_TYPE_LIST}}}) do
    pcall(function()
        local bestId, bestProgress = nil, nil
        for i = 0, 62 do
            local p = Players[i]
            if p and p:IsMajor() and p:IsAlive() then
                local progress = Game.GetVictoryProgressForTeam(vt, p:GetTeam())
                if progress ~= nil and (bestProgress == nil or progress > bestProgress) then
                    bestId, bestProgress = i, progress
                end
            end
        end
        if bestId ~= nil then
            print("{_LEAD}" .. vt .. "|" .. (metOrMe(bestId) and bestId or -1) .. "|"
                .. tostring(bestProgress))
        end
    end)
end
print("{_DONE}")
print("{SENTINEL}")
"""

UNMET = -1


@dataclass(frozen=True)
class CivScore:
    player_id: int  # UNMET (-1) for a civ we have not met: an anonymous slot.
    name: str  # Empty for an unmet civ.
    score: int

    @property
    def met(self) -> bool:
        return self.player_id != UNMET


@dataclass(frozen=True)
class VictoryLeader:
    victory: str
    player_id: int  # UNMET for an unmet leader.
    progress: float

    @property
    def short(self) -> str:
        return VICTORY_TYPES.get(self.victory, self.victory)


@dataclass(frozen=True)
class WorldRankings:
    our_id: int
    civs: tuple[CivScore, ...]  # Every living major civ, us included, best score first.
    leaders: tuple[VictoryLeader, ...] = ()
    turn: int | None = None  # Set by the handler that read it.

    @property
    def alive(self) -> int:
        return len(self.civs)

    @property
    def ours(self) -> CivScore | None:
        return next((c for c in self.civs if c.player_id == self.our_id), None)

    @property
    def rivals(self) -> tuple[CivScore, ...]:
        return tuple(c for c in self.civs if c.player_id != self.our_id)

    @property
    def rank(self) -> int | None:
        ours = self.ours
        return None if ours is None else 1 + sum(c.score > ours.score for c in self.rivals)

    @property
    def leader_score(self) -> int | None:
        return max((c.score for c in self.civs), default=None)

    @property
    def gap(self) -> int | None:
        """Our score minus the best score among the others: negative when we trail."""
        ours = self.ours
        if ours is None or not self.rivals:
            return None
        return ours.score - max(c.score for c in self.rivals)

    @property
    def unmet(self) -> int:
        return sum(not c.met for c in self.civs)

    def unmet_leaders(self) -> tuple[VictoryLeader, ...]:
        return tuple(x for x in self.leaders if x.player_id == UNMET)

    def fields(self) -> dict:
        """The per-turn record fields. Rivals only, best first; no id of an unmet civ."""
        return {
            "players_alive": self.alive,
            "all_scores": [c.score for c in sorted(self.rivals, key=lambda c: -c.score)],
            "rank": self.rank,
            "leader_score": self.leader_score,
        }

    def describe(self) -> str:
        rank = f"rank={self.rank if self.ours else 'unknown'}/{self.alive}"
        scores = " ".join(
            f"{c.name or 'unmet'}={c.score}" for c in sorted(self.civs, key=lambda c: -c.score)
        )
        gap = "unknown" if self.gap is None else f"{self.gap:+d}"
        leaders = "; ".join(
            f"{x.short}:{'unmet' if x.player_id == UNMET else self._name(x.player_id)}"
            f" {x.progress:g}"
            for x in self.leaders
        )
        return (
            f"{rank} alive={self.alive} unmet={self.unmet} leader_score={self.leader_score} "
            f"gap={gap} scores=[{scores}] victory_leaders=[{leaders or 'none'}]"
        )

    def _name(self, player_id: int) -> str:
        if player_id == self.our_id:
            return "us"
        return next((c.name or str(player_id) for c in self.civs if c.player_id == player_id), "?")


def parse_world_rankings(output: str) -> WorldRankings | None:
    """Read the reply. None unless it is complete: a bad read is never half used."""
    our_id: int | None = None
    civs: list[CivScore] = []
    leaders: list[VictoryLeader] = []
    done = False
    try:
        for line in output.splitlines():
            if line.startswith(_ME):
                our_id = int(line[len(_ME) :])
            elif line.startswith(_SCORE):
                pid, name, score = line[len(_SCORE) :].split("|")
                if int(score) < 0:
                    return None  # A living civ whose score could not be read: not a full table.
                civs.append(CivScore(int(pid), name.strip(), int(score)))
            elif line.strip() == _DONE:
                done = True
            elif line.startswith(_LEAD):
                victory, pid, progress = line[len(_LEAD) :].split("|")
                leaders.append(VictoryLeader(victory, int(pid), float(progress)))
    except ValueError:
        return None
    if not done or our_id is None or not civs:
        return None
    civs.sort(key=lambda c: (-c.score, c.player_id))
    return WorldRankings(our_id, tuple(civs), tuple(leaders))


class LuaGame(Protocol):
    async def execute_lua(self, code: str, context: str = "gamecore") -> str: ...


async def read_world_rankings(game: LuaGame) -> WorldRankings | None:
    """One call: the UI context first (the screen's own), GameCore if that fails."""
    for context in ("ingame", "gamecore"):
        try:
            rankings = parse_world_rankings(await game.execute_lua(_WORLD_RANKINGS_LUA, context))
        except ConnectionError:
            raise
        except Exception:
            log.debug("World rankings read failed in %s", context, exc_info=True)
            continue
        if rankings is not None:
            return rankings
    return None


class RankingsHolder:
    """The newest reading, shared between the turn handler and the places that use it."""

    def __init__(self) -> None:
        self.last: WorldRankings | None = None

    def current(self, turn: int | None = None) -> WorldRankings | None:
        """The reading, only if it is from this turn: an old one would mislead the record."""
        last = self.last
        return last if last is not None and (turn is None or last.turn == turn) else None


class _OverviewGame(Protocol):
    async def get_game_overview(self) -> object: ...


class WorldRankingsLog:
    """Turn handler: read the rankings, keep them, and log one `world_rankings:` line."""

    name = "world_rankings"

    def __init__(self, lua_game: LuaGame, holder: RankingsHolder) -> None:
        self._lua = lua_game
        self._holder = holder
        self._warned = False

    async def run(self, game: _OverviewGame) -> None:
        self._holder.last = None  # Whatever fails below, an old turn's table is never reused.
        turn = int((await game.get_game_overview()).turn)  # type: ignore[attr-defined]
        rankings = await read_world_rankings(self._lua)
        if rankings is None:
            self._holder.last = None
            if not self._warned:
                self._warned = True
                log.warning("world_rankings: turn=%s could not read the rankings", turn)
            return
        rankings = WorldRankings(rankings.our_id, rankings.civs, rankings.leaders, turn)
        self._holder.last = rankings
        log.info("world_rankings: turn=%s %s", turn, rankings.describe())
