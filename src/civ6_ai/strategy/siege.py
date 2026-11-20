"""Estimate the force needed to take a city (issue 303, slice 1). Pure: no game, no model calls.

The bot never declares war yet. This module answers "how many of which unit would it take, and how
sure are we", so a later muster step (see docs/conquest.md) can build only what is needed. The
owner's lesson from the first human game: an unwalled city-state fell to 2 Legions and a Battering
Ram, and the ram probably was not needed. Walls change the answer a lot, so they are modelled.

Every constant below is APPROXIMATE, written from memory of Civ VI, and must be verified against
the combat estimate the game gives (the `get_combat_estimate` read of the civ MCP) on a live
attack: compare `expected_damage` here with the estimate for the same two units, and adjust
`DAMAGE_BASE`, `DAMAGE_SLOPE` and the wall factors. Until then treat the output as a planning
figure, not a promise. Everything is seeded, so one input always gives one answer.
"""

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field

# Civ VI damage per attack is about 30 * exp(0.04 * (attacker - defender strength)), times a random
# factor of roughly 0.8 to 1.2. APPROXIMATE: verify against the in-game combat estimate.
DAMAGE_BASE = 30.0
DAMAGE_SLOPE = 0.04
DAMAGE_RANDOM_LOW = 0.8
DAMAGE_RANDOM_HIGH = 1.2

# Share of an attack's damage that lands on the walls while they stand. Melee barely scratches
# them, ranged does about half, siege units (ram, catapult) do full damage. APPROXIMATE.
WALL_FACTOR = {"melee": 0.15, "ranged": 0.5, "siege": 1.0}
# A battering ram is support: it has no attack of its own, but with one beside them melee units
# hit the walls at full strength. More than one ram adds nothing. APPROXIMATE.
WALL_FACTOR_MELEE_WITH_RAM = 1.0

DEFAULT_CITY_HP = 200
DEFAULT_WALL_HP = 100  # Ancient walls; later walls are stronger and need a larger value.
CITY_REGEN_PER_TURN = 10  # City HP the defenders repair each turn. APPROXIMATE.
DEFAULT_CONFIDENCE = 0.8
DEFAULT_MAX_TURNS = 10  # Turns before reinforcements or peace are assumed to spoil the attack.
DEFAULT_TRIALS = 120  # Simulations per candidate force. Enough for 80% to be stable and fast.
DEFAULT_SEED = 303
MAX_MELEE = 8  # Search bounds; a force larger than this is reported infeasible ("leave it").
MAX_SIEGE = 4  # Catapult-like attackers; a ram is capped at 1 because more add nothing.
MAX_RANGED = 4
_ORDER = (
    "siege",
    "ranged",
    "melee",
    "ram",
)  # Siege opens the walls, ranged softens, melee captures.


@dataclass(frozen=True)
class AttackUnit:
    """One attacker. `kind` is melee, ranged, siege or ram.

    Only melee can walk in and capture. A `ram` is support: no attack of its own, it lets melee
    units damage walls at full strength.
    """

    name: str
    strength: float
    kind: str = "melee"
    hp: int = 100
    cost: int = 1  # A rough production cost, only used to break ties between equal-size forces.


# Plausible ancient-era stats (combat strength; ranged and siege use their ranged strength).
UNIT_STATS: dict[str, AttackUnit] = {
    "warrior": AttackUnit("warrior", 20, "melee", cost=40),
    "swordsman": AttackUnit("swordsman", 36, "melee", cost=90),
    "legion": AttackUnit("legion", 40, "melee", cost=110),
    "archer": AttackUnit("archer", 25, "ranged", cost=60),
    "catapult": AttackUnit("catapult", 35, "siege", cost=120),
    "battering_ram": AttackUnit("battering_ram", 35, "ram", cost=100),
}


@dataclass(frozen=True)
class GarrisonUnit:
    name: str
    strength: float
    hp: int = 100
    ranged: bool = False


@dataclass(frozen=True)
class TargetCity:
    """What the player may see of the city: strength, walls, garrison, terrain."""

    strength: float  # The city's own combat strength.
    hp: int = DEFAULT_CITY_HP
    has_walls: bool = False
    wall_hp: int = DEFAULT_WALL_HP
    garrison: tuple[GarrisonUnit, ...] = ()
    terrain_bonus: float = 0.0  # Hills, river or similar, added to the defender's strength.


@dataclass(frozen=True)
class Force:
    units: tuple[AttackUnit, ...]

    @staticmethod
    def of(counts: Mapping[str, int]) -> "Force":
        units: list[AttackUnit] = []
        for name in sorted(counts):
            units.extend([UNIT_STATS[name]] * counts[name])
        return Force(tuple(units))


@dataclass(frozen=True)
class SiegeEstimate:
    required: dict[str, int]  # Unit counts by type for the chosen confidence; empty if infeasible.
    win_probability: float  # Of the force the caller gave, or of the required force if none.
    turns_to_capture: int | None  # Median over winning trials.
    expected_losses: float
    feasible: bool
    reasons: list[str] = field(default_factory=list)


def defender_strength(city: TargetCity) -> float:
    """The best of the city and its garrison, plus terrain: the strength attackers face."""
    best = max([city.strength, *(g.strength for g in city.garrison)])
    return best + city.terrain_bonus


def expected_damage(attacker_strength: float, defender_strength_: float) -> float:
    """Mean damage of one attack from the strength difference (the verify-me formula)."""
    mean_factor = (DAMAGE_RANDOM_LOW + DAMAGE_RANDOM_HIGH) / 2
    diff = attacker_strength - defender_strength_
    return DAMAGE_BASE * math.exp(DAMAGE_SLOPE * diff) * mean_factor


def _damage(rng: random.Random, attacker: float, defender: float) -> float:
    factor = rng.uniform(DAMAGE_RANDOM_LOW, DAMAGE_RANDOM_HIGH)
    return DAMAGE_BASE * math.exp(DAMAGE_SLOPE * (attacker - defender)) * factor


def _simulate(
    city: TargetCity, units: tuple[AttackUnit, ...], max_turns: int, rng: random.Random
) -> tuple[int | None, int]:
    """One siege. Returns (capture turn or None, attackers lost)."""
    defence = defender_strength(city)
    hp = {i: float(u.hp) for i, u in enumerate(units)}
    city_hp = float(city.hp)
    wall_hp = float(city.wall_hp) if city.has_walls else 0.0
    has_ram = any(u.kind == "ram" for u in units)
    # The city shoots only with walls (as MilitaryPolicy._city_can_strike); ranged garrison always.
    shooters = (1 if city.has_walls else 0) + sum(1 for g in city.garrison if g.ranged)
    order = sorted(
        (i for i, u in enumerate(units) if u.kind != "ram"),
        key=lambda k: (_ORDER.index(units[k].kind), k),
    )
    for turn in range(1, max_turns + 1):
        for i in order:
            if hp[i] <= 0:
                continue
            u = units[i]
            dmg = _damage(rng, u.strength, defence)
            if wall_hp > 0:
                factor = WALL_FACTOR_MELEE_WITH_RAM if u.kind == "melee" and has_ram else None
                wall_hp -= dmg * (WALL_FACTOR[u.kind] if factor is None else factor)
            else:
                city_hp -= dmg
            if u.kind == "melee":  # Melee takes the blow back; ranged and siege are not hit.
                hp[i] -= _damage(rng, defence, u.strength)
                # Capture needs a melee attack that empties the city and leaves its attacker alive.
                if city_hp <= 0 and wall_hp <= 0 and hp[i] > 0:
                    return turn, sum(1 for h in hp.values() if h <= 0)
        for _ in range(shooters):  # Defenders shoot the weakest attacker.
            alive = [k for k in hp if hp[k] > 0]
            if not alive:
                break
            target = min(alive, key=lambda k: (hp[k], k))
            hp[target] -= _damage(rng, defence, units[target].strength)
        if not any(h > 0 and units[k].kind == "melee" for k, h in hp.items()):
            return None, sum(1 for h in hp.values() if h <= 0)
        city_hp = min(float(city.hp), city_hp + CITY_REGEN_PER_TURN)
    return None, sum(1 for h in hp.values() if h <= 0)


def win_probability(
    city: TargetCity,
    force: Force,
    max_turns: int = DEFAULT_MAX_TURNS,
    trials: int = DEFAULT_TRIALS,
    seed: int = DEFAULT_SEED,
) -> tuple[float, int | None, float]:
    """(chance of capture within max_turns, median capture turn, mean attackers lost)."""
    rng = random.Random(seed)
    wins: list[int] = []
    losses = 0
    for _ in range(trials):
        turn, lost = _simulate(city, force.units, max_turns, rng)
        losses += lost
        if turn is not None:
            wins.append(turn)
    median = sorted(wins)[len(wins) // 2] if wins else None
    return len(wins) / trials, median, losses / trials


def estimate_force(
    city: TargetCity,
    *,
    current: Mapping[str, int] | None = None,
    confidence: float = DEFAULT_CONFIDENCE,
    melee_type: str = "legion",
    siege_type: str = "battering_ram",
    ranged_type: str = "archer",
    max_turns: int = DEFAULT_MAX_TURNS,
    trials: int = DEFAULT_TRIALS,
    seed: int = DEFAULT_SEED,
) -> SiegeEstimate:
    """The smallest force that takes the city at `confidence`, and the odds for `current`.

    Searches melee, siege and ranged counts in order of total size (then rough cost), so a ram
    appears in the answer only if it earns its place.
    """
    defence = defender_strength(city)
    reasons = [
        f"defender strength {defence:g} (city {city.strength:g}, "
        f"garrison {len(city.garrison)}, terrain +{city.terrain_bonus:g})"
    ]
    if city.has_walls:
        reasons.append(
            f"walls ({city.wall_hp} HP): melee does {WALL_FACTOR['melee']:.0%} to them, "
            f"siege does full damage; bring a {siege_type}"
        )
    else:
        reasons.append("no walls: a ram is not needed")
    current_p = None
    if current is not None:
        current_p = win_probability(city, Force.of(current), max_turns, trials, seed)[0]
        reasons.append(f"current force wins {current_p:.0%} within {max_turns} turns")

    max_siege = 1 if UNIT_STATS[siege_type].kind == "ram" else MAX_SIEGE
    candidates: list[tuple[int, int, dict[str, int]]] = []
    for m in range(1, MAX_MELEE + 1):
        # Siege units only open walls; against an open city a melee unit does the job better.
        for s in range(max_siege + 1 if city.has_walls else 1):
            for r in range(MAX_RANGED + 1):
                counts = {melee_type: m, siege_type: s, ranged_type: r}
                cost = sum(UNIT_STATS[n].cost * c for n, c in counts.items())
                candidates.append((m + s + r, cost, counts))
    candidates.sort(key=lambda c: (c[0], c[1], sorted(c[2].items())))
    for _, _, counts in candidates:
        prob, turns, lost = win_probability(city, Force.of(counts), max_turns, trials, seed)
        if prob >= confidence:
            required = {n: c for n, c in counts.items() if c > 0}
            reasons.append(
                f"{confidence:.0%} confidence reached at {prob:.0%}, "
                f"capture in about {turns} turns, {lost:.1f} units lost"
            )
            return SiegeEstimate(
                required, current_p if current_p is not None else prob, turns, lost, True, reasons
            )
    reasons.append(f"no force within the search bounds reaches {confidence:.0%}: leave it")
    return SiegeEstimate({}, current_p or 0.0, None, 0.0, False, reasons)


def units_needed(estimate: SiegeEstimate, have: Mapping[str, int]) -> dict[str, int]:
    """What is still missing from `have` to reach the estimate's required force."""
    return {n: c - have.get(n, 0) for n, c in estimate.required.items() if c - have.get(n, 0) > 0}
