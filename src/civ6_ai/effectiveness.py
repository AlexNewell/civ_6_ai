"""Measure whether each policy achieves its purpose, from the logs a run already writes.

Every check pairs a "created" count with a "used" count (units built vs fought, great people seen vs
activated, faith held vs spent, builds tried vs refused), or counts a failure that should stay near
zero. It reads turn logs, bot logs, event logs and settle logs. It makes no game or model calls.

A check is "no data" when the logs of that run can't support it, never zero: the older turn logs
have no per-call counts, and the oldest bot logs lack some lines. The findings are in
docs/policy-effectiveness.md.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from civ6_ai.metrics.costs import STAMPED_LOG

PASS, FAIL, NO_DATA = "pass", "FAIL", "no data"
MIN_CREATED = 5  # Fewer created than this and a ratio says little.

# The bot-log line: "<date> <time> civ6_ai.<logger> <LEVEL> <message>".
LOG_LINE = re.compile(r"^\S+ \S+ civ6_ai\.(?P<logger>\S+) (?P<level>[A-Z]+) (?P<msg>.*)$")
GP_LINE = re.compile(r"great_people turn (\d+): (\d+) on the map: (.*)$")
GP_NAME = re.compile(r"(?:^|; )(.+?) \((UNIT_GREAT_\w+)\) at ")
FAITH_IDLE = re.compile(r"Faith (\d+) above reserve (\d+) but nothing bought")
CANT_BUILD = re.compile(r"^(?P<city>.+?) can't build (?P<item>\w+): (?P<why>[A-Za-z_:]+)")
BUILDS = re.compile(r"^(.+?) builds (\w+)")
NO_HANDLER = re.compile(r"No handler for blocker (\w+)")
SLOT_TAKES = re.compile(r"^Slot (\d+) takes (\w+)")
FINISHED = re.compile(r"^\s*>> (.+?) finished building (\w+)\.")
OWN_KILLED = re.compile(r"^\s*!!! Your .+? \((UNIT_\w+)\) was killed")
EMPTY_QUEUE = re.compile(r"^\s*>> (?:Research|Civic) complete: .*Now: None")
CIVIC_RESEARCH_DONE = re.compile(r"^\s*>> (?:Research|Civic) complete:")
STARVING = re.compile(r"^\s*!!! STARVING")
IDLE_ROUTES = re.compile(r"^\s*>> IDLE TRADE ROUTE")
NON_COMBAT = ("UNIT_BUILDER", "UNIT_SETTLER", "UNIT_TRADER", "UNIT_SCOUT", "UNIT_MISSIONARY")
NON_COMBAT += ("UNIT_APOSTLE", "UNIT_INQUISITOR", "UNIT_GREAT", "UNIT_MILITARY_ENGINEER")

# Thresholds. Each is the point where a run "did nothing for the feature" or wasted most of it.
GP_MIN_USED_RATIO = 0.5
GP_MAX_WAITING = 5
MAX_UNHANDLED_BLOCKERS = 0
MAX_IDLE_FAITH = 250
MAX_REFUSED_BUILD_SHARE = 0.5
MAX_REPEAT_REFUSALS = 25
MAX_BUILDER_FAIL_SHARE = 0.1
MAX_BUILDER_IDLE_SHARE = 0.5
MAX_MOVE_REFUSED_SHARE = 0.25
MIN_ATTACKS_PER_MILITARY_UNIT = 0.5
MIN_FOUNDED_PER_SETTLER = 0.8
MAX_NO_SITE_SHARE = 0.5
MAX_IDLE_GOLD_SHARE = 0.5
MAX_FALSE_REFUSALS = 0
MAX_ERROR_TURN_SHARE = 0.02
MAX_POLICY_CHURN = 3.0
MAX_GOVERNOR_KEPT = 10
MIN_DEAL_ACCEPT_SHARE = 0.25
MIN_ROUTE_SUCCESS = 0.5
LOYALTY_LOOKBACK_TURNS = 30  # A loyalty warning this close before a loss makes it a loyalty loss.
MAX_STARVING_NOTICES = 25


@dataclass
class RunLogs:
    """Everything one run wrote. A run is one or more sessions that continue the same game."""

    records: list[dict] = field(default_factory=list)
    bot: list[tuple[str, str, str]] = field(default_factory=list)  # (logger, level, message)
    events: list[dict] = field(default_factory=list)
    settle: list[dict] = field(default_factory=list)
    has_bot_log: bool = False
    has_events: bool = False
    has_settle: bool = False
    dropped: list[str] = field(default_factory=list)  # sessions left out for lacking a bot log

    @property
    def turns(self) -> int:
        return len({r["turn"] for r in self.records})

    @property
    def has_calls(self) -> bool:
        return any(any(d.values()) for r in self.records for d in [r.get("handler_calls", {})])

    def calls(
        self, name: str, records: list[dict] | None = None, handler: str | None = None
    ) -> int:
        """How many times a game call was made, across the run or the given records.

        With `handler`, only that handler's calls count (faith buys with the same call as gold).
        """
        return sum(
            d[name][0]
            for r in (self.records if records is None else records)
            for h, d in r.get("handler_calls", {}).items()
            if name in d and handler in (None, h)
        )

    def messages(self, logger: str) -> list[str]:
        return [m for lg, _, m in self.bot if lg == logger]

    def detail_lines(self, records: list[dict] | None = None) -> list[str]:
        rows = self.records if records is None else records
        return [ln for r in rows for ln in r.get("detail", "").splitlines()]

    def settle_records(self) -> list[dict]:
        """The turn records of sessions that wrote a settle log."""
        return [r for r in self.records if r.get("_settle")]


@dataclass(frozen=True)
class Check:
    policy: str
    name: str
    created: float | None
    used: float | None
    status: str
    rule: str
    note: str = ""
    kind: str = "ratio"  # ratio: used of created; share: bad of total; count: a value; info.

    @property
    def ratio(self) -> float | None:
        if self.created and self.used is not None:
            return self.used / self.created
        return None

    def figures(self) -> str:
        """The numbers as a short phrase: '25 of 40 (62%)', '12', or '-'."""
        if self.created is None and self.used is None:
            return "-"
        if self.kind == "info":
            return f"{self.used:g}"
        if self.kind == "count" or self.used is None:
            return f"{self.created:g}"
        pct = f" ({self.ratio:.0%})" if self.ratio is not None else ""
        if self.kind == "share":
            return f"{self.used:g} of {self.created:g}{pct}"
        return f"{self.created:g} created, {self.used:g} used{pct}"


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_run(turn_logs: list[Path]) -> RunLogs:
    """Read the turn logs of one run and the bot, event and settle logs written beside them.

    If some sessions have a bot log and others don't, the bare ones are left out (and listed in
    `dropped`), so that no count mixes sessions the bot log covers with sessions it doesn't.
    """
    run = RunLogs()
    # A glob also catches the _events and _settle files, which are not turn logs.
    logs = sorted(p for p in turn_logs if STAMPED_LOG.match(p.name))

    def sibling(log: Path, suffix: str) -> Path:
        return log.with_name(f"{log.stem}{suffix}")

    with_bot = [log for log in logs if sibling(log, "_bot.log").exists()]
    if with_bot:
        run.dropped = [log.name for log in logs if log not in with_bot]
        logs = with_bot
    run.has_bot_log = bool(with_bot)
    for log in logs:
        settle = sibling(log, "_settle.jsonl")
        for record in _jsonl(log):
            run.records.append({**record, "_settle": settle.exists()})
        bot = sibling(log, "_bot.log")
        if bot.exists():
            for line in bot.read_text(encoding="utf-8", errors="replace").splitlines():
                if m := LOG_LINE.match(line):
                    run.bot.append((m["logger"], m["level"], m["msg"]))
        events = sibling(log, "_events.jsonl")
        if events.exists():
            run.has_events = True
            run.events += _jsonl(events)
        if settle.exists():
            run.has_settle = True
            run.settle += _jsonl(settle)
    return run


def _decisions(run: RunLogs, logger: str, tag: str) -> list[dict]:
    """The JSON payloads of `<tag> {...}` lines (military_decision, builder_decision)."""
    out = []
    for msg in run.messages(logger):
        if msg.startswith(tag + " "):
            try:
                out.append(json.loads(msg[len(tag) + 1 :]))
            except json.JSONDecodeError:
                continue
    return out


def _ratio_check(
    policy: str, name: str, created: int, used: int, floor: float, rule: str, note: str = ""
) -> Check:
    if created < MIN_CREATED:
        return Check(policy, name, created, used, PASS, rule, f"{note} (too few to judge)".strip())
    ok = used / created >= floor
    return Check(policy, name, created, used, PASS if ok else FAIL, rule, note)


def _share_check(
    policy: str, name: str, bad: int, total: int, ceiling: float, rule: str, note: str = ""
) -> Check:
    """`bad` of `total` should stay under `ceiling`."""
    if total < MIN_CREATED:
        return Check(policy, name, total, bad, PASS, rule, f"{note} (too few to judge)", "share")
    status = PASS if bad / total <= ceiling else FAIL
    return Check(policy, name, total, bad, status, rule, note, "share")


def _count_check(
    policy: str, name: str, value: int, ceiling: float, rule: str, note: str = ""
) -> Check:
    status = PASS if value <= ceiling else FAIL
    return Check(policy, name, value, None, status, rule, note, "count")


def _info(policy: str, name: str, value: int, note: str = "") -> Check:
    return Check(policy, name, value, value, PASS, "count only", note, "info")


def _no_data(policy: str, name: str, rule: str, why: str) -> Check:
    return Check(policy, name, None, None, NO_DATA, rule, why)


@dataclass(frozen=True)
class CityLosses:
    """City-loss events split into permanent losses, flips that came back, and their causes."""

    lost: int
    returned: int
    loyalty: int

    @property
    def permanent(self) -> int:
        return self.lost - self.returned

    def note(self) -> str:
        if not self.lost:
            return ""
        parts = [f"{self.lost} lost events", f"{self.returned} came back"]
        parts.append(f"{self.loyalty} after a loyalty warning")
        return ", ".join(parts)


def city_losses(events: list[dict]) -> CityLosses:
    """Count `city_lost` events, separating cities that came back and loyalty losses.

    A loss is a loyalty loss when the same city had a `city_loyalty_warning` up to
    LOYALTY_LOOKBACK_TURNS before it. A loss "came back" when a later `city_gained` event names the
    same city id (the monitor logs the gain from issue 156 on; older logs never carry one, so they
    count every loss as permanent). A lost event with no city id is counted as lost for good.
    """
    warned: dict[object, list[int]] = {}
    for e in events:
        if e.get("kind") == "city_loyalty_warning" and "city_id" in e:
            warned.setdefault(e["city_id"], []).append(int(e.get("turn", 0)))
    lost = returned = loyalty = 0
    for e in events:
        if e.get("kind") != "city_lost":
            continue
        lost += 1
        city, turn = e.get("city_id"), int(e.get("turn", 0))
        if any(0 <= turn - t <= LOYALTY_LOOKBACK_TURNS for t in warned.get(city, [])):
            loyalty += 1
        if city is not None and any(
            g.get("kind") == "city_gained"
            and g.get("city_id") == city
            and int(g.get("turn", 0)) >= turn
            for g in events
        ):
            returned += 1
    return CityLosses(lost, returned, loyalty)


def activation_refused(run: RunLogs) -> int:
    """Activations the game refused: the secondary handler's, and the faith handler's prophets."""
    secondary = sum(1 for m in run.messages("policies.secondary") if "can't activate at" in m)
    prophets = sum(1 for m in run.messages("policies.faith") if "but refused:" in m)
    return secondary + prophets


def great_people_checks(run: RunLogs) -> list[Check]:
    rule_used = f"activated >= {GP_MIN_USED_RATIO:.0%} of those seen"
    out: list[Check] = []
    seen: dict[str, str] = {}
    peak = 0
    for msg in run.messages("policies.secondary"):
        if m := GP_LINE.match(msg):
            peak = max(peak, int(m[2]))
            for name, kind in GP_NAME.findall(m[3]):
                seen[name] = kind
    if not run.has_bot_log or not run.has_calls:
        out.append(
            _no_data(
                "great people",
                "seen vs activated",
                rule_used,
                "needs the bot log and per-call counts",
            )
        )
    elif not seen:
        out.append(
            _no_data(
                "great people", "seen vs activated", rule_used, "no great_people lines in this log"
            )
        )
    else:
        out.append(
            _ratio_check(
                "great people",
                "seen vs activated",
                len(seen),
                max(run.calls("activate_great_person") - activation_refused(run), 0),
                GP_MIN_USED_RATIO,
                rule_used,
                "activate calls minus the refusals logged; a person seen twice counts once",
            )
        )
    if seen:
        out.append(
            _count_check(
                "great people",
                "most waiting at once",
                peak,
                GP_MAX_WAITING,
                f"at most {GP_MAX_WAITING} on the map",
            )
        )
    if not run.has_bot_log:
        return out
    waits = sum(
        1
        for m in run.messages("policies.secondary")
        if "no district to activate" in m or "nowhere to activate" in m
    )
    out.append(
        _count_check(
            "great people",
            "'no district' waits logged",
            waits,
            100,
            "at most 100 waits; many means a class is parked",
        )
    )
    out.append(
        _count_check(
            "great people",
            "activation refused",
            activation_refused(run),
            20,
            "at most 20 refused activations",
        )
    )
    return out


def blocker_checks(run: RunLogs) -> list[Check]:
    if not run.has_bot_log:
        return [_no_data("blockers", "unhandled blockers", "none", "needs the bot log")]
    unhandled = Counter(
        m[1] for lg, _, msg in run.bot if lg == "blockers" and (m := NO_HANDLER.search(msg))
    )
    names = ", ".join(f"{k} x{v}" for k, v in unhandled.most_common(3))
    out = [
        _count_check(
            "blockers",
            "turns held by an unhandled blocker",
            sum(unhandled.values()),
            MAX_UNHANDLED_BLOCKERS,
            "no blocker without a handler",
            names,
        )
    ]
    held = sum(1 for lg, _, m in run.bot if lg == "blockers" and " was held" in m)
    out.append(
        _info("blockers", "turn holds cleared and retried", held, "a held turn costs a retry")
    )
    return out


def faith_checks(run: RunLogs) -> list[Check]:
    if not run.has_bot_log:
        return [_no_data("faith", "held vs spent", "see rule", "needs the bot log")]
    idle = [
        int(m[1]) - int(m[2])
        for msg in run.messages("policies.faith")
        if (m := FAITH_IDLE.search(msg))
    ]
    bought = sum(1 for m in run.messages("policies.faith") if m.startswith("Faith bought"))
    # A turn can buy twice, so spending turns come from the calls when the run has them.
    spending_turns = sum(
        1
        for r in run.records
        if any(
            c in r.get("handler_calls", {}).get("faith", {})
            for c in ("patronize_great_person", "purchase_item")
        )
    )
    # A refused buy is still a call, but only a success is logged as bought: bound by that.
    turns_spent = min(spending_turns, bought) if run.has_calls else bought
    if not idle and not bought and not spending_turns:
        return [_no_data("faith", "held vs spent", "see rule", "no faith lines in this log")]
    worst = max(idle, default=0)
    return [
        _ratio_check(
            "faith",
            "turns idle vs turns spent",
            len(idle) + turns_spent,
            turns_spent,
            0.2,
            "faith spent on at least 20% of the turns it was above reserve",
            f"{len(idle)} idle turns, {bought} purchases logged"
            + ("" if run.has_calls else "; no call counts, so a turn buying twice counts twice"),
        ),
        _count_check(
            "faith",
            "most faith idle above reserve",
            worst,
            MAX_IDLE_FAITH,
            f"at most {MAX_IDLE_FAITH} unspent above the reserve",
        ),
    ]


def gold_successes(run: RunLogs) -> int:
    """Gold buys the game took. Older logs call UPGRADED and TILE_PURCHASED results refusals."""
    marks = ("Upgraded ", "Bought ", "refused: UPGRADED|", "refused: TILE_PURCHASED|")
    return sum(1 for m in run.messages("policies.gold") if any(k in m for k in marks))


def gold_turns(run: RunLogs) -> tuple[int, int]:
    """Turns the gold handler got past its reserve gate, and how many of those it bought on.

    Below the reserve the handler reads only the overview. Above it, it reads the cities or tiles,
    so those reads mark an eligible turn, and a purchase or upgrade call marks a turn that spent.
    """
    eligible = spent = 0
    for record in run.records:
        gold = record.get("handler_calls", {}).get("gold", {})
        bought = any(c in gold for c in ("purchase_item", "purchase_tile", "upgrade_unit"))
        # An upgrade can use the budget before the city and tile reads, so a buy marks a turn too.
        if bought or "list_city_production" in gold or "get_purchasable_tiles" in gold:
            eligible += 1
            spent += bought
    # The calls count whether or not the game took them, but only a success is logged as one.
    return eligible, (min(spent, gold_successes(run)) if run.has_bot_log else spent)


def gold_checks(run: RunLogs) -> list[Check]:
    out: list[Check] = []
    if run.has_calls:
        eligible, spent = gold_turns(run)
        out.append(
            _share_check(
                "gold",
                "turns above reserve with nothing bought",
                eligible - spent,
                eligible,
                MAX_IDLE_GOLD_SHARE,
                f"nothing bought on at most {MAX_IDLE_GOLD_SHARE:.0%} of turns above the reserve",
                "from the gold handler's calls; no gold-spent total is logged",
            )
        )
    else:
        out.append(
            _no_data("gold", "turns above reserve with nothing bought", "see rule", "needs calls")
        )
    if not run.has_bot_log:
        return [*out, _no_data("gold", "upgrade logged as refused", "see rule", "needs bot log")]
    msgs = run.messages("policies.gold")
    false_refusals = sum(1 for m in msgs if "refused: UPGRADED|" in m)
    out.append(
        _count_check(
            "gold",
            "upgrade logged as refused when it succeeded",
            false_refusals,
            MAX_FALSE_REFUSALS,
            "the log must not call a success a refusal",
        )
    )
    if run.has_calls:
        spent = sum(
            run.calls(name, handler="gold")
            for name in ("purchase_item", "purchase_tile", "upgrade_unit")
        )
        out.append(
            _info(
                "gold",
                "purchase calls",
                spent,
                "purchase_item + purchase_tile + upgrade_unit; no spent-gold total is logged",
            )
        )
    return out


def production_checks(run: RunLogs) -> list[Check]:
    if not run.has_bot_log:
        return [_no_data("production", "builds refused", "see rule", "needs the bot log")]
    msgs = run.messages("policies.production")
    refused: Counter[tuple[str, str]] = Counter()
    reasons: Counter[str] = Counter()
    ok = 0
    for msg in msgs:
        if m := CANT_BUILD.match(msg):
            refused[(m["city"], m["item"])] += 1
            reasons[m["why"].split(":")[0] if m["why"].startswith("ERR") else m["why"]] += 1
        elif BUILDS.match(msg):
            ok += 1
    total = sum(refused.values())
    worst = refused.most_common(1)[0] if refused else (("", ""), 0)
    top = ", ".join(f"{k} x{v}" for k, v in reasons.most_common(3))
    return [
        _share_check(
            "production",
            "refused vs accepted choices",
            total,
            total + ok,
            MAX_REFUSED_BUILD_SHARE,
            f"at most {MAX_REFUSED_BUILD_SHARE:.0%} of choices refused",
            top,
        ),
        _count_check(
            "production",
            "most repeats of one refused build",
            worst[1],
            MAX_REPEAT_REFUSALS,
            f"one city/item refused at most {MAX_REPEAT_REFUSALS} times",
            f"{worst[0][0]} {worst[0][1]}" if worst[1] else "",
        ),
    ]


def builder_checks(run: RunLogs) -> list[Check]:
    decisions = _decisions(run, "policies.builder", "builder_decision")
    if not decisions:
        return [
            _no_data("builders", "improvements failed", "see rule", "no builder_decision lines")
        ]
    actions = Counter(d["action"] for d in decisions)
    work = sum(n for action, n in actions.items() if action not in ("idle", "failed"))
    failed = actions["failed"]
    repeat = Counter(
        (d.get("target") and tuple(d["target"]), d.get("improvement"))
        for d in decisions
        if d["action"] == "failed"
    )
    worst = repeat.most_common(1)[0][1] if repeat else 0
    idle = actions["idle"]
    return [
        _share_check(
            "builders",
            "builder actions the game refused",
            failed,
            failed + work,
            MAX_BUILDER_FAIL_SHARE,
            f"at most {MAX_BUILDER_FAIL_SHARE:.0%} of builder actions refused",
            f"moves, improves and repairs; {worst} is the most repeats of one tile and improvement",
        ),
        _share_check(
            "builders",
            "builder-turns idle",
            idle,
            idle + work,
            MAX_BUILDER_IDLE_SHARE,
            f"idle on at most {MAX_BUILDER_IDLE_SHARE:.0%} of builder-turns",
            "idle means no job on offer",
        ),
    ]


def military_checks(run: RunLogs) -> list[Check]:
    out: list[Check] = []
    decisions = _decisions(run, "policies.military", "military_decision")
    if decisions:
        acting = [d for d in decisions if d["action"] != "skip"]
        refused = sum(1 for d in acting if "move refused" in (d.get("reason") or ""))
        out.append(
            _share_check(
                "military",
                "moves refused by the game",
                refused,
                len(acting),
                MAX_MOVE_REFUSED_SHARE,
                f"at most {MAX_MOVE_REFUSED_SHARE:.0%} of unit decisions end in a refused move",
                "STACKING_CONFLICT is the usual cause",
            )
        )
    else:
        out.append(_no_data("military", "moves refused", "see rule", "no military_decision lines"))
    built = Counter()
    lost = Counter()
    for ln in run.detail_lines():
        if m := FINISHED.match(ln):
            if m[2].startswith("UNIT_") and not m[2].startswith(NON_COMBAT):
                built[m[2]] += 1
        elif m := OWN_KILLED.match(ln):
            lost[m[1]] += 1
    # Calls count refused attacks too, so count the decisions that did not end in a refusal.
    fought = sum(
        1
        for d in decisions
        if d["action"] in ("attack", "city_strike") and "refused" not in (d.get("reason") or "")
    )
    if not decisions:
        out.append(
            _no_data("military", "units built vs attacks", "see rule", "no military_decision lines")
        )
    else:
        out.append(
            _ratio_check(
                "military",
                "combat units built vs attacks made",
                sum(built.values()),
                fought,
                MIN_ATTACKS_PER_MILITARY_UNIT,
                f"at least {MIN_ATTACKS_PER_MILITARY_UNIT} attacks per unit built",
                f"{sum(lost.values())} own units reported killed (units built are read from "
                "completion notices, a lower bound)",
            )
        )
    return out


def settling_checks(run: RunLogs) -> list[Check]:
    out: list[Check] = []
    covered = run.settle_records() if run.has_settle else run.records
    settlers = sum(
        1
        for ln in run.detail_lines(covered)
        if (m := FINISHED.match(ln)) and m[2] == "UNIT_SETTLER"
    )
    if run.has_calls and run.has_settle:
        # A found_city call the game refused is logged as found_failed in the settle log.
        failed = sum(1 for s in run.settle if s.get("action") == "found_failed")
        out.append(
            _ratio_check(
                "settling",
                "settlers built vs cities founded",
                settlers,
                max(run.calls("found_city", covered) - failed, 0),
                MIN_FOUNDED_PER_SETTLER,
                f"at least {MIN_FOUNDED_PER_SETTLER:.0%} of settlers found a city",
                "settlers are read from completion notices, a lower bound",
            )
        )
    else:
        out.append(
            _no_data(
                "settling",
                "settlers vs cities",
                "see rule",
                "needs per-call counts and the settle log",
            )
        )
    actions = Counter(s.get("action") for s in run.settle)
    if run.settle:
        out.append(
            _share_check(
                "settling",
                "decisions with no site found",
                actions["no_site"] + actions["hold"],
                sum(actions.values()),
                MAX_NO_SITE_SHARE,
                f"no_site or hold on at most {MAX_NO_SITE_SHARE:.0%} of decisions",
                ", ".join(f"{k} {v}" for k, v in actions.most_common(4)),
            )
        )
    else:
        out.append(_no_data("settling", "no-site decisions", "see rule", "no settle log"))
    if run.has_events:
        lost = city_losses(run.events)
        out.append(
            _count_check(
                "settling",
                "cities lost",
                lost.permanent,
                0,
                "no city lost for good",
                lost.note(),
            )
        )
    else:
        out.append(_no_data("settling", "cities lost", "no city lost", "no events log"))
    return out


def research_checks(run: RunLogs) -> list[Check]:
    lines = run.detail_lines()
    done = sum(1 for ln in lines if CIVIC_RESEARCH_DONE.match(ln))
    empty = sum(1 for ln in lines if EMPTY_QUEUE.match(ln))
    return [
        _info(
            "research",
            "completions that left the queue empty",
            empty,
            f"of {done} tech and civic completions; the notice is the game's, written before the "
            "bot's handler refills the queue, so it does not show lost turns",
        )
    ]


def government_checks(run: RunLogs) -> list[Check]:
    if not run.has_bot_log:
        return [_no_data("government", "policy changes", "see rule", "needs the bot log")]
    msgs = run.messages("policies.government")
    slots = Counter(m.groups() for msg in msgs if (m := SLOT_TAKES.match(msg)))
    distinct = len(slots)
    total = sum(slots.values())
    kept = sum(1 for m in msgs if "kept the point" in m)
    unfinished = sum(1 for m in msgs if m.startswith("Left something unfinished"))
    out = [
        _count_check(
            "government",
            "'left something unfinished' warnings",
            unfinished,
            20,
            "at most 20; each is a handler step that did not finish",
        ),
        _count_check(
            "government",
            "times each slot/policy pair was set again",
            round(total / distinct, 1) if distinct else 0,
            MAX_POLICY_CHURN,
            f"each pair set at most {MAX_POLICY_CHURN:.0f} times on average",
            f"{total} 'Slot N takes' lines for {distinct} distinct pairs",
        ),
        _count_check(
            "government",
            "governor actions that kept the point",
            kept,
            MAX_GOVERNOR_KEPT,
            f"at most {MAX_GOVERNOR_KEPT} governor actions that left the point unspent",
        ),
    ]
    return out


def diplomacy_checks(run: RunLogs) -> list[Check]:
    if not run.has_bot_log:
        return [_no_data("diplomacy", "deals answered", "see rule", "needs the bot log")]
    msgs = run.messages("policies.diplomacy")
    # The decision is logged before the game is asked, and a refused answer follows it. Pair each
    # refusal with the decision just before, and take off only the acceptances that failed.
    logged = refused = failed_accepts = 0
    last = None
    for m in msgs:
        if re.match(r"Deal from player \d+ accepted", m):
            logged, last = logged + 1, "accepted"
        elif re.match(r"Deal from player \d+ refused", m):
            refused, last = refused + 1, "refused"
        elif m.startswith("The game refused our answer to the deal"):
            failed_accepts += last == "accepted"
            last = None
    accepted = logged - failed_accepts
    unanswered = sum(1 for m in msgs if "unanswered" in m or "was still open" in m)
    out = [
        _ratio_check(
            "diplomacy",
            "deals offered vs accepted",
            logged + refused,
            accepted,
            MIN_DEAL_ACCEPT_SHARE,
            f"at least {MIN_DEAL_ACCEPT_SHARE:.0%} of deals accepted",
            "refusing a bad deal is correct, so this is informational unless it is near zero",
        ),
        _count_check(
            "diplomacy",
            "screens closed unanswered",
            unanswered,
            5,
            "at most 5 sessions closed without an answer",
        ),
    ]
    if run.has_calls:
        sent = run.calls("send_envoy")
        out.append(_info("diplomacy", "envoys sent", sent, "tokens available are not logged"))
    return out


def trade_checks(run: RunLogs) -> list[Check]:
    if not run.has_bot_log or not run.has_calls:
        return [_no_data("trade", "routes made vs refused", "see rule", "needs bot log and calls")]
    refused = sum(1 for m in run.messages("policies.secondary") if m.startswith("Trade route to"))
    made = run.calls("make_trade_route") - refused
    idle = sum(1 for ln in run.detail_lines() if IDLE_ROUTES.match(ln))
    return [
        _ratio_check(
            "trade",
            "route attempts that worked",
            run.calls("make_trade_route"),
            max(made, 0),
            MIN_ROUTE_SUCCESS,
            f"at least {MIN_ROUTE_SUCCESS:.0%} of route calls accepted",
            f"{idle} turns report unused route capacity",
        )
    ]


def health_checks(run: RunLogs) -> list[Check]:
    errored = Counter(h for r in run.records for h in r.get("handler_errors", {}))
    error_turns = {r["turn"] for r in run.records if r.get("handler_errors")}
    turns = max(run.turns, 1)
    stops = Counter(r.get("stop_reason") for r in run.records if r.get("stop_reason"))
    starving = sum(1 for ln in run.detail_lines() if STARVING.match(ln))
    return [
        _share_check(
            "handlers",
            "turn-handler failures",
            len(error_turns),
            turns,
            MAX_ERROR_TURN_SHARE,
            f"handler errors on at most {MAX_ERROR_TURN_SHARE:.0%} of turns",
            ", ".join(f"{k} x{v}" for k, v in errored.most_common(3)),
        ),
        _info(
            "run",
            "stops that were not the game ending",
            sum(v for k, v in stops.items() if k != "game_over"),
            ", ".join(f"{k} x{v}" for k, v in stops.most_common()),
        ),
        _count_check(
            "cities",
            "starving notices",
            starving,
            MAX_STARVING_NOTICES,
            f"at most {MAX_STARVING_NOTICES} starving notices",
            "a city-tile worked or a food building queued would clear these",
        ),
    ]


ALL_CHECKS = (
    great_people_checks,
    blocker_checks,
    faith_checks,
    gold_checks,
    production_checks,
    builder_checks,
    military_checks,
    settling_checks,
    research_checks,
    government_checks,
    diplomacy_checks,
    trade_checks,
    health_checks,
)


def run_checks(run: RunLogs) -> list[Check]:
    return [check for fn in ALL_CHECKS for check in fn(run)]


def format_report(label: str, run: RunLogs, checks: list[Check]) -> str:
    failing = [c for c in checks if c.status == FAIL]
    lines = [
        f"Policy effectiveness: {label} ({run.turns} turns, {len(run.records)} turn records)",
        *([f"Left out for lacking a bot log: {', '.join(run.dropped)}"] if run.dropped else []),
        f"{len(failing)} of {len(checks)} checks failed",
        "",
    ]
    for c in checks:
        lines.append(
            f"[{c.status:7}] {c.policy:11} {c.name}: {c.figures()}; {c.rule}"
            + (f" | {c.note}" if c.note else "")
        )
    return "\n".join(lines)
