"""Put the turn loop together: which handlers run, in what order, and what they share.

Order within a turn:

1. `Monitors` read the turn first, so they see it before anything acts on it. When there is a
   policy document, `PolicyDocumentHandler` follows them and sets the weights for the turn, then
   `TaskHandler` checks the tracked tasks and publishes the overdue ones, when there is a task file.
2. `DiplomacyPolicy` answers every open proposal next, before anything else spends gold or acts.
   It moves no unit, and a deal it accepts changes gold and resources that production and trade
   then see. Leaving a session open while the rest of the turn plays is what can hold the game.
   `WarPolicy` follows it: it logs every war and offers peace. It closes its own sessions.
   `TradeOfferPolicy` runs right after it and offers surplus resources to other civs. It closes
   every session it opens.
3. `SettlingPolicy` moves settlers and scouts, which changes cities and units.
4. `MilitaryPolicy` attacks, heals, garrisons and spends promotions. It runs after settling,
   which has moved its escorts and scouts first (it never touches the units settling reserves),
   and before production, builders and the resolver, whose skip must come last.
5. `ResearchPolicy` keeps a tech and a civic in progress.
6. `GovernmentPolicy` changes government, fills policy slots and spends governor points. It runs
   after research, whose completed civics decide which governments are open.
7. `CityProductionPolicy` gives idle cities something to build, after settling may have founded one.
8. `GoldPolicy` spends gold above its reserve on upgrades, purchases and tiles, after production
   has chosen what each city builds.
9. `BuilderPolicy` sends builders to work. Settling moves only settlers, scouts and combat escorts,
   and military only combat units, so no unit is ordered twice.
10. `SecondaryPolicy` runs pantheon, religion, great people and trade routes. It comes after
   production, so a trader built earlier is routed this turn, and moves only great people and
   traders, which no other handler touches.
11. `FaithPolicy` spends faith above a reserve and finds a use for idle Great Prophets. It runs
   after `SecondaryPolicy`, which acts on prophets first, and buys with faith only.
12. `BlockerResolver` goes last, because it clears what is left and skips any idle units. It gets
   the blocker policies of production, research, government, secondary, military and diplomacy,
   with its defaults as fallback. Government, military and diplomacy are each one instance,
   shared by handler and blocker table, so their bounds hold across both. Two policies claiming
   the same blocker type raise `ValueError` when the bot is built.
"""

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from civ6_ai.blockers import BlockerResolver, ResolvingGame
from civ6_ai.call_timer import CallTimer
from civ6_ai.defeat import is_defeated
from civ6_ai.monitors import EventBus, Monitors
from civ6_ai.orchestrator import Handler, Orchestrator, TurnLog
from civ6_ai.policies import research
from civ6_ai.policies.builder import BuilderPolicy
from civ6_ai.policies.diplomacy import DiplomacyPolicy
from civ6_ai.policies.faith import FaithPolicy
from civ6_ai.policies.gold import GoldPolicy
from civ6_ai.policies.government import GovernmentPolicy
from civ6_ai.policies.military import MilitaryPolicy
from civ6_ai.policies.production import CityProductionPolicy
from civ6_ai.policies.secondary import SecondaryPolicy
from civ6_ai.policies.settling import SettlingPolicy
from civ6_ai.policies.trade_offers import TradeOfferPolicy
from civ6_ai.policies.wars import WarPolicy
from civ6_ai.screenshots import Screenshotter
from civ6_ai.state.reader import IncompleteRead, check_cities_complete
from civ6_ai.strategy.apply import PolicyDocumentHandler
from civ6_ai.strategy.tasks import TaskDB, TaskHandler

SETTLING_LOGGER = "civ6_ai.policies.settling"
_DECISION_PREFIX = "settle_decision "

# Reads that more than one handler makes each turn. They can be shared until something changes the
# game, because a turn's handlers run one after another with nothing else moving.
_SHARED_READS = frozenset(
    {
        "get_game_overview",
        "get_cities",
        "get_empire_resources",
        "get_diplomacy",
        "get_victory_progress",
        "get_threat_scan",
        # Read by several handlers and by the blocker resolver, and by the government handler
        # up to five times a turn. Left out on purpose: the diplomacy, notification and congress
        # reads, because the end-turn watcher polls them while the game is moving.
        "get_units",
        "get_tech_civics",
        "get_policies",
        "get_governors",
        "get_trade_routes",
        "get_pantheon_status",
        "get_city_states",
    }
)
_PLAIN_READS = ("get_", "list_", "check_")

LOGGER = logging.getLogger(__name__)

# A game read now and then returns a stray Lua log line (for instance "Omitting feature label:
# CHOCOLATE HILLS") in place of data, and civ6-mcp's parser raises a ValueError naming it. The
# read is fine a moment later. The noise is not one string ("Request to BulkHide( true, Diplomacy )"
# is another), so what is retried is the shape: a field-count error whose reply has no "|"
# delimiter at all, so it is not data. Any other error is real and goes up at once.
_NOISE_ERROR = re.compile(r"has \d+ fields, expected [^:]*: [^|]*$", re.DOTALL)
NOISE_RETRIES = 2
NOISE_PAUSE_SECONDS = 0.3


def is_log_noise(error: BaseException) -> bool:
    """True for a parse ValueError whose only 'field' is a stray Lua log line."""
    return isinstance(error, ValueError) and _NOISE_ERROR.search(str(error)) is not None


async def read_retrying_noise(name: str, read: Callable[[], Awaitable[Any]]) -> Any:
    """Make a read, asking again (up to `NOISE_RETRIES` times) only if it hit log noise."""
    for attempt in range(NOISE_RETRIES + 1):
        try:
            return await read()
        except ValueError as error:
            if attempt == NOISE_RETRIES or not is_log_noise(error):
                raise
            LOGGER.warning(
                "Read %s returned a stray log line, retrying (%d of %d): %s",
                name,
                attempt + 1,
                NOISE_RETRIES,
                error,
            )
            await asyncio.sleep(NOISE_PAUSE_SECONDS)
    raise AssertionError("unreachable")


def _is_empty(value: Any) -> bool:
    """True for a read that came back with nothing, which civ6-mcp also returns on a timeout."""
    if value is None:
        return True
    if isinstance(value, tuple):
        return all(_is_empty(part) for part in value)
    return isinstance(value, list | dict | set | str) and not value


class SharedReads:
    """A game that answers repeated reads from memory until anything else is called.

    Any call that isn't a plain read clears the memory first, as does ending the turn, so no read
    outlives a change or a turn. A read that raises isn't kept.
    """

    def __init__(self, game: Any) -> None:
        self._game = game
        self._kept: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._game, name)
        if not callable(attribute):
            return attribute
        if name in _SHARED_READS:
            return partial(self._shared, name, attribute)
        if name.startswith(_PLAIN_READS):
            if inspect.iscoroutinefunction(attribute):
                return self._plain(name, attribute)
            return attribute
        return partial(self._changing, attribute)

    @staticmethod
    def _plain(name: str, read: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def plain(*args: Any, **kwargs: Any) -> Any:
            return await read_retrying_noise(name, lambda: read(*args, **kwargs))

        return plain

    async def _shared(self, name: str, read: Callable[[], Awaitable[Any]]) -> Any:
        if name in self._kept:
            return self._kept[name]
        value = await read_retrying_noise(name, read)  # A raised read is never kept.
        if name == "get_cities":
            # Kept only once checked against a real overview. On a mismatch either read may be the
            # short one, so both are dropped and the next ask reads them again.
            overview = await self.get_game_overview()
            if overview is None:
                return value
            try:
                check_cities_complete(value[0], overview)
            except IncompleteRead:
                self._kept.pop("get_game_overview", None)
                raise
        if not _is_empty(value):
            self._kept[name] = value  # An empty answer may be a timeout, so ask again next time.
        return value

    async def _changing(self, call: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any):
        self._kept.clear()
        try:
            return await call(*args, **kwargs)
        finally:
            self._kept.clear()  # The call itself may have changed what a read would return.


class SettleDecisionLog(logging.Handler):
    """Writes the settling policy's `settle_decision` log lines to a file, one JSON object each."""

    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.INFO)
        self.path = path
        self._level_before = logging.NOTSET
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message.startswith(_DECISION_PREFIX):
            return
        try:
            line = json.dumps(json.loads(message[len(_DECISION_PREFIX) :]), sort_keys=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except Exception:
            self.handleError(record)

    def attach(self) -> None:
        """Start listening. The logger is lowered to INFO if it would drop the lines."""
        logger = logging.getLogger(SETTLING_LOGGER)
        self._level_before = logger.level
        if not logger.isEnabledFor(logging.INFO):
            logger.setLevel(logging.INFO)
        logger.addHandler(self)

    def detach(self) -> None:
        logger = logging.getLogger(SETTLING_LOGGER)
        logger.removeHandler(self)
        logger.setLevel(self._level_before)
        self.close()


def build_orchestrator(
    game: Any,
    bus: EventBus,
    turn_log: TurnLog,
    trade_offers: bool = True,
    screenshots: Screenshotter | None = None,
    policy_document: Path | None = None,
    wars_state: Path | None = None,
    task_db: Path | TaskDB | None = None,
) -> Orchestrator:
    """Wire the handlers to a game. `game` is civ6-mcp's `GameState`, or a fake with its reads.

    `trade_offers=False` leaves out `TradeOfferPolicy`. `screenshots` saves the screen when a turn
    stops the run or a handler first fails.
    `policy_document` is a JSON file of strategy (see `strategy.policy_document`) that sets the
    policies' weights, re-read whenever it changes. With none, every policy keeps its defaults.
    `wars_state` is a file where the war tracker keeps its wars, so a restart of the same game
    keeps each war's start turn.
    `task_db` is the strategist's task file, or a `TaskDB` to share with the code that adds tasks.
    It is checked every turn (see `strategy.tasks`) and overdue tasks are published on `bus`. A file
    changed by another writer is re-read first. With none, there are no tasks.
    """
    production = CityProductionPolicy()
    government = GovernmentPolicy()  # One instance, so handler and blockers share its bounds.
    secondary = SecondaryPolicy()
    military = MilitaryPolicy()
    diplomacy = DiplomacyPolicy()
    research_policy = research.ResearchPolicy()
    gold = GoldPolicy()
    tables = [
        production.blocker_policies(),
        research.blocker_policies(),
        government.blocker_policies(),
        secondary.blocker_policies(),
        military.blocker_policies(),
        diplomacy.blocker_policies(),
    ]
    merged: dict[str, Any] = {}
    for table in tables:
        if overlap := merged.keys() & table.keys():
            raise ValueError(f"Two policies claim the same blocker: {sorted(overlap)}")
        merged.update(table)
    resolver = BlockerResolver(merged)
    handlers: list[Handler] = [
        Monitors(bus),
        *(
            [
                PolicyDocumentHandler(
                    policy_document,
                    production=production,
                    research=research_policy,
                    secondary=secondary,
                    military=military,
                    gold=gold,
                )
            ]
            if policy_document is not None
            else []
        ),
        *(
            [TaskHandler(task_db if isinstance(task_db, TaskDB) else TaskDB(task_db), bus)]
            if task_db is not None
            else []
        ),
        diplomacy,
        WarPolicy(state_path=wars_state),
        *([TradeOfferPolicy(diplomacy.weights)] if trade_offers else []),
        SettlingPolicy(),
        military,
        research_policy,
        government,
        production,
        gold,
        BuilderPolicy(),
        secondary,
        FaithPolicy(),
        resolver,
    ]
    timer = CallTimer(game)  # Under SharedReads, so it times real calls and not remembered ones.
    shared = ResolvingGame(SharedReads(timer), resolver, defeat_check=partial(is_defeated, game))
    return Orchestrator(
        shared,
        handlers,
        turn_log,
        defeat_check=partial(is_defeated, game),
        call_timer=timer,
        screenshots=screenshots,
    )
