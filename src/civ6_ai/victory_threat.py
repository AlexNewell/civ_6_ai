"""Notice a rival nearing a victory, and log it every turn (roadmap item 1.46).

`VictoryWatch` is fed by `VictoryMonitor`, which reads `get_victory_progress` every
`VICTORY_PERIOD` turns. Nothing here makes a game call. `VictoryThreatLog` is a turn handler that
logs one `victory_threat:` line each turn from the last reading, and says how old that reading is.
The same line carries our science rank among the met civs (issue 182), from the same reading.

A rival is flagged on a victory path in two ways:

- `near`: it is close to the end condition. Diplomatic points and religions at `NEAR_SHARE` of
  what a win needs, science at `NEAR_SCIENCE_SHARE` of the space race points or with a spaceport
  built (a spaceport comes well before the points do).
- `lead`: it leads by a clear margin. On points, `LEAD_MARGIN_SHARE` of what a win needs ahead of
  the next civ (counting us), from `LEAD_MIN_SHARE`. On science, `TECH_MARGIN` techs ahead of
  every other civ (counting us), from `TECH_FLOOR` techs.

The numbers are not tuned. The run logs hold no per-turn science points, techs or spaceports, only
scores, so they could only be checked against the warning events: in the last runs a rival's space
race points were first seen at 17 to 18 of 33 (turns 306 and 319), and the game ended 3 to 8 turns
later, so the points alone give little warning. Techs and spaceports are the earlier signals, and
the first live run with this line shows how early they fire.

Only met civs are read, as in `VictoryMonitor`: `get_victory_progress` lists those. Conquest and
culture are not assessed, because the reads can't say who is responsible or what a rival's culture
tourism is.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

SCIENCE = "VICTORY_TECHNOLOGY"
DIPLOMATIC = "VICTORY_DIPLOMATIC"
RELIGIOUS = "VICTORY_RELIGIOUS"
_SHORT = {SCIENCE: "science", DIPLOMATIC: "diplomatic", RELIGIOUS: "religious"}

NEAR_SHARE = 0.5
NEAR_SCIENCE_SHARE = 0.25
LEAD_MIN_SHARE = 0.25
LEAD_MARGIN_SHARE = 0.15
TECH_FLOOR = 40
TECH_MARGIN = 5


@dataclass(frozen=True)
class Standing:
    """The civ furthest along one victory path."""

    path: str
    rival_id: int
    rival: str
    value: int
    needed: int
    margin: int  # Over the next civ on the path, counting us.
    flag: str = ""  # "near", "lead", "near+lead", or "" when it is not a threat.
    detail: str = ""

    @property
    def flagged(self) -> bool:
        return bool(self.flag)

    def describe(self) -> str:
        flag = f" {self.flag}" if self.flag else ""
        detail = f" {self.detail}" if self.detail else ""
        return (
            f"{_SHORT.get(self.path, self.path)}:{self.rival} "
            f"{self.value}/{self.needed} margin {self.margin:+d}{detail}{flag}"
        )


@dataclass(frozen=True)
class ScienceRank:
    """Our science output against the met civs that report one."""

    rank: int
    civs: int
    ours: float
    leader: str
    leader_yield: float
    techs_rank: int

    @property
    def share(self) -> float:
        return self.ours / self.leader_yield if self.leader_yield > 0 else 1.0

    def describe(self) -> str:
        return (
            f"science_rank={self.rank}/{self.civs} yield={self.ours:.1f} "
            f"top={self.leader} {self.leader_yield:.1f} ({self.share:.0%}) "
            f"techs_rank={self.techs_rank}/{self.civs}"
        )


@dataclass(frozen=True)
class Assessment:
    turn: int
    standings: tuple[Standing, ...]
    science_rank: ScienceRank | None

    @property
    def threats(self) -> tuple[Standing, ...]:
        return tuple(s for s in self.standings if s.flagged)

    @property
    def threat_ids(self) -> tuple[int, ...]:
        """Flagged rivals, the most advanced first, each once."""
        ordered = sorted(self.threats, key=lambda s: -s.value / max(s.needed, 1))
        return tuple(dict.fromkeys(s.rival_id for s in ordered))


def _science_standing(players: list[Any], our_id: int) -> Standing | None:
    rivals = [p for p in players if p.player_id != our_id and p.science_vp_needed > 0]
    if not rivals:
        return None
    best = max(rivals, key=lambda p: (p.science_vp, p.spaceports, p.techs_researched))
    others_vp = max((p.science_vp for p in players if p is not best), default=0)
    others_techs = max((p.techs_researched for p in players if p is not best), default=0)
    needed = best.science_vp_needed
    flags = []
    if best.science_vp >= NEAR_SCIENCE_SHARE * needed > 0 or best.spaceports >= 1:
        flags.append("near")
    tech_lead = best.techs_researched - others_techs
    if (
        best.techs_researched >= TECH_FLOOR
        and tech_lead >= TECH_MARGIN
        or best.science_vp >= LEAD_MIN_SHARE * needed
        and (best.science_vp - others_vp >= LEAD_MARGIN_SHARE * needed)
    ):
        flags.append("lead")
    return Standing(
        SCIENCE,
        best.player_id,
        best.name,
        best.science_vp,
        needed,
        best.science_vp - others_vp,
        "+".join(dict.fromkeys(flags)),
        f"spaceports {best.spaceports} techs {best.techs_researched} ({tech_lead:+d})",
    )


def _points_standing(
    path: str, rows: list[tuple], ours: int, names: dict[int, str]
) -> Standing | None:
    """The leading rival on a path from (path, rival_id, name, value, needed) rows."""
    mine = [row for row in rows if row[0] == path and row[1] is not None]
    if not mine:
        return None
    _, rival_id, subject, value, needed = max(mine, key=lambda row: row[3])
    name = names.get(rival_id, subject)
    detail = "" if name == subject else subject
    others = max([ours, *(row[3] for row in mine if row[1] != rival_id)])
    margin = value - others
    flags = []
    if needed > 0 and value >= NEAR_SHARE * needed:
        flags.append("near")
    if needed > 0 and value >= LEAD_MIN_SHARE * needed and margin >= LEAD_MARGIN_SHARE * needed:
        flags.append("lead")
    return Standing(path, rival_id, name, value, needed, margin, "+".join(flags), detail)


def science_rank(players: list[Any], our_id: int) -> ScienceRank | None:
    """Our rank by science yield and by techs among the civs in the read, us included."""
    if not any(p.player_id == our_id for p in players):
        return None
    by_yield = sorted(players, key=lambda p: (-p.science_yield, p.player_id))
    by_techs = sorted(players, key=lambda p: (-p.techs_researched, p.player_id))
    me = next(p for p in players if p.player_id == our_id)
    top = by_yield[0]
    return ScienceRank(
        rank=by_yield.index(me) + 1,
        civs=len(players),
        ours=me.science_yield,
        leader=top.name,
        leader_yield=top.science_yield,
        techs_rank=by_techs.index(me) + 1,
    )


def assess(turn: int, players: list[Any], rows: list[tuple], our_id: int) -> Assessment:
    """Pure. `rows` are the victory monitor's (path, rival_id, name, value, needed) rows."""
    ours = next((p for p in players if p.player_id == our_id), None)
    standings: list[Standing] = []
    if any(row[0] == SCIENCE for row in rows):
        science = _science_standing(players, our_id)
        if science:
            standings.append(science)
    for path, mine in (
        (DIPLOMATIC, ours.diplomatic_vp if ours else 0),
        (RELIGIOUS, 0),
    ):
        standing = _points_standing(path, rows, mine, {q.player_id: q.name for q in players})
        if standing:
            standings.append(standing)
    return Assessment(turn, tuple(standings), science_rank(players, our_id))


def format_line(turn: int, read: Assessment | None) -> str:
    if read is None:
        return f"victory_threat: turn={turn} no reading yet"
    flagged = "; ".join(s.describe() for s in read.threats) or "none"
    leaders = "; ".join(s.describe() for s in read.standings if not s.flagged) or "none"
    rank = read.science_rank.describe() if read.science_rank else "science_rank=unknown"
    return (
        f"victory_threat: turn={turn} read={read.turn} age={turn - read.turn} "
        f"flagged=[{flagged}] others=[{leaders}] {rank}"
    )


class VictoryWatch:
    """The last assessment, carried between readings."""

    def __init__(self) -> None:
        self.last: Assessment | None = None

    def update(self, assessment: Assessment) -> None:
        self.last = assessment

    def reset(self) -> None:
        self.last = None

    def threat_ids(self) -> tuple[int, ...]:
        return self.last.threat_ids if self.last else ()


class _OverviewGame(Protocol):
    async def get_game_overview(self) -> Any: ...


class VictoryThreatLog:
    """Turn handler: one `victory_threat:` line each turn, from the last reading."""

    name = "victory_threat"

    def __init__(self, watch: VictoryWatch) -> None:
        self._watch = watch
        self._flagged: tuple[int, ...] = ()
        self._last_turn: int | None = None

    async def run(self, game: _OverviewGame) -> None:
        turn = (await game.get_game_overview()).turn
        if self._last_turn is not None and turn < self._last_turn:
            self._watch.reset()  # A save was loaded, so the old reading is of another game.
        self._last_turn = turn
        read = self._watch.last
        log.info(format_line(turn, read))
        ids = read.threat_ids if read else ()
        if ids != self._flagged:
            if ids:
                log.warning(
                    "victory_threat: now flagged rivals %s (was %s)", list(ids), list(self._flagged)
                )
            self._flagged = ids
