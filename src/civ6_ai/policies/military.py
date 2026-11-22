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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace
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

# Our civilians that an enemy may take (issue 280).
_CAPTURE_KINDS = ("UNIT_SETTLER", "UNIT_TRADER")  # Ones whose vanishing may be a capture.
_CIVILIAN_WORDS = ("SETTLER", "BUILDER", "TRADER", "MISSIONARY", "APOSTLE")

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
    # Issue 295. A camp whose garrison was never seen, or not lately, is not empty: it is sized
    # against one defender of this strength until a sighting says otherwise.
    camp_unseen_strength: float = 15.0
    camp_min_hp: float = 0.8  # Only healthy units are sent to a camp.
    # Issue 279: the force is sized by simulated combat. Attacks to kill every known defender,
    # with this share more as a margin, must fit in `camp_strike_turns` turns of one attack per
    # unit. A force with no unit yet is sized with units of `camp_ref_strength`.
    camp_margin: float = 0.25
    camp_strike_turns: int = 3
    camp_ref_strength: float = 25.0
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
    recapture: bool = True  # Step onto a hostile civilian (issue 280).
    recapture_reach: int = 10  # Only a civilian this close to one of our cities.
    recapture_age: int = 3  # Turns a sighting stays believed (the threat map rescans every 3).
    recapture_threat: float = 4.0  # The most threat-map strength allowed on the tile stepped to.
    recapture_march_turns: int = 3  # A civilian this many turns of walking away is marched on.
    # Issue 293: a spare unit goes to look at where the threat map suspects an unseen camp.
    probe: bool = True
    probe_stand: int = 2  # It stops this many tiles short of the guess: it looks, never attacks.
    probe_threat: float = 4.0  # The most threat-map strength allowed on a tile it stops on.
    capture_radius: int = 4  # Tiles from where a settler was last seen that a captor may stand.
    capture_window: int = 4  # Turns a vanished settler waits for a hostile civilian to show.
    capture_creep: int = 2  # Tiles added to that radius for each turn a captor may have moved.
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
    # Issue 380 (STD_T1_04, T48-52): the plan judged the whole force and each unit then walked in
    # alone, past barbarians it had just rated as losing fights, until it was surrounded. An
    # advance step is scored, not forbidden (the owner: an absolute rule would stifle edge cases):
    # gain minus risk minus a penalty for going in short of the force, taken when it is at least
    # `camp_step_min_net`. The defaults make the lone Warrior's walk lose (two neighbours that
    # deal ~0.55 of its health each, nothing within 2) and let a step that sets up a combined
    # attack, or a cheap unit screening a settler, win.
    camp_group_radius: int = 2  # Units this close, or inside the muster ring too, go in together.
    camp_step_reach: int = 1  # Hostile units this close to the stop tile are counted as risk.
    camp_step_min_net: float = 0.0
    camp_risk_weight: float = 5.0  # Per unit's whole health expected to be lost to the hostiles.
    # Walking on past hostiles that can hit it next turn is the riskiest option: nobody covers its
    # back and it is not dug in, so its risk is this many times a fighter's or a holder's.
    camp_advance_risk_factor: float = 2.0
    camp_group_weight: float = 3.0  # Penalty for a force fully short of `needed` (scaled by gap).
    camp_progress_gain: float = 1.0  # Per tile closer to the camp.
    camp_setup_gain: float = 2.0  # Ends beside the camp with the whole force near.
    camp_finish_gain: float = 1.5  # A hostile in reach it would surely kill.
    camp_screen_gain: float = 2.0  # A friendly civilian beside the tile that the unit covers.
    camp_cover_gain: float = 0.5  # Per friendly soldier beside the tile (two at most).
    camp_escape_gain: float = 3.0  # Per unit of exposure the stop tile has less than here.
    # Fighting is one more option on the same scale (the owner: the Warrior walked toward the camp
    # past hostiles beside it instead of fighting or falling back). Attack: gain for the share of
    # the target's health dealt, for a sure kill, and a credit for the damage that unit would
    # deal to us anyway if left alone (a fight it cannot avoid is better taken on our terms).
    camp_attack_gain: float = 4.0
    camp_kill_gain: float = 2.0
    # Which target (issue 380, owner): other hits that land before it acts (a city-state unit
    # beside it, our units with attacks left, a city's strike) raise a target's value by the share
    # of its remaining health they take, and a kill they make possible is worth this much.
    camp_followup_gain: float = 2.0
    camp_followup_kill_gain: float = 1.0
    camp_unavoidable_credit: float = 2.0
    max_camp_turns: int = 8  # Turns spent assaulting one camp before it is set aside.
    camp_travel_turns: int = 20  # Turns a force may take to reach the muster point first.
    camp_retry_turns: int = 30
    # Issue 279: defenders seen at a camp in this many turns are what the force is sized against
    # (the old slowly decaying peak kept a need of 150 thirty turns after the sighting).
    camp_memory_turns: int = 10
    camp_pullback_hp: float = 0.5  # A unit on a camp mission under this health does not attack.
    camp_loss_limit: int = 3  # Units lost on a camp mission within `camp_loss_window` turns, with
    camp_loss_window: int = 10  # no hit on the camp's defenders in that time, end the campaign.
    camp_suspend_turns: int = 25  # How long such a camp is left alone, so defence comes first.
    # Issue 268: a known camp this close to a city is a goal. Production builds the force that
    # clears it when the spare units fall short (Run 20: a camp 5 tiles off for 70 turns, with
    # nothing spare, so never attacked).
    camp_goal_radius: int = 8
    # Issue 295. A camp this near a city (or nearer) may pull the force off a camp that is farther.
    camp_urgent_radius: int = 3
    camp_max_build: int = 4  # Units asked of production for one camp at a time.
    camp_build_turns: int = 4  # Turns counted for each unit still to be built, in the ETA.
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

    Run 20: a ranged unit's order came back as `MELEE_ATTACK`. The unit was stacked with another
    military unit, and civ6-mcp fell back to a melee attack that deals nothing. Only that is a
    failure here. Issue 275: civ6-mcp's reply always shows the enemy's hit points from before the
    combat (`enemy HP:80 -> 80/100` on a hit that killed), so a melee unit's result is judged by
    reading the target again (`_verify_melee`), never from this text.
    """
    lines = [line.strip() for line in result.splitlines()]
    if unit.ranged_strength > 0 and any(line.startswith("MELEE_ATTACK") for line in lines):
        return "a ranged unit's attack came back as MELEE_ATTACK"
    return None


def garrison_required(city: City, settings: MilitarySettings) -> int:
    """Defenders a city should hold: one, and a second when hostile units crowd it."""
    if len(city.threats) >= settings.reinforce_threat_count:
        return settings.max_garrison
    return 1


@dataclass(frozen=True)
class CampPlan:
    """The camp this empire means to clear, and what the force lacks (issue 268).

    Made by the military policy each turn and read by production through `DefenceState`.
    `needed` and `available` are strength totals on the scale of `camp_force_ok`; `units_wanted`
    is how many more combat units production should build, and `wants` the kind most missing
    (a melee unit takes the tile, a ranged one softens a strong defender).
    """

    target: tuple[int, int]
    distance: int  # From the camp to our nearest city.
    defenders: int
    needed: float
    available: float
    units_available: int
    units_wanted: int
    wants: str  # "melee", "ranged", "any", or "" when the force is enough.
    eta: int  # Turns until the force can strike: the walk, plus the units still to be built.
    rank: float  # Ongoing cost over the cost to clear; the highest ranked camp is the target.
    # The strongest defenders seen at the camp, remembered while they are out of sight (issue 275):
    # production picks units by how they match up against these.
    defender: float = 0.0
    defender_ranged: float = 0.0
    committed: bool = False  # The gathered force met the need and is going in.

    @property
    def met(self) -> bool:
        return self.units_wanted == 0


def _is_melee(unit: Unit) -> bool:
    """A unit that can close in and take the camp tile. Production counts the same units as melee
    (by the unit table's class), so a Persian Immortal, with a ranged attack too, qualifies."""
    if unit.combat_strength <= 0:
        return False
    stats = COMBAT_UNITS.get(unit.unit_type)
    if stats is not None:
        return stats[4] not in ("RANGED", "SIEGE", "RECON")
    return unit.ranged_strength == 0


def camp_force_ok(
    attackers: Iterable[Unit],
    defenders: Iterable[Threat],
    weights: MilitaryWeights,
    min_units: int = 2,
    need_melee: bool = False,
    need: float | None = None,
) -> bool:
    """True if the healthy units on hand can kill the camp's defenders in `camp_strike_turns`.

    `need` is the attacks required (default: `camp_attacks_needed`, with the margin). A force has
    one attack per unit a turn. An unseen garrison can be larger, so the margin is not a promise.
    """
    ours = [u for u in attackers if u.hp >= weights.camp_min_hp * u.max_hp]
    if len(ours) < min_units:
        return False
    if need_melee and not any(_is_melee(u) for u in ours):
        return False  # Only a unit that closes in takes the camp tile.
    wanted = camp_attacks_needed(ours, defenders, weights) if need is None else need
    return len(ours) * weights.camp_strike_turns >= wanted


def _attack_strength(unit: Unit) -> float:
    return max(unit.combat_strength, unit.ranged_strength) * unit.hp / unit.max_hp


def camp_attacks_needed(
    force: Iterable[Unit], defenders: Iterable[Threat], weights: MilitaryWeights
) -> float:
    """Attacks it takes the force to kill every known defender, plus the margin (issue 279).

    Simulated with `expected_damage`: ranged units shoot first, the strongest hitters first, and
    each hit goes to the strongest defender left, whose strength is raised by the terrain and
    fortification we cannot see. The units repeat in order for as long as it takes; with no unit
    yet a unit of `camp_ref_strength` is assumed. No defender seen needs no attack.
    """
    guards = sorted(
        ((d.combat_strength + weights.assumed_defender_bonus, float(d.hp)) for d in defenders),
        reverse=True,
    )
    if not guards:
        return 0.0
    units = sorted(force, key=lambda u: (u.ranged_strength <= 0, -_attack_strength(u), u.unit_id))
    strengths = [_attack_strength(u) for u in units] or [weights.camp_ref_strength]
    attacks = 0
    for guard, hp in guards:
        while hp > 0 and attacks < 200:
            hp -= expected_damage(strengths[attacks % len(strengths)], guard)
            attacks += 1
    return math.ceil(attacks * (1.0 + weights.camp_margin))


def camp_force_needed(defenders: Iterable[Threat], weights: MilitaryWeights) -> float:
    """The cost of clearing a camp, for ranking camps: strength with a margin over the defenders."""
    return weights.camp_force_ratio * sum(
        max(d.combat_strength, d.ranged_strength) + weights.assumed_defender_bonus
        for d in defenders
    )


def remembered_defenders(
    memory: list[tuple[int, tuple[Threat, ...]]],
    turn: int,
    seen: Iterable[Threat],
    window: int,
) -> tuple[list[tuple[int, tuple[Threat, ...]]], list[Threat]]:
    """The defenders a camp force is sized against: the strongest group seen in `window` turns.

    `memory` holds (turn, defenders) for each turn some were in sight. Older sightings drop out
    (issue 279: the slowly decaying peak of issue 275 kept a need from 30 turns back). Returns
    the kept memory and the group, the one with the greatest total strength (latest on a tie).
    """
    seen = tuple(seen)
    kept = [m for m in memory if m[0] >= turn - window and not (seen and m[0] == turn)]
    if seen:
        kept.append((turn, seen))
    if not kept:
        return kept, []
    best = max(kept, key=lambda m: (sum(d.combat_strength for d in m[1]), m[0]))
    return kept, list(best[1])


def camp_group_near(
    unit: Unit,
    force: Iterable[Unit],
    position: Callable[[Unit], tuple[int, int]],
    camp: tuple[int, int],
    radius: int,
    muster: int,
    width: int | None,
) -> list[Unit]:
    """The units of the force that go in with `unit` (issue 380), itself included.

    Those within `radius` of it, and, while it is inside the muster ring, those inside it too
    and within twice the radius (live STD_T1_04: a lead unit's mates on the far side of the ring,
    6 tiles off, made it "together" and it went in alone).
    """
    here = position(unit)
    ringed = hex_distance(*here, *camp, width) <= muster
    near = [unit]
    for other in force:
        if other.unit_id == unit.unit_id:
            continue
        spot = position(other)
        gap = hex_distance(*here, *spot, width)
        if gap <= radius or (
            ringed and gap <= 2 * radius and hex_distance(*spot, *camp, width) <= muster
        ):
            near.append(other)
    return near


@dataclass(frozen=True)
class StepScore:
    """A camp advance step weighed (issue 380): taken when `net` is at least the minimum."""

    risk: float
    penalty: float
    gain: float
    net: float
    exposure: float  # Expected damage from hostiles in reach, as a share of the unit's health.
    gap: float  # How far the force near is under the need, 0 (enough) to 1 (none).


def incoming_damage(unit: Unit, hostile: Threat) -> float:
    """Expected damage `hostile` deals to `unit` if it attacks it (it shoots when it has a ranged
    attack, and our melee strength defends), not the retaliation we would take attacking it."""
    attack = max(hostile.combat_strength, hostile.ranged_strength)
    if attack <= 0:
        return 0.0
    return expected_damage(attack, unit.combat_strength or unit.ranged_strength or attack)


def step_exposure(
    unit: Unit,
    tile: tuple[int, int],
    hostiles: Iterable[Threat],
    weights: MilitaryWeights,
    width: int | None,
    reach: int,
) -> tuple[float, bool]:
    """Expected damage over the unit's health from hostiles within `reach` of `tile` attacking it
    next turn, and whether the unit would surely kill one of them (the outgoing estimate behind
    the attack skips, "deals 23, takes 25", kept apart: a ranged unit takes nothing back from a
    melee unit it shoots, but that unit still attacks it)."""
    taken = 0.0
    kills = False
    for h in hostiles:
        if hex_distance(h.x, h.y, *tile, width) > reach:
            continue
        taken += incoming_damage(unit, h)
        odds = assess_attack(unit, Target(h.unit_type, h.x, h.y, h.hp), h, weights, None, width)
        kills = kills or (odds is not None and odds.sure_kill)
    return taken / max(unit.hp, 1), kills


@dataclass(frozen=True)
class TargetScore:
    """One attack on one target, weighed (issue 380). Every part is logged."""

    damage: float  # Share of its remaining health we deal, by its defence against our attack.
    kill: float  # A sure kill alone, or one the follow-up hits make.
    follow: float  # Share of what is left that the follow-up hits take.
    threat: float  # The damage it would deal us next turn, which removing it spares.
    retaliation: float  # Damage we take back attacking it (none for a ranged unit against melee).
    total: float


def score_target(
    unit: Unit,
    target: Threat,
    hp: float,
    odds: AttackOdds,
    followup: float,
    settings: "MilitarySettings",
) -> TargetScore:
    """Value of `unit` attacking `target` (remaining health `hp`) on one scale, from `odds` (the
    existing estimate) and `followup`, the expected damage other hits deal it before it acts."""
    s = settings
    own = max(unit.hp, 1)
    damage = s.camp_attack_gain * min(odds.dealt, hp) / max(hp, 1)
    left = max(hp - odds.dealt, 0.0)
    follow = s.camp_followup_gain * min(1.0, followup / left) if left > 0 else 0.0
    kill = s.camp_kill_gain if odds.sure_kill else 0.0
    v = s.weights.damage_variance
    if not odds.sure_kill and odds.dealt + followup * (1 - v) >= hp:
        kill = s.camp_followup_kill_gain
    threat = s.camp_unavoidable_credit * incoming_damage(unit, target) / own
    retaliation = s.camp_risk_weight * odds.taken / own
    return TargetScore(
        damage, kill, follow, threat, retaliation, damage + kill + follow + threat - retaliation
    )


def camp_step_score(
    unit: Unit,
    here: tuple[int, int],
    tile: tuple[int, int],
    camp: tuple[int, int],
    hostiles: Iterable[Threat],
    friends: Iterable[Unit],
    position: Callable[[Unit], tuple[int, int]],
    gap: float,
    settings: "MilitarySettings",
    advancing: bool = True,
) -> StepScore:
    """Weigh stepping to `tile`: gain (progress to the camp, a combined attack set up, a sure
    kill, screening a civilian, cover from friends, escaping a worse tile) against the risk
    (expected damage from hostiles that can reach it) and the penalty for a force short of
    `needed` (`gap`, 0 to 1). All weights are in `MilitarySettings`."""
    s = settings
    width = s.map_width
    hostiles = list(hostiles)
    exposure, kills = step_exposure(unit, tile, hostiles, s.weights, width, s.camp_step_reach)
    here_exposure, _ = step_exposure(unit, here, hostiles, s.weights, width, s.camp_step_reach)
    # Progress counts in full only with the force: alone it is a reason to wait, not to go on.
    gain = (
        s.camp_progress_gain
        * (1.0 - gap)
        * max(0, hex_distance(*here, *camp, width) - hex_distance(*tile, *camp, width))
        if advancing
        else 0.0
    )
    if advancing and hex_distance(*tile, *camp, width) <= 1 and gap <= 0:
        gain += s.camp_setup_gain
    if kills:
        gain += s.camp_finish_gain
    beside = [
        f
        for f in friends
        if f.unit_id != unit.unit_id and hex_distance(*position(f), *tile, width) <= 1
    ]
    soldiers = [f for f in beside if f.combat_strength > 0 or f.ranged_strength > 0]
    if exposure > 0 and len(soldiers) < len(beside):
        gain += s.camp_screen_gain
    gain += s.camp_cover_gain * min(len(soldiers), 2)
    gain += s.camp_escape_gain * max(0.0, here_exposure - exposure)
    risk = s.camp_risk_weight * exposure * (s.camp_advance_risk_factor if advancing else 1.0)
    penalty = s.camp_group_weight * gap
    return StepScore(risk, penalty, gain, gain - risk - penalty, exposure, gap)


def camp_matchup(
    unit_type: str,
    defender: float,
    defender_ranged: float,
    weights: "MilitaryWeights",
    wants: str = "",
) -> float | None:
    """Expected odds of one unit against the strongest known defender of a camp, or None.

    Dealt damage over damage taken, both from `expected_damage` with the defender's terrain and
    fortification assumed. A ranged unit takes damage back only from a ranged defender. A melee
    unit that loses one-on-one (odds under 1) scores 0 unless the force lacks a melee unit to take
    the tile, which an emptied camp needs of nothing more than standing there (`wants` "melee").
    None when the table does not know the unit or it cannot fight (siege, scouts).
    """
    stats = COMBAT_UNITS.get(unit_type)
    if stats is None or stats[4] in ("SIEGE", "RECON") or defender <= 0:
        return None
    melee, ranged = float(stats[0]), float(stats[1])
    guard = defender + weights.assumed_defender_bonus
    if ranged > 0:
        dealt = expected_damage(ranged, guard)
        taken = expected_damage(defender_ranged, melee or ranged) if defender_ranged > 0 else 0.0
        return dealt / max(taken, 1.0)
    if melee <= 0:
        return None
    odds = expected_damage(melee, guard) / max(expected_damage(guard, melee), 1.0)
    if odds < 1.0 and wants != "melee":
        return 0.0
    return odds


def plan_camp_force(
    force: Iterable[Unit],
    defenders: Iterable[Threat],
    weights: MilitaryWeights,
    settings: "MilitarySettings",
) -> tuple[float, float, int, str]:
    """What a camp force lacks: (attacks needed, attacks available, units to build, kind wanted).

    A healthy force that meets `camp_force_ok` (two units, one of them melee, and enough attacks
    in `camp_strike_turns` turns) lacks nothing. Otherwise the missing attacks are counted in
    units (one attack a turn each), at least enough to make two and to include a melee unit, and
    no more than `camp_max_build`.
    """
    defenders = list(defenders)
    ours = [u for u in force if u.hp >= weights.camp_min_hp * u.max_hp]
    needed = camp_attacks_needed(ours, defenders, weights)
    have = float(len(ours) * weights.camp_strike_turns)
    if camp_force_ok(ours, defenders, weights, settings.camp_min_units, True, needed):
        return needed, have, 0, ""
    gap = max(0.0, needed - have)
    wanted = max(
        settings.camp_min_units - len(ours),
        math.ceil(gap / weights.camp_strike_turns) if gap > 0 else 0,
        0 if any(_is_melee(u) for u in ours) else 1,
        1,
    )
    if not any(_is_melee(u) for u in ours):
        kind = "melee"
    elif defenders and not any(u.ranged_strength > 0 for u in ours):
        kind = "ranged"
    else:
        kind = "any"
    return needed, have, min(wanted, settings.camp_max_build), kind


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
    # Targets a melee attack landed on. Its reply shows the enemy's health from before the combat,
    # so these are read again after the attacks and judged by what is there (issue 275).
    melee_hit: dict[tuple[int, int], set[int]] = field(default_factory=dict)
    # Barbarian tiles of the threats before the re-read, so an attack on one that died still counts.
    barb_before: set[tuple[int, int]] = field(default_factory=set)
    # Per target, the best attack turned down: its odds, the unit and the reason.
    rejected: dict[tuple[int, int], tuple[AttackOdds, Unit, str]] = field(default_factory=dict)
    # Friendly units (city-state soldiers, at war with the barbarians) that hit a target before it
    # acts. The threat scan lists only units at war with us, so nothing fills this yet (issue 380).
    helpers: list[Threat] = field(default_factory=list)


@dataclass
class MilitaryPolicy:
    """Defends the empire. Run it after settling and before the blocker resolver."""

    settings: MilitarySettings = field(default_factory=MilitarySettings)
    name: str = "military"
    decisions: list[MilitaryDecision] = field(default_factory=list)
    # Per unit, kept across turns and pruned each turn against the units that still exist.
    _resting: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    # Kills made so far, for the boost judgement (#344): by the killer's unit type without its
    # `UNIT_` prefix (`SLINGER`), plus `BARBARIAN` for barbarian units and `BARBARIAN_OUTPOST`
    # for camps we cleared. Each tile's kill is counted once per turn.
    kills: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _kills_counted: set[tuple[int, tuple[int, int]]] = field(
        default_factory=set, init=False, repr=False
    )
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
    # The terrain store (issue 370), set by the bot's wiring. Without it every tile counts as
    # passable, which is the old behaviour. Fallback, swap and approach tiles skip water and
    # mountains the store knows; a tile it has not read stays a candidate.
    terrain: Any = field(default=None, init=False, repr=False)
    _released: int = field(default=0, init=False, repr=False)
    # Camps marched on that have gone, reported once each on the next offense line.
    _camps_gone: int = field(default=0, init=False, repr=False)
    _gone_noted: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    _muster_bad: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _mission: dict[int, tuple[tuple[int, int], int]] = field(
        default_factory=dict, init=False, repr=False
    )  # Unit id to the camp it was sent at and the turn.
    _camp_home_scan: int = field(default=-99, init=False, repr=False)
    # Issues 275 and 279. Per camp: the (turn, defenders) sightings of the last
    # `camp_memory_turns`, kept while the defenders are out of sight; the turn the force
    # committed; turns units on its mission died; and the last turn one of our attacks hit its
    # defenders.
    _camp_memory: dict[tuple[int, int], list[tuple[int, tuple[Threat, ...]]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _known_last: set[tuple[int, int]] | None = field(default=None, init=False, repr=False)
    _camps_ours: int = field(default=0, init=False, repr=False)
    _committed: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    # Issue 295: the camp the plan chose last, kept while the force is on it; and the last turn
    # each camp's own tile was in sight (an empty camp seen is not an unseen garrison).
    _plan_target: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _camp_seen: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _losses: dict[tuple[int, int], list[int]] = field(default_factory=dict, init=False, repr=False)
    _camp_hit: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    # The threat state (see `defence.DefenceState`), or None to play without it.
    defence: Any = field(default=None, repr=False)
    # Issue 280. Owners seen hostile (a civ at war), our civilians as last seen, and what the
    # offense line reports once: civilians retaken, civilians of ours that were captured.
    _hostile_owners: set[str] = field(default_factory=set, init=False, repr=False)
    _own_civilians: dict[int, tuple[str, int, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    # Settlers and traders that vanished and are not yet told from used: id -> (kind, x, y, turn).
    _vanished: dict[int, tuple[str, int, int, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _city_count: int = field(default=0, init=False, repr=False)
    _recaptured: int = field(default=0, init=False, repr=False)
    _captured: int = field(default=0, init=False, repr=False)
    # Issue 293: the last reason no probe was sent (logged on change), and the unit walking out.
    _probe_why: tuple[Any, ...] | None = field(default=None, init=False, repr=False)
    _probe_unit: int | None = field(default=None, init=False, repr=False)

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
        self._note_civilians(t)
        if await self._split_stacks(game, t):
            # The moved units stand elsewhere now, with targets seen from their new tiles.
            units = await _read_units(game, s.map_width)
            t.units = tuple(units)
            t.threats = await _read_threats(game)  # A move may reveal a new target.
            t.mine = [u for u in units if is_land_military(u) and u.unit_id not in t.reserved]
            self._count_defenders(t)
        await self._attack(game, t)
        await self._recapture(game, t)
        await self._heal(game, t)
        await self._garrison(game, t)
        self._note_sightings(t)
        await self._clear_camp(game, t)
        await self._probe_suspect(game, t)
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

    def _passable(self, p: tuple[int, int]) -> bool:
        """Whether a land unit can enter the tile, as far as the terrain store knows (issue 370).

        Water and mountains are refused by the game, so a candidate on one wastes an order and
        the unit ends the turn where it stood. An unread tile is kept: the game decides.
        """
        terrain = self.terrain or getattr(self.threat_map, "terrain", None)
        return terrain is None or terrain.step_cost(p[0], p[1], naval=False) is not None

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
        found = {p for p in found if self._passable(p)}
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

    # ----- hostile civilians (issue 280) -----

    def _hostile_civilians(self, t: _Turn, guarded: bool = False) -> list[Any]:
        """Hostile civilians the threat map has seen lately, nearest our cities first.

        Barbarians always; any other civ only once a unit of it was seen hostile (at war). The
        threat scan lists military units only, so these come from the tile reads the map already
        makes.
        """
        if self.threat_map is None:
            return []
        for th in t.threats:  # What the scan says now: peace takes a civ off the list.
            if th.owner_id == BARBARIAN_ID:
                continue
            if th.hostile:
                self._hostile_owners.add(th.owner)
            else:
                self._hostile_owners.discard(th.owner)
        s = self.settings
        found = []
        for civ in getattr(self.threat_map, "civilians", {}).values():
            if t.turn - civ.seen > s.recapture_age:
                continue
            if not self._is_hostile(civ.owner):
                continue
            if civ.guarded and not guarded:
                continue  # A soldier stands with it: a target to fight, not to step onto.
            home = self._home_distance(t, (civ.x, civ.y))
            if home <= s.recapture_reach:
                found.append((home, civ.x, civ.y, civ))
        return [f[3] for f in sorted(found, key=lambda f: f[:3])]

    def _is_hostile(self, owner: str) -> bool:
        return owner.startswith("Barbarian") or owner in self._hostile_owners

    def _note_civilians(self, t: _Turn) -> None:
        """Tell a civilian of ours that was taken from one that was killed.

        The game reports both as "was killed". It was taken when a hostile civilian of its kind
        stands near where it was last seen. That sighting may come a turn or two after the loss
        (live, issue 283: the tile read that shows the taken settler came a turn late, and it
        stood under the soldier that took it), so a vanished settler is kept for a few turns and
        looked for again each turn, with the reach widened by what its captor may have walked.
        """
        s = self.settings
        now = {
            u.unit_id: (u.unit_type, u.x, u.y)
            for u in t.units
            if not u.is_combat and any(w in u.unit_type for w in _CIVILIAN_WORDS)
        }
        hostile = self._hostile_civilians(t, guarded=True)
        founded = len(t.cities) > self._city_count
        for uid, (kind, x, y) in self._own_civilians.items():
            if uid in now or kind not in _CAPTURE_KINDS or founded:
                continue  # A builder spends its charges and a settler founds: used, not taken.
            self._vanished[uid] = (kind, x, y, t.turn)
        for uid in [u for u in self._vanished if u in now]:
            del self._vanished[uid]
        for uid, (kind, x, y, lost) in list(self._vanished.items()):
            age = t.turn - lost
            if age > s.capture_window:
                del self._vanished[uid]
                log.info(
                    "military_capture_unknown %s",
                    json.dumps({"turn": t.turn, "unit": kind, "from": [x, y], "lost": lost}),
                )
                continue
            reach = s.capture_radius + s.capture_creep * age
            taken = [
                c
                for c in hostile
                if c.seen >= lost
                and (kind.removeprefix("UNIT_") == c.unit or c.unit in kind)
                and hex_distance(c.x, c.y, x, y, s.map_width) <= reach
            ]
            if not taken:
                continue  # Not seen yet: it may still show on a later read.
            del self._vanished[uid]
            self._captured += 1
            civ = min(taken, key=lambda c: hex_distance(c.x, c.y, x, y, s.map_width))
            log.info(
                "military_capture %s",
                json.dumps(
                    {
                        "turn": t.turn,
                        "lost": lost,
                        "unit": kind,
                        "from": [x, y],
                        "now_at": [civ.x, civ.y],
                        "guarded": civ.guarded,
                        "by": civ.owner,
                    },
                    sort_keys=True,
                ),
            )
        self._own_civilians = now
        self._city_count = len(t.cities)

    def _skip_recapture(self, t: _Turn, civ: Any, reason: str, **extra: Any) -> None:
        """Say why a hostile civilian in sight was left alone this turn (issue 283)."""
        log.info(
            "military_recapture_skip %s",
            json.dumps(
                {
                    "turn": t.turn,
                    "at": [civ.x, civ.y],
                    "owner": civ.owner,
                    "unit": civ.unit,
                    "reason": reason,
                    **extra,
                },
                sort_keys=True,
            ),
        )

    async def _recapture(self, game: MilitaryGame, t: _Turn) -> None:
        """Move a combat unit onto a hostile civilian it can reach, to take it back.

        The unit must be spare (no city is left short by it leaving), healthy, and the tile must
        be safe on the threat map. A civilian within a turn's walk is read again first, as the
        sighting may be a turn or two old, then stepped onto. One a few turns of walking away
        is marched on, the unit stopping short and going on next turn. Every civilian left alone
        is logged with the reason.
        """
        s = self.settings
        if not s.recapture or self.threat_map is None:
            return
        for civ in self._hostile_civilians(t, guarded=True):
            where = (civ.x, civ.y)
            if civ.guarded:
                self._skip_recapture(t, civ, "a soldier stands on the tile with it")
                continue
            if where in t.dead:
                self._skip_recapture(t, civ, "a unit of ours just died there")
                continue
            if t.actions >= s.max_actions:
                self._skip_recapture(t, civ, "the action limit for the turn is used")
                continue
            if not self._passable((civ.x, civ.y)):
                self._skip_recapture(t, civ, "the tile is water or impassable (needs embarking)")
                continue
            strength = self.threat_map.threat_at(civ.x, civ.y, t.turn)
            if strength > s.recapture_threat:
                self._skip_recapture(
                    t, civ, "inside a camp's or a hostile unit's reach", threat=round(strength, 1)
                )
                continue
            candidates = sorted(
                (
                    (hex_distance(u.x, u.y, civ.x, civ.y, s.map_width), u.unit_id, u)
                    for u in t.mine
                    if self._available(t, u, "recapture")
                    and u.hp >= s.weights.heal_below * u.max_hp
                    and self._spare(t, u)
                ),
                key=lambda c: c[:2],
            )
            if not candidates:
                self._skip_recapture(t, civ, "no spare unit")
                continue
            unit = next((u for d, _, u in candidates if 0 < d <= int(u.moves)), None)
            marching = unit is None
            if marching:
                unit = next(
                    (
                        u
                        for d, _, u in candidates
                        if 0 < d <= max(1, int(u.moves)) * s.recapture_march_turns
                        and not self._on_camp_mission(t, u)  # Not pulled off a camp.
                    ),
                    None,
                )
            if unit is None:
                d, _, near = candidates[0]
                self._skip_recapture(
                    t, civ, "out of reach", nearest=d, moves=int(near.moves),
                    turns_allowed=s.recapture_march_turns,
                )  # fmt: skip
                continue
            if not marching:
                try:
                    area = await read_area(game, civ.x, civ.y, 0)
                except ConnectionError:
                    raise
                except Exception:
                    log.exception("Reading the tile of a hostile civilian failed")
                    self._skip_recapture(t, civ, "reading the tile failed")
                    continue
                self.threat_map.see_tiles(t.turn, area)
                seen = next((x for x in area if (x.x, x.y) == where and x.visible), None)
                civ = getattr(self.threat_map, "civilians", {}).get(where) if seen else None
                if civ is None or civ.seen != t.turn or not self._is_hostile(civ.owner):
                    self._skip_recapture(
                        t, SimpleNamespace(x=where[0], y=where[1], owner="?", unit="?"),
                        "not in sight now, gone, taken back, or no longer at war",
                    )  # fmt: skip
                    continue
                if civ.guarded:
                    self._skip_recapture(t, civ, "a soldier stands on the tile with it")
                    continue
            goal = where
            if marching:
                # Stop on a tile this turn's walk reaches, and only if it is safe: the target
                # being safe says nothing of the way there.
                line = hex_line((unit.x, unit.y), where, s.map_width)
                reach = min(max(1, int(unit.moves)), len(line))
                goal = None
                stop = 0.0
                for step in reversed(line[:reach]):  # The furthest tile that can be stood on.
                    if not self._passable(step):
                        continue  # Water or a mountain on the straight line.
                    stop = self.threat_map.threat_at(step[0], step[1], t.turn)
                    goal = step
                    break
                if goal is None or stop > s.recapture_threat:
                    self._skip_recapture(
                        t, civ,
                        "no passable tile on the way" if goal is None
                        else "the march would stop in a threatened tile",
                        stop=list(goal) if goal else None, threat=round(stop, 1),
                    )  # fmt: skip
                    continue
            result = await self._order(t, unit, "recapture", game.move_unit(unit.unit_index, *goal))
            if result is not None and move_succeeded(result):
                self._leave(t, unit)
                t.acted.add(unit.unit_id)
                at = self._reached(result, where)
                t.placed[unit.unit_id] = at
                if at == where:
                    self._recaptured += 1
                    note = f"recapture: {civ.owner} {civ.unit} at {where[0]},{where[1]}"
                else:  # Terrain cost a longer walk: the unit is on its way, nothing is taken yet.
                    note = f"recapture: stopped at {at[0]},{at[1]} short of {where[0]},{where[1]}"
                self._log(t, unit, "recapture", note, where, result=result)
            else:
                self._refuse(t, unit, "recapture")
                self._log(
                    t, unit, "recapture", "recapture: move refused", where, result=result or ""
                )

    # ----- offense summary -----

    def _log_offense(self, t: _Turn) -> None:
        """One line a turn on how much offense there was and what it faced (Run 15).

        The effectiveness report reads it to judge barbarian handling in the early game and
        attacking later. `kills_barb` counts the barbarian targets that died this turn: the reply
        said "killed", the estimated damage took the last hit points, or a later attack found the
        tile empty (Run 16 counted only replies saying "killed", which the game rarely prints, and
        read 0 over a hundred and fifty turns). `camps_gone` counts camps we marched on that are
        no longer known and not taken by one of our units (cleared by someone else, or gone from
        the map); `camps_cleared_by_us` counts those with one of our units on or beside the tile
        (issue 279).
        Turns with no combat unit write no line.
        """
        s = self.settings
        barbs = [b for b in t.threats if b.owner_id == BARBARIAN_ID and b.hostile]
        barb_tiles = {(b.x, b.y) for b in barbs} | t.barb_before
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
            "recaptures": self._recaptured,
            "civilians_captured": self._captured,
            "civilians_hostile": len(self._hostile_civilians(t)),
            "camps_cleared_by_us": self._camps_ours,
            "camp_orders": sum(1 for d in mine if d.action == "camp" and "refused" not in d.reason),
            "map_use": {**t.map_use, "missions_released": self._released},
            "map_on": self.threat_map is not None,
        }
        self._count_kills(t, barb_tiles)
        self._released = 0
        self._camps_gone = 0
        self._recaptured = self._captured = 0
        self._camps_ours = 0
        log.info("military_offense %s", json.dumps(line, sort_keys=True))

    def _count_kills(self, t: _Turn, barb_tiles: set[tuple[int, int]]) -> None:
        """Add this turn's kills to `kills`: the killer's type, and barbarians and camps."""
        self._kills_counted = {k for k in self._kills_counted if k[0] == t.turn}
        types = {u.unit_id: u.unit_type.removeprefix("UNIT_") for u in t.units}
        for where in sorted(t.killed):
            if (t.turn, where) in self._kills_counted:
                continue  # Military ran twice this turn, or the tile was counted already.
            self._kills_counted.add((t.turn, where))
            if where in barb_tiles:
                self.kills["BARBARIAN"] = self.kills.get("BARBARIAN", 0) + 1
            # The last landed attack on the tile made the kill. A city strike has no unit type.
            hits = [
                d for d in self.decisions
                if d.turn == t.turn and d.action == "attack" and d.target == where
                and "refused" not in d.reason and "refused" not in d.result
            ]  # fmt: skip
            killer = types.get(hits[-1].unit_id) if hits else None
            if killer:
                self.kills[killer] = self.kills.get(killer, 0) + 1
        if self._camps_ours:
            self.kills["BARBARIAN_OUTPOST"] = (
                self.kills.get("BARBARIAN_OUTPOST", 0) + self._camps_ours
            )

    # ----- bookkeeping -----

    def _forget(self, units: Iterable[Unit], turn: int) -> None:
        """Drop what is kept for units that are gone, have moved or healed, and old set-asides."""
        by_id = {u.unit_id: u for u in units}
        for uid, (camp, sent) in list(self._mission.items()):
            if uid not in by_id:
                del self._mission[uid]
                if turn - sent <= self.settings.camp_mission_turns:
                    self._losses.setdefault(camp, []).append(turn)  # Lost on the way or at it.
        window = self.settings.camp_loss_window
        self._losses = {
            c: [x for x in v if turn - x <= window]
            for c, v in self._losses.items()
            if any(turn - x <= window for x in v)
        }
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

    def _keep_one_garrison(self, t: _Turn, units: list[Unit]) -> list[Unit]:
        """Leave one unit in each city: of the units standing in a city, take all but one."""
        left = dict(t.at_city)
        kept = []
        for unit in sorted(units, key=lambda u: u.unit_id):
            tile = (unit.x, unit.y)
            if tile in left and self._city_at(t, unit) is not None:
                if left[tile] <= 1:
                    continue
                left[tile] -= 1
            kept.append(unit)
        return kept

    def _city_short(self, city: City) -> bool:
        """True if the threat state calls the city short of defenders (by its map reading where
        hostile units are near, else the head count). No state: not short."""
        found = self.defence.cities.get(city.city_id) if self.defence is not None else None
        return found is not None and found.short

    def _camp_available(self, t: _Turn, unit: Unit) -> bool:
        """True if the unit can join a camp force (issue 279).

        A city that is not short keeps one garrison, and its other units (in it, or beside it)
        are free, though the threat state marks it. A short city's units are never taken. A unit
        in no city's care is as `_spare`.
        """
        city = self._city_at(t, unit)
        if city is not None:
            return t.at_city.get((unit.x, unit.y), 0) > 1 and not self._city_short(city)
        w = self.settings.map_width
        beside = [c for c in t.cities if hex_distance(c.x, c.y, *self._pos(t, unit), w) == 1]
        if beside:
            return not any(self._city_short(c) for c in beside)
        return self._spare(t, unit)

    def _log_hold(
        self,
        t: _Turn,
        reason: str,
        camp: tuple[int, int],
        plan: "CampPlan | None",
        free: list[Unit],
        gathered: list[Unit] | None = None,
    ) -> None:
        """Say why the camp force did not commit this turn (issue 279): the need, what is
        available, the gap, and what each city holds (units in it, beside it, and free of it)."""
        w = self.settings.map_width
        free_ids = {u.unit_id for u in free}
        held = {}
        for city in t.cities:
            near = [u for u in t.mine if hex_distance(city.x, city.y, *self._pos(t, u), w) <= 1]
            held[city.name] = {
                "inside": t.at_city.get((city.x, city.y), 0),
                "beside": self._beside(t, city),
                "short": self._city_short(city),
                "free": sum(1 for u in near if u.unit_id in free_ids),
            }
        needed = plan.needed if plan is not None else 0.0
        have = plan.available if plan is not None else 0.0
        log.info(
            "military_camp_hold %s",
            json.dumps(
                {
                    "turn": t.turn,
                    "camp": list(camp),
                    "reason": reason,
                    "needed": needed,
                    "available": have,
                    "gap": round(max(0.0, needed - have), 1),
                    "units_free": len(free),
                    "units_gathered": len(gathered) if gathered is not None else None,
                    "held": held,
                },
                sort_keys=True,
            ),
        )

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
        await self._verify_melee(game, t)
        self._log_rejections(t)

    async def _verify_melee(self, game: MilitaryGame, t: _Turn) -> None:
        """Judge the melee attacks of this turn by one read of the threats after all of them.

        The reply to a melee attack shows the enemy's health from before the combat, so a kill
        was only ever guessed from the damage estimate. A tile with no hostile unit now is a
        kill, and one with a unit shows its real health. The read also refreshes `t.threats` for
        the steps that follow. One call a turn, and only on a turn with a melee attack.
        """
        if not t.melee_hit:
            return
        try:
            threats = await _read_threats(game)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Reading the threats after the melee attacks failed")
            return
        t.barb_before = {(b.x, b.y) for b in t.threats if b.owner_id == BARBARIAN_ID}
        t.threats = threats
        alive: set[int] | None = None
        for where, attackers in t.melee_hit.items():
            left = [th for th in threats if (th.x, th.y) == where and th.hostile]
            if not left:
                # The scan lists visible units only: if every attacker died, the target may be
                # out of sight and alive, so a kill is confirmed only with an attacker standing.
                if alive is None:
                    try:
                        alive = {
                            u.unit_id for u in await _read_units(game, self.settings.map_width)
                        }
                    except ConnectionError:
                        raise
                    except Exception:
                        log.exception("Reading the units after the melee attacks failed")
                        alive = set()
                if attackers & alive:
                    t.dead.add(where)
                    t.killed.add(where)
                    t.confirmed.add(where)
            elif where not in t.confirmed:
                # Still standing: the estimate that killed it was wrong, and what is left is real.
                t.dead.discard(where)
                t.killed.discard(where)
                t.target_hp[where] = max(th.hp for th in left)

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
        foe = self._defender_at(t, (target.x, target.y))
        numbers = asdict(odds)
        if foe is not None:  # The weighing behind the choice of target (issue 380).
            numbers["target_score"] = asdict(self._target_score(t, unit, foe, odds))
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
                target=where, odds=numbers, result=result or "",
            )  # fmt: skip
            return False
        failure = attack_failed(unit, result)
        if failure is not None:
            # Not a hit: no kill, no attack counted, and the unit is free to try something else.
            self._refused[unit.unit_id, "attack"] = t.turn  # Not asked to attack again this turn.
            self._log(
                t, unit, "attack", f"attack failed: {failure}",
                target=where, odds=numbers, result=result,
            )  # fmt: skip
            return False
        t.acted.add(unit.unit_id)
        t.attacked.add(where)
        if unit.ranged_strength <= 0:
            t.melee_hit.setdefault(where, set()).add(unit.unit_id)
        t.hp[unit.unit_id] = max(0.0, t.hp.get(unit.unit_id, unit.hp) - odds.taken)  # Expected.
        self._fortified.pop(unit.unit_id, None)  # Attacking ends a fortification.
        self._record_hit(t, where, result, odds.dealt, target.hp)
        for camp in self._known_camps():
            if hex_distance(*where, *camp, self.settings.map_width) <= (
                self.settings.camp_defender_radius
            ):
                self._camp_hit[camp] = t.turn  # The campaign is doing damage.
        self._log(t, unit, "attack", reason, target=where, odds=numbers, result=result)
        return True

    def _attack_options(self, t: _Turn, unit: Unit) -> list[tuple[AttackOdds, Target, Threat]]:
        """Attacks this unit would take, best first. Turned-down ones are kept for the log."""
        s = self.settings
        w = s.weights
        floor = self._floor_for(t, unit)
        used = w.min_hp_after_attack if floor is None else floor
        hurt = unit.hp < w.heal_below * unit.max_hp
        sent = self._mission.get(unit.unit_id)
        if (
            sent is not None
            and t.turn - sent[1] <= s.camp_mission_turns
            and unit.hp < s.camp_pullback_hp * unit.max_hp
        ):
            return []  # Issue 275: a wounded unit of the campaign pulls back, sure kill or not.
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
        found.sort(
            key=lambda f: (
                -self._target_score(t, unit, f[2], f[0]).total,
                -f[0].rank,
                f[1].x,
                f[1].y,
            )
        )
        return found

    def _followup_damage(self, t: _Turn, unit: Unit, foe: Threat) -> float:
        """Expected damage that hits other than `unit`'s land on `foe` before it acts: our other
        units that can still attack it, a city-state unit beside it (at war with the barbarians),
        and a city strike."""
        s = self.settings
        where = (foe.x, foe.y)
        guard = foe.combat_strength + s.weights.assumed_defender_bonus
        total = 0.0
        for u in t.mine:
            if u.unit_id == unit.unit_id or not self._available(t, u, "attack"):
                continue
            if any((tg.x, tg.y) == where for tg in u.targets):
                total += expected_damage(max(u.combat_strength, u.ranged_strength), guard)
        for th in t.helpers:
            near = hex_distance(th.x, th.y, *where, s.map_width) <= 1
            if near and th.combat_strength > 0:
                total += expected_damage(max(th.combat_strength, th.ranged_strength), guard)
        for city in t.cities:
            in_range = hex_distance(city.x, city.y, *where, s.map_width) <= s.city_strike_range
            odds = self._city_strike_odds(city, foe, foe.hp) if in_range else None
            if odds is not None and self._city_can_strike(t, city):
                total += odds.dealt
        return total

    def _target_score(self, t: _Turn, unit: Unit, foe: Threat, odds: AttackOdds) -> TargetScore:
        hp = t.target_hp.get((foe.x, foe.y), foe.hp)
        return score_target(unit, foe, hp, odds, self._followup_damage(t, unit, foe), self.settings)

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
        found = {p for p in found if self._passable(p)}  # Not water or a mountain (issue 370).
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
        found = {p for p in found if self._passable(p)}  # Not water or a mountain (issue 370).
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
            and self._camp_available(t, u)
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
            and self._camp_available(t, u)
        ]
        spare = self._keep_one_garrison(t, spare)
        if t.camp_scanned:
            return
        t.camp_scanned = True
        await self._scan_camps(game, t, barbarians)
        if self.defence is not None:  # The strongest barbarian about, for the army floor (279).
            self.defence.hostile = (
                max((b.combat_strength for b in barbarians), default=0.0),
                max((b.ranged_strength for b in barbarians), default=0.0),
            )
        known = self._known_camps()
        self._gone_noted -= known  # A camp that is back is a new one.
        vanished = (self._known_last or set()) - known
        for camp in sorted({m[0] for m in self._mission.values()} - known | vanished):
            self._note_gone(camp, t)  # Before the force is sent at another camp.
        self._known_last = set(known)
        own = set(self._camps)
        if self.threat_map is not None:
            t.map_use["camps_only_in_map"] = len(known - own)
        self._suspend_failing(t, sorted(known))
        camps = [
            c
            for c in sorted(known)
            if self._camp_aside.get(c, -1) < t.turn
            and self._home_distance(t, c) <= s.camp_home_radius
        ]
        plan = self._plan_camps(t, camps, spare)
        if len(spare) < s.camp_min_units or not camps:
            if plan is not None:
                self._log_hold(t, "too few units available", plan.target, plan, spare)
            return
        where = (
            plan.target
            if plan is not None
            else min(camps, key=lambda c: (self._home_distance(t, c), c))
        )
        if where not in own:
            t.map_use["target_from_map"] = 1  # The map alone put this camp on the list.
        defenders = self._assume_unseen(
            t, where, self._remember(t, where, self._camp_defenders(t, where))[0]
        )
        force = [u for u in spare if hex_distance(u.x, u.y, *where, s.map_width) <= s.camp_reach]
        if not camp_force_ok(force, defenders, s.weights, s.camp_min_units, True):
            self._committed.pop(where, None)
            self._log_hold(t, "force short of the need", where, plan, spare)
            return
        muster = s.camp_muster_radius
        # Issue 275: the force gathers at the rally ring, out of the camp's reach, and goes in
        # together once what is gathered meets the need. A unit never goes in alone.
        rallied = [u for u in force if hex_distance(u.x, u.y, *where, s.map_width) <= muster]
        committed = where in self._committed
        outgrown = plan is not None and plan.target == where and plan.needed > plan.available
        if outgrown:
            self._committed.pop(where, None)  # Issue 380: the need outgrew the force; regroup.
            committed = False
        ready = camp_force_ok(rallied, defenders, s.weights, s.camp_min_units, True)
        if not committed and not outgrown and ready:
            committed = True
            self._committed[where] = t.turn
            t.map_use["camp_committed"] = 1
        if self.defence is not None and self.defence.camp_plan is not None:
            self.defence.camp_plan = replace(self.defence.camp_plan, committed=committed)
        # Two limits. The walk to the muster point has its own, longer one (Run 18: a far camp's
        # force took eight turns to arrive, which used up the assault's limit before anyone was
        # there, and 4-9 spare units then sat idle for twenty turns). The assault keeps its own.
        key = (where, committed)
        limit = s.max_camp_turns if key[1] else s.camp_travel_turns
        spent = self._camp_turns.get(key, (0, t.turn))[0] + 1
        self._camp_turns[key] = (spent, t.turn)
        if not committed:
            self._log_hold(t, "gathering at the rally ring", where, plan, spare, rallied)
        if spent > limit:
            self._camp_aside[where] = t.turn + s.camp_retry_turns
            self._camp_turns.pop((where, True), None)
            self._camp_turns.pop((where, False), None)
            self._committed.pop(where, None)
            return
        occupied = any((b.x, b.y) == where for b in t.threats if b.hostile)
        for unit in sorted(force, key=lambda u: u.unit_id):
            self._mission[unit.unit_id] = (where, t.turn)
            if not self._available(t, unit, "move"):
                continue
            far = hex_distance(unit.x, unit.y, *where, s.map_width) > muster
            adjacent = hex_distance(unit.x, unit.y, *where, s.map_width) <= 1
            if not far and not committed and not (adjacent and not occupied):
                t.acted.add(unit.unit_id)  # Waiting for the rest; not idle, not spare.
                t.map_use["muster_waits"] = t.map_use.get("muster_waits", 0) + 1
                self._log(t, unit, "camp", "gathered, waiting for the rest of the force", where)
                continue
            # Run 15: a move straight onto a camp three tiles off is refused (the game only allows
            # that capture move from beside it), so walk to the tile beside it first.
            if adjacent and not occupied:
                goals = [where]
            elif adjacent:
                continue  # In reach of the guards: the attack step takes it from here.
            elif far:
                goals = self._muster_goals(t, unit, where)
                t.map_use["muster_moves"] = t.map_use.get("muster_moves", 0) + 1
            else:
                goals = self._stage_goals(t, unit, where)
            gap = 0.0
            near = [unit]
            if committed and not far:
                near = camp_group_near(
                    unit, force, lambda u: self._pos(t, u), where, s.camp_group_radius,
                    muster, s.map_width,
                )  # fmt: skip
                need = camp_attacks_needed(near, defenders, s.weights)
                have = len(near) * s.weights.camp_strike_turns
                gap = max(0.0, 1.0 - have / need) if need > 0 else 0.0
                if len(near) < s.camp_min_units:
                    gap = 1.0  # A lone unit is the whole shortfall.
                if adjacent and not occupied:
                    gap = 0.0  # Taking an empty camp from beside it needs no force.
            # With the force together the camp's own barbarian guards are the fight it came for;
            # every other hostile unit, of any nation at war with us, is risk. On the walk to the
            # muster all of them are.
            guards = [
                b for b in t.threats
                if b.hostile
                and (b.owner_id != BARBARIAN_ID or far or gap > 0
                     or hex_distance(b.x, b.y, *where, s.map_width) > s.camp_defender_radius)
            ]  # fmt: skip
            here = self._pos(t, unit)

            def weigh(
                goal: tuple[int, int], unit=unit, guards=guards, here=here, base=gap, **kw: Any
            ) -> StepScore:
                # The tile it stops on this turn. `hex_line` leaves out the start, so a unit with
                # two moves stops at index 1.
                line = hex_line(here, goal, s.map_width)
                stop = line[min(max(1, int(unit.moves)), len(line)) - 1] if line else goal
                return camp_step_score(
                    unit, here, stop, where, guards, t.units, lambda other: self._pos(t, other),
                    kw.pop("gap", base), s, **kw,
                )  # fmt: skip

            # Every option on one scale: advance, fight, hold, fall back (issue 380).
            advance = [(g, weigh(g)) for g in goals]
            advance = [(g, sc) for g, sc in advance if sc.net >= s.camp_step_min_net]
            advance.sort(key=lambda gs: -gs[1].net)  # The goal that won is the one tried first.
            options: dict[str, float] = {}
            if advance:
                options["advance"] = max(sc.net for _, sc in advance)
            hold = weigh(here, gap=0.0, advancing=False)
            options["hold"] = hold.net
            # Falling back is toward the friends (one step along the line to the nearest) or
            # onto a muster tile no nearer the camp, never deeper in.
            mates = sorted(
                (hex_distance(*here, *self._pos(t, u), s.map_width), u.unit_id, self._pos(t, u))
                for u in t.mine
                if u.unit_id != unit.unit_id
            )
            taken_tiles = {self._pos(t, u) for u in t.units} | {(b.x, b.y) for b in t.threats}
            retreats = [
                g for g in self._muster_goals(t, unit, where)
                if hex_distance(*g, *where, s.map_width) >= hex_distance(*here, *where, s.map_width)
            ]  # fmt: skip
            if mates:
                line = hex_line(here, mates[0][2], s.map_width)
                if len(line) > 1 and line[0] not in taken_tiles and self._passable(line[0]):
                    retreats.insert(0, line[0])
            back = [
                (g, weigh(g, gap=0.0, advancing=False))
                for g in dict.fromkeys(retreats)
                if g != here
            ]
            if back:
                options["fall_back"] = max(sc.net for _, sc in back)
            fights = []
            for spot, (odds, who, _) in t.rejected.items():
                foe = self._defender_at(t, spot)
                if who.unit_id == unit.unit_id and foe and spot not in t.attacked | t.dead:
                    fights.append((self._target_score(t, unit, foe, odds).total, spot, odds, foe))
            if fights:
                options["attack"] = max(f[0] for f in fights)
            order = ["attack", "hold", "fall_back", "advance"]  # Ties go to the careful one.
            choice = max((o for o in order if o in options), key=lambda o: options[o])
            numbers = {"chosen": choice, "options": {k: round(v, 2) for k, v in options.items()}}
            step = None
            message = "odds are clearly good, advancing on the camp"
            if choice == "attack":
                _, spot, odds, foe = max(fights, key=lambda f: f[0])
                target = self._live(t, Target(foe.unit_type, spot[0], spot[1], foe.hp))
                if await self._strike(
                    game, t, unit, target, odds, "camp unit fights what stands beside it"
                ):
                    continue
                choice = "hold"
            if choice == "hold":
                t.acted.add(unit.unit_id)  # Holds the ground it stands on.
                waiting = hold.exposure <= 0 and gap > 0
                why = (
                    f"waiting for the force ({len(near)} of {len(force)} near)"
                    if waiting
                    else "step unsafe, holding"
                )
                self._log(t, unit, "camp", why, where, odds={**numbers, **asdict(hold)})
                continue
            if choice == "fall_back":
                best = max(back, key=lambda gs: gs[1].net)
                goals = [best[0]]
                step = best[1]
                message = "falling back from the hostiles beside it"
            else:
                goals = [g for g, _ in advance]
                step = advance[0][1]
            numbers.update(asdict(step))
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
                    self._log(t, unit, "camp", message, where, odds=numbers,
                              result=result)  # fmt: skip
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

    def _remember(
        self, t: _Turn, camp: tuple[int, int], defenders: list[Threat]
    ) -> tuple[list[Threat], float, float]:
        """The defenders to size the force against, and the strongest melee and ranged strength.

        The strongest group seen at the camp in the last `camp_memory_turns` (issue 279).
        """
        memory, group = remembered_defenders(
            self._camp_memory.get(camp, []), t.turn, defenders, self.settings.camp_memory_turns
        )
        if memory:
            self._camp_memory[camp] = memory
        else:
            self._camp_memory.pop(camp, None)
        strong = max((d.combat_strength for d in group), default=0.0)
        ranged = max((d.ranged_strength for d in group), default=0.0)
        return group, strong, ranged

    def _suspend_failing(self, t: _Turn, camps: Iterable[tuple[int, int]]) -> None:
        """End a campaign that keeps losing units and never hurts the camp (issue 275).

        `camp_loss_limit` units lost within `camp_loss_window` turns, with none of our attacks
        hitting the camp's defenders in that time, sets the camp aside for `camp_suspend_turns`.
        The force goes home and production stops building for it, so defence comes first.
        """
        s = self.settings
        for camp in camps:
            lost = self._losses.get(camp, [])
            if len(lost) < s.camp_loss_limit:
                continue
            if t.turn - self._camp_hit.get(camp, -999) <= s.camp_loss_window:
                continue
            self._camp_aside[camp] = t.turn + s.camp_suspend_turns
            self._committed.pop(camp, None)
            self._losses.pop(camp, None)
            for uid in [u for u, m in self._mission.items() if m[0] == camp]:
                del self._mission[uid]
            log.info(
                "military_camp_suspended %s",
                json.dumps({"turn": t.turn, "camp": list(camp), "lost": len(lost)}),
            )

    def _camp_defenders(self, t: _Turn, camp: tuple[int, int]) -> list[Threat]:
        """The barbarians in sight within `camp_defender_radius` of the camp."""
        s = self.settings
        return [
            b
            for b in t.threats
            if b.owner_id == BARBARIAN_ID
            and b.hostile
            and hex_distance(b.x, b.y, *camp, s.map_width) <= s.camp_defender_radius
        ]

    def _assume_unseen(self, t: _Turn, camp: tuple[int, int], group: list[Threat]) -> list[Threat]:
        """The group, or one assumed defender when none is known and the camp was not in sight
        since the last scan of the cities (issue 295: an unseen garrison is not an empty camp)."""
        if group or t.turn - self._camp_seen.get(camp, -999) <= self.settings.camp_home_scan_turns:
            return group
        cs = int(self.settings.weights.camp_unseen_strength)
        return [Threat(BARBARIAN_ID, "Barbarians", "UNIT_ASSUMED_DEFENDER", *camp, 100, 100, cs, 0,
                       self._home_distance(t, camp), True)]  # fmt: skip

    def _known_defenders(self, t: _Turn, camp: tuple[int, int]) -> list[Threat]:
        """What `_remember` would return for the camp, without touching the memory."""
        group = remembered_defenders(
            self._camp_memory.get(camp, []),
            t.turn,
            self._camp_defenders(t, camp),
            self.settings.camp_memory_turns,
        )[1]
        return self._assume_unseen(t, camp, group)

    def _plan_camps(
        self, t: _Turn, camps: list[tuple[int, int]], spare: list[Unit]
    ) -> CampPlan | None:
        """Choose the camp to clear and size its force (issue 268), then log and share the plan.

        A camp within `camp_goal_radius` of a city is a goal whether or not any unit is spare.
        Each camp is ranked by what it costs while it stands (nearness to a city, and the
        barbarians about it) over what it costs to clear (the defenders' strength with a margin,
        and the walk). The best one gets a force from the spare units, never a city's garrison;
        what it lacks goes to production as `CampPlan.units_wanted`.
        """
        s = self.settings
        w = s.weights
        goals = [c for c in camps if self._home_distance(t, c) <= s.camp_goal_radius]
        if not goals:
            if self.defence is not None:
                self.defence.camp_plan = None
            self._plan_target = None
            return None
        barbs = [b for b in t.threats if b.owner_id == BARBARIAN_ID and b.hostile]

        def rank(camp: tuple[int, int]) -> float:
            near = self._home_distance(t, camp)
            about = sum(
                1
                for b in barbs
                if hex_distance(b.x, b.y, *camp, s.map_width) <= s.camp_search_radius
            )
            ongoing = (s.camp_goal_radius + 1 - near) + 0.5 * about
            cost = camp_force_needed(self._known_defenders(t, camp), w) + near
            return ongoing / max(cost, 1.0)

        ranked = sorted(goals, key=lambda c: (-rank(c), self._home_distance(t, c), c))
        target, why = self._pick_target(t, goals, ranked, rank)
        defenders = self._camp_defenders(t, target)
        group = self._assume_unseen(t, target, self._remember(t, target, defenders)[0])
        strong = max((d.combat_strength for d in group), default=0.0)
        strong_ranged = max((d.ranged_strength for d in group), default=0.0)
        force = [u for u in spare if hex_distance(u.x, u.y, *target, s.map_width) <= s.camp_reach]
        needed, have, wanted, kind = plan_camp_force(force, group, w, s)
        walk = max(
            (math.ceil(hex_distance(u.x, u.y, *target, s.map_width) / 2) for u in force), default=0
        )
        plan = CampPlan(
            target=target,
            distance=self._home_distance(t, target),
            defenders=len(group),
            needed=round(needed, 1),
            available=round(have, 1),
            units_available=len(force),
            units_wanted=wanted,
            wants=kind,
            eta=walk + wanted * s.camp_build_turns,
            rank=round(rank(target), 3),
            defender=round(strong, 1),
            defender_ranged=round(strong_ranged, 1),
            committed=target in self._committed,
        )
        if self.defence is not None:
            self.defence.camp_plan = plan
        log.info(
            "military_camp_plan %s",
            json.dumps(
                {
                    "turn": t.turn,
                    "camps": len(goals),
                    "chosen_by": why,
                    "ranks": {f"{c[0]},{c[1]}": round(rank(c), 3) for c in ranked},
                    **asdict(plan),
                },
                sort_keys=True,
            ),
        )
        return plan

    def _pick_target(
        self,
        t: _Turn,
        goals: list[tuple[int, int]],
        ranked: list[tuple[int, int]],
        rank: Any,
    ) -> tuple[tuple[int, int], str]:
        """The camp to clear: the one the force is on until it is gone, set aside or out of
        range, or until another camp is a threat to a city while this one is not (issue 295).
        Only the first call of a turn can change it; the rank picks while no force is on a camp."""
        s = self.settings
        best = ranked[0]
        old = self._plan_target
        if old is None:
            self._plan_target = best
            return best, "rank"
        urgent = [
            c for c in ranked if c != old and self._home_distance(t, c) <= s.camp_urgent_radius
        ]
        threatened = old in goals and self._home_distance(t, old) <= s.camp_urgent_radius
        if old == best and (threatened or not urgent):
            return best, "rank"
        ranks = {f"{c[0]},{c[1]}": round(rank(c), 3) for c in ranked}
        # A camp at a city is kept while it is being reinforced, whether or not the force is on it.
        on_it = old in goals and (
            threatened
            or old in self._committed
            or any(
                m[0] == old and t.turn - m[1] <= s.camp_mission_turns
                for m in self._mission.values()
            )
        )
        if old not in goals:
            known = old in self._known_camps()
            reason = "previous camp set aside or out of range" if known else "previous camp is gone"
        elif not on_it:
            if old == best:
                return best, "rank"
            reason = "no force on the previous camp, rank decides"
        elif not threatened and urgent:
            best, reason = urgent[0], "another camp now threatens a city"
            self._committed.pop(old, None)  # The force is called off the far camp.
            self._camp_turns.pop((old, True), None)  # Its campaign clock starts over.
            self._camp_turns.pop((old, False), None)
            for uid in [u for u, m in self._mission.items() if m[0] == old]:
                del self._mission[uid]
        else:
            return old, "kept: force is on it"
        if best == old:
            return best, "rank"
        log.info(
            "military_camp_switch %s",
            json.dumps(
                {
                    "turn": t.turn,
                    "from": list(old),
                    "to": list(best),
                    "reason": reason,
                    "ranks": ranks,
                },
                sort_keys=True,
            ),
        )
        self._plan_target = best
        return best, reason

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
            self._note_gone(sent[0], t)
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
                    if tile.visible:
                        self._camp_seen[tile.x, tile.y] = t.turn
                    if tile.visible or (
                        self._camp_baseline
                        and (self.threat_map is None or (tile.x, tile.y) in mapped)
                    ):
                        self._camps.setdefault((tile.x, tile.y), t.turn)
                elif tile.visible:
                    self._camps.pop((tile.x, tile.y), None)
        self._camp_baseline = False

    def _note_gone(self, camp: tuple[int, int], t: _Turn | None = None) -> None:
        """A camp is no longer known. Ours if a unit of ours stood on its tile (or beside it after
        hitting its defenders), else someone else cleared it (issue 279). Logged once; its force
        is released."""
        w = self.settings.map_width
        hit = (
            t is not None
            and t.turn - self._camp_hit.get(camp, -999) <= self.settings.camp_loss_window
        )
        # Ours only if a unit of ours stands on the tile (it took it), or stands beside it after
        # hitting its defenders lately. Being near says nothing of who cleared it.
        ours = t is not None and any(
            (d := hex_distance(*self._pos(t, u), *camp, w)) == 0 or (d == 1 and hit) for u in t.mine
        )
        for kept in (
            self._camp_memory,
            self._committed,
            self._losses,
            self._camp_hit,
            self._camp_seen,
        ):
            kept.pop(camp, None)  # A camp that is gone leaves nothing to remember.
        for uid in [u for u, m in self._mission.items() if m[0] == camp]:
            del self._mission[uid]  # Released: the units are free for the next order.
        if camp in self._gone_noted:
            return
        self._gone_noted.add(camp)
        if ours:
            self._camps_ours += 1
        else:
            self._camps_gone += 1
        log.info(
            "military_camp_gone %s",
            json.dumps(
                {
                    "turn": t.turn if t is not None else None,
                    "camp": list(camp),
                    "by": "us" if ours else "other",
                },
                sort_keys=True,
            ),
        )

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
        first = next(
            (p for p in line if hex_distance(*p, *camp, width) <= muster and self._passable(p)),
            camp,
        )
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
                          and self._passable(p) and hex_distance(*p, *camp, width) <= muster)
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
        free = [p for p in neighbours(*camp, width) if p not in taken and self._passable(p)]
        free.sort(key=lambda p: (hex_distance(unit.x, unit.y, *p, width), p))
        return free[:tries]

    def _stage_tile(self, unit: Unit, camp: tuple[int, int]) -> tuple[int, int] | None:
        """The tile next to the camp on the way there, or None if already adjacent."""
        width = self.settings.map_width
        if hex_distance(unit.x, unit.y, *camp, width) <= 1:
            return None  # In reach, so the attack step takes it from here.
        line = hex_line((unit.x, unit.y), camp, width)
        # The last passable tile before the camp, so a lake or a ridge on the line is skipped.
        return next((p for p in reversed(line[:-1]) if self._passable(p)), None)

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

    # ----- suspected camp (issue 293) -----

    def _probe_note(self, t: _Turn, why: str, sus: Any = None, **extra: Any) -> None:
        """Log why no unit went to look, when the reason (or the guess) changes."""
        key = (why, None if sus is None else (sus.x, sus.y))
        if key == self._probe_why:
            return
        self._probe_why = key
        log.info(
            "military_probe_skip %s",
            json.dumps(
                {"turn": t.turn, "reason": why, "suspect": None if sus is None else sus.as_row(),
                 **extra},
                sort_keys=True,
            ),
        )  # fmt: skip

    async def _probe_suspect(self, game: MilitaryGame, t: _Turn) -> None:
        """Send one spare unit to look at a camp the threat map infers but nobody has seen.

        Barbarian units keep first appearing from the same fogged area, so a camp is probably
        there (`ThreatMap.suspected_camp`). A scout does this if we have one (settling sends it);
        else the cheapest, fastest spare unit walks toward the guess, stopping short of it on a
        tile under the threat limit (as the recapture march does). It looks and does not attack:
        once the camp is in sight the camp plan takes over.
        """
        s = self.settings
        if not s.probe or self.threat_map is None or not hasattr(self.threat_map, "suspected_camp"):
            return
        sus = self.threat_map.suspected_camp()
        if sus is None:
            self._probe_unit = None
            self._probe_note(t, "no suspicion: no area keeps sending barbarian units")
            return
        goal = (sus.x, sus.y)
        if any(u.unit_type == "UNIT_SCOUT" for u in t.units):
            self._probe_note(t, "we have a scout, and settling sends it", sus)
            return
        pool = [
            u
            for u in t.mine
            if self._available(t, u, "probe")
            and u.hp >= s.weights.heal_below * u.max_hp
            and self._spare(t, u)
            and u.unit_id not in self._recon
            and u.unit_id not in self._explorers
            and not self._on_camp_mission(t, u)
        ]
        if not pool:
            self._probe_note(t, "no spare unit", sus)
            return
        pool.sort(
            key=lambda u: (
                u.unit_id != self._probe_unit,  # The one already walking out goes on.
                -u.max_moves,
                max(u.combat_strength, u.ranged_strength),
                u.unit_id,
            )
        )
        stops = []
        for unit in pool[:3]:
            here = hex_distance(unit.x, unit.y, *goal, s.map_width)
            if here <= s.probe_stand:
                continue  # Already looking.
            line = hex_line((unit.x, unit.y), goal, s.map_width)
            line = [p for p in line if hex_distance(*p, *goal, s.map_width) >= s.probe_stand]
            line = line[: max(1, int(unit.moves))]
            stop = threat = None
            for step in reversed(line):
                if not self._passable(step):
                    continue
                threat = self.threat_map.threat_at(step[0], step[1], t.turn)
                if threat <= s.probe_threat:
                    stop = step
                    break
            if stop is None:
                stops.append((unit, None, threat))
                continue
            stops.append((unit, stop, threat))
            break
        chosen = next(((u, p) for u, p, _ in stops if p is not None), None)
        if chosen is None:
            self._probe_note(
                t, "no safe stop on the way (threatened, impassable, or already close)", sus,
                tried=[u.unit_id for u, _, _ in stops],
            )  # fmt: skip
            return
        unit, stop = chosen
        result = await self._order(t, unit, "probe", game.move_unit(unit.unit_index, *stop))
        if result is None or not move_succeeded(result):
            self._refuse(t, unit, "probe")
            self._probe_note(t, "the move was refused", sus, result=result or "")
            return
        t.acted.add(unit.unit_id)
        self._leave(t, unit)
        self._fortified.pop(unit.unit_id, None)
        self._probe_unit = unit.unit_id
        self._probe_why = None
        self._log(
            t, unit, "probe",
            f"barbarians keep first appearing near {goal[0]},{goal[1]} "
            f"({sus.arrivals} arrivals): going to look, not to attack", stop, result=result,
        )  # fmt: skip

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
