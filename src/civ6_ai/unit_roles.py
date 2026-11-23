"""One `military_roles` line a turn: what each combat unit was for (issue 416, slice 1).

About half of all unit-turns had no `military_decision` line (119 of 219 on STD_T1_04, turns
30-60), so what those units did could not be read from the log. This logs every own combat unit
every turn, with one role, the policy that had it, what it did, and why. It reads state and the
turn's decisions; it orders nothing, makes no game call and changes no counter. A failure while
building the line is logged and swallowed, so it can never change play.

Why a unit has no `military_decision` (each proven in `policies/military.py`):

- `settling_reserved`: `units_reserved_for_settling` removes the settler's escort and scouts from
  `_Turn.mine` before any military stage runs, so none of them is ever logged there.
- `not_land_military`: `is_land_military` leaves out scouts, ships and aircraft.
- `resting_until_healed`: `_heal` adds a unit already resting to `acted` and goes on, no log.
- `heal_not_available`: a hurt unit with no moves, or whose heal order was refused, is skipped by
  `_heal` through `_available` with no log.
- `held_city_garrison`: `_garrison` and `_idle` skip a city that is not threatened, and
  `_spare_units` skips a unit that is needed for its garrison (`not self._spare`), all silently.
- `on_camp_mission`: `_spare_units` skips a unit marching on a camp; the camp stage logs only the
  orders it gives, and `adjacent: continue` logs none.
- `game_explorer`: `_spare_units` leaves a unit the game's explorer is walking alone.
- `covering_dug_in`: `_idle` logs the first fortify only; a covering unit already fortified is
  skipped.
- `exposed_outside`: `_spare_units` skips a lone unit with hostiles near; `_idle` then logs only
  the order it gives.
- `no_moves_left`: `_available` is false for a unit with no moves, so no stage orders it.

A cause the code cannot name is logged as role `other` with the code path, never as a garrison.
"""

import json
import logging
from typing import Any

from civ6_ai.state.reader import hex_distance

log = logging.getLogger(__name__)

ROLES = (
    "garrison", "cover", "camp", "escort", "recon", "recapture", "attack", "heal_retreat",
    "stack", "spare", "other",
)  # fmt: skip
WHY_CHARS = 70  # The longest reason kept, so the line stays compact.
# Decisions whose action fixes the role (fortify and skip are read by their reason).
_BY_ACTION = {
    "garrison": "garrison", "swap": "garrison", "cover": "cover", "heal": "heal_retreat",
    "retreat": "heal_retreat", "flee": "heal_retreat", "camp": "camp", "attack": "attack",
    "city_strike": "attack", "hunt": "attack", "recapture": "recapture", "recon": "recon",
    "explore": "recon", "stack": "stack",
}  # fmt: skip


def _decision_role(action: str, reason: str) -> str | None:
    """The role an order or a reason names, or None when it names none (a promotion, a rejected
    attack, a refused order: the unit is then read by where it stands)."""
    if "refused" in reason or "failed" in reason:
        return None
    if action in _BY_ACTION:
        return _BY_ACTION[action]
    if action == "fortify":
        if "hostile units near" in reason:
            return "garrison"
        if "covering" in reason:
            return "cover"
        return "spare"
    if action == "skip":
        return "spare" if reason.startswith("spare") else None
    return None


def _short(text: str) -> str:
    return text if len(text) <= WHY_CHARS else text[: WHY_CHARS - 1] + "~"


def _tile(policy: Any, t: Any, unit: Any) -> tuple[int, int]:
    return policy._pos(t, unit)


def _city_view(policy: Any, t: Any, city: Any) -> dict[str, Any]:
    """A city as the threat map reads it: short or not, net damage a turn, defenders present."""
    found = policy.defence.cities.get(city.city_id) if policy.defence is not None else None
    if found is None:
        return {
            "id": city.city_id, "short": False, "net": None,
            "def": t.at_city.get((city.x, city.y), 0) + policy._beside(t, city),
        }  # fmt: skip
    reading = found.reading
    net = None if reading is None else round(reading.net, 1)
    return {"id": city.city_id, "short": bool(found.short), "net": net, "def": found.defenders}


def _served(policy: Any, t: Any, unit: Any, decision: Any) -> Any:
    """The city a unit holds: the one it stands in or beside, or the one it was sent to."""
    if decision is not None and decision.target is not None:
        sent = next((c for c in t.cities if (c.x, c.y) == tuple(decision.target)), None)
        if sent is not None:
            return sent
    city = policy._city_at(t, unit)
    if city is not None:
        return city
    w = policy.settings.map_width
    beside = [c for c in t.cities if hex_distance(c.x, c.y, *_tile(policy, t, unit), w) == 1]
    return min(beside, key=lambda c: c.city_id) if beside else None


def _unavailable(policy: Any, t: Any, unit: Any) -> str | None:
    """Why `_available` would be false for the unit, read without ordering anything."""
    s = policy.settings
    if not unit.can_move:
        return "no moves left"
    if unit.unit_id in t.acted:
        return "already acted"
    if t.actions >= s.max_actions:
        return "turn's order cap used"
    if t.calls.get(unit.unit_id, 0) >= s.attempts_per_unit:
        return "per-unit order cap used"
    return None


def _by_state(policy: Any, t: Any, unit: Any) -> tuple[str, str]:
    """Role and reason for a unit with no decision of its own, from where it stands."""
    s = policy.settings
    uid = unit.unit_id
    hurt = unit.hp < s.weights.heal_below * unit.max_hp
    if hurt and policy._resting.get(uid) == (unit.x, unit.y):
        return "heal_retreat", "resting_until_healed (_heal skips)"
    if hurt:
        return "heal_retreat", f"heal_not_available: {_unavailable(policy, t, unit) or 'refused'}"
    city = policy._city_at(t, unit)
    sent = policy._mission.get(uid)
    if sent is not None and t.turn - sent[1] <= s.camp_mission_turns:
        return "camp", "on_camp_mission (no order this turn)"
    if uid in policy._explorers and city is None:
        return "recon", "game_explorer (_spare_units leaves it)"
    if city is not None:
        if not policy._spare(t, unit):
            return "garrison", "held_city_garrison (no stage orders it)"
        return "spare", _unavailable(policy, t, unit) or "spare in city, nothing to do"
    cover = policy._covering(t, unit)
    if cover is not None:
        return "cover", "covering_dug_in (_idle logs the first fortify only)"
    if policy._exposed(t, unit):
        return "other", "exposed_outside (military._idle/_spare_units, no order given)"
    return "spare", _unavailable(policy, t, unit) or "spare outside, nothing to do"


def record_camp_force(policy: Any, t: Any, first: list[Any], counted: list[Any]) -> None:
    """Called by `_clear_camp` with the force it made (`counted`) and the list before the
    one-garrison cut (`first`): keep the ids, and for every other unit the first test it failed,
    in the order the stage applies them. The roles line logs this record, so it cannot disagree
    with the stage. Never raises."""
    try:
        s = policy.settings
        ids = {u.unit_id for u in counted}
        before = {u.unit_id for u in first}
        left = {}
        for unit in t.mine:
            if unit.unit_id in ids:
                continue
            if unit.unit_id in before:
                left[unit.unit_id] = "kept as its city's one garrison"
            elif not policy._available(t, unit, "move"):
                refused = policy._refused.get((unit.unit_id, "move"), -1) >= t.turn
                left[unit.unit_id] = (
                    "move order refused lately" if refused else _unavailable(policy, t, unit) or ""
                )
            elif unit.unit_id in t.roles:
                left[unit.unit_id] = f"city role {t.roles[unit.unit_id]} this turn"
            elif unit.hp < s.weights.camp_min_hp * unit.max_hp:
                left[unit.unit_id] = "too hurt"
            else:
                left[unit.unit_id] = _why_not_available(policy, t, unit)
        t.camp_counted, t.camp_left = ids, left
    except Exception:
        log.warning("camp force record failed", exc_info=True)


def _why_not_available(policy: Any, t: Any, unit: Any) -> str:
    city = policy._city_at(t, unit)
    if city is not None:
        return "city short" if policy._city_short(city) else "sole defender of its city"
    return "holds a short or threatened city"


def _counted(policy: Any, t: Any, unit: Any, plan: Any, decision: Any) -> tuple[bool, str]:
    """Whether the unit is in the camp force, and if not why: the camp stage's own record
    (`record_camp_force`). Only when the stage never made its list (it did not run) is it
    recomputed at the end of the turn, and the reason then starts with "recomputed"."""
    if t.camp_counted is not None:  # The camp stage's own record.
        if unit.unit_id in t.camp_counted:
            return True, ""
        return False, t.camp_left.get(unit.unit_id) or "not in the camp stage's list"
    ok, why = _recomputed(policy, t, unit, plan, decision)
    return ok, f"recomputed: {why}" if why else ""


def _recomputed(policy: Any, t: Any, unit: Any, plan: Any, decision: Any) -> tuple[bool, str]:
    if decision is not None and decision.action == "camp":
        return True, ""
    if plan is None:
        return False, "no camp plan this turn"
    s = policy.settings
    if unit.unit_id in t.roles:
        return False, f"city role {t.roles[unit.unit_id]} this turn"
    if not unit.can_move:
        return False, "no moves left"
    if unit.hp < s.weights.camp_min_hp * unit.max_hp:
        return False, "too hurt"
    if not policy._camp_available(t, unit):
        city = policy._city_at(t, unit)
        if city is not None:
            return False, "city short" if policy._city_short(city) else "sole defender of its city"
        return False, "holds a short or threatened city"
    if policy._walk_cost(_tile(policy, t, unit), plan.target) > s.camp_reach:
        return False, "beyond the camp's reach"
    return True, ""


def _need_walk(policy: Any, t: Any, unit: Any, plan: Any) -> tuple[int, str] | None:
    """Walking turns from a spare unit to the nearest unmet need (a short city, or the camp
    muster while the plan is short), or None when no terrain is known or there is no need."""
    mv = policy._mv()
    if mv is None:
        return None
    goals = []
    if policy.defence is not None:
        by_id = {c.city_id: c for c in t.cities}
        goals += [
            ("city", (by_id[i].x, by_id[i].y))
            for i, found in policy.defence.cities.items()
            if found.short and i in by_id
        ]
    if plan is not None and plan.available < plan.needed:
        goals.append(("camp", tuple(plan.target)))
    here = _tile(policy, t, unit)
    moves = max(1.0, unit.max_moves)
    found = [
        (turns, kind)
        for kind, goal in goals
        if (turns := mv.walk_turns(here, goal, moves)) is not None
    ]
    return min(found) if found else None


def _counts(policy: Any, t: Any, plan: Any) -> dict[int, tuple[bool, str]]:
    """`_counted` for every unit of the policy, then the same `_keep_one_garrison` cut the camp
    stage makes: of the units in one city that pass, all but one go to the force."""
    found = {}
    sent = set()  # Units the camp stage ordered: in the force, whatever the cut says.
    for unit in t.mine:
        decision = next(
            (d for d in reversed(policy.decisions) if d.turn == t.turn and d.unit_id == unit.unit_id
             and d.action == "camp"), None
        )  # fmt: skip
        if decision is not None:
            sent.add(unit.unit_id)
        found[unit.unit_id] = _counted(policy, t, unit, plan, decision)
    if t.camp_counted is not None:
        return found
    passing = [u for u in t.mine if found[u.unit_id][0] and u.unit_id not in sent]
    kept = {u.unit_id for u in policy._keep_one_garrison(t, passing)}
    for unit in passing:
        if unit.unit_id not in kept:
            found[unit.unit_id] = (False, "recomputed: kept as its city's one garrison")
    return found


def _entry(
    policy: Any, t: Any, unit: Any, plan: Any, counts: dict[int, tuple[bool, str]]
) -> dict[str, Any]:
    uid = unit.unit_id
    mine = {u.unit_id for u in t.mine}
    made = [d for d in policy.decisions if d.turn == t.turn and d.unit_id == uid]
    role: str | None = None
    decision = None
    why = ""
    for i, d in enumerate(reversed(made)):  # The last order that names a role.
        role = _decision_role(d.action, d.reason)
        if role is None and d.action == "fortify" and "refused" in d.reason:
            # `_fallback`: the move a role asked for was refused, so it fortified in place.
            asked = [e.action for e in made[: len(made) - 1 - i] if e.action in _BY_ACTION]
            role = _BY_ACTION[asked[-1]] if asked else None
        if role is not None:
            decision, why = d, d.reason
            break
    owner = "military"
    if uid in t.reserved:
        owner = "settling"
        role = "recon" if unit.unit_type.endswith("SCOUT") else "escort"
        why = "settling_reserved (units_reserved_for_settling)"
    elif uid not in mine:
        owner, role, why = "none", "other", "not_land_military (is_land_military: ship/air)"
    elif role is None:
        role, why = _by_state(policy, t, unit)
        if made:  # Say what was tried when the only record is a refusal or a rejected attack.
            why = f"{why}; last {made[-1].action}: {made[-1].reason}"
    row: dict[str, Any] = {
        "u": uid, "t": unit.unit_type.removeprefix("UNIT_"), "x": unit.x, "y": unit.y,
        "hp": unit.hp, "mv": unit.moves, "role": role, "own": owner,
        "act": decision.action if decision is not None else "none", "why": _short(why),
    }  # fmt: skip
    if uid in t.placed:
        row["to"] = list(t.placed[uid])
    if uid in mine:
        city = _served(policy, t, unit, decision)
        if city is not None:
            row["city"] = _city_view(policy, t, city)
        row["cnt"], reason = counts[uid]
        if reason:
            row["cnt_why"] = reason
        if role == "spare":
            need = _need_walk(policy, t, unit, plan)
            if need is not None:
                row["nw"], row["nk"] = need
    return row


def log_unit_roles(policy: Any, t: Any) -> None:
    """Log one `military_roles` line for the turn. Never raises."""
    try:
        line = build_line(policy, t)
        if not line["units"] and "gone" not in line:
            return  # No combat unit now or before: nothing to say.
        log.info("military_roles %s", json.dumps(line, sort_keys=True, separators=(",", ":")))
    except Exception:  # Logging must never change play.
        log.warning("military_roles failed", exc_info=True)


def build_line(policy: Any, t: Any) -> dict[str, Any]:
    defence = policy.defence
    plan = defence.camp_plan if defence is not None else None
    gone = []
    units = []
    now = {u.unit_id for u in t.units}
    counts = _counts(policy, t, plan)
    for uid, (kind, x, y) in sorted(policy._roles_seen.items()):
        if uid not in now:
            gone.append({"u": uid, "t": kind, "x": x, "y": y, "why": "absent from the unit read"})
    for unit in sorted((u for u in t.units if u.is_combat), key=lambda u: u.unit_id):
        if t.hp.get(unit.unit_id, 1.0) <= 0:  # Expected damage took its last hit points.
            gone.append({"u": unit.unit_id, "t": unit.unit_type.removeprefix("UNIT_"),
                         "x": unit.x, "y": unit.y,
                         "why": "killed this turn (expected damage took its last hp)"})  # fmt: skip
            continue
        units.append(_entry(policy, t, unit, plan, counts))
    policy._roles_seen = {
        u.unit_id: (u.unit_type.removeprefix("UNIT_"), u.x, u.y) for u in t.units if u.is_combat
    }
    line: dict[str, Any] = {"turn": t.turn, "cities": len(t.cities), "units": units}
    if gone:
        line["gone"] = gone
    if defence is not None:
        line["short"] = [
            _city_view(policy, t, c)
            for c in t.cities
            if (found := defence.cities.get(c.city_id)) is not None and found.short
        ]
        line["plan"] = (
            None
            if plan is None
            else {
                "at": list(plan.target), "need": plan.needed, "have": plan.available,
                "units": plan.units_available, "committed": plan.committed,
            }
        )  # fmt: skip
    return line
