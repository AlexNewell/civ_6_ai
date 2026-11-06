"""Defence: attack what is in reach when the odds are good, heal, garrison and spend promotions.

`MilitaryPolicy` is a turn handler (see `orchestrator.Handler`). It makes no model calls and the
same state and settings always give the same orders. Run it after `SettlingPolicy` and before the
blocker resolver, so that settling has moved its escorts first and the resolver's skip comes last.

This is the defensive slice of issue 13. It does not declare wars, march on a civ or change what
is built. What it does, in the order a unit is considered (a unit gets one order a turn):

1. Attack. A unit with a target in reach attacks when the odds favour it (`assess_attack`).
   A ranged unit shoots; it never closes to melee range on purpose.
2. Heal. A unit below `heal_below` of its health fortifies until healed, in a city, or walks to
   the nearest city first. It is never left outside on its own to recover.
3. Garrison. A city with hostile units near it, and fewer defenders than `garrison_required`,
   calls the nearest spare unit.
4. Clear a barbarian camp, only when the force on hand is `camp_force_ratio` times theirs.
5. Idle units in a threatened city fortify, and units outside run for a city if hostile units
   are close.

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
from civ6_ai.policies.settling import SCOUT, hex_line, units_reserved_for_settling
from civ6_ai.state import City, GameReads, Threat, Tile, Unit, read_area
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


def is_land_military(unit: Unit) -> bool:
    """A combat unit this policy may order: not a scout, ship or aircraft."""
    return (
        unit.is_combat
        and unit.unit_type != SCOUT
        and not any(word in unit.unit_type for word in _NOT_LAND)
    )


@dataclass(frozen=True)
class MilitaryWeights:
    """How cautious the unit is. Fractions are of a unit's maximum health."""

    heal_below: float = 0.7  # A unit under this goes to heal instead of fighting.
    min_hp_after_attack: float = 0.4  # Health left after the bad-side roll, or no attack.
    garrison_min_hp_after: float = 0.6  # The same, for the unit holding a city on its own.
    min_damage_ratio: float = 1.5  # Damage dealt over taken, unless the attack kills.
    damage_variance: float = 0.2  # The game rolls damage within this fraction either way.
    assumed_defender_bonus: float = 6.0  # Strength for terrain and fortification we can't see.
    kill_bonus: float = 50.0  # Rank a kill above any amount of damage.
    camp_force_ratio: float = 2.0  # Our strength over theirs, to go for a camp.
    camp_min_hp: float = 0.8  # Only healthy units are sent to a camp.


@dataclass(frozen=True)
class MilitarySettings:
    """Bounds on reach, retrying and work per turn, so nothing stalls or runs away."""

    weights: MilitaryWeights = field(default_factory=MilitaryWeights)
    max_garrison: int = 2  # Defenders wanted in a city under heavy threat.
    reinforce_threat_count: int = 3  # Hostile units near a city that call for a second defender.
    garrison_reach: int = 4  # How far a spare unit is called from.
    retreat_reach: int = 6  # How far a hurt or exposed unit will walk to a city.
    flee_radius: int = 3  # A unit outside a city this close to a hostile unit goes home.
    camp_search_radius: int = 8  # Barbarians this close to a city start a look for a camp.
    camp_scan_radius: int = 3  # Tiles read around the nearest barbarian to find the camp.
    camp_reach: int = 4  # How far a unit is called from to a camp.
    camp_defender_radius: int = 2  # Barbarians this close to a camp defend it.
    camp_min_units: int = 2
    max_camp_turns: int = 8  # Turns spent advancing on one camp before it is set aside.
    camp_retry_turns: int = 30
    camp_idle_turns: int = 10  # A camp not advanced on for this long starts its count again.
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
) -> AttackOdds | None:
    """Judge an attack by `unit` on `target`, or None if the target has no strength to judge.

    A ranged unit attacks with its ranged strength and takes damage back only from a defender
    that has a ranged attack of its own. A melee unit trades blows.
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
        sure_kill or taken == 0 or dealt * (1 - v) >= weights.min_damage_ratio * taken * (1 + v)
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
    action: str  # attack, heal, retreat, garrison, camp, fortify, flee, promote, skip
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
    hp: dict[int, float] = field(default_factory=dict)
    target_hp: dict[tuple[int, int], float] = field(default_factory=dict)
    at_city: dict[tuple[int, int], int] = field(default_factory=dict)
    camp_scanned: bool = False


@dataclass
class MilitaryPolicy:
    """Defends the empire. Run it after settling and before the blocker resolver."""

    settings: MilitarySettings = field(default_factory=MilitarySettings)
    name: str = "military"
    decisions: list[MilitaryDecision] = field(default_factory=list)
    # Per unit, kept across turns and pruned each turn against the units that still exist.
    _resting: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _fortified: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _refused: dict[tuple[int, str], int] = field(default_factory=dict, init=False, repr=False)
    _promotion_refused: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _promotion_asked: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    # Per camp tile: turns spent advancing on it and the last of them; and when it is set aside to.
    _camp_turns: dict[tuple[int, int], tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _camp_aside: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)

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
        reserved = units_reserved_for_settling(
            units, [(c.x, c.y) for c in cities], map_width=s.map_width
        )
        mine = [u for u in mine if u.unit_id not in reserved]
        t = _Turn(turn, tuple(units), cities, threats, mine, reserved)
        city_tiles = {(c.x, c.y) for c in cities}
        for u in mine:  # A reserved unit may leave with a settler, so it is not a defender.
            if (u.x, u.y) in city_tiles:
                t.at_city[u.x, u.y] = t.at_city.get((u.x, u.y), 0) + 1
        await self._attack(game, t)
        await self._heal(game, t)
        await self._garrison(game, t)
        await self._clear_camp(game, t)
        await self._idle(game, t)

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
        self._refused = {k: v for k, v in self._refused.items() if v >= turn and k[0] in by_id}
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
        return city is None or t.at_city.get((unit.x, unit.y), 0) > garrison_required(
            city, self.settings
        )

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
            odds, target, defender = options[0]
            result = await self._order(
                t, unit, "attack", game.attack_unit(unit.unit_index, target.x, target.y)
            )
            where = (target.x, target.y)
            if result is None or not attack_landed(result):
                self._refuse(t, unit, "attack")
                self._log(
                    t,
                    unit,
                    "attack",
                    "attack refused",
                    target=where,
                    odds=asdict(odds),
                    result=result or "",
                )
                continue
            t.acted.add(unit.unit_id)
            self._fortified.pop(unit.unit_id, None)  # Attacking ends a fortification.
            killed = "killed" in result.lower() or "eliminated" in result.lower()
            dealt = _DAMAGE_DEALT.search(result)
            hp_now = t.target_hp.get(where, target.hp)
            t.target_hp[where] = 0 if killed else hp_now - (int(dealt[1]) if dealt else odds.dealt)
            if killed or t.target_hp[where] <= 0:
                t.dead.add(where)
            self._log(
                t,
                unit,
                "attack",
                "odds favour the attack",
                target=where,
                odds=asdict(odds),
                result=result,
            )

    def _attack_options(self, t: _Turn, unit: Unit) -> list[tuple[AttackOdds, Target, Threat]]:
        s = self.settings
        city = self._city_at(t, unit)
        sole = city is not None and t.at_city.get((unit.x, unit.y), 0) <= garrison_required(city, s)
        floor = s.weights.garrison_min_hp_after if sole else None
        found = []
        for target in unit.targets:
            where = (target.x, target.y)
            if where in t.dead:
                continue
            defenders = [d for d in t.threats if (d.x, d.y) == where and d.hostile]
            if not defenders:
                continue  # The scan did not show it, so there is nothing to judge it by.
            defender = max(defenders, key=lambda d: (d.combat_strength, d.ranged_strength))
            live = Target(
                target.unit_type, target.x, target.y, int(t.target_hp.get(where, target.hp))
            )
            odds = assess_attack(unit, live, defender, s.weights, floor, s.map_width)
            hurt = unit.hp < s.weights.heal_below * unit.max_hp
            if odds is not None and odds.favourable and (odds.sure_kill or not hurt):
                found.append((odds, live, defender))
        found.sort(key=lambda f: (-f[0].rank, f[1].x, f[1].y))
        return found

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
        result = await self._order(t, unit, kind, game.move_unit(unit.unit_index, *where))
        if result is not None and move_succeeded(result):
            t.acted.add(unit.unit_id)
            self._leave(t, unit)
            self._arrive(t, city, result)
            self._log(t, unit, action, reason, where, result=result)
            return True
        self._refuse(t, unit, kind)
        self._log(t, unit, action, "move refused", where, result=result or "")
        return False

    # ----- garrison -----

    async def _garrison(self, game: MilitaryGame, t: _Turn) -> None:
        s = self.settings
        for city in sorted(t.cities, key=lambda c: (-len(c.threats), c.city_id)):
            if not city.is_threatened:
                continue
            while t.at_city.get((city.x, city.y), 0) < garrison_required(city, s):
                candidates = [
                    (hex_distance(u.x, u.y, city.x, city.y, s.map_width), u.unit_id, u)
                    for u in t.mine
                    if self._available(t, u, "move")
                    and (u.x, u.y) != (city.x, city.y)
                    and u.hp >= s.weights.heal_below * u.max_hp
                    and self._spare(t, u)
                ]
                candidates = [c for c in candidates if c[0] <= s.garrison_reach]
                if not candidates:
                    break
                unit = min(candidates, key=lambda c: c[:2])[2]
                before = t.at_city.get((city.x, city.y), 0)
                if not await self._walk_home(
                    game, t, unit, city, "garrison", "city has hostile units near it and a gap"
                ):
                    continue  # Refused, so it is set aside and the next unit is tried.
                if t.at_city.get((city.x, city.y), 0) == before:
                    # En route: it counts for this turn, so the same gap does not call another.
                    t.at_city[city.x, city.y] = before + 1

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
        if not barbarians or len(spare) < s.camp_min_units or t.camp_scanned:
            return
        t.camp_scanned = True
        nearest = min(barbarians, key=lambda b: (b.distance, b.x, b.y))
        try:
            tiles = await read_area(game, nearest.x, nearest.y, s.camp_scan_radius)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Reading the area for a barbarian camp failed")
            return
        camps = [c for c in tiles if self._is_open_camp(c, t.turn)]
        if not camps:
            return
        camp = min(camps, key=lambda c: (self._home_distance(t, c), c.x, c.y))
        where = (camp.x, camp.y)
        defenders = [
            b
            for b in barbarians
            if hex_distance(b.x, b.y, camp.x, camp.y, s.map_width) <= s.camp_defender_radius
        ]
        force = [
            u for u in spare if hex_distance(u.x, u.y, camp.x, camp.y, s.map_width) <= s.camp_reach
        ]
        if not camp_force_ok(force, defenders, s.weights, s.camp_min_units):
            return
        spent = self._camp_turns.get(where, (0, t.turn))[0] + 1
        self._camp_turns[where] = (spent, t.turn)
        if spent > s.max_camp_turns:
            self._camp_aside[where] = t.turn + s.camp_retry_turns
            self._camp_turns.pop(where)
            return
        occupied = any((b.x, b.y) == where for b in t.threats if b.hostile)
        for unit in sorted(force, key=lambda u: u.unit_id):
            if not self._available(t, unit, "move"):
                continue
            goal = where if not occupied else self._stage_tile(unit, where)
            if goal is None or goal == (unit.x, unit.y):
                continue
            result = await self._order(t, unit, "move", game.move_unit(unit.unit_index, *goal))
            if result is not None and move_succeeded(result):
                t.acted.add(unit.unit_id)
                self._leave(t, unit)
                self._log(t, unit, "camp", "odds are clearly good, advancing on the camp", where,
                          result=result)  # fmt: skip
            else:
                self._refuse(t, unit, "move")
                self._log(t, unit, "camp", "move refused", where, result=result or "")

    def _is_open_camp(self, tile: Tile, turn: int) -> bool:
        return (
            tile.visible
            and tile.improvement == CAMP_IMPROVEMENT
            and self._camp_aside.get((tile.x, tile.y), -1) < turn
        )

    def _home_distance(self, t: _Turn, tile: Tile) -> int:
        return min(
            (hex_distance(tile.x, tile.y, c.x, c.y, self.settings.map_width) for c in t.cities),
            default=0,
        )

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
                if city.is_threatened and self._fortified.get(unit.unit_id) != (unit.x, unit.y):
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
            near_hostile = any(
                th.hostile
                and hex_distance(unit.x, unit.y, th.x, th.y, s.map_width) <= s.flee_radius
                for th in t.threats
            )
            home = self._nearest_city(t, unit, s.retreat_reach) if near_hostile else None
            if home is not None:
                await self._walk_home(
                    game, t, unit, home, "flee", "alone outside with hostile units near"
                )

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
