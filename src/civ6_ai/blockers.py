"""Clear everything that can stop a turn from ending.

`docs/blockers.md` is the catalogue of blocker types. Every default here is a simple safe choice
that keeps the game moving. A domain policy takes a type over by passing its own function in
`policies`, and the default stays behind it as a fallback, so a policy bug can't stall a game.

`BlockerResolver` is the last handler of a turn. `ResolvingGame` wraps the game for what can only
appear once the turn has been ended, such as an AI proposal during its turn.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from civ_mcp.lua import (
    SENTINEL,
    build_end_turn,
    build_end_turn_blocking_query,
    parse_end_turn_blocking,
)

log = logging.getLogger(__name__)

MAX_PASSES = 3  # Clearing one blocker can reveal another, such as an idle governor.
# The turn loop reads this prefix as the end of the game, see `orchestrator._REPORT_PREFIXES`.
GAME_OVER_REPORT = "GAME OVER: the game ended while the turn was waiting to end."
MAX_END_TURN_RETRIES = 8  # Enough for all five opponents to interrupt one turn, plus a blocker.
# civ6-mcp's end_turn only looks at what holds a turn after polling for about 9 minutes, so the
# bot checks itself while it waits. The first check leaves time for a normal AI turn.
WATCH_FIRST_SECONDS = 10.0
WATCH_EVERY_SECONDS = 10.0
_CAN_END_TURN_LUA = f'print("CAN_END_TURN|" .. tostring(UI.CanEndTurn())); print("{SENTINEL}")'
MAX_DIPLOMACY_ROUNDS = 4  # A first meeting takes 2-3 rounds.

_NOTHING_CHOSEN = "None"  # civ6-mcp's value when no tech or civic is in progress.
_NO_PRODUCTION = "nothing"  # civ6-mcp's value for a city with an empty queue.
_CLAIM_GREAT_PERSON = "NOTIFICATION_CLAIM_GREAT_PERSON"

# civ6-mcp's `submit_congress` also sends ACTION_ENDTURN, and a second one from `end_turn` can skip
# turns, so this sends only the submit and leaves ending the turn to `end_turn`.
_SUBMIT_CONGRESS_LUA = f"""
local me = Game.GetLocalPlayer()
UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_SUBMIT_TURN, {{}})
print("{SENTINEL}")
"""

# Reports from `end_turn` that mean the turn should be tried again. They're text, not errors.
# The AI may still be working behind these, so only diplomacy is safe to touch.
_DIPLOMACY_REPORTS = (
    "Turn paused",
    "WAR DECLARED",
    "Cannot end turn: diplomacy",
    "Cannot end turn: incoming trade deal",
)
_BLOCKED_REPORTS = ("Cannot end turn", "World Congress fires", "End turn blocked")


def _has_line_starting(report: str, prefixes: tuple[str, ...]) -> bool:
    """True if any line of the report starts with a prefix, as `stop_reason_in_report` matches."""
    return any(line.lstrip().startswith(prefixes) for line in report.splitlines())


class BlockerType(StrEnum):
    # Found by their own queries, because they aren't end-turn blocking notifications.
    DIPLOMACY_SESSION = "DIPLOMACY_SESSION"
    TRADE_DEAL = "TRADE_DEAL"
    WORLD_CONGRESS_VOTE = "WORLD_CONGRESS_VOTE"
    GREAT_PERSON_CLAIM = "GREAT_PERSON_CLAIM"
    # The game's own end-turn blocking types.
    CLAIM_GREAT_PERSON = "ENDTURN_BLOCKING_CLAIM_GREAT_PERSON"  # Held the turn in Runs 3 to 5.
    RESEARCH = "ENDTURN_BLOCKING_RESEARCH"
    CIVIC = "ENDTURN_BLOCKING_CIVIC"
    PRODUCTION = "ENDTURN_BLOCKING_PRODUCTION"
    FILL_CIVIC_SLOT = "ENDTURN_BLOCKING_FILL_CIVIC_SLOT"
    GOVERNOR_APPOINTMENT = "ENDTURN_BLOCKING_GOVERNOR_APPOINTMENT"
    GOVERNOR_OPPORTUNITY = (
        "ENDTURN_BLOCKING_GOVERNOR_OPPORTUNITY"  # "New Governor Title Available".
    )
    GOVERNOR_IDLE = "ENDTURN_BLOCKING_GOVERNOR_IDLE"
    UNIT_PROMOTION = "ENDTURN_BLOCKING_UNIT_PROMOTION"
    PANTHEON = "ENDTURN_BLOCKING_PANTHEON"
    COMMEMORATION_AVAILABLE = "ENDTURN_BLOCKING_COMMEMORATION_AVAILABLE"
    GIVE_INFLUENCE_TOKEN = "ENDTURN_BLOCKING_GIVE_INFLUENCE_TOKEN"
    WORLD_CONGRESS_SESSION = "ENDTURN_BLOCKING_WORLD_CONGRESS_SESSION"
    WORLD_CONGRESS_LOOK = "ENDTURN_BLOCKING_WORLD_CONGRESS_LOOK"
    WORLD_CONGRESS_SPECIAL_SESSION = "ENDTURN_BLOCKING_WORLD_CONGRESS_SPECIAL_SESSION"
    CONSIDER_GOVERNMENT_CHANGE = "ENDTURN_BLOCKING_CONSIDER_GOVERNMENT_CHANGE"
    CONSIDER_RAZE_CITY = "ENDTURN_BLOCKING_CONSIDER_RAZE_CITY"
    CONSIDER_DISLOYAL_CITY = "ENDTURN_BLOCKING_CONSIDER_DISLOYAL_CITY"
    SPY_CHOOSE_ESCAPE_ROUTE = "ENDTURN_BLOCKING_SPY_CHOOSE_ESCAPE_ROUTE"
    STACKED_UNITS = "ENDTURN_BLOCKING_STACKED_UNITS"
    UNITS = "ENDTURN_BLOCKING_UNITS"


@dataclass(frozen=True)
class Blocker:
    type: str  # A `BlockerType`, or the game's own name for a type this project doesn't know.
    message: str = ""


class UnresolvedBlockers(Exception):
    """Raised after clearing left blockers open, so the turn log records them."""


class BlockerGame(Protocol):
    """The part of civ6-mcp's `GameState` that blocker handling uses."""

    async def execute_lua(self, code: str, context: str = "gamecore") -> str: ...
    async def get_notifications(self) -> list[Any]: ...
    async def get_diplomacy_sessions(self) -> list[Any]: ...
    async def diplomacy_respond(self, other_player_id: int, response: str) -> str: ...
    async def get_pending_deals(self) -> list[Any]: ...
    async def respond_to_deal(self, other_player_id: int, accept: bool) -> str: ...
    async def get_world_congress(self) -> Any: ...
    async def queue_wc_votes(self, votes: list[dict]) -> str: ...
    async def vote_world_congress(
        self, resolution_hash: int, option: int, target_index: int, num_votes: int
    ) -> str: ...
    async def get_tech_civics(self) -> Any: ...
    async def set_research(self, tech_name: str) -> str: ...
    async def set_civic(self, civic_name: str) -> str: ...
    async def get_cities(self) -> tuple[list[Any], list[str]]: ...
    async def list_city_production(self, city_id: int) -> list[Any]: ...
    async def set_city_production(self, city_id: int, item_type: str, item_name: str) -> str: ...
    async def get_policies(self) -> Any: ...
    async def set_policies(self, assignments: dict[int, str]) -> str: ...
    async def get_governors(self) -> Any: ...
    async def appoint_governor(self, governor_type: str) -> str: ...
    async def promote_governor(self, governor_type: str, promotion_type: str) -> str: ...
    async def get_units(self) -> list[Any]: ...
    async def get_unit_promotions(self, unit_id: int) -> Any: ...
    async def promote_unit(self, unit_id: int, promotion_type: str) -> str: ...
    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...
    async def skip_remaining_units(self) -> str: ...
    async def dismiss_popup(self) -> str: ...
    async def get_game_overview(self) -> Any: ...
    async def get_pantheon_status(self) -> Any: ...
    async def choose_pantheon(self, belief_type: str) -> str: ...
    async def get_dedications(self) -> Any: ...
    async def choose_dedication(self, dedication_index: int) -> str: ...
    async def get_city_states(self) -> Any: ...
    async def send_envoy(self, city_state_player_id: int) -> str: ...
    async def get_great_people(self) -> list[Any]: ...
    async def recruit_great_person(self, individual_id: int) -> str: ...


# Acts on a blocker. Returns False if it's still open and worth trying again.
Resolve = Callable[[BlockerGame, Blocker], Awaitable[bool]]


def _succeeded(result: str) -> bool:
    """Read an action's text. civ6-mcp reports a failed action without raising."""
    return not result.startswith(("Error", "ERR:"))


def action_succeeded(result: str) -> bool:
    """Public form of `_succeeded`, for policies that act on the game."""
    return _succeeded(result)


def move_succeeded(result: str) -> bool:
    """A move can be accepted yet go nowhere. civ6-mcp then adds `|BLOCKED (reason)`."""
    return _succeeded(result) and "BLOCKED" not in result


_DIPLOMACY_VIEW_LUA = (
    'do local c = ContextPtr:LookUpControl("/InGame/DiplomacyActionView") '
    'if c and not c:IsHidden() then print("DIPLOMACY_VIEW|open") end end '
    'print("---END---")'
)
_DIPLOMACY_VIEW_STATE = "DiplomacyActionView"


async def stray_diplomacy_view_open(game: BlockerGame) -> bool:
    """True if the diplomacy screen is up although the game lists no session behind it.

    Run 4's turn 221 stuck here: the AI turn waited on Sumeria's screen for ten minutes, and
    `get_diplomacy_sessions` was empty, so no blocker was read and nothing closed it.
    """
    if "DIPLOMACY_VIEW|open" not in await game.execute_lua(_DIPLOMACY_VIEW_LUA, "ingame"):
        return False
    return not await game.get_diplomacy_sessions()


async def close_stray_diplomacy_view(game: BlockerGame) -> bool:
    """Close the screen with its own `OnClose`, in its own Lua state, as the game's button does.

    Respond-and-exit finds no session (NO_SESSION) and hiding it from InGame leaves it up, but
    this closed it on turn 221 and the game went on to turn 222. True if it is closed now.
    """
    states = getattr(getattr(game, "conn", None), "lua_states", {})
    index = next((i for i, name in states.items() if name == _DIPLOMACY_VIEW_STATE), None)
    if index is None:
        log.warning(
            "No Lua state named %s to close the stray diplomacy screen", _DIPLOMACY_VIEW_STATE
        )
        return False
    await game.execute_lua('pcall(OnClose) print("---END---")', str(index))
    return not await stray_diplomacy_view_open(game)


async def leave_to_end_turn(game: BlockerGame, blocker: Blocker) -> bool:
    """Do nothing, because `end_turn` clears this type itself."""
    return True


async def answer_diplomacy(game: BlockerGame, blocker: Blocker) -> bool:
    """Close war declarations, refuse deals and answer anything else in a friendly way."""
    answered = True
    for session in await game.get_diplomacy_sessions():
        player = session.other_player_id
        if session.is_at_war:  # It can't be declined, and `end_turn` closes these the same way.
            answered &= _succeeded(await game.diplomacy_respond(player, "EXIT"))
        elif session.deal_summary:
            answered &= _succeeded(await game.respond_to_deal(player, False))
        else:
            answered &= await _talk(game, player)
    return answered


async def _talk(game: BlockerGame, player: int) -> bool:
    for _ in range(MAX_DIPLOMACY_ROUNDS):
        result = await game.diplomacy_respond(player, "POSITIVE")
        if not _succeeded(result):
            return False
        if "SESSION_CONTINUES" not in result:
            return True
    # civ6-mcp warns that forcing a session shut risks a hang, but this one would block anyway.
    log.warning("Forcing the diplomacy session with player %s closed", player)
    return _succeeded(await game.diplomacy_respond(player, "EXIT"))


async def reject_deals(game: BlockerGame, blocker: Blocker) -> bool:
    """Refuse every incoming deal, because the bot can't value one yet."""
    deals = await game.get_pending_deals()
    results = [await game.respond_to_deal(deal.other_player_id, False) for deal in deals]
    return all(_succeeded(result) for result in results)


async def register_default_votes(game: BlockerGame, blocker: Blocker) -> bool:
    """Queue civ6-mcp's default voter, which casts the votes during `end_turn`."""
    return _succeeded(await game.queue_wc_votes([]))


async def submit_world_congress_votes(game: BlockerGame) -> None:
    """Submit the votes cast so far, which closes the session. Leaves ending the turn alone."""
    await game.execute_lua(_SUBMIT_CONGRESS_LUA, "ingame")


async def vote_in_open_session(game: BlockerGame, blocker: Blocker) -> bool:
    """Cast one free vote on each resolution of a session that's already open, then submit."""
    status = await game.get_world_congress()
    # `end_turn` refuses while a session is open unless a voter is registered, even once votes
    # are submitted, so register one in both cases.
    registered = await register_default_votes(game, blocker)
    if not status.is_in_session:
        return registered
    for resolution in status.resolutions:
        if not resolution.is_passed:
            vote = await game.vote_world_congress(resolution.resolution_hash, 1, 0, 1)
            log.info(
                "congress (default voter): %s, option A, target 0, 1 vote: %s",
                resolution.name,
                str(vote)[:200],
            )
            if not _succeeded(vote):
                return False
    await submit_world_congress_votes(game)
    return registered


async def research_fastest(game: BlockerGame, blocker: Blocker) -> bool:
    status = await game.get_tech_civics()
    if status.current_research != _NOTHING_CHOSEN:
        return True  # A stale notification, which `end_turn` clears.
    if not status.available_techs:
        return False
    fastest = min(status.available_techs, key=lambda tech: tech.turns)
    return _succeeded(await game.set_research(fastest.tech_type))


async def study_fastest(game: BlockerGame, blocker: Blocker) -> bool:
    status = await game.get_tech_civics()
    if status.current_civic != _NOTHING_CHOSEN:
        return True  # A stale notification, which `end_turn` clears.
    if not status.available_civics:
        return False
    fastest = min(status.available_civics, key=lambda civic: civic.turns)
    return _succeeded(await game.set_civic(fastest.civic_type))


async def build_cheapest(game: BlockerGame, blocker: Blocker) -> bool:
    """Start the cheapest item in each idle city, leaving units until last for their upkeep."""
    cities, _ = await game.get_cities()
    idle = [city.city_id for city in cities if city.currently_building == _NO_PRODUCTION]
    return all([await _start_production(game, city_id) for city_id in idle])


async def _start_production(game: BlockerGame, city_id: int) -> bool:
    options = await game.list_city_production(city_id)
    # Districts need a tile, and choosing one is a job for the build policy.
    buildable = [o for o in options if o.category != "DISTRICT" and not o.is_repair]
    buildable.sort(key=lambda option: (option.category == "UNIT", option.cost))
    # Wonders list as buildings but are refused without a tile. A refusal costs one call and
    # changes nothing, so every option is tried, and a unit at the end still clears the block.
    for option in buildable:
        result = await game.set_city_production(city_id, option.category, option.item_name)
        if _succeeded(result):
            return True
    return False


async def fill_policy_slots(game: BlockerGame, blocker: Blocker) -> bool:
    """Put an unused policy that fits into each empty slot."""
    status = await game.get_policies()
    empty = [slot for slot in status.slots if slot.current_policy is None]
    if not empty:
        return True
    slotted = {slot.current_policy for slot in status.slots}
    spare = [p for p in status.available_policies if p.policy_type not in slotted]
    assignments: dict[int, str] = {}
    for slot in empty:
        fits = (p for p in spare if slot.slot_type in (p.slot_type, "SLOT_WILDCARD"))
        choice = next(fits, None)
        if choice is not None:
            spare.remove(choice)
            assignments[slot.slot_index] = choice.policy_type
    if not assignments:
        return False
    result = await game.set_policies(assignments)
    return _succeeded(result) and "SILENT_FAILURE" not in result


async def appoint_or_promote_governor(game: BlockerGame, blocker: Blocker) -> bool:
    """Spend a governor point. `end_turn` clears the idle governor this leaves."""
    status = await game.get_governors()
    if status.can_appoint:
        for candidate in status.available_to_appoint:
            if _succeeded(await game.appoint_governor(candidate.governor_type)):
                return True
    for governor in status.appointed:
        if governor.available_promotions:
            promotion = governor.available_promotions[0].promotion_type
            return _succeeded(await game.promote_governor(governor.governor_type, promotion))
    return status.points_available == 0


async def promote_units(game: BlockerGame, blocker: Blocker) -> bool:
    """Give each unit that has earned one its first offered promotion."""
    # `UnitInfo.needs_promotion` is never set, on purpose. civ6-mcp lists promotions only for a
    # unit with the XP to take one, so each unit has to be asked.
    promoted = True
    for unit in await game.get_units():
        if unit.combat_strength == 0 and unit.ranged_strength == 0:
            continue
        try:
            options = (await game.get_unit_promotions(unit.unit_id)).promotions
        except ValueError:  # civ6-mcp's error for a unit it can't read experience from.
            continue
        if options:
            result = await game.promote_unit(unit.unit_id, options[0].promotion_type)
            promoted &= _succeeded(result)
    return promoted


async def choose_first_pantheon(game: BlockerGame, blocker: Blocker) -> bool:
    status = await game.get_pantheon_status()
    if status.has_pantheon:
        return True
    if not status.available_beliefs:
        return False
    return _succeeded(await game.choose_pantheon(status.available_beliefs[0].belief_type))


async def choose_first_dedications(game: BlockerGame, blocker: Blocker) -> bool:
    status = await game.get_dedications()
    chosen = status.choices[: status.selections_allowed]
    return all([_succeeded(await game.choose_dedication(choice.index)) for choice in chosen])


async def send_envoys_to_one(game: BlockerGame, blocker: Blocker) -> bool:
    """Send envoys to one city-state where it accepts them, which earns more than spreading them."""
    status = await game.get_city_states()
    if status.tokens_available == 0:
        return True  # A stale notification, which `end_turn` clears.
    targets = [city for city in status.city_states if city.can_send_envoy]
    if not targets:
        return False
    # Spend each envoy on the first city-state that takes it, so one refusal can't strand them.
    for _ in range(status.tokens_available):
        for target in targets:
            if _succeeded(await game.send_envoy(target.player_id)):
                break
        else:
            return False
    return True


_NEIGHBOUR_OFFSETS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]


async def unstack_units(game: BlockerGame, blocker: Blocker) -> bool:
    """Move the extra units off any tile that holds two of a kind."""
    taken: set[tuple[int, int, bool]] = set()
    moved = True
    for unit in await game.get_units():
        place = (unit.x, unit.y, unit.combat_strength > 0 or unit.ranged_strength > 0)
        if place in taken:
            moved &= await _step_aside(game, unit)
        taken.add(place)
    return moved


async def _step_aside(game: BlockerGame, unit: Any) -> bool:
    if unit.moves_remaining <= 0:
        return False
    # Rows are staggered, so which diagonals touch depends on the row. Try all eight offsets.
    for dx, dy in _NEIGHBOUR_OFFSETS:
        x, y = unit.x + dx, unit.y + dy
        result = await game.move_unit(unit.unit_index, x, y)
        if move_succeeded(result):
            return True
    return False


async def skip_units(game: BlockerGame, blocker: Blocker) -> bool:
    """Fortify or skip every unit that still has moves."""
    return _succeeded(await game.skip_remaining_units())


async def recruit_great_people(game: BlockerGame, blocker: Blocker) -> bool:
    """Recruit everyone available, which spends points but no gold or faith.

    Also the default for `ENDTURN_BLOCKING_CLAIM_GREAT_PERSON`. Patronage is left to the faith
    policy, which keeps its reserve, so this never spends gold or faith. A failed recruit returns
    False and the resolver leaves the blocker open for `end_turn` to judge.
    """
    people = [person for person in await game.get_great_people() if person.can_recruit]
    results = [await game.recruit_great_person(person.individual_id) for person in people]
    return all(_succeeded(result) for result in results)


DEFAULT_RESOLVERS: Mapping[str, Resolve] = {
    BlockerType.DIPLOMACY_SESSION: answer_diplomacy,
    BlockerType.TRADE_DEAL: reject_deals,
    BlockerType.WORLD_CONGRESS_VOTE: register_default_votes,
    BlockerType.GREAT_PERSON_CLAIM: recruit_great_people,
    BlockerType.CLAIM_GREAT_PERSON: recruit_great_people,
    BlockerType.RESEARCH: research_fastest,
    BlockerType.CIVIC: study_fastest,
    BlockerType.PRODUCTION: build_cheapest,
    BlockerType.FILL_CIVIC_SLOT: fill_policy_slots,
    BlockerType.GOVERNOR_APPOINTMENT: appoint_or_promote_governor,
    BlockerType.GOVERNOR_OPPORTUNITY: appoint_or_promote_governor,
    BlockerType.GOVERNOR_IDLE: leave_to_end_turn,
    BlockerType.UNIT_PROMOTION: promote_units,
    BlockerType.PANTHEON: choose_first_pantheon,
    BlockerType.COMMEMORATION_AVAILABLE: choose_first_dedications,
    BlockerType.GIVE_INFLUENCE_TOKEN: send_envoys_to_one,
    BlockerType.WORLD_CONGRESS_SESSION: vote_in_open_session,
    BlockerType.WORLD_CONGRESS_LOOK: leave_to_end_turn,
    BlockerType.WORLD_CONGRESS_SPECIAL_SESSION: leave_to_end_turn,
    BlockerType.CONSIDER_GOVERNMENT_CHANGE: leave_to_end_turn,
    BlockerType.CONSIDER_RAZE_CITY: leave_to_end_turn,
    BlockerType.CONSIDER_DISLOYAL_CITY: leave_to_end_turn,
    BlockerType.SPY_CHOOSE_ESCAPE_ROUTE: leave_to_end_turn,
    BlockerType.STACKED_UNITS: unstack_units,
    BlockerType.UNITS: skip_units,
}


async def _read_diplomacy(game: BlockerGame) -> list[Blocker]:
    blockers = []
    if await game.get_diplomacy_sessions():
        blockers.append(Blocker(BlockerType.DIPLOMACY_SESSION))
    if await game.get_pending_deals():
        blockers.append(Blocker(BlockerType.TRADE_DEAL))
    return blockers


async def _read_congress(game: BlockerGame) -> list[Blocker]:
    """Apply `end_turn`'s own test. A game with no congress yet reads as -1 turns away."""
    status = await game.get_world_congress()
    fires = status.turns_until_next <= 0 or status.is_in_session
    if fires and (status.resolutions or status.is_in_session):
        return [Blocker(BlockerType.WORLD_CONGRESS_VOTE)]
    return []


async def _read_end_turn_blocking(game: BlockerGame) -> list[Blocker]:
    output = await game.execute_lua(build_end_turn_blocking_query(), "ingame")
    return [Blocker(*found) for found in parse_end_turn_blocking(output.splitlines())]


async def _read_great_people(game: BlockerGame) -> list[Blocker]:
    notifications = await game.get_notifications()
    if any(notification.type_name == _CLAIM_GREAT_PERSON for notification in notifications):
        return [Blocker(BlockerType.GREAT_PERSON_CLAIM)]
    return []


_ALL_READERS = (_read_diplomacy, _read_congress, _read_end_turn_blocking, _read_great_people)


async def _read_open_session(game: BlockerGame) -> list[Blocker]:
    """An open session can hold a turn without being in the game's blocking list.

    That happens when the voter was registered after the session opened, so it never fires.
    """
    status = await game.get_world_congress()
    return [Blocker(BlockerType.WORLD_CONGRESS_SESSION)] if status.is_in_session else []


# What the end-turn watcher checks while a turn is held.
# Diplomacy is in the list so that a session or deal is answered by our own rules within a tick or
# two. Left to civ6-mcp's end_turn it waited about 40 s for the report "Turn paused - AI diplomatic
# proposal", and in Run 4 each turn held by one cost 68 s of end-turn wait against 6.6 s otherwise.
_WATCH_READERS = (_read_end_turn_blocking, _read_open_session, _read_diplomacy)
_DIPLOMACY_TYPES = frozenset({BlockerType.DIPLOMACY_SESSION, BlockerType.TRADE_DEAL})


class BlockerResolver:
    """A turn handler that clears blockers. Run it last, because it skips any idle units."""

    name = "blockers"

    def __init__(self, policies: Mapping[str, Resolve] | None = None) -> None:
        self._policies = dict(policies or {})

    async def run(self, game: BlockerGame) -> None:
        unresolved = await self.resolve(game)
        if unresolved:
            raise UnresolvedBlockers(", ".join(blocker.type for blocker in unresolved))

    async def resolve(
        self, game: BlockerGame, diplomacy_only: bool = False, readers: tuple | None = None
    ) -> list[Blocker]:
        """Clear what's open and return what's left. A settled type isn't touched again."""
        if readers is None:
            readers = (_read_diplomacy,) if diplomacy_only else _ALL_READERS
        settled: set[str] = set()
        open_blockers = await _read_all(game, readers)
        for _ in range(MAX_PASSES):
            if not open_blockers:
                break
            # Units go last, because a skipped unit can't step off a stack.
            for blocker in sorted(open_blockers, key=lambda b: b.type == BlockerType.UNITS):
                if await self._act_on(game, blocker):
                    settled.add(blocker.type)
            found = await _read_all(game, readers)
            open_blockers = [blocker for blocker in found if blocker.type not in settled]
        return open_blockers

    async def _act_on(self, game: BlockerGame, blocker: Blocker) -> bool:
        policy = self._policies.get(blocker.type)
        default = DEFAULT_RESOLVERS.get(blocker.type)
        if policy is None and default is None:
            log.warning("No handler for blocker %s: %s", blocker.type, blocker.message)
        for resolve in (policy, default):
            if resolve is not None and await _attempt(resolve, game, blocker):
                return True
        return False


async def _read_all(game: BlockerGame, readers: tuple) -> list[Blocker]:
    """Read each source. If one fails the rest still count, as they do in `end_turn`."""
    blockers: list[Blocker] = []
    for read in readers:
        try:
            blockers += await read(game)
        except ConnectionError:
            raise  # The game is gone, which nothing here can work around.
        except Exception:
            log.warning("Reading blockers failed in %s", read.__name__, exc_info=True)
    # An open session is cast directly. The default voter only fires on the next session, so
    # queueing it first would hide the session blocker without clearing it.
    if any(blocker.type == BlockerType.WORLD_CONGRESS_SESSION for blocker in blockers):
        blockers = [b for b in blockers if b.type != BlockerType.WORLD_CONGRESS_VOTE]
    return blockers


async def _attempt(resolve: Resolve, game: BlockerGame, blocker: Blocker) -> bool:
    try:
        return await resolve(game, blocker)
    except ConnectionError:
        raise
    except Exception:
        log.exception("Handling blocker %s failed", blocker.type)
        return False


class ResolvingGame:
    """A game whose `end_turn` clears what it reports and tries again.

    Blockers can arrive after a turn is ended, and the turn loop stops on any turn that doesn't
    advance. Pass this to `Orchestrator` in place of the game. Anything else reaches the game.
    """

    def __init__(
        self,
        game: Any,
        resolver: BlockerResolver,
        defeat_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._game = game
        self._resolver = resolver
        self._defeat_check = defeat_check

    def __getattr__(self, name: str) -> Any:
        return getattr(self._game, name)

    async def end_turn(self) -> str:
        report = await self._watched_end_turn()
        for _ in range(MAX_END_TURN_RETRIES):
            if _has_line_starting(report, _DIPLOMACY_REPORTS):
                await self._clear(diplomacy_only=True)
            elif _has_line_starting(report, _BLOCKED_REPORTS):
                await self._clear(diplomacy_only=False)
            else:
                return report  # It advanced, or it's something for the turn loop to judge.
            log.info("Retrying end of turn after: %s", report.splitlines()[0])
            report = await self._watched_end_turn()
        return report

    async def dismiss_popup(self) -> str:
        """Answer open diplomacy, then dismiss popups.

        The orchestrator calls this when a turn did not advance. Run 5 stopped on turn 102 there:
        a leader's statement opened during the AI turn, `end_turn` returned an ordinary report,
        and dismissing the leader screen alone left the session open and unanswered. So any open
        session or deal is answered first, by the same rules as at the start of a turn.
        """
        await self._clear(diplomacy_only=True)
        return await self._game.dismiss_popup()

    async def _watched_end_turn(self) -> str:
        """Run `end_turn`, clearing what holds the turn while it waits. Never sends a duplicate."""
        turn_before = await self._turn()
        self._last_hold: tuple | None = None
        self._stray_seen: int | None = None
        self._submitted_on: int | None = None  # The turn a held session was last submitted on.
        task = asyncio.ensure_future(self._game.end_turn())
        delay = WATCH_FIRST_SECONDS
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=delay)
                if done:
                    return task.result()
                delay = WATCH_EVERY_SECONDS
                if await self._game_over():
                    # Run 4, turn 325: the defeat cinematic played while the watcher kept
                    # clearing a civic-slot hold and asking again for minutes.
                    task.cancel()
                    return GAME_OVER_REPORT
                if turn_before is not None:
                    await self._unstick(turn_before)
        except BaseException:
            task.cancel()  # Only on a lost connection or cancellation: nothing is left to wait for.
            raise

    async def _unstick(self, turn_before: int) -> None:
        """Close popups and clear blockers. Ask again only after clearing a blocker, and only if
        the game says the turn can end, since a request while the AI plays can skip a turn.

        A listed blocker means the game refused the request. A closed popup lets a held AI turn
        carry on by itself. `end_turn` keeps polling throughout and returns once the turn moves.
        """
        try:
            now = await self._turn()
            if now != turn_before:
                log.info("Watch tick: turn reads %s, not %s, so leaving it", now, turn_before)
                return
            dismissed = await self._game.dismiss_popup()
            readers = _WATCH_READERS
            blockers = await _read_all(self._game, readers)
            if "Dismissed" in dismissed:
                log.info("Turn %s was held, closed: %s", turn_before, dismissed[:200])
            # Closed only if seen on two checks in a row, so a screen opening as a session starts
            # is left alone.
            stray = await stray_diplomacy_view_open(self._game)
            log.info("Watch tick on turn %s: stray diplomacy screen=%s", turn_before, stray)
            if stray:
                if self._stray_seen == turn_before:
                    closed = await close_stray_diplomacy_view(self._game)
                    log.info("Turn %s: stray diplomacy screen, closed=%s", turn_before, closed)
                self._stray_seen = turn_before
            else:
                self._stray_seen = None
            if not blockers:
                self._last_hold = None
                return
            # Act only on a hold seen twice with the same turn. As a turn changes, the turn can
            # still read as the old one while the new turn's blockers show. Clearing those ended
            # turn 200 of Run 3 before the bot played it.
            hold = (turn_before, frozenset(blocker.type for blocker in blockers))
            if hold != self._last_hold:
                self._last_hold = hold
                if all(blocker.type in _DIPLOMACY_TYPES for blocker in blockers):
                    # Answering a session never ends a turn, so it needn't wait for a second look.
                    # It is not followed by a new end-turn request: the AI carries on by itself.
                    await self._resolver.resolve(self._game, readers=readers)
                    log.info("Turn %s: answered %s at once", turn_before, sorted(hold[1]))
                return
            await self._resolver.resolve(self._game, readers=readers)
            # A World Congress session can run as the next turn starts, while the turn still reads
            # as the old one. Then submitting lets the game carry on, and a new request ends the
            # new turn unplayed, as turn 303 of Run 3 was. Otherwise the session stays open until
            # the turn is requested again, as on turn 306. So ask again only if it's still open.
            session = any(b.type == BlockerType.WORLD_CONGRESS_SESSION for b in blockers)
            if session and self._submitted_on != turn_before:
                self._submitted_on = turn_before
                log.info("Turn %s was held by a World Congress session, submitted it", turn_before)
                return
            if await self._can_end_turn() and await self._turn() == turn_before:
                await self._game.execute_lua(build_end_turn(), "ingame")
                log.info(
                    "Turn %s was held by %s, cleared them and asked again to end it",
                    turn_before,
                    [blocker.type for blocker in blockers],
                )
        except ConnectionError:
            raise
        except Exception:
            log.warning("Clearing a held turn failed", exc_info=True)

    async def _game_over(self) -> bool:
        """Ask the defeat check, if there is one. A check that fails counts as not over."""
        if self._defeat_check is None:
            return False
        try:
            return await self._defeat_check()
        except ConnectionError:
            raise
        except Exception:
            log.warning("Game-over check failed during the end-turn wait", exc_info=True)
            return False

    async def _can_end_turn(self) -> bool:
        """True only while our turn is still open, so a new request can't be a duplicate."""
        output = await self._game.execute_lua(_CAN_END_TURN_LUA, "ingame")
        return "CAN_END_TURN|true" in output

    async def _turn(self) -> int | None:
        try:
            return (await self._game.get_game_overview()).turn
        except ConnectionError:
            raise
        except Exception:
            return None  # A defeat screen can refuse reads. Then just wait for end_turn.

    async def _clear(self, diplomacy_only: bool) -> None:
        """Clear what can be cleared. `end_turn` may still pass with something left open."""
        try:
            unresolved = await self._resolver.resolve(self._game, diplomacy_only)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Clearing blockers failed")
            return
        if unresolved:
            log.warning("Still blocked by %s", [blocker.type for blocker in unresolved])
