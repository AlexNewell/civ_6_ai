"""Defence: attack what is in reach when the odds are good, heal, garrison and spend promotions.

`MilitaryPolicy` is a turn handler (see `orchestrator.Handler`). It makes no model calls and the
same state and settings always give the same orders. Run it after `SettlingPolicy` and before the
blocker resolver, so that settling has moved its escorts first and the resolver's skip comes last.

This is the defensive slice of issue 13. It does not declare wars, march on a civ or change what
is built. What it does, in the order a unit is considered (a unit gets one order a turn):

1. Attack, in three steps. A walled city strikes a hostile unit within 2 tiles first, as it takes
   no damage back. Then units attack as a group where together they are planned to kill the
   target this turn, each above the lower `planned_kill_hp_after` floor. Then each remaining
   unit attacks alone when the odds favour it (`assess_attack`). A ranged unit shoots; it never
   closes to melee range on purpose. The best attack turned down for each target is logged.
2. Heal. A unit below `heal_below` of its health fortifies until healed, in a city, or walks to
   the nearest city first. It is never left outside on its own to recover.
3. Garrison. A city with hostile units near it, and fewer defenders than `garrison_required`,
   calls the nearest spare unit.
4. Clear a barbarian camp, only when the force on hand is `camp_force_ratio` times theirs.
5. Idle units in a threatened city fortify, and units outside run for a city if hostile units
   are close.
6. Spare units (see below) get one useful order. They hunt a hostile unit just out of reach when
   the odds favour it, and the destination is within `spare_leash` of a city. Otherwise they
   fortify in place. A spare unit with nothing to do is logged as such.

A spare unit is a healthy combat unit that is not reserved for settling and is not needed for its
city's `garrison_required`. A unit outside a city is spare. Step 1 already covers attacks on
units of a civ we are at war with and on barbarians, as `Threat.hostile` is true for both, so
no second war list is kept here. Step 4 already covers camps, so spare units are not sent
anywhere else. At most `max_spare_orders` spare orders go out in a turn.

A refused move to a city is not the end of the unit's turn: it tries up to `fallback_tiles` tiles
beside the city, then fortifies where it stands.

Promotions are spent from the `NOTIFICATION_UNIT_PROMOTION_AVAILABLE` notification every turn,
and through `blocker_policies()` when the game blocks the turn on one. Live, the notification sat
unanswered for 20 turns because it did not block, so the blocker alone is not enough.

Units settling may use (scouts and settler escorts) are never touched: `units_reserved_for_settling`
is the single definition, and settling picks its escort with the same function.

Combat odds. civ6-mcp's GameState has no separate combat estimate: `attack_unit` runs one only to
print it, then attacks. So the odds here use the documented Civ VI damage rule, with named
constants, on strengths the player can see. The defender's terrain and fortification are not
visible, so `assumed_defender_bonus` stands in for them. All settings are in `MilitaryWeights` and
`MilitarySettings`, plain inputs for the Phase 2 strategist.
"""

import json
import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from civ6_ai.blockers import Blocker, BlockerType, action_succeeded, move_succeeded
from civ6_ai.policies.game_data import COMBAT_UNITS
from civ6_ai.policies.settling import (
    _SEA_UNITS,
    SCOUT,
    hex_line,
    neighbours,
    spare_garrison_tiles,
    units_reserved_for_settling,
)
from civ6_ai.state import City, GameReads, Threat, Unit, read_area
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_threats as _read_threats
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.fog import BARBARIAN_ID
from civ6_ai.state.models import Target
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

log = logging.getLogger(__name__)

# The damage rule. A fight between equals costs each side BASE_DAMAGE hit points, and each point
# of strength difference scales the damage by e^DAMAGE_PER_STRENGTH (about 4%, so 17 points
# double it). Both are the community-documented figures, not read from this game: check them
# against an attack's "your HP: a -> b" line (docs/live-checks.md). `damage_variance` covers the
# game's random roll, and the odds are judged on its bad side.
BASE_DAMAGE = 24.0
DAMAGE_PER_STRENGTH = 0.04

CAMP_IMPROVEMENT = "IMPROVEMENT_BARBARIAN_CAMP"
PROMOTION_AVAILABLE = "NOTIFICATION_UNIT_PROMOTION_AVAILABLE"
_NOT_LAND = (
    "GALLEY",
    "QUADRIREME",
    "CARAVEL",
    "FRIGATE",
    "IRONCLAD",
    "DESTROYER",
    "CRUISER",
    "BATTLESHIP",
    "CARRIER",
    "SUBMARINE",
    "PRIVATEER",
    "SEADOG",
    "CORSAIR",
    "UBOAT",
    "LONGSHIP",
    "BIREME",
    # Run 13: two Drones (the Observation Balloon's upgrade, which cannot fortify) stood in Coba
    # and counted as its garrison, so the city read as held and built a Trader while Mapuche
    # artillery and mechanized infantry closed in. They hold nothing.
    "DRONE",
    "BALLOON",
    "FIGHTER",
    "BOMBER",
    "HELICOPTER",
    "ROCKET",
    "MISSILE",
)
_ATTACK_TAGS = ("RANGE_ATTACK", "MELEE_ATTACK", "AIR_ATTACK")
_DAMAGE_DEALT = re.compile(r"damage dealt:~?(\d+)")
_ENEMY_HP = re.compile(r"enemy HP:\s*(-?\d+)\s*->\s*(-?\d+)")
_HP_AFTER = re.compile(r"your HP:\s*-?\d+\s*->\s*(-?\d+)")
_NOW_AT = re.compile(r"now_at:(-?\d+),(-?\d+)")
_ALREADY_ON = re.compile(r"STACKING_CONFLICT.*already on \((-?\d+),\s*(-?\d+)\)")
# Units that hold a tile like a military unit does: the game refuses a second one of the class
# (Run 7: "Friendly UNIT_MILITARY_ENGINEER already on (64,28). Cannot stack same formation class").
_TILE_HOLDERS = ("MILITARY_ENGINEER",)
WANTED_DEFENDERS = 2  # In a threatened city or on a tile beside it.
SWAP_REACH = 2  # How near a stronger unit must be to be swapped in for a weaker holder.
SWAP_MARGIN = 1.3  # The newcomer must be this much stronger than the unit it replaces.


def is_land_military(unit: Unit) -> bool:
    """A combat unit this policy may order: not a scout, ship or aircraft."""
    return (
        unit.is_combat
        and unit.unit_type != SCOUT
        and not any(word in unit.unit_type for word in _NOT_LAND)
    )


def _unit_key(unit: Unit) -> int:
    return unit.unit_id


def holds_tile(unit: Unit) -> bool:
    """True for a unit the game will not stack with a military unit (`STACKING_CONFLICT`)."""
    return is_land_military(unit) or any(word in unit.unit_type for word in _TILE_HOLDERS)


def _hold_strength(unit: Unit) -> float:
    """How well a unit holds a city. A unit that is no defender (an engineer) counts as nothing."""
    stats = COMBAT_UNITS.get(unit.unit_type)
    if stats is not None:
        return max(float(stats[0]), 0.9 * stats[1])
    return float(max(unit.combat_strength, 0.9 * unit.ranged_strength))


@dataclass(frozen=True)
class MilitaryWeights:
    """How cautious the unit is. Fractions are of a unit's maximum health."""

    heal_below: float = 0.7  # A unit under this goes to heal instead of fighting.
    min_hp_after_attack: float = 0.4  # Health left after the bad-side roll, or no attack.
    garrison_min_hp_after: float = 0.6  # The same, for the unit holding a city on its own.
    # Dealt over taken for a target on our own doorstep (within `defend_radius` of a city). The
    # unit is judged on survival alone: damage taken while a city holds it is cheap, doing nothing
    # lets the target pillage, and the attack is one blow of a group fight. 1.0 turns it off.
    defend_damage_ratio: float = 0.0
    min_damage_ratio: float = (
        1.0  # Dealt over taken, unless it kills (Run 15: 1.5 to 1.0, score 61 to 69)
    )
    damage_variance: float = 0.2  # The game rolls damage within this fraction either way.
    assumed_defender_bonus: float = 6.0  # Strength for terrain and fortification we can't see.
    kill_bonus: float = 50.0  # Rank a kill above any amount of damage.
    # Health left after the bad-side roll, for a unit in a planned group kill. Below the lone
    # floor of 0.4 because the plan removes the target this turn, so the unit is not left next to
    # it, and a hurt unit heals in a city. Still well above 0: the roll is already the bad side,
    # and the plan is abandoned as soon as an attack goes worse than planned.
    planned_kill_hp_after: float = 0.25
    camp_force_ratio: float = 2.0  # Our strength over theirs, to go for a camp.
    camp_min_hp: float = 0.8  # Only healthy units are sent to a camp.
    camp_leave_hp: float = 0.5  # A unit on a camp mission goes home once under this health.


@dataclass(frozen=True)
class MilitarySettings:
    """Bounds on reach, retrying and work per turn, so nothing stalls or runs away."""

    weights: MilitaryWeights = field(default_factory=MilitaryWeights)
    max_garrison: int = 2  # Defenders wanted in a city under heavy threat.
    reinforce_threat_count: int = 3  # Hostile units near a city that call for a second defender.
    garrison_reach: int = 4  # How far a spare unit is called from.
    reinforce_reach: int = 8  # The same, for a short city with hostile units at it.
    retreat_reach: int = 6  # How far a hurt or exposed unit will walk to a city.
    flee_radius: int = 3  # A unit outside a city this close to a hostile unit goes home.
    camp_search_radius: int = 8  # Barbarians this close to a city start a look for a camp.
    camp_scan_radius: int = 3  # Tiles read around the nearest barbarian to find the camp.
    camp_home_scan_radius: int = 7  # Tiles read around each city to find a camp in plain view.
    camp_home_scan_turns: int = 3  # Turns between those reads, as they cost a call per city.
    camp_home_radius: int = 13  # A camp further than this from every city is left alone.
    # A force gathers this close to a camp before any unit goes in, so the first to arrive
    # does not meet the guards alone (the units of a far camp arrive on different turns).
    camp_muster_radius: int = 4
    muster_bad_turns: int = 10  # A muster tile the game refused is not tried again for this long.
    camp_reach: int = 14  # How far a unit is called from to a camp (idle units sat 8-14 off).
    # Run 15 practice save: a camp 8 tiles off sent raiders for 30 turns while twelve spare units
    # sat at home, because a camp is only known once a unit has seen it. With spare units idle and
    # no known camp, a small party walks to where barbarians were last seen, and what it sees is
    # scanned. 0 parties turns this off.
    recon_units: int = 3  # Party size. It leaves together or not at all.
    recon_min_spare: int = 4  # Spare units needed before the party goes.
    recon_radius: int = 14  # Sightings further than this from every city are ignored.
    recon_push: int = 6  # Tiles past the farthest sighting the party aims for.
    recon_turns: int = 12  # A party that has not arrived by now is called off.
    recon_memory_turns: int = 25  # How long a sighting is believed.
    camp_mission_turns: int = 6  # A unit sent at a camp keeps to it this long after the last order.
    camp_defender_radius: int = 2  # Barbarians this close to a camp defend it.
    camp_min_units: int = 2
    max_camp_turns: int = 8  # Turns spent assaulting one camp before it is set aside.
    camp_travel_turns: int = 20  # Turns a force may take to reach the muster point first.
    camp_retry_turns: int = 30
    camp_idle_turns: int = 10  # A camp not advanced on for this long starts its count again.
    city_strike_range: int = 2  # civ6-mcp: "city attack range is 2".
    fallback_tiles: int = (
        2  # Other tiles tried for a unit whose move was refused, before it fortifies.
    )
    split_stacks: bool = True  # Move all but one unit off a tile that two of ours share.
    stack_tiles: int = 4  # Tiles tried for a unit leaving a stack, as the game may refuse some.
    defend_radius: int = 3  # A target this close to one of our cities is on our doorstep.
    spare_hunt_radius: int = 3  # A hostile unit this close to a spare unit may be walked at.
    spare_leash: int = 4  # A spare unit never goes further than this from its nearest city.
    # Run 15: a settler waited on a scout that could not leave while three spare units fortified
    # beside unexplored land. While no hostile unit is in sight a spare unit explores (the game's
    # own explorer), up to this many at once; the first hostile unit in sight brings it home
    # through the usual flee step. 0 turns this off, as a map that is mostly barbarians may want.
    spare_explorers: int = 2
    spare_explore_until: int = 120  # Turn after which spare units stay home.
    explore_clear_radius: int = 6  # A hostile land unit this close to a unit keeps it at home.
    max_spare_orders: int = 6  # Hunt and fortify orders for spare units in one turn.
    max_actions: int = 12  # Orders sent to the game in one turn, whatever the number of units.
    attempts_per_unit: int = 2  # Calls spent on one unit in a turn, as each refusal costs one.
    retry_turns: int = 3  # How long a refused order for a unit is not repeated.
    max_promotion_checks: int = 10  # Units asked for promotions in one turn.
    max_promotions: int = 6
    decisions_kept: int = 500
    # Taken first when offered, then the first listed. Names are as the game gives them.
    promotion_preference: tuple[str, ...] = (
        "PROMOTION_GARRISON",
        "PROMOTION_TORTOISE",
        "PROMOTION_BATTLECRY",
        "PROMOTION_VOLLEY",
        "PROMOTION_ARROW_STORM",
    )
    map_width: int | None = STANDARD_MAP_WIDTH
    map_height: int = 46  # The standard map's rows; a projected recon target stays inside them.


# ----- odds -----


def expected_damage(attack: float, defence: float) -> float:
    """Hit points an attack of strength `attack` takes off a defender of strength `defence`."""
    return BASE_DAMAGE * math.exp(DAMAGE_PER_STRENGTH * (attack - defence))


@dataclass(frozen=True)
class AttackOdds:
    """What one attack is expected to do, before the roll and on the bad side of it."""

    dealt: float  # Expected damage to the target.
    taken: float  # Expected damage back to the attacker.
    sure_kill: bool  # Dead even on a low roll.
    hp_after: float  # The attacker's health after a high roll against it.
    favourable: bool
    rank: float


def assess_attack(
    unit: Unit,
    target: Target,
    defender: Threat,
    weights: MilitaryWeights,
    min_hp_after: float | None = None,
    map_width: int | None = STANDARD_MAP_WIDTH,
    min_ratio: float | None = None,
) -> AttackOdds | None:
    """Judge an attack by `unit` on `target`, or None if the target has no strength to judge.

    A ranged unit attacks with its ranged strength and takes damage back only from a defender
    that has a ranged attack of its own. A melee unit trades blows.

    These odds are the bot's own. A reply that opens "Combat Estimate (Melee)" with the melee
    strength and ~24 damage back is civ6-mcp's own estimate text (issue 266: Run 20, turn 57 was
    a RANGE_ATTACK at distance 1); it never feeds `taken`, so the logged odds were already ranged.
    """
    defence = defender.combat_strength + weights.assumed_defender_bonus
    if defender.combat_strength <= 0 and defender.ranged_strength <= 0:
        return None  # A civilian. Capturing one is not defence.
    ranged = unit.ranged_strength > 0
    attack = unit.ranged_strength if ranged else unit.combat_strength
    if attack <= 0:
        return None
    dealt = expected_damage(attack, defence)
    if ranged:
        # Only a defender with a ranged attack of its own hits back, and our melee strength defends.
        hits_back = defender.ranged_strength > 0
        taken = (
            expected_damage(defender.ranged_strength, unit.combat_strength or attack)
            if hits_back
            else 0
        )
    else:
        taken = expected_damage(defence, unit.combat_strength)  # The same difference, reversed.
    v = weights.damage_variance
    sure_kill = dealt * (1 - v) >= target.hp
    hp_after = unit.hp - taken * (1 + v)
    floor = weights.min_hp_after_attack if min_hp_after is None else min_hp_after
    safe = hp_after >= floor * unit.max_hp
    worthwhile = (
        sure_kill
        or taken == 0
        or dealt * (1 - v)
        >= (weights.min_damage_ratio if min_ratio is None else min_ratio) * taken * (1 + v)
    )
    rank = min(dealt, target.hp) - taken + (weights.kill_bonus if sure_kill else 0.0)
    return AttackOdds(dealt, taken, sure_kill, hp_after, safe and worthwhile, rank)


def attack_landed(result: str) -> bool:
    """True if the attack happened. `attack_unit` puts its combat estimate before the result, so
    a refusal does not start with "Error" and `action_succeeded` alone would call it a success.
    """
    lines = [line.strip() for line in result.splitlines()]
    return any(line.startswith(_ATTACK_TAGS) for line in lines) and not any(
        line.startswith("Error") for line in lines
    )


def attack_failed(unit: Unit, result: str) -> str | None:
    """Why an attack that `attack_landed` calls a success did nothing, or None if it did something.

    Run 20: a ranged unit's order came back as `MELEE_ATTACK` with `enemy HP:100 -> 100/100`.
    The unit was stacked with another military unit, and civ6-mcp fell back to a melee attack
    that deals nothing. A ranged unit's melee reply, or any reply showing the enemy's hit points
    unchanged, is a failure.
    """
    lines = [line.strip() for line in result.splitlines()]
    if unit.ranged_strength > 0 and any(line.startswith("MELEE_ATTACK") for line in lines):
        return "a ranged unit's attack came back as MELEE_ATTACK"
    for found in _ENEMY_HP.finditer(result):
        if found.group(1) == found.group(2):
            return f"the enemy's hit points did not change ({found.group(1)})"
    return None


def garrison_required(city: City, settings: MilitarySettings) -> int:
    """Defenders a city should hold: one, and a second when hostile units crowd it."""
    if len(city.threats) >= settings.reinforce_threat_count:
        return settings.max_garrison
    return 1


def camp_force_ok(
    attackers: Iterable[Unit],
    defenders: Iterable[Threat],
    weights: MilitaryWeights,
    min_units: int = 2,
) -> bool:
    """True if the units on hand are clearly stronger than the camp's, counting health.

    An unseen camp garrison can be larger, so the ratio is a margin and not a promise.
    """
    ours = [u for u in attackers if u.hp >= weights.camp_min_hp * u.max_hp]
    if len(ours) < min_units:
        return False
    ours_total = sum(max(u.combat_strength, u.ranged_strength) * u.hp / u.max_hp for u in ours)
    theirs_total = sum(
        max(d.combat_strength, d.ranged_strength) + weights.assumed_defender_bonus
        for d in defenders
    )
    return ours_total >= weights.camp_force_ratio * theirs_total


def choose_promotion(options: Iterable[Any], preference: tuple[str, ...]) -> Any | None:
    """The preferred option, else the first offered. Equal choices go to the one listed first."""
    options = list(options)
    rank = {name: i for i, name in enumerate(preference)}
    ordered = sorted(
        enumerate(options), key=lambda e: (rank.get(e[1].promotion_type, len(rank)), e[0])
    )
    return ordered[0][1] if ordered else None


# ----- the handler -----


@dataclass(frozen=True)
class MilitaryDecision:
    """One order, or one reason for none, with the evidence."""

    turn: int
    unit_id: int
    x: int
    y: int
    # attack, city_strike, heal, retreat, garrison, camp, hunt, fortify, flee, promote, skip
    action: str
    reason: str
    target: tuple[int, int] | None = None
    odds: dict[str, Any] | None = None
    result: str = ""


class MilitaryGame(GameReads, Protocol):
    """The reads plus the actions this policy takes."""

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...

    async def attack_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...

    async def fortify_unit(self, unit_index: int) -> str: ...

    async def heal_unit(self, unit_index: int) -> str: ...

    async def city_attack(self, city_id: int, target_x: int, target_y: int) -> str: ...

    async def get_notifications(self) -> list[Any]: ...

    async def get_unit_promotions(self, unit_id: int) -> Any: ...

    async def promote_unit(self, unit_id: int, promotion_type: str) -> str: ...


@dataclass
class _Turn:
    """Everything known and spent in one turn. A new one is made each run, so none carries over."""

    turn: int
    units: tuple[Unit, ...]
    cities: tuple[City, ...]
    threats: tuple[Threat, ...]
    mine: list[Unit]
    reserved: frozenset[int]
    actions: int = 0
    acted: set[int] = field(default_factory=set)
    calls: dict[int, int] = field(default_factory=dict)
    dead: set[tuple[int, int]] = field(default_factory=set)
    # Tiles where a target died: the reply said so, our estimate took its last hit points, or a
    # later attack found nobody there (Run 16: the game's reply says "killed" only sometimes).
    killed: set[tuple[int, int]] = field(default_factory=set)
    # Of those, the ones the game's reply confirmed (not an estimate that took the last hit
    # points): only these leave the threat map and the threat state.
    confirmed: set[tuple[int, int]] = field(default_factory=set)
    hp: dict[int, float] = field(default_factory=dict)
    target_hp: dict[tuple[int, int], float] = field(default_factory=dict)
    at_city: dict[tuple[int, int], int] = field(default_factory=dict)
    camp_scanned: bool = False
    placed: dict[int, tuple[int, int]] = field(default_factory=dict)  # Where a unit moved to.
    blocked: set[tuple[int, int]] = field(default_factory=set)  # Tiles the game said are full.
    en_route: dict[int, int] = field(default_factory=dict)  # City id to cover units still walking.
    spare_orders: int = 0
    # What the threat map changed this turn (issue 238), for the offense line and the slice
    # report: camps known only through the map, whether the target came from it, and so on.
    map_use: dict[str, int] = field(default_factory=dict)
    fired: set[int] = field(default_factory=set)  # Cities that have used their strike.
    struck: set[tuple[int, int]] = field(default_factory=set)  # Targets a city has hit.
    attacked: set[tuple[int, int]] = field(default_factory=set)  # Targets any attack landed on.
    # Per target, the best attack turned down: its odds, the unit and the reason.
    rejected: dict[tuple[int, int], tuple[AttackOdds, Unit, str]] = field(default_factory=dict)


@dataclass
class MilitaryPolicy:
    """Defends the empire. Run it after settling and before the blocker resolver."""

    settings: MilitarySettings = field(default_factory=MilitarySettings)
    name: str = "military"
    decisions: list[MilitaryDecision] = field(default_factory=list)
    # Per unit, kept across turns and pruned each turn against the units that still exist.
    _resting: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    # Units sent exploring, with where each stood when last seen. One that has not moved since is
    # done (the game's explorer stops when nothing is left to find) and is free to be used again.
    _explorers: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _fortified: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _refused: dict[tuple[int, str], int] = field(default_factory=dict, init=False, repr=False)
    _strike_refused: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _promotion_refused: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _promotion_asked: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    # Per camp tile: turns spent advancing on it and the last of them; and when it is set aside to.
    _camp_turns: dict[tuple[tuple[int, int], bool], tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _camp_aside: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _camps: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _sightings: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _recon: dict[int, tuple[tuple[int, int], int]] = field(
        default_factory=dict, init=False, repr=False
    )  # Unit id to the tile it was sent to see and the turn.
    _camp_baseline: bool = field(default=True, init=False, repr=False)
    _recon_bad: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _recon_started: int = field(default=-99, init=False, repr=False)
    # The shared threat map (issue 238), set by the bot's wiring; camps come from it too.
    threat_map: Any = field(default=None, init=False, repr=False)
    _released: int = field(default=0, init=False, repr=False)
    # Camps marched on that have gone, reported once each on the next offense line.
    _camps_gone: int = field(default=0, init=False, repr=False)
    _gone_noted: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    _muster_bad: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _mission: dict[int, tuple[tuple[int, int], int]] = field(
        default_factory=dict, init=False, repr=False
    )  # Unit id to the camp it was sent at and the turn.
    _camp_home_scan: int = field(default=-99, init=False, repr=False)
    # The threat state (see `defence.DefenceState`), or None to play without it.
    defence: Any = field(default=None, repr=False)

    def blocker_policies(self) -> Mapping[str, Any]:
        """Policies to pass to `BlockerResolver`, which takes over the promotion blocker."""
        return {BlockerType.UNIT_PROMOTION: self.resolve_promotions}

    async def resolve_promotions(self, game: MilitaryGame, blocker: Blocker) -> bool:
        turn = (await game.get_game_overview()).turn
        units = await _read_units(game, self.settings.map_width)
        return await self._promote(game, units, turn)

    async def run(self, game: MilitaryGame) -> None:
        s = self.settings
        turn = (await game.get_game_overview()).turn
        units = await _read_units(game, s.map_width)
        self._forget(units, turn)
        await self._spend_promotions(game, units, turn)
        mine = [u for u in units if is_land_military(u)]
        if not mine:
            return
        cities = await _read_cities(game, s.map_width)
        threats = await _read_threats(game)
        lone_ok: frozenset = frozenset()
        if self.defence is not None:  # The same read settling makes, so both agree on who leaves.
            lone_ok = spare_garrison_tiles(
                cities, threats, self.defence.at_war, map_width=s.map_width
            )
        reserved = units_reserved_for_settling(
            units, [(c.x, c.y) for c in cities], map_width=s.map_width, lone_ok=lone_ok
        )
        mine = [u for u in mine if u.unit_id not in reserved]
        t = _Turn(turn, tuple(units), cities, threats, mine, reserved)
        self._count_defenders(t)
        if await self._split_stacks(game, t):
            # The moved units stand elsewhere now, with targets seen from their new tiles.
            units = await _read_units(game, s.map_width)
            t.units = tuple(units)
            t.mine = [u for u in units if is_land_military(u) and u.unit_id not in t.reserved]
            self._count_defenders(t)
        await self._attack(game, t)
        await self._heal(game, t)
        await self._garrison(game, t)
        self._note_sightings(t)
        await self._clear_camp(game, t)
        await self._spare_units(game, t)
        await self._idle(game, t)
        self._publish_defence(t)
        self._log_offense(t)

    @staticmethod
    def _count_defenders(t: _Turn) -> None:
        """Count the land military units standing on each of our city tiles."""
        t.at_city.clear()
        city_tiles = {(c.x, c.y) for c in t.cities}
        for u in t.mine:  # A reserved unit may leave with a settler, so it is not a defender.
            if (u.x, u.y) in city_tiles:
                t.at_city[u.x, u.y] = t.at_city.get((u.x, u.y), 0) + 1

    # ----- stacks (issue 266) -----

    def _stack_tiles(self, t: _Turn, unit: Unit) -> list[tuple[int, int]]:
        """Free tiles beside the unit, best first, for the unit to leave a stack for.

        A tile with a hostile unit in reach of the unit's attack comes first, so it can still
        fight this turn. Then the least threat and the most cover on the threat map.
        """
        w = self.settings.map_width
        here = (unit.x, unit.y)
        taken = {self._pos(t, u) for u in t.units if u.unit_id != unit.unit_id and holds_tile(u)}
        taken |= {(th.x, th.y) for th in t.threats} | t.blocked | {(c.x, c.y) for c in t.cities}
        found = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x, y = unit.x + dx, unit.y + dy
                if w:
                    x %= w
                if (x, y) != here and hex_distance(unit.x, unit.y, x, y, w) == 1:
                    found.add((x, y))
        reach = 2 if unit.ranged_strength > 0 else 1
        hostile = [(th.x, th.y) for th in t.threats if th.hostile]

        def rank(p: tuple[int, int]) -> tuple[int, float, float, tuple[int, int]]:
            can_attack = any(hex_distance(p[0], p[1], h[0], h[1], w) <= reach for h in hostile)
            if self.threat_map is None:
                return (0 if can_attack else 1, 0.0, 0.0, p)
            return (
                0 if can_attack else 1,
                self.threat_map.threat_at(p[0], p[1], t.turn),
                -self.threat_map.cover_at(p[0], p[1]),
                p,
            )

        return sorted(found - taken, key=rank)

    async def _split_stacks(self, game: MilitaryGame, t: _Turn) -> int:
        """Move all but one unit off any tile that two of our land military units share.

        The game places a unit trained or upgraded in a city on a tile that already holds one,
        and the second cannot make a ranged attack until it moves off (Run 20: the attack came
        back as a no-damage MELEE_ATTACK). The unit kept is the one already fortified, then the
        stronger one. The rest go to the best free adjacent tile, trying the next when a move is
        refused. Returns how many units moved.
        """
        if not self.settings.split_stacks:
            return 0
        stacks: dict[tuple[int, int], list[Unit]] = {}
        for u in t.mine:
            stacks.setdefault((u.x, u.y), []).append(u)
        moved = 0
        for where, group in sorted(stacks.items()):
            if len(group) < 2:
                continue
            keeper = max(
                group,
                key=lambda u: (
                    self._fortified.get(u.unit_id) == where,
                    _hold_strength(u),
                    u.hp,
                    -u.unit_id,
                ),
            )
            others = sorted((u for u in group if u is not keeper), key=lambda u: u.unit_id)
            names = ", ".join(f"{u.unit_type} {u.unit_id}" for u in sorted(group, key=_unit_key))
            self._log(
                t, keeper, "stack",
                f"stack found: {len(group)} land military units on one tile ({names}); "
                f"keeping {keeper.unit_type} {keeper.unit_id}",
                where,
            )  # fmt: skip
            for unit in others:
                if await self._split_off(game, t, unit, where):
                    moved += 1
        return moved

    async def _split_off(
        self, game: MilitaryGame, t: _Turn, unit: Unit, where: tuple[int, int]
    ) -> bool:
        """Move one unit of a stack to the best free adjacent tile that accepts it."""
        if not unit.can_move:
            self._log(t, unit, "stack", "stack not split: no moves left", where)
            return False
        for goal in self._stack_tiles(t, unit)[: self.settings.stack_tiles]:
            if t.actions >= self.settings.max_actions:
                break
            result = await self._order(t, unit, "split", game.move_unit(unit.unit_index, *goal))
            if result is not None and move_succeeded(result):
                at = self._reached(result, goal)
                t.placed[unit.unit_id] = at
                self._log(
                    t, unit, "stack",
                    f"stack split: {unit.unit_type} {unit.unit_id} "
                    f"left {where[0]},{where[1]} for {at[0]},{at[1]}",
                    goal, result=result,
                )  # fmt: skip
                return True
            self._learn_full_tile(t, result)
            self._log(t, unit, "stack", "stack split: move refused", goal, result=result or "")
        self._log(t, unit, "stack", "stack not split: no tile accepted the unit", where)
        return False

    # ----- offense summary -----

    def _log_offense(self, t: _Turn) -> None:
        """One line a turn on how much offense there was and what it faced (Run 15).

        The effectiveness report reads it to judge barbarian handling in the early game and
        attacking later. `kills_barb` counts the barbarian targets that died this turn: the reply
        said "killed", the estimated damage took the last hit points, or a later attack found the
        tile empty (Run 16 counted only replies saying "killed", which the game rarely prints, and
        read 0 over a hundred and fifty turns). `camps_gone` counts camps we marched on that are
        no longer known (taken by a unit stepping on the emptied tile, or seen empty).
        Turns with no combat unit write no line.
        """
        s = self.settings
        barbs = [b for b in t.threats if b.owner_id == BARBARIAN_ID and b.hostile]
        barb_tiles = {(b.x, b.y) for b in barbs}
        near = [
            b
            for b in barbs
            if any(hex_distance(b.x, b.y, c.x, c.y, s.map_width) <= s.camp_search_radius
                   for c in t.cities)
        ]  # fmt: skip
        mine = [d for d in self.decisions if d.turn == t.turn]
        landed = [
            d
            for d in mine
            if d.action in ("attack", "city_strike") and "refused" not in d.reason
            and "failed" not in d.reason and "refused" not in d.result
        ]  # fmt: skip
        on_barb = [d for d in landed if d.target in barb_tiles]
        line = {
            "turn": t.turn,
            "combat_units": len(t.mine),
            "spare": sum(
                1 for u in t.mine if self._spare(t, u) and u.hp >= s.weights.heal_below * u.max_hp
            ),
            "barb_visible": len(barbs),
            "barb_near": len(near),
            "attacks_barb": len(on_barb),
            "attacks_other": len(landed) - len(on_barb),
            "kills_barb": len(t.killed & barb_tiles),
            "camps_gone": self._camps_gone,
            "camp_orders": sum(1 for d in mine if d.action == "camp" and "refused" not in d.reason),
            "map_use": {**t.map_use, "missions_released": self._released},
            "map_on": self.threat_map is not None,
        }
        self._released = 0
        self._camps_gone = 0
        log.info("military_offense %s", json.dumps(line, sort_keys=True))

    # ----- bookkeeping -----

    def _forget(self, units: Iterable[Unit], turn: int) -> None:
        """Drop what is kept for units that are gone, have moved or healed, and old set-asides."""
        by_id = {u.unit_id: u for u in units}
        self._resting = {
            k: v
            for k, v in self._resting.items()
            if k in by_id and (by_id[k].x, by_id[k].y) == v and by_id[k].is_hurt
        }
        self._fortified = {
            k: v for k, v in self._fortified.items() if k in by_id and (by_id[k].x, by_id[k].y) == v
        }
        self._recon = {
            k: v
            for k, v in self._recon.items()
            if k in by_id and turn - v[1] <= self.settings.recon_turns
        }
        self._explorers = {
            k: (by_id[k].x, by_id[k].y)
            for k, pos in self._explorers.items()
            if k in by_id and (by_id[k].x, by_id[k].y) != pos
        }
        self._refused = {k: v for k, v in self._refused.items() if v >= turn and k[0] in by_id}
        self._strike_refused = {k: v for k, v in self._strike_refused.items() if v >= turn}
        self._promotion_refused = {
            k: v for k, v in self._promotion_refused.items() if v >= turn and k in by_id
        }
        self._promotion_asked = {k: v for k, v in self._promotion_asked.items() if k in by_id}
        self._camp_aside = {k: v for k, v in self._camp_aside.items() if v >= turn}
        # A camp not advanced on for a while is gone or forgotten, so its count starts again.
        stale = turn - self.settings.camp_idle_turns
        self._camp_turns = {k: v for k, v in self._camp_turns.items() if v[1] >= stale}

    def _record(self, decision: MilitaryDecision) -> None:
        self.decisions.append(decision)
        del self.decisions[: -self.settings.decisions_kept]
        log.info("military_decision %s", json.dumps(asdict(decision), sort_keys=True))

    def _log(
        self,
        t: _Turn,
        unit: Unit,
        action: str,
        reason: str,
        target: tuple[int, int] | None = None,
        **extra: Any,
    ) -> None:
        decision = MilitaryDecision(
            t.turn, unit.unit_id, unit.x, unit.y, action, reason, target, **extra
        )
        self._record(decision)

    def _available(self, t: _Turn, unit: Unit, kind: str) -> bool:
        """True if the unit may be ordered now: not acted on, not refused, within the bounds."""
        s = self.settings
        return (
            unit.unit_id not in t.acted
            and unit.can_move
            and t.actions < s.max_actions
            and t.calls.get(unit.unit_id, 0) < s.attempts_per_unit
            and self._refused.get((unit.unit_id, kind), -1) < t.turn
        )

    async def _order(self, t: _Turn, unit: Unit, kind: str, call: Any) -> str | None:
        """Send one order. Returns the game's text, or None if the call raised."""
        t.actions += 1
        t.calls[unit.unit_id] = t.calls.get(unit.unit_id, 0) + 1
        try:
            return await call
        except ConnectionError:
            raise
        except Exception:
            log.exception("Military order %s failed for unit %s", kind, unit.unit_id)
            self._refused[unit.unit_id, kind] = t.turn + self.settings.retry_turns
            return None

    def _refuse(self, t: _Turn, unit: Unit, kind: str) -> None:
        self._refused[unit.unit_id, kind] = t.turn + self.settings.retry_turns

    def _city_at(self, t: _Turn, unit: Unit) -> City | None:
        return next((c for c in t.cities if (c.x, c.y) == (unit.x, unit.y)), None)

    def _nearest_city(self, t: _Turn, unit: Unit, reach: int) -> City | None:
        s = self.settings
        found = [
            (hex_distance(unit.x, unit.y, c.x, c.y, s.map_width), c.city_id, c) for c in t.cities
        ]
        found = [f for f in found if f[0] <= reach]
        return min(found, key=lambda f: f[:2])[2] if found else None

    def _spare(self, t: _Turn, unit: Unit) -> bool:
        """True if the unit can leave where it stands without leaving a city short."""
        city = self._city_at(t, unit)
        if city is None:
            return self._covering(t, unit) is None
        return t.at_city.get((unit.x, unit.y), 0) > garrison_required(city, self.settings)

    def _reach(self, city: City) -> int:
        """How far a spare unit is called from. Further for a city with hostile units at it.

        Run 10 and 13: the city under attack stood short for turns while spare units sat just past
        `garrison_reach`. A city with a hostile unit within the threat radius and fewer defenders
        than wanted calls from `reinforce_reach`, two turns of marching. Only units that are
        already spare (outside any city, or beyond a city's own garrison) come, so no other
        city is stripped, and each still has to pass the usual walk-home and hp checks.
        """
        s = self.settings
        found = self.defence.cities.get(city.city_id) if self.defence is not None else None
        if found is not None and found.hostile_units > 0 and found.short:
            return max(s.garrison_reach, s.reinforce_reach)
        return s.garrison_reach

    def _state_threatened(self, city: City) -> bool:
        return self.defence is not None and self.defence.is_threatened(city.city_id)

    def _pos(self, t: _Turn, unit: Unit) -> tuple[int, int]:
        return t.placed.get(unit.unit_id, (unit.x, unit.y))

    def _beside(self, t: _Turn, city: City) -> int:
        """Land military units on the tiles around the city."""
        w = self.settings.map_width
        return sum(1 for u in t.mine if hex_distance(city.x, city.y, *self._pos(t, u), w) == 1)

    def _covering(self, t: _Turn, unit: Unit) -> City | None:
        """The threatened city this unit helps hold from the tile beside it, if it is needed."""
        if self.defence is None:
            return None
        w = self.settings.map_width
        for city in t.cities:
            if (
                self._state_threatened(city)
                and hex_distance(city.x, city.y, *self._pos(t, unit), w) == 1
                and t.at_city.get((city.x, city.y), 0) + self._beside(t, city) <= WANTED_DEFENDERS
            ):
                return city
        return None

    def _held(self, t: _Turn, where: tuple[int, int], unit: Unit) -> bool:
        """True if another land military unit already stands on the tile.

        The game refuses a second one (`STACKING_CONFLICT`: same formation class), so the move
        is not sent. Units that left the tile this turn are no longer counted.
        """
        if t.at_city.get(where, 0) > 0 or where in t.blocked:
            return True
        return any(
            u.unit_id != unit.unit_id and self._pos(t, u) == where and holds_tile(u)
            for u in t.units
        )

    @staticmethod
    def _reached(result: str, goal: tuple[int, int]) -> tuple[int, int]:
        """Where a move ended, from `now_at`, or the goal if the reply does not say."""
        found = _NOW_AT.search(result)
        return (int(found.group(1)), int(found.group(2))) if found else goal

    @staticmethod
    def _learn_full_tile(t: _Turn, result: str | None) -> None:
        """A `STACKING_CONFLICT` names the tile that is full, so no one else tries it this turn."""
        found = _ALREADY_ON.search(result or "")
        if found:
            t.blocked.add((int(found.group(1)), int(found.group(2))))

    def _leave(self, t: _Turn, unit: Unit) -> None:
        if (unit.x, unit.y) in t.at_city:
            t.at_city[unit.x, unit.y] -= 1

    def _arrive(self, t: _Turn, city: City, result: str) -> None:
        """Count a unit that reached the city's tile this turn as one of its defenders."""
        reached = _NOW_AT.search(result)
        if reached and (int(reached.group(1)), int(reached.group(2))) == (city.x, city.y):
            t.at_city[city.x, city.y] = t.at_city.get((city.x, city.y), 0) + 1

    # ----- attack -----

    async def _attack(self, game: MilitaryGame, t: _Turn) -> None:
        """City strikes first, then planned group kills, then one attacker at a time."""
        await self._city_strikes(game, t)
        await self._group_kills(game, t)
        await self._solo_attacks(game, t)
        self._log_rejections(t)

    def _defender_at(self, t: _Turn, where: tuple[int, int]) -> Threat | None:
        defenders = [d for d in t.threats if (d.x, d.y) == where and d.hostile]
        if not defenders:
            return None  # The scan did not show it, so there is nothing to judge it by.
        return max(defenders, key=lambda d: (d.combat_strength, d.ranged_strength))

    def _doorstep_ratio(self, t: _Turn, target: Target) -> float | None:
        """The ratio to use for a target near one of our cities, else None for the usual one."""
        s = self.settings
        near = any(
            hex_distance(c.x, c.y, target.x, target.y, s.map_width) <= s.defend_radius
            for c in t.cities
        )
        return s.weights.defend_damage_ratio if near else None

    def _live(self, t: _Turn, target: Target) -> Target:
        where = (target.x, target.y)
        return Target(target.unit_type, target.x, target.y, int(t.target_hp.get(where, target.hp)))

    def _floor_for(self, t: _Turn, unit: Unit) -> float | None:
        """The garrison floor for a city's only defender, else None for the weights' own."""
        city = self._city_at(t, unit)
        sole = city is not None and t.at_city.get((unit.x, unit.y), 0) <= garrison_required(
            city, self.settings
        )
        return self.settings.weights.garrison_min_hp_after if sole else None

    def _record_hit(
        self, t: _Turn, where: tuple[int, int], result: str, fallback: float, hp: int
    ) -> None:
        killed = "killed" in result.lower() or "eliminated" in result.lower()
        dealt = _DAMAGE_DEALT.search(result)
        hp_now = t.target_hp.get(where, hp)
        t.target_hp[where] = 0 if killed else hp_now - (int(dealt[1]) if dealt else fallback)
        if killed or t.target_hp[where] <= 0:
            t.dead.add(where)
            t.killed.add(where)
        if killed:
            t.confirmed.add(where)

    # City strikes.

    def _city_strike_odds(self, city: City, defender: Threat, hp: float) -> AttackOdds | None:
        """What a strike by `city` is expected to do. A city takes nothing back.

        civ6-mcp does not report a city's ranged strength, so its defence strength stands in
        for it. That is an assumption to check live.
        """
        w = self.settings.weights
        if city.defense_strength <= 0:
            return None
        if defender.combat_strength <= 0 and defender.ranged_strength <= 0:
            return None
        defence = defender.combat_strength + w.assumed_defender_bonus
        dealt = expected_damage(city.defense_strength, defence)
        sure = dealt * (1 - w.damage_variance) >= hp
        rank = min(dealt, hp) + (w.kill_bonus if sure else 0.0)
        return AttackOdds(dealt, 0.0, sure, 1.0, True, rank)

    def _city_can_strike(self, t: _Turn, city: City) -> bool:
        # civ6-mcp refuses a strike from a city without walls ("ERR:NO_WALLS") or one that has
        # already fired ("ERR:ALREADY_FIRED"), so those are not asked.
        return city.has_walls and city.city_id not in t.fired

    async def _city_strikes(self, game: MilitaryGame, t: _Turn) -> None:
        s = self.settings
        for city in sorted(t.cities, key=lambda c: c.city_id):
            if t.actions >= s.max_actions or not self._city_can_strike(t, city):
                continue
            if self._strike_refused.get(city.city_id, -1) >= t.turn:
                continue
            options = []
            for threat in t.threats:
                where = (threat.x, threat.y)
                near = hex_distance(city.x, city.y, *where, s.map_width) <= s.city_strike_range
                if not threat.hostile or where in t.dead or not near:
                    continue
                hp = int(t.target_hp.get(where, threat.hp))
                odds = self._city_strike_odds(city, threat, hp)
                if odds is None:
                    continue
                # Prefer a target the units can then finish together with the strike.
                low = odds.dealt * (1 - s.weights.damage_variance)
                after = Target(threat.unit_type, threat.x, threat.y, max(1, int(hp - low)))
                plan = self._group_plan(t, after, threat, counted=1)
                live = Target(threat.unit_type, threat.x, threat.y, hp)
                options.append((plan is not None, odds.rank, -threat.x, -threat.y, odds, live))
            if not options:
                continue
            *_, odds, live = max(options, key=lambda o: o[:4])
            where = (live.x, live.y)
            t.fired.add(city.city_id)  # One strike a turn, whatever the answer.
            t.actions += 1
            try:
                result = await game.city_attack(city.city_id, *where)
            except ConnectionError:
                raise
            except Exception:
                log.exception("City strike from %s failed", city.name)
                result = None
            ok = (
                result is not None
                and result.startswith("CITY_RANGE_ATTACK")
                and action_succeeded(result)
            )
            self._record(
                MilitaryDecision(
                    t.turn, -city.city_id, city.x, city.y, "city_strike",
                    "city strike, no damage back, so first" if ok else "strike refused",
                    where, asdict(odds), result or "",
                )
            )  # fmt: skip
            if not ok or result is None:
                self._strike_refused[city.city_id] = t.turn + s.retry_turns
                continue
            t.attacked.add(where)
            t.struck.add(where)
            self._record_hit(t, where, result, odds.dealt, live.hp)

    # Planned group kills.

    def _group_plan(
        self, t: _Turn, target: Target, defender: Threat, counted: int = 0
    ) -> list[tuple[Unit, AttackOdds]] | None:
        """Units that together kill `target` this turn, fewest and safest first, or None.

        The group must kill even on a low roll, so the plan does not rely on luck. Each unit is
        judged on a lower floor than a lone attack (`planned_kill_hp_after`), unless it is the
        only defender of its city. `counted` is the number of strikes already made or about to
        be made on this target: a group needs two or more participants, or it is a lone attack
        and the lone-attack rule applies.
        """
        s = self.settings
        w = s.weights
        where = (target.x, target.y)
        pool = []
        for unit in t.mine:
            if (
                not any((tg.x, tg.y) == where for tg in unit.targets)
                or not self._available(t, unit, "attack")
                or unit.hp < w.heal_below * unit.max_hp
            ):
                continue
            floor = self._floor_for(t, unit)
            floor = w.planned_kill_hp_after if floor is None else floor
            odds = assess_attack(unit, target, defender, w, floor, s.map_width)
            if odds is None or odds.dealt <= 0 or odds.hp_after < floor * unit.max_hp:
                continue
            pool.append((unit, odds))
        # Units that take the least damage back first, then the hardest hitters.
        pool.sort(key=lambda p: (p[1].taken, -p[1].dealt, p[0].unit_id))
        chosen: list[tuple[Unit, AttackOdds]] = []
        total = 0.0
        for unit, odds in pool:
            if total >= target.hp:
                break
            chosen.append((unit, odds))
            total += odds.dealt * (1 - w.damage_variance)
        struck = counted + (1 if where in t.struck else 0)
        if total < target.hp or len(chosen) + struck < 2:
            return None
        return chosen

    async def _group_kills(self, game: MilitaryGame, t: _Turn) -> None:
        """Attack as a group where the group is planned to kill.

        It replans after every attack, so a bad roll stops the plan before more units are spent
        on a kill that no longer works. Bounded by `max_actions` and the per-unit call cap.
        """
        s = self.settings
        for _ in range(s.max_actions):
            if t.actions >= s.max_actions:
                return
            best = None
            seen: set[tuple[int, int]] = set()
            for unit in t.mine:
                for tg in unit.targets:
                    where = (tg.x, tg.y)
                    defender = self._defender_at(t, where)
                    if where in seen or where in t.dead or defender is None:
                        continue
                    seen.add(where)
                    live = self._live(t, tg)
                    plan = self._group_plan(t, live, defender)
                    if plan is None:
                        continue
                    key = (defender.combat_strength, -len(plan), -where[0], -where[1])
                    if best is None or key > best[0]:
                        best = (key, live, plan)
            if best is None:
                return
            _, live, plan = best
            unit, odds = plan[0]
            reason = f"planned kill: {len(plan)} unit(s) expected to kill it this turn"
            await self._strike(game, t, unit, live, odds, reason)

    # One attacker at a time.

    async def _solo_attacks(self, game: MilitaryGame, t: _Turn) -> None:
        ready = [u for u in t.mine if u.targets]
        # Ranged units first, since they take no damage back, then the strongest.
        ready.sort(
            key=lambda u: (u.ranged_strength == 0, -max(u.combat_strength, u.ranged_strength),
                           u.unit_id)
        )  # fmt: skip
        for unit in ready:
            if not self._available(t, unit, "attack"):
                continue
            options = self._attack_options(t, unit)
            if not options:
                continue
            odds, target, _ = options[0]
            await self._strike(game, t, unit, target, odds, "odds favour the attack")

    async def _strike(
        self,
        game: MilitaryGame,
        t: _Turn,
        unit: Unit,
        target: Target,
        odds: AttackOdds,
        reason: str,
    ) -> bool:
        """Order one unit's attack. True if it landed."""
        result = await self._order(
            t, unit, "attack", game.attack_unit(unit.unit_index, target.x, target.y)
        )
        where = (target.x, target.y)
        if result is None or not attack_landed(result):
            if result and "NO_ENEMY" in result and where in t.attacked:
                t.dead.add(where)  # An earlier attack this turn killed it.
                t.killed.add(where)
                t.confirmed.add(where)  # The game says nobody is there.
            self._refuse(t, unit, "attack")
            self._log(
                t, unit, "attack", "attack refused",
                target=where, odds=asdict(odds), result=result or "",
            )  # fmt: skip
            return False
        failure = attack_failed(unit, result)
        if failure is not None:
            # Not a hit: no kill, no attack counted, and the unit is free to try something else.
            self._refused[unit.unit_id, "attack"] = t.turn  # Not asked to attack again this turn.
            self._log(
                t, unit, "attack", f"attack failed: {failure}",
                target=where, odds=asdict(odds), result=result,
            )  # fmt: skip
            return False
        t.acted.add(unit.unit_id)
        t.attacked.add(where)
        t.hp[unit.unit_id] = max(0.0, t.hp.get(unit.unit_id, unit.hp) - odds.taken)  # Expected.
        self._fortified.pop(unit.unit_id, None)  # Attacking ends a fortification.
        self._record_hit(t, where, result, odds.dealt, target.hp)
        self._log(t, unit, "attack", reason, target=where, odds=asdict(odds), result=result)
        return True

    def _attack_options(self, t: _Turn, unit: Unit) -> list[tuple[AttackOdds, Target, Threat]]:
        """Attacks this unit would take, best first. Turned-down ones are kept for the log."""
        s = self.settings
        w = s.weights
        floor = self._floor_for(t, unit)
        used = w.min_hp_after_attack if floor is None else floor
        hurt = unit.hp < w.heal_below * unit.max_hp
        v = w.damage_variance
        found = []
        for target in unit.targets:
            where = (target.x, target.y)
            defender = self._defender_at(t, where)
            if where in t.dead or defender is None:
                continue
            live = self._live(t, target)
            odds = assess_attack(
                unit, live, defender, w, floor, s.map_width, self._doorstep_ratio(t, target)
            )
            if odds is None:
                continue
            # Run 15: a hurt slinger beside a city under attack sat out every turn while barbarians
            # pillaged. A hurt unit only needs to heal rather than fight when the fight can cost
            # it health: a shot that takes nothing back (ranged at a melee unit) is free.
            risky = hurt and not odds.sure_kill and odds.taken > 0
            if odds.favourable and not risky:
                found.append((odds, live, defender))
                continue
            if risky:
                reason = f"unit is hurt (hp {unit.hp}/{unit.max_hp}) and this is no sure kill"
            elif odds.hp_after < used * unit.max_hp:
                reason = (
                    f"would be left with {max(odds.hp_after, 0):.0f} hp on a bad roll, "
                    f"under the floor of {used * unit.max_hp:.0f}"
                )
            else:
                reason = (
                    f"deals {odds.dealt:.0f} and takes {odds.taken:.0f} "
                    f"(low roll {odds.dealt * (1 - v):.0f} against {w.min_damage_ratio} "
                    f"times the high-roll {odds.taken * (1 + v):.0f}), so the ratio is too poor"
                )
            kept = t.rejected.get(where)
            if kept is None or odds.rank > kept[0].rank:
                t.rejected[where] = (odds, unit, reason)
        found.sort(key=lambda f: (-f[0].rank, f[1].x, f[1].y))
        return found

    def _log_rejections(self, t: _Turn) -> None:
        """Once per target, the best option turned down, so a quiet turn can be audited."""
        for where, (odds, unit, reason) in sorted(t.rejected.items()):
            if where in t.attacked:
                continue
            self._log(t, unit, "skip", f"no attack: {reason}", target=where, odds=asdict(odds))

    # ----- heal -----

    async def _heal(self, game: MilitaryGame, t: _Turn) -> None:
        s = self.settings
        for unit in sorted(t.mine, key=lambda u: u.unit_id):
            if unit.unit_id in t.acted or unit.hp >= s.weights.heal_below * unit.max_hp:
                continue
            if self._resting.get(unit.unit_id) == (unit.x, unit.y):
                t.acted.add(unit.unit_id)  # Already fortified until healed.
                continue
            if not self._available(t, unit, "heal"):
                continue
            city = self._city_at(t, unit)
            if city is None:
                home = self._nearest_city(t, unit, s.retreat_reach)
                if home is not None:
                    await self._walk_home(game, t, unit, home, "retreat", "hurt, so going to heal")
                    continue
            await self._rest(game, t, unit, "heal", "hurt, so healing where it stands")

    async def _rest(
        self, game: MilitaryGame, t: _Turn, unit: Unit, action: str, reason: str
    ) -> None:
        result = await self._order(t, unit, "heal", game.heal_unit(unit.unit_index))
        if result is not None and not action_succeeded(result) and self._available(t, unit, "heal"):
            # Fortify-until-healed was refused (some units can't), so fortify and let it mend.
            result = await self._order(t, unit, "heal", game.fortify_unit(unit.unit_index))
        if result is not None and action_succeeded(result):
            t.acted.add(unit.unit_id)
            self._resting[unit.unit_id] = (unit.x, unit.y)
            self._log(t, unit, action, reason, result=result)
        else:
            self._refuse(t, unit, "heal")
            self._log(t, unit, action, "healing refused", result=result or "")

    async def _walk_home(
        self, game: MilitaryGame, t: _Turn, unit: Unit, city: City, action: str, reason: str
    ) -> bool:
        where = (city.x, city.y)
        kind = "move"
        if not self._available(t, unit, kind):
            return False
        if self._held(t, where, unit):
            # The city tile already holds a unit of this class: skip the refused move.
            self._refuse(t, unit, kind)
            await self._fallback(game, t, unit, city, action)
            return False
        result = await self._order(t, unit, kind, game.move_unit(unit.unit_index, *where))
        if result is not None and move_succeeded(result):
            t.acted.add(unit.unit_id)
            self._leave(t, unit)
            self._arrive(t, city, result)
            reached = _NOW_AT.search(result)
            t.placed[unit.unit_id] = (
                (int(reached.group(1)), int(reached.group(2))) if reached else where
            )
            self._log(t, unit, action, reason, where, result=result)
            return True
        self._refuse(t, unit, kind)
        self._learn_full_tile(t, result)
        self._log(t, unit, action, "move refused", where, result=result or "")
        await self._fallback(game, t, unit, city, action)
        return False

    def _fallback_tiles(self, t: _Turn, unit: Unit, city: City) -> list[tuple[int, int]]:
        """Tiles beside the city, nearest the unit first, with no unit on them."""
        s = self.settings
        width = s.map_width
        taken = {self._pos(t, u) for u in t.units if u.unit_id != unit.unit_id}
        taken |= {(th.x, th.y) for th in t.threats} | t.blocked
        found = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x, y = city.x + dx, city.y + dy
                if width:
                    x %= width
                if (x, y) != (city.x, city.y) and hex_distance(city.x, city.y, x, y, width) == 1:
                    found.add((x, y))
        found -= taken
        found.discard((unit.x, unit.y))
        ordered = sorted(found, key=lambda p: (hex_distance(unit.x, unit.y, *p, width), p))
        return ordered[: s.fallback_tiles]

    async def _fallback(
        self, game: MilitaryGame, t: _Turn, unit: Unit, city: City, action: str
    ) -> None:
        """A refused move leaves the unit without an order, so give it one: try the next-best
        tile beside the city, then fortify where it stands. Bounded by `fallback_tiles`, one
        fortify, and `max_actions`. Whatever happens the unit is not tried again this turn.
        """
        s = self.settings
        if unit.unit_id in t.acted or not unit.can_move:
            return
        if hex_distance(unit.x, unit.y, city.x, city.y, s.map_width) > 1:
            for goal in self._fallback_tiles(t, unit, city):
                if t.actions >= s.max_actions:
                    return
                result = await self._order(
                    t, unit, "fallback", game.move_unit(unit.unit_index, *goal)
                )
                if result is not None and move_succeeded(result):
                    t.acted.add(unit.unit_id)
                    self._leave(t, unit)
                    t.placed[unit.unit_id] = self._reached(result, goal)
                    self._log(
                        t,
                        unit,
                        action,
                        "city tile refused, so went to a tile beside it",
                        goal,
                        result=result,
                    )
                    return
                self._learn_full_tile(t, result)
                self._log(t, unit, action, "fallback move refused", goal, result=result or "")
        if t.actions >= s.max_actions:
            return
        result = await self._order(t, unit, "fallback", game.fortify_unit(unit.unit_index))
        if result is not None and action_succeeded(result):
            t.acted.add(unit.unit_id)
            self._fortified[unit.unit_id] = (unit.x, unit.y)
            self._log(
                t, unit, "fortify", "move refused, so fortified where it stands", result=result
            )
        else:
            t.acted.add(unit.unit_id)  # Out of options, so not tried again this turn.
            self._refuse(t, unit, "spare")
            self._log(
                t, unit, "fortify", "fortify refused after a refused move", result=result or ""
            )

    # ----- garrison -----

    async def _garrison(self, game: MilitaryGame, t: _Turn) -> None:
        s = self.settings
        for city in sorted(t.cities, key=lambda c: (-len(c.threats), c.city_id)):
            state_threat = self._state_threatened(city)
            if not city.is_threatened and not state_threat:
                continue
            if state_threat:
                await self._swap(game, t, city)
            while t.at_city.get((city.x, city.y), 0) < garrison_required(city, s):
                if t.at_city.get((city.x, city.y), 0) > 0:
                    break  # One land unit per tile: the game refuses a second to stack.
                candidates = [
                    (hex_distance(u.x, u.y, city.x, city.y, s.map_width), u.unit_id, u)
                    for u in t.mine
                    if self._available(t, u, "move")
                    and (u.x, u.y) != (city.x, city.y)
                    and u.hp >= s.weights.heal_below * u.max_hp
                    and (self._spare(t, u) or self._covering(t, u) is city)
                ]
                candidates = [c for c in candidates if c[0] <= self._reach(city)]
                if not candidates:
                    break
                unit = min(candidates, key=lambda c: c[:2])[2]
                before = t.at_city.get((city.x, city.y), 0)
                if not await self._walk_home(
                    game, t, unit, city, "garrison", "city has hostile units near it and a gap"
                ):
                    continue  # Refused, so it is set aside and the next unit is tried.
                self._note(city, f"garrison: moved {unit.unit_type} into the city")
                if t.at_city.get((city.x, city.y), 0) == before:
                    # En route: it counts for this turn, so the same gap does not call another.
                    t.at_city[city.x, city.y] = before + 1
            if state_threat:
                await self._cover(game, t, city)

    def _note(self, city: City, text: str) -> None:
        if self.defence is not None:
            self.defence.note(city.city_id, text)

    def _ring(self, t: _Turn, city: City, unit: Unit) -> list[tuple[int, int]]:
        """Free tiles around the city, nearest the unit first. None holds a unit or a threat."""
        w = self.settings.map_width
        taken = {self._pos(t, u) for u in t.units if u.unit_id != unit.unit_id and holds_tile(u)}
        taken |= {(th.x, th.y) for th in t.threats} | t.blocked
        found = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                x, y = city.x + dx, city.y + dy
                if w:
                    x %= w
                if (x, y) != (city.x, city.y) and hex_distance(city.x, city.y, x, y, w) == 1:
                    found.add((x, y))
        found -= taken
        found.discard(self._pos(t, unit))
        return sorted(found, key=lambda p: (hex_distance(unit.x, unit.y, *p, w), p))

    async def _step_out(
        self, game: MilitaryGame, t: _Turn, unit: Unit, city: City, action: str, reason: str
    ) -> bool:
        """Move a unit to a free tile beside the city. Tries `fallback_tiles` tiles."""
        s = self.settings
        for goal in self._ring(t, city, unit)[: s.fallback_tiles]:
            if not self._available(t, unit, action):
                return False
            result = await self._order(t, unit, action, game.move_unit(unit.unit_index, *goal))
            if result is not None and move_succeeded(result):
                t.acted.add(unit.unit_id)
                if any(u.unit_id == unit.unit_id for u in t.mine):
                    self._leave(t, unit)  # An engineer was never counted as a defender.
                at = self._reached(result, goal)
                t.placed[unit.unit_id] = at
                if at != goal:  # Still walking: not a defender yet, but no other unit is sent.
                    t.en_route[city.city_id] = t.en_route.get(city.city_id, 0) + 1
                self._log(t, unit, action, reason, goal, result=result)
                self._note(city, f"{action}: {unit.unit_type} to {goal[0]},{goal[1]}")
                return True
            self._learn_full_tile(t, result)
            self._log(t, unit, action, "move refused", goal, result=result or "")
        self._refuse(t, unit, action)
        return False

    async def _cover(self, game: MilitaryGame, t: _Turn, city: City) -> None:
        """Get a second defender onto a tile beside the city, so two hold it (roadmap 1.36)."""
        s = self.settings
        while t.actions < s.max_actions:
            inside = t.at_city.get((city.x, city.y), 0)
            if inside + self._beside(t, city) + t.en_route.get(city.city_id, 0) >= WANTED_DEFENDERS:
                return
            candidates = [
                (hex_distance(*self._pos(t, u), city.x, city.y, s.map_width), u.unit_id, u)
                for u in t.mine
                if self._available(t, u, "cover")
                and hex_distance(*self._pos(t, u), city.x, city.y, s.map_width) > 1
                and u.hp >= s.weights.heal_below * u.max_hp
                and self._spare(t, u)
            ]
            candidates = [c for c in candidates if c[0] <= self._reach(city)]
            if not candidates:
                return
            unit = min(candidates, key=lambda c: c[:2])[2]
            await self._step_out(
                game, t, unit, city, "cover", "threatened city has fewer than two defenders"
            )

    async def _swap(self, game: MilitaryGame, t: _Turn, city: City) -> None:
        """Put a clearly stronger unit on the city tile and its weaker holder beside it.

        The holder may be a unit that is no defender at all, such as a Military Engineer, which
        the game also refuses to stack with a military unit.
        """
        s = self.settings
        where = (city.x, city.y)
        holder = next(
            (
                u
                for u in sorted(t.units, key=lambda u: u.unit_id)
                if self._pos(t, u) == where and holds_tile(u) and self._available(t, u, "swap")
            ),
            None,
        )
        if holder is None:
            return
        weak = _hold_strength(holder)
        candidates = [
            (hex_distance(u.x, u.y, city.x, city.y, s.map_width), u.unit_id, u)
            for u in t.mine
            if u.unit_id != holder.unit_id
            and self._available(t, u, "move")
            and u.hp >= s.weights.heal_below * u.max_hp
            and _hold_strength(u) >= max(weak * SWAP_MARGIN, weak + 1)
            and (self._spare(t, u) or self._covering(t, u) is city)
        ]
        candidates = [c for c in candidates if 0 < c[0] <= SWAP_REACH]
        if not candidates:
            return
        # The strongest, then the nearest.
        newcomer = min(candidates, key=lambda c: (-_hold_strength(c[2]), c[0], c[1]))[2]
        if not await self._step_out(
            game, t, holder, city, "swap", "city tile held by a weaker unit, so it steps aside"
        ):
            return
        if await self._walk_home(
            game, t, newcomer, city, "swap", "stronger unit takes the city tile"
        ):
            self._note(city, f"swap: {newcomer.unit_type} in for {holder.unit_type}")
            if t.at_city.get(where, 0) == 0:
                t.at_city[where] = 1  # En route, counted for this turn.

    def _publish_defence(self, t: _Turn) -> None:
        """Hand the defender counts, as they stand after this turn's orders, to the threat state.

        Hostile units killed this turn are dropped from the threat map and from each city's count
        first, so production, which runs next, does not answer a unit that is already dead.
        """
        if self.threat_map is not None:
            self.threat_map.forget_units_at(t.confirmed)
            # Cover moves with the units that moved, and shrinks with the ones that took damage.
            health = {
                u.unit_id: t.hp[u.unit_id] / max(u.max_hp, 1) for u in t.units if u.unit_id in t.hp
            }
            self.threat_map.relocate(t.placed, health)
        if self.defence is None:
            return
        self.defence.forget_killed(t.confirmed)
        self.defence.refresh()
        for city in t.cities:
            if self._state_threatened(city):
                self.defence.set_defenders(
                    city.city_id, t.at_city.get((city.x, city.y), 0), self._beside(t, city)
                )

    # ----- barbarian camps -----

    async def _clear_camp(self, game: MilitaryGame, t: _Turn) -> None:
        s = self.settings
        barbarians = [
            b
            for b in t.threats
            if b.owner_id == BARBARIAN_ID
            and b.hostile
            and any(hex_distance(b.x, b.y, c.x, c.y, s.map_width) <= s.camp_search_radius
                    for c in t.cities)
        ]  # fmt: skip
        spare = [
            u
            for u in t.mine
            if self._available(t, u, "move")
            and u.hp >= s.weights.camp_min_hp * u.max_hp
            and self._spare(t, u)
        ]
        # A unit already marching on a camp stays in the force on a turn when another order was
        # refused or it is between orders (Run 19: with one unit free to move the camp was not
        # ordered, the marching unit went exploring, and the walk began again).
        counted = {u.unit_id for u in spare}
        spare += [
            u
            for u in t.mine
            if u.unit_id not in counted
            and u.unit_id not in self._recon
            and self._on_camp_mission(t, u)
            and u.hp >= s.weights.camp_min_hp * u.max_hp
            and self._spare(t, u)
        ]
        if len(spare) < s.camp_min_units or t.camp_scanned:
            return
        t.camp_scanned = True
        await self._scan_camps(game, t, barbarians)
        known = self._known_camps()
        self._gone_noted -= known  # A camp that is back is a new one.
        for camp in {m[0] for m in self._mission.values()} - known:
            self._note_gone(camp)  # Before the force is sent at another camp.
        own = set(self._camps)
        if self.threat_map is not None:
            t.map_use["camps_only_in_map"] = len(known - own)
        camps = [
            c
            for c in sorted(known)
            if self._camp_aside.get(c, -1) < t.turn
            and self._home_distance(t, c) <= s.camp_home_radius
        ]
        if not camps:
            return
        where = min(camps, key=lambda c: (self._home_distance(t, c), c))
        if where not in own:
            t.map_use["target_from_map"] = 1  # The map alone put this camp on the list.
        defenders = [
            b
            for b in barbarians
            if hex_distance(b.x, b.y, *where, s.map_width) <= s.camp_defender_radius
        ]
        force = [u for u in spare if hex_distance(u.x, u.y, *where, s.map_width) <= s.camp_reach]
        if not camp_force_ok(force, defenders, s.weights, s.camp_min_units):
            return
        muster = s.camp_muster_radius
        gathered = sum(1 for u in force if hex_distance(u.x, u.y, *where, s.map_width) <= muster)
        # Two limits. The walk to the muster point has its own, longer one (Run 18: a far camp's
        # force took eight turns to arrive, which used up the assault's limit before anyone was
        # there, and 4-9 spare units then sat idle for twenty turns). The assault keeps its own.
        key = (where, gathered >= s.camp_min_units)
        limit = s.max_camp_turns if key[1] else s.camp_travel_turns
        spent = self._camp_turns.get(key, (0, t.turn))[0] + 1
        self._camp_turns[key] = (spent, t.turn)
        if spent > limit:
            self._camp_aside[where] = t.turn + s.camp_retry_turns
            self._camp_turns.pop((where, True), None)
            self._camp_turns.pop((where, False), None)
            return
        occupied = any((b.x, b.y) == where for b in t.threats if b.hostile)
        for unit in sorted(force, key=lambda u: u.unit_id):
            self._mission[unit.unit_id] = (where, t.turn)
            if not self._available(t, unit, "move"):
                continue
            far = hex_distance(unit.x, unit.y, *where, s.map_width) > muster
            if not far and gathered < s.camp_min_units:
                t.acted.add(unit.unit_id)  # Waiting for the rest; not idle, not spare.
                t.map_use["muster_waits"] = t.map_use.get("muster_waits", 0) + 1
                self._log(t, unit, "camp", "waiting for the rest of the force", where)
                continue
            # Run 15: a move straight onto a camp three tiles off is refused (the game only allows
            # that capture move from beside it), so walk to the tile beside it first.
            adjacent = hex_distance(unit.x, unit.y, *where, s.map_width) <= 1
            if adjacent and not occupied:
                goals = [where]
            elif adjacent:
                continue  # In reach of the guards: the attack step takes it from here.
            elif far:
                goals = self._muster_goals(t, unit, where)
                t.map_use["muster_moves"] = t.map_use.get("muster_moves", 0) + 1
            else:
                goals = self._stage_goals(t, unit, where)
            result = None
            out_of_orders = False
            for goal in goals:
                if result is not None and not self._available(t, unit, "move"):
                    out_of_orders = True  # The retries too count against the bounds.
                    break
                result = await self._order(t, unit, "move", game.move_unit(unit.unit_index, *goal))
                if result is not None and move_succeeded(result):
                    t.acted.add(unit.unit_id)
                    t.placed[unit.unit_id] = self._reached(result, goal)
                    self._leave(t, unit)
                    self._log(t, unit, "camp", "odds are clearly good, advancing on the camp",
                              where, result=result)  # fmt: skip
                    break
                self._learn_full_tile(t, result)  # Another unit holds it: try the next tile.
                if "STACKING_CONFLICT" not in (result or ""):
                    if not far:
                        break
                    # A muster tile the game will not take us to (impassable ground, another
                    # nation's land): remember it, and try the next one now. Run 19: a unit was
                    # refused the same unreachable tile every few turns and never arrived.
                    self._muster_bad[goal] = t.turn + s.muster_bad_turns
            else:
                result = result or ""
            if unit.unit_id not in t.acted and not out_of_orders:
                self._refuse(t, unit, "move")
                self._log(t, unit, "camp", "move refused", where, result=result or "")

    def _note_sightings(self, t: _Turn) -> None:
        """Where barbarian land units were seen, the trail back to their camp."""
        s = self.settings
        for th in t.threats:
            land = not any(w in th.unit_type for w in _SEA_UNITS)
            if th.owner_id == BARBARIAN_ID and th.hostile and land:
                self._sightings[th.x, th.y] = t.turn
        self._sightings = {
            p: seen for p, seen in self._sightings.items() if t.turn - seen <= s.recon_memory_turns
        }
        self._recon_bad = {p: v for p, v in self._recon_bad.items() if v >= t.turn}

    def _recon_target(self, t: _Turn) -> tuple[int, int] | None:
        """The farthest recent sighting that is still within reach of a city, else None."""
        s = self.settings
        found = [
            (self._home_distance(t, p), seen, p)
            for p, seen in self._sightings.items()
            if p not in self._recon_bad and self._home_distance(t, p) <= s.recon_radius
        ]
        if not found:
            return None
        home, _, seen = max(found)
        # Barbarians are only seen near our cities, so the camp lies further out along the same
        # line: aim `recon_push` tiles past the sighting, where the party's own eyes will reach.
        city = min(t.cities, key=lambda c: (hex_distance(*seen, c.x, c.y, s.map_width), c.city_id))
        away = hex_distance(*seen, city.x, city.y, s.map_width)
        if away <= 0:
            return seen
        reach = min(away + s.recon_push, s.recon_radius)
        factor = reach / away
        # The sighting may lie across the east-west seam: use the copy of it nearest the city.
        seen_x = min(
            (seen[0] + shift * s.map_width for shift in (-1, 0, 1)),
            key=lambda x: hex_distance(x, seen[1], city.x, city.y, None),
        )
        goal = (
            round(city.x + (seen_x - city.x) * factor) % s.map_width,
            min(s.map_height - 1, max(0, round(city.y + (seen[1] - city.y) * factor))),
        )
        return goal if goal not in self._recon_bad else seen

    async def _recon_step(self, game: MilitaryGame, t: _Turn, unit: Unit) -> bool:
        """Send a spare unit to where barbarians come from, or keep one walking there."""
        s = self.settings
        sent = self._recon.get(unit.unit_id)
        if sent is not None:
            target, turn_sent = sent
            if hex_distance(unit.x, unit.y, *target, s.map_width) <= 1 or (
                t.turn - turn_sent > s.recon_turns
            ):
                self._recon.pop(unit.unit_id)  # Arrived and scanned, or called off.
                return False
        else:
            # Only units that can go count: a wounded or already-ordered one would leave the
            # party short and send the first alone.
            spare = sum(
                1
                for u in t.mine
                if self._spare(t, u)
                and u.can_move
                and u.unit_id not in t.acted
                and u.unit_id not in self._explorers
                and u.hp >= s.weights.heal_below * u.max_hp
            )
            if (
                s.recon_units <= 0
                or self._camps
                or len(self._recon) >= s.recon_units
                # A party leaves together on one turn. Run 15: units sent one by one met the
                # raiders alone and three were lost.
                or (self._recon and self._recon_started != t.turn)
                or (not self._recon and spare < s.recon_min_spare)  # Joiners need no recount.
                or any(th.hostile and th.owner_id != BARBARIAN_ID for th in t.threats)
                or self._land_threat_near(t, unit)
            ):
                return False
            target = self._recon_target(t)
            turn_sent = t.turn
            if target is None:
                return False
        if not self._available(t, unit, "spare"):
            return False
        t.spare_orders += 1
        result = await self._order(t, unit, "spare", game.move_unit(unit.unit_index, *target))
        if result is None or not move_succeeded(result):
            self._refuse(t, unit, "spare")
            self._recon_bad[target] = t.turn + s.camp_retry_turns
            self._recon.pop(unit.unit_id, None)
            return False
        t.acted.add(unit.unit_id)
        self._leave(t, unit)
        if not self._recon:
            self._recon_started = t.turn
        self._recon[unit.unit_id] = (target, turn_sent)
        self._fortified.pop(unit.unit_id, None)
        self._log(t, unit, "recon", "idle spare unit, no camp known, so looking where "
                  "barbarians were last seen", target, result=result)  # fmt: skip
        return True

    def _on_camp_mission(self, t: _Turn, unit: Unit) -> bool:
        """A healthy unit sent at a camp lately, still near it. Run 15: a unit that had shot the
        camp's guards for two turns fled home the moment they were within `flee_radius`."""
        s = self.settings
        if unit.unit_id in self._recon:
            return unit.hp >= s.weights.camp_leave_hp * unit.max_hp
        sent = self._mission.get(unit.unit_id)
        if sent is None or t.turn - sent[1] > s.camp_mission_turns:
            return False
        if sent[0] not in self._known_camps():
            # The camp is gone (taken, or seen empty). The force is no longer on a mission: it
            # must not hold the tiles around it (Run 18: units waiting beside a taken camp blocked
            # the settler's best site for three turns and cost the fourth city).
            self._mission.pop(unit.unit_id, None)
            self._released += 1  # Reported on the next offense line.
            self._note_gone(sent[0])
            return False
        near = hex_distance(unit.x, unit.y, *sent[0], s.map_width) <= s.camp_reach
        return near and unit.hp >= s.weights.camp_leave_hp * unit.max_hp

    async def _scan_camps(self, game: MilitaryGame, t: _Turn, barbarians: list[Threat]) -> None:
        """Remember each camp seen on a visible tile, and forget one seen gone.

        Run 15: the scan only looked around the nearest barbarian, a ship in a bay, while a camp
        stood three tiles from the capital in plain view for ten turns. Now it also looks around
        each city every `camp_home_scan_turns`. Only visible tiles are believed (a fogged tile
        shows the live state, which the player could not know), but a camp once seen is kept
        while it is fogged, as the player's own map would keep it.
        """
        s = self.settings
        centres = []
        land = [b for b in barbarians if not any(w in b.unit_type for w in _SEA_UNITS)]
        if land:
            nearest = min(land, key=lambda b: (b.distance, b.x, b.y))
            centres.append((nearest.x, nearest.y, s.camp_scan_radius))
        for unit in t.mine:
            if unit.unit_id in self._recon and self._city_at(t, unit) is None:
                centres.append((unit.x, unit.y, s.camp_scan_radius))  # What the party sees.
        for camp, sent_turn in {m[0]: m[1] for m in self._mission.values()}.items():
            if t.turn - sent_turn <= s.camp_mission_turns:
                centres.append((*camp, 2))  # See it gone when the force takes it.
        if t.turn - self._camp_home_scan >= s.camp_home_scan_turns:
            self._camp_home_scan = t.turn
            centres.extend((c.x, c.y, s.camp_home_scan_radius) for c in t.cities)
            # A remembered camp beyond the city scans is looked at too, so one seen cleared goes.
            centres.extend((*camp, 1) for camp in sorted(self._camps))
        for x, y, radius in centres:
            try:
                tiles = await read_area(game, x, y, radius)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Reading the area for a barbarian camp failed")
                continue
            if self.threat_map is not None:
                self.threat_map.see_tiles(t.turn, tiles)  # What we read keeps the map true.
            mapped = (
                {(c.x, c.y) for c in self.threat_map.camps()}
                if self.threat_map is not None
                else set()
            )
            for tile in tiles:
                if tile.improvement == CAMP_IMPROVEMENT:
                    # A fogged tile shows the live state, which may hold a camp that spawned since
                    # the player last looked. Only the session's first scan stands for the player's
                    # own map; after that a camp must be seen to be learned (and is then kept).
                    if tile.visible or (
                        self._camp_baseline
                        and (self.threat_map is None or (tile.x, tile.y) in mapped)
                    ):
                        self._camps.setdefault((tile.x, tile.y), t.turn)
                elif tile.visible:
                    self._camps.pop((tile.x, tile.y), None)
        self._camp_baseline = False

    def _note_gone(self, camp: tuple[int, int]) -> None:
        if camp not in self._gone_noted:
            self._gone_noted.add(camp)
            self._camps_gone += 1

    def _known_camps(self) -> set[tuple[int, int]]:
        known = set(self._camps)
        if self.threat_map is not None:
            known |= {(c.x, c.y) for c in self.threat_map.camps()}
        return known

    def _home_distance(self, t: _Turn, where: tuple[int, int]) -> int:
        return min(
            (hex_distance(*where, c.x, c.y, self.settings.map_width) for c in t.cities),
            default=0,
        )

    def _muster_goals(
        self, t: _Turn, unit: Unit, camp: tuple[int, int], tries: int = 4
    ) -> list[tuple[int, int]]:
        """Where a far unit gathers: the first tile on its line to the camp that is within the
        muster radius, then free tiles beside it. It stops there until the force is whole."""
        width = self.settings.map_width
        muster = self.settings.camp_muster_radius
        line = hex_line((unit.x, unit.y), camp, width)
        first = next((p for p in line if hex_distance(*p, *camp, width) <= muster), camp)
        taken = {self._pos(t, u) for u in t.units if u.unit_id != unit.unit_id}
        taken |= {(th.x, th.y) for th in t.threats} | t.blocked
        taken |= {p for p, until in self._muster_bad.items() if until >= t.turn}
        ring = sorted(
            (hex_distance(unit.x, unit.y, x, y, width), x, y)
            for y in range(camp[1] - muster, camp[1] + muster + 1)
            for x in range(camp[0] - muster - 1, camp[0] + muster + 2)
            if hex_distance(x, y, *camp, width) <= muster
        )
        tiles = [first, *neighbours(*first, width), *((x, y) for _, x, y in ring)]
        free = list(
            dict.fromkeys(p for p in tiles if p not in taken and p != camp
                          and hex_distance(*p, *camp, width) <= muster)
        )  # fmt: skip
        return free[:tries]

    def _stage_goals(
        self, t: _Turn, unit: Unit, camp: tuple[int, int], tries: int = 3
    ) -> list[tuple[int, int]]:
        """Free tiles beside the camp, nearest the unit first. One unit per tile: the game
        refuses a second land unit on a tile, and every unit aimed at the same stage tile left
        the rest standing where they were (Run 15: three units, one in position)."""
        width = self.settings.map_width
        taken = {self._pos(t, u) for u in t.units if u.unit_id != unit.unit_id}
        taken |= {(th.x, th.y) for th in t.threats} | t.blocked
        free = [p for p in neighbours(*camp, width) if p not in taken]
        free.sort(key=lambda p: (hex_distance(unit.x, unit.y, *p, width), p))
        return free[:tries]

    def _stage_tile(self, unit: Unit, camp: tuple[int, int]) -> tuple[int, int] | None:
        """The tile next to the camp on the way there, or None if already adjacent."""
        width = self.settings.map_width
        if hex_distance(unit.x, unit.y, *camp, width) <= 1:
            return None  # In reach, so the attack step takes it from here.
        line = hex_line((unit.x, unit.y), camp, width)
        return line[-2] if len(line) >= 2 else None

    # ----- idle units -----

    async def _idle(self, game: MilitaryGame, t: _Turn) -> None:
        s = self.settings
        for unit in sorted(t.mine, key=lambda u: u.unit_id):
            if not self._available(t, unit, "idle"):
                continue
            city = self._city_at(t, unit)
            if city is not None:
                if (city.is_threatened or self._state_threatened(city)) and (
                    self._fortified.get(unit.unit_id) != (unit.x, unit.y)
                ):
                    result = await self._order(t, unit, "idle", game.fortify_unit(unit.unit_index))
                    if result is not None and action_succeeded(result):
                        t.acted.add(unit.unit_id)
                        self._fortified[unit.unit_id] = (unit.x, unit.y)
                        self._log(
                            t, unit, "fortify", "city has hostile units near it", result=result
                        )
                    else:
                        self._refuse(t, unit, "idle")
                continue
            beside = self._covering(t, unit)
            if beside is not None:  # Holding a threatened city from beside it: stay and dig in.
                if self._fortified.get(unit.unit_id) != self._pos(t, unit):
                    result = await self._order(t, unit, "idle", game.fortify_unit(unit.unit_index))
                    if result is not None and action_succeeded(result):
                        t.acted.add(unit.unit_id)
                        self._fortified[unit.unit_id] = self._pos(t, unit)
                        self._log(t, unit, "fortify", "covering a threatened city", result=result)
                    else:
                        self._refuse(t, unit, "idle")
                continue
            home = self._nearest_city(t, unit, s.retreat_reach) if self._exposed(t, unit) else None
            if home is not None:
                await self._walk_home(
                    game, t, unit, home, "flee", "alone outside with hostile units near"
                )

    # ----- spare units -----

    async def _spare_units(self, game: MilitaryGame, t: _Turn) -> None:
        """One useful order for each healthy unit nobody else ordered and no city needs."""
        s = self.settings
        w = s.weights
        for unit in sorted(t.mine, key=lambda u: u.unit_id):
            if unit.unit_id in t.acted or unit.hp < w.heal_below * unit.max_hp:
                continue
            if not self._spare(t, unit):
                continue  # Needed for the garrison, so it stays as it is.
            if unit.unit_id not in self._recon and self._on_camp_mission(t, unit):
                continue  # Marching on a camp: not idle, so not for exploring or fortifying.
            if self._city_at(t, unit) is None and self._exposed(t, unit):
                continue  # Alone outside with hostile units close: the idle step runs for home.
            if unit.unit_id in self._explorers:
                if self._city_at(t, unit) is None:
                    continue  # Out exploring, nothing hostile close: leave the game's explorer be.
                self._explorers.pop(unit.unit_id, None)  # Back home, free to be sent again.
            if t.spare_orders >= s.max_spare_orders or t.actions >= s.max_actions:
                self._log(t, unit, "skip", "spare, but the orders for this turn are used up")
                continue
            if await self._hunt(game, t, unit):
                continue
            if await self._recon_step(game, t, unit):
                continue
            if await self._explore(game, t, unit):
                continue
            if self._fortified.get(unit.unit_id) == (unit.x, unit.y) or (
                self._resting.get(unit.unit_id) == (unit.x, unit.y)
            ):
                self._log(t, unit, "skip", "spare, already fortified, nothing to do")
                continue
            if not self._available(t, unit, "spare"):
                self._log(
                    t, unit, "skip", "spare, but an order was refused lately or is not allowed"
                )
                continue
            t.spare_orders += 1
            result = await self._order(t, unit, "spare", game.fortify_unit(unit.unit_index))
            if result is not None and "CANNOT_FORTIFY" in result:
                # The game says it cannot fortify or sleep: it already holds its post (fortified
                # or in a stack). Treat it as done, not as a refusal that sidelines the unit.
                self._fortified[unit.unit_id] = (unit.x, unit.y)
                self._log(t, unit, "skip", "spare, cannot fortify (already holding its post)")
                continue
            if result is not None and action_succeeded(result):
                t.acted.add(unit.unit_id)
                self._fortified[unit.unit_id] = (unit.x, unit.y)
                self._log(t, unit, "fortify", "spare unit, nothing to attack, so fortified",
                          result=result)  # fmt: skip
            else:
                self._refuse(t, unit, "spare")
                self._log(t, unit, "skip", "spare, fortify refused", result=result or "")

    async def _explore(self, game: MilitaryGame, t: _Turn, unit: Unit) -> bool:
        """Send a spare unit to explore while nothing hostile is in sight. True if it was sent."""
        s = self.settings
        automate = getattr(game, "automate_explore", None)
        if (
            automate is None
            or s.spare_explorers <= 0
            or t.turn > s.spare_explore_until
            or self._land_threat_near(t, unit)
            or len(self._explorers) >= s.spare_explorers
            or not self._available(t, unit, "spare")
        ):
            return False
        t.spare_orders += 1
        result = await self._order(t, unit, "spare", automate(unit.unit_index))
        if result is None or not action_succeeded(result):
            self._refuse(t, unit, "spare")
            return False
        t.acted.add(unit.unit_id)
        self._explorers[unit.unit_id] = (unit.x, unit.y)
        if (unit.x, unit.y) in t.at_city:
            t.at_city[unit.x, unit.y] -= 1  # It is leaving, so the city's count is one fewer.
        self._fortified.pop(unit.unit_id, None)
        self._log(t, unit, "explore", "spare unit, nothing hostile in sight, so exploring",
                  result=result)  # fmt: skip
        return True

    def _land_threat_near(self, t: _Turn, unit: Unit) -> bool:
        """A hostile land unit within `explore_clear_radius` of the unit. Run 15 screenshots: a
        barbarian ship in a bay or a camp across the map kept every spare unit home for ten turns,
        while nothing could reach the ship. Only what could reach the explorer counts."""
        s = self.settings
        return any(
            th.hostile
            and not any(word in th.unit_type for word in _SEA_UNITS)
            and hex_distance(unit.x, unit.y, th.x, th.y, s.map_width) <= s.explore_clear_radius
            for th in t.threats
        )

    def _exposed(self, t: _Turn, unit: Unit) -> bool:
        s = self.settings
        if self._on_camp_mission(t, unit):
            return False  # The guards beside it are the point, not a reason to go home.
        return any(
            th.hostile and hex_distance(unit.x, unit.y, th.x, th.y, s.map_width) <= s.flee_radius
            for th in t.threats
        )

    async def _hunt(self, game: MilitaryGame, t: _Turn, unit: Unit) -> bool:
        """Walk a spare unit next to the nearest hostile unit it would attack, if close and safe.

        The attack itself is left to the next turn's attack step. Never past `spare_leash` from a
        city. True if the unit was ordered.
        """
        s = self.settings
        w = s.weights
        if not self._available(t, unit, "move") or unit.targets:
            return False  # Targets in reach were judged by the attack step and turned down.
        options = []
        for threat in t.threats:
            where = (threat.x, threat.y)
            dist = hex_distance(unit.x, unit.y, *where, s.map_width)
            if not threat.hostile or where in t.dead or dist > s.spare_hunt_radius or dist <= 1:
                continue
            tgt = Target(threat.unit_type, threat.x, threat.y, threat.hp)
            odds = assess_attack(unit, tgt, threat, w, None, s.map_width)
            if odds is None or not odds.favourable:
                continue
            stage = self._stage_tile(unit, where)
            if stage is None:
                continue
            home = min(
                (hex_distance(stage[0], stage[1], c.x, c.y, s.map_width) for c in t.cities),
                default=None,
            )
            if home is None or home > s.spare_leash:
                continue
            options.append((dist, -odds.rank, where, stage, odds))
        if not options:
            return False
        *_, where, stage, odds = min(options, key=lambda o: o[:3])
        t.spare_orders += 1
        result = await self._order(t, unit, "move", game.move_unit(unit.unit_index, *stage))
        if result is not None and move_succeeded(result):
            t.acted.add(unit.unit_id)
            self._leave(t, unit)
            self._fortified.pop(unit.unit_id, None)
            self._log(t, unit, "hunt", "spare unit, moving next to a hostile unit it can beat",
                      where, odds=asdict(odds), result=result)  # fmt: skip
            return True
        self._refuse(t, unit, "move")
        self._log(t, unit, "hunt", "move refused", where, result=result or "")
        return False

    # ----- promotions -----

    async def _spend_promotions(self, game: MilitaryGame, units: Iterable[Unit], turn: int) -> None:
        try:
            notices = await game.get_notifications()
        except ConnectionError:
            raise
        except Exception:
            log.exception("Reading notifications for promotions failed")
            return
        if any(getattr(n, "type_name", "") == PROMOTION_AVAILABLE for n in notices):
            await self._promote(game, units, turn)

    async def _promote(self, game: MilitaryGame, units: Iterable[Unit], turn: int) -> bool:
        """Give promotions to units that have one waiting. False if any was refused."""
        s = self.settings
        asked = promoted = 0
        clean = True
        # Least recently asked first, so a cap on checks can't starve the units at the end.
        combat = (u for u in units if u.is_combat)
        for unit in sorted(
            combat, key=lambda u: (self._promotion_asked.get(u.unit_id, -1), u.unit_id)
        ):
            if asked >= s.max_promotion_checks or promoted >= s.max_promotions:
                break
            if self._promotion_refused.get(unit.unit_id, -1) >= turn:
                continue
            asked += 1
            self._promotion_asked[unit.unit_id] = turn
            try:
                options = (await game.get_unit_promotions(unit.unit_id)).promotions
            except ConnectionError:
                raise
            except ValueError:  # civ6-mcp's error for a unit it can't read experience from.
                continue
            except Exception:
                log.exception("Reading promotions for unit %s failed", unit.unit_id)
                self._promotion_refused[unit.unit_id] = turn + s.retry_turns
                clean = False
                continue
            choice = choose_promotion(options, s.promotion_preference)
            if choice is None:
                continue
            promoted += 1
            try:
                result = await game.promote_unit(unit.unit_id, choice.promotion_type)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Promoting unit %s failed", unit.unit_id)
                result = "Error: raised"
            ok = action_succeeded(result)
            if not ok:
                self._promotion_refused[unit.unit_id] = turn + s.retry_turns
                clean = False
            self._record(
                MilitaryDecision(
                    turn, unit.unit_id, unit.x, unit.y, "promote",
                    "promotion spent" if ok else "promotion refused",
                    result=f"{choice.promotion_type}: {result}",
                )
            )  # fmt: skip
        return clean
