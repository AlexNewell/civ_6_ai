"""Answer every AI proposal by rule, and vote in the World Congress, with no model calls.

`DiplomacyPolicy` is a turn handler, and `blocker_policies()` hands the same instance to
`BlockerResolver` for the four blocker types it takes over. Keep one instance for a whole game.

What it does with each incoming proposal (see `respond`):

- Deals: priced by `DiplomacyWeights`. Accepted only if what we get is worth at least what we give
  plus `min_net_value`. Never accepted, whatever the price: any alliance, any joint war, and any
  city of ours. A strategic resource or gold given for nothing prices below zero, so it is refused.
  A deal whose items civ6-mcp cannot name (delegations, embassies, research agreements) has no
  price. civ6-mcp's query drops every item it has no name for, which hides every agreement but
  open borders, joint war and alliance (a third-party war or pact could be one). So before any
  acceptance a raw read lists every item of the deal, and a deal is refused if the raw count
  differs from the parsed count or the read fails. Only a deal with no parsed item at all can be
  accepted, and only if `accept_unreadable_deals` is set (off by default).
- Conversations with no deal: a first meeting or a delegation gets a friendly answer. Friendship
  is accepted unless we are at war with them, they are denounced, or nothing is known about them.
  Alliances, war talk and demands to stop something are refused.
- War declarations (a session while at war, with no deal) are closed, as `end_turn` does.

Bounds: `max_session_rounds` per conversation, `max_attempts_per_proposal` and
`max_responses_per_turn` per turn. Past a bound, a proposal is closed at once (a deal is refused, a
conversation is exited) instead of being negotiated. Refusals are remembered for
`refusal_memory_turns`, so the same offer is refused without being priced again. Every path ends by
closing the session, because a session left open can hold the game, and any failure inside one
proposal closes it and moves on to the next.

World Congress: the session opens and closes inside `end_turn`, and civ6-mcp refuses to end the
turn without a handler, so the votes must be chosen before the resolutions can be read.
`build_voter_lua` registers a handler that reads each resolution as it arrives and applies the
rules below. If the session is open already (`vote_in_open_session`), the same rules run in Python
over the resolutions that are listed.

- A resolution that gives or takes Diplomatic Victory Points is the only one voted on by default.
  Rivals at or above `strip_from_vp` are voted down (option B, when its text removes points), and
  we vote to give ourselves points. We never vote to give a rival points, and a rival is only
  ever the target of a vote that takes points away.
- Any other resolution is left unvoted, which costs no favor, unless `resolution_rules` names it.
- The first vote on a resolution is free. More are bought only where the rules ask, and never
  below `favor_reserve`.

civ6-mcp's own default voter votes option A on target 0 everywhere. On the Diplomatic Victory
resolution that can give a rival points. CivBench's published material in this repo says nothing
about diplomatic victory, so this rule comes from civ6-mcp's own warnings in `end_turn`: 20 points
win at once, and points must be stripped before a rival reaches 20.

Fog of war: only civs we have met are read or voted on. `get_diplomacy` and
`get_victory_progress` are both filtered to met civs, see `READ_AUDIT`.

`DiplomacyWeights` is the plain typed input the Phase 2 strategist will set.
"""

import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Protocol

from civ_mcp.lua import SENTINEL

from civ6_ai.blockers import (
    Blocker,
    BlockerType,
    action_succeeded,
    submit_world_congress_votes,
)

log = logging.getLogger(__name__)

POSITIVE = "POSITIVE"
NEGATIVE = "NEGATIVE"
EXIT = "EXIT"

DIPLOMATIC_VICTORY_HASH = 334823573  # Named in civ6-mcp's own World Congress guidance.
DIPLOMATIC_VICTORY_TYPE = "WC_RES_DIPLOMATIC_VICTORY"
_MAX_VOTE_OPTION = 2  # 1 is option A and 2 is option B, as `vote_world_congress` takes them.

# Localized names civ6-mcp prints for strategic resources.
DEFAULT_STRATEGICS = ("Horses", "Iron", "Niter", "Coal", "Oil", "Aluminum", "Uranium")

# Words in a leader's dialogue, checked in this order. They are English text from the game's UI, so
# a missed word falls through to the friendly default, and a wrong guess is refused or closed.
_ALLIANCE_WORDS = ("allian", "ally", "allies")
_WAR_WORDS = ("war", "denounce")
_DEMAND_WORDS = (
    "stop ",
    "settl",
    "remove",
    "leave",
    "withdraw",
    "troops",
    "tribute",
    "demand",
    "border",
)
_FRIEND_WORDS = ("friend",)
_LEADER_SAYS = re.compile(r'Leader says: "(.*)"')
# Text that says a resolution takes points away, or gives them. Lower case.
_REMOVES = re.compile(r"\b(?:lose|loses|remov\w*|minus|deduct\w*|reduc\w*|strip\w*)\b|-\s?\d")
_GRANTS = re.compile(r"\b(?:gain|gains|receive|receives|grant\w*|earn\w*)\b|\+\s?\d")
_POINTS = "victory point"
_TYPE_NAME = re.compile(r"^[A-Z0-9_]+$")


@dataclass(frozen=True)
class DiplomacyWeights:
    """How much each item is worth, and every bound. The numbers are not tuned against games."""

    # Value of what a deal gives or takes, in gold.
    gold_value: float = 1.0  # Per gold in a lump sum.
    gold_per_turn_factor: float = 0.8  # Per-turn gold is worth this share of amount x duration.
    luxury_value: float = 40.0  # Per resource unit.
    strategic_value: float = 60.0  # Per resource unit.
    favor_value: float = 3.0  # Per point of diplomatic favor.
    great_work_value: float = 120.0
    city_value: float = 2000.0  # Only ever counted for a city they give. Ours is never given.
    open_borders_value: float = 5.0  # Each way, so an even swap prices as even.
    min_net_value: float = 0.0  # A deal must return at least what it takes plus this.
    accept_unreadable_deals: bool = False
    strategic_resources: tuple[str, ...] = DEFAULT_STRATEGICS
    # Conversations.
    friendship_blocked_states: tuple[str, ...] = ("DENOUNCED", "WAR")
    denounce_memory_turns: int = 10  # A denounced civ stays blocked this long after it was seen.
    # Bounds.
    max_session_rounds: int = 4  # A first meeting takes 2 or 3.
    max_attempts_per_proposal: int = 2  # Per turn, before it is closed unanswered.
    max_responses_per_turn: int = 12
    refusal_memory_turns: int = 20
    max_remembered_refusals: int = 64
    # World Congress.
    strip_from_vp: int = 4  # A rival with this many Diplomatic Victory Points is voted down.
    strip_votes: int = 2
    urgent_vp: int = 12  # From here a rival is close to 20, so more favor is spent.
    urgent_votes: int = 4
    self_votes: int = 1
    favor_reserve: int = 0  # Favor never spent.
    # Other resolutions to vote on: (type without WC_RES_, option 1 or 2, votes). Target 0, and
    # never a resolution that targets a player.
    resolution_rules: tuple[tuple[str, int, int], ...] = ()

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "accept_unreadable_deals":
                if not isinstance(value, bool):
                    raise ValueError("accept_unreadable_deals must be True or False")
            elif isinstance(value, bool):
                raise ValueError(f"{item.name} must be a number")
            elif isinstance(value, int | float) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{item.name} must be a finite number of at least 0")
        if self.max_session_rounds < 1 or self.max_attempts_per_proposal < 1:
            raise ValueError("rounds and attempts must be at least 1")
        for name, option, votes in self.resolution_rules:
            if not _TYPE_NAME.match(name) or option not in (1, _MAX_VOTE_OPTION) or votes < 1:
                raise ValueError(f"bad resolution rule for {name!r}")


class DiplomacyGame(Protocol):
    """The part of civ6-mcp's `GameState` that this policy uses."""

    async def get_game_overview(self) -> Any: ...
    async def get_diplomacy(self) -> Any: ...
    async def get_victory_progress(self) -> Any: ...
    async def get_diplomacy_sessions(self) -> Any: ...
    async def diplomacy_respond(self, other_player_id: int, response: str) -> str: ...
    async def get_pending_deals(self) -> Any: ...
    async def respond_to_deal(self, other_player_id: int, accept: bool) -> str: ...
    async def get_world_congress(self) -> Any: ...
    async def vote_world_congress(
        self, resolution_hash: int, option: int, target_index: int, num_votes: int
    ) -> str: ...
    async def execute_lua(self, code: str, context: str = "gamecore") -> str: ...


@dataclass(frozen=True)
class Counterpart:
    """What we know of a civ we have met."""

    player_id: int
    at_war: bool
    state: str  # civ6-mcp's names: ALLIED, DECLARED_FRIEND, FRIENDLY, NEUTRAL, DENOUNCED, WAR...
    recently_denounced: bool = False


# --- Deals --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Judgement:
    accept: bool
    reason: str
    received: float = 0.0
    given: float = 0.0


def _is_strategic(name: str, weights: DiplomacyWeights) -> bool:
    wanted = name.removeprefix("RESOURCE_").casefold()
    return any(wanted == known.casefold() for known in weights.strategic_resources)


def _item_value(item: Any, weights: DiplomacyWeights) -> float:
    """What one item is worth to whoever receives it."""
    kind = item.item_type
    if kind == "GOLD":
        if item.duration > 0:
            return item.amount * item.duration * weights.gold_per_turn_factor * weights.gold_value
        return item.amount * weights.gold_value
    if kind == "RESOURCE":
        unit = (
            weights.strategic_value if _is_strategic(item.name, weights) else weights.luxury_value
        )
        return unit * max(1, item.amount)
    if kind == "FAVOR":
        return item.amount * weights.favor_value
    if kind == "GREAT_WORK":
        return weights.great_work_value * max(1, item.amount)
    if kind == "CITY":
        return weights.city_value
    if kind == "AGREEMENT" and item.name == "Open Borders":
        return weights.open_borders_value
    return 0.0


item_value = _item_value  # Public name, so trade offers price with the same rules.


def forbidden_item(item: Any, ours: bool) -> str | None:
    """Why an item can never be part of a deal we accept, or None."""
    name = item.name.casefold()
    if item.item_type == "AGREEMENT" and "allian" in name:
        return "an alliance"
    if item.item_type == "AGREEMENT" and "war" in name:
        return "a joint war"
    if item.item_type == "CITY" and ours:
        return "giving a city"
    if item.item_type == "UNKNOWN":
        return "an item civ6-mcp could not name"
    return None


def judge_deal(deal: Any, weights: DiplomacyWeights) -> Judgement:
    """Accept or refuse a pending deal. Pure: the same deal and weights give the same answer."""
    for ours, items in ((False, deal.items_from_them), (True, deal.items_from_us)):
        for item in items:
            banned = forbidden_item(item, ours)
            if banned:
                return Judgement(False, f"it includes {banned}")
    received = sum(_item_value(item, weights) for item in deal.items_from_them)
    given = sum(_item_value(item, weights) for item in deal.items_from_us)
    if not deal.items_from_them and not deal.items_from_us:
        if weights.accept_unreadable_deals:
            return Judgement(True, "it has no item we can read")
        return Judgement(False, "its items cannot be read")
    if received - given < weights.min_net_value:
        return Judgement(False, f"it returns {received:g} for {given:g}", received, given)
    return Judgement(True, f"it returns {received:g} for {given:g}", received, given)


# Lists every item of a pending deal, raw, found the way civ6-mcp's `build_pending_deals_query`
# finds it. That query drops each item it has no name for, so it hides every agreement but open
# borders, joint war and alliance. A hidden one could be a third-party war or a defensive pact.
# Only calls the query itself makes are used, and no agreement name is guessed.
_RAW_DEAL_LUA = """
local me = Game.GetLocalPlayer()
local i = @PLAYER@
local sid = DiplomacyManager.FindOpenSessionID(me, i)
if sid and sid >= 0 then
    local ok, deal = pcall(function()
        return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, i)
    end)
    if ok and deal then
        local count = deal:GetItemCount() or 0
        print("RAWDEAL|" .. i .. "|" .. count)
        for item in deal:Items() do
            local function get(f) local k, v = pcall(f); return k and tostring(v) or "?" end
            print("RAWITEM|" .. i .. "|" .. get(function() return item:GetFromPlayerID() end)
                .. "|" .. get(function() return item:GetType() end)
                .. "|" .. get(function() return item:GetSubType() end)
                .. "|" .. get(function() return item:GetValueType() end)
                .. "|" .. get(function() return item:GetAmount() end)
                .. "|" .. get(function() return item:GetDuration() end))
        end
    end
end
print("@SENTINEL@")
"""


def build_raw_deal_lua(player_id: int) -> str:
    return _RAW_DEAL_LUA.replace("@PLAYER@", str(int(player_id))).replace("@SENTINEL@", SENTINEL)


def parse_raw_deal(output: str, player_id: int) -> list[str] | None:
    """The raw item lines of the deal, or None if the read does not account for itself.

    The `RAWDEAL` line states the item count, and the items listed must match it.
    """
    count: int | None = None
    items: list[str] = []
    for line in output.splitlines():
        parts = line.strip().split("|")
        if parts[0] == "RAWDEAL" and len(parts) == 3 and parts[1] == str(player_id):
            count = int(parts[2]) if parts[2].isdigit() else None
        elif parts[0] == "RAWITEM" and len(parts) == 8 and parts[1] == str(player_id):
            items.append("|".join(parts[2:]))
    if count is None or count != len(items):
        return None
    return items


def hidden_items_reason(deal: Any, raw: list[str] | None, weights: DiplomacyWeights) -> str | None:
    """Why a deal must be refused for what civ6-mcp's query hid, or None if it hid nothing.

    The raw count must equal the parsed count. The one exception is a deal with no parsed item at
    all, which `accept_unreadable_deals` may accept.
    """
    if raw is None:
        return "its items could not be read in full"
    parsed = len(deal.items_from_them) + len(deal.items_from_us)
    if parsed == len(raw):
        return None
    if parsed == 0 and weights.accept_unreadable_deals:
        return None
    return f"{len(raw) - parsed} of its {len(raw)} items are hidden"


def deal_signature(deal: Any) -> tuple:
    """What makes two offers the same offer, for remembering a refusal."""

    def keys(items: Any) -> tuple:
        return tuple(sorted((i.item_type, i.name, i.amount, i.duration) for i in items))

    return ("deal", deal.other_player_id, keys(deal.items_from_them), keys(deal.items_from_us))


# --- Conversations ------------------------------------------------------------------------------


def classify_talk(text: str) -> str:
    """Sort a leader's words into alliance, war, demand, friendship or greeting."""
    lowered = text.casefold()
    for kind, words in (
        ("alliance", _ALLIANCE_WORDS),
        ("war", _WAR_WORDS),
        ("demand", _DEMAND_WORDS),
        ("friendship", _FRIEND_WORDS),
    ):
        if any(word in lowered for word in words):
            return kind
    return "greeting"


def answer_talk(kind: str, counterpart: Counterpart | None, weights: DiplomacyWeights) -> str:
    """POSITIVE or NEGATIVE for one kind of conversation."""
    if kind == "greeting":
        return POSITIVE
    if kind == "friendship":
        if counterpart is None:
            return NEGATIVE  # Nothing known about them, so we cannot tell if they are denounced.
        blocked = counterpart.state in weights.friendship_blocked_states
        if counterpart.at_war or counterpart.recently_denounced or blocked:
            return NEGATIVE
        return POSITIVE
    return NEGATIVE


# --- World Congress -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Vote:
    option: int  # 1 is A and 2 is B.
    target_index: int
    num_votes: int


@dataclass(frozen=True)
class VoteContext:
    """What a vote needs. `strip` is met rivals with their Diplomatic Victory Points."""

    my_id: int | None
    favor: int
    favor_costs: tuple[int, ...]  # Cumulative: costs[n - 1] is the favor for n votes.
    strip: tuple[tuple[int, int], ...] = ()


def _targets(resolution: Any) -> list[int | None]:
    """The player id of each target in game order. None where the target is not a player."""
    found: list[int | None] = []
    for target in resolution.possible_targets:
        head = target.split(":", 1)[0]
        found.append(int(head) if head.isdigit() else None)
    return found


def _names_players(resolution: Any) -> bool:
    """True unless the game says the targets are something else, such as yields or religions."""
    kind = resolution.target_kind.upper()
    return not kind or "PLAYER" in kind


def is_diplomatic_victory(resolution: Any) -> bool:
    return (
        resolution.resolution_hash == DIPLOMATIC_VICTORY_HASH
        or resolution.resolution_type == DIPLOMATIC_VICTORY_TYPE
    )


def _text_says(pattern: re.Pattern[str], text: str) -> bool:
    lowered = text.casefold()
    return _POINTS in lowered and pattern.search(lowered) is not None


def affordable_votes(wanted: int, favor: int, costs: tuple[int, ...], reserve: int) -> int:
    """The most votes up to `wanted` whose cumulative cost fits. One vote is always free."""
    votes = 1
    for count in range(2, wanted + 1):
        if count - 1 >= len(costs) or costs[count - 1] > favor - reserve:
            break
        votes = count
    return votes


def strip_votes_for(points: int, weights: DiplomacyWeights) -> int:
    """How many votes to spend taking points from a rival that has `points`."""
    return weights.urgent_votes if points >= weights.urgent_vp else weights.strip_votes


def choose_vote(resolution: Any, context: VoteContext, weights: DiplomacyWeights) -> Vote | None:
    """The vote for one resolution, or None to leave it unvoted. Pure.

    A vote that adds Diplomatic Victory Points can only ever name ourselves. A rival is named only
    by a vote whose text removes points, and only when it has `strip_from_vp` or more.
    """
    targets = _targets(resolution)
    if is_diplomatic_victory(resolution):
        if _text_says(_REMOVES, resolution.effect_b):
            for rival, points in context.strip:
                if rival in targets and points >= weights.strip_from_vp:
                    votes = affordable_votes(
                        strip_votes_for(points, weights),
                        context.favor,
                        context.favor_costs,
                        weights.favor_reserve,
                    )
                    return Vote(2, targets.index(rival), votes)
        if (
            context.my_id is not None
            and context.my_id in targets
            and _text_says(_GRANTS, resolution.effect_a)
            and not _text_says(_REMOVES, resolution.effect_a)
        ):
            votes = affordable_votes(
                weights.self_votes, context.favor, context.favor_costs, weights.favor_reserve
            )
            return Vote(1, targets.index(context.my_id), votes)
        return None
    if _names_players(resolution):
        return None  # A rule never names a player.
    short = resolution.resolution_type.removeprefix("WC_RES_")
    for name, option, wanted in weights.resolution_rules:
        if name == short:
            votes = affordable_votes(
                wanted, context.favor, context.favor_costs, weights.favor_reserve
            )
            return Vote(option, 0, votes)
    return None


def _lua_number(value: int) -> str:
    return str(int(value))


def build_voter_lua(strip: tuple[tuple[int, int], ...], weights: DiplomacyWeights) -> str:
    """Lua that registers the one-shot World Congress voter, shaped like civ6-mcp's own.

    `strip` is (player id, votes) pairs. It sets `__civmcp_wc_handler`, which is what civ6-mcp's
    `end_turn` looks for before it lets a turn with a congress end. Each resolution is handled
    in `pcall`, so a bad one costs only its own vote, and the submit and the cleanup always run.
    Only numbers and checked type names are written into the text.
    """
    rival_rows = ", ".join(
        f"{{id={_lua_number(rival)}, v={_lua_number(votes)}}}" for rival, votes in strip
    )
    rule_rows = ", ".join(
        f'["WC_RES_{name}"] = {{o={_lua_number(option)}, v={_lua_number(votes)}}}'
        for name, option, votes in weights.resolution_rules
    )
    return (
        _VOTER_LUA.replace("@STRIP@", rival_rows)
        .replace("@RULES@", rule_rows)
        .replace("@RESERVE@", _lua_number(weights.favor_reserve))
        .replace("@SELF_VOTES@", _lua_number(weights.self_votes))
        .replace("@DV_HASH@", _lua_number(DIPLOMATIC_VICTORY_HASH))
        .replace("@SENTINEL@", SENTINEL)
    )


# Not run against a game yet, see the live checks in the PR that added it.
_VOTER_LUA = """
if __civmcp_wc_handler then
    pcall(function() Events.WorldCongressStage1.Remove(__civmcp_wc_handler) end)
    __civmcp_wc_handler = nil
end
__civmcp_wc_votes = nil

local STRIP = {@STRIP@}
local RULES = {@RULES@}
local RESERVE = @RESERVE@
local SELF_VOTES = @SELF_VOTES@
local DV_HASH = @DV_HASH@

local function mentions(text, words)
    for _, w in ipairs(words) do
        if string.find(text, w, 1, true) then return true end
    end
    return false
end
local function removes(text)
    return string.find(text, "victory point", 1, true)
        and (mentions(text, {"lose", "remov", "minus", "deduct", "reduc", "strip"})
             or string.find(text, "%-%s?%d"))
end
local function grants(text)
    return string.find(text, "victory point", 1, true) and not removes(text)
        and (mentions(text, {"gain", "receive", "grant", "earn"}) or string.find(text, "%+%s?%d"))
end

local function handler()
    local me = Game.GetLocalPlayer()
    local wc = Game.GetWorldCongress()
    if not wc or not wc:IsInSession() then return end
    local favor = Players[me]:GetFavor() - RESERVE
    local costs = wc:GetVotesandFavorCost()
    local maxV = costs.MaxVotes or 5

    local function cast(res, option, targetIdx, wanted)
        local votes, cost = 1, 0
        for n = 2, math.min(wanted, maxV) do
            local total = costs[n - 1] or 99999
            if total <= favor then votes, cost = n, total else break end
        end
        favor = favor - cost
        local kParams = {}
        kParams[PlayerOperations.PARAM_RESOLUTION_TYPE] = res.Type
        kParams[PlayerOperations.PARAM_WORLD_CONGRESS_VOTES] = votes
        kParams[PlayerOperations.PARAM_RESOLUTION_OPTION] = option
        kParams[PlayerOperations.PARAM_RESOLUTION_SELECTION] = targetIdx
        UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_RESOLUTION_VOTE, kParams)
    end

    local ress = wc:GetResolutions()
    if ress then
        for _, res in ipairs(ress) do
            pcall(function()
                local row = nil
                for r in GameInfo.Resolutions() do
                    if r.Hash == res.Type then row = r end
                end
                local typeName = row and row.ResolutionType or ""
                local isPlayer = (res.TargetType == "PlayerType")
                if res.Type == DV_HASH or typeName == "WC_RES_DIPLOMATIC_VICTORY" then
                    if not (isPlayer and res.PossibleTargets) then return end
                    local effA, effB = "", ""
                    if row and row.Effect1Description then
                        effA = string.lower(Locale.Lookup(row.Effect1Description))
                    end
                    if row and row.Effect2Description then
                        effB = string.lower(Locale.Lookup(row.Effect2Description))
                    end
                    if removes(effB) then
                        for _, s in ipairs(STRIP) do
                            for ti, tgt in ipairs(res.PossibleTargets) do
                                if tonumber(tgt) == s.id then
                                    cast(res, 2, ti - 1, s.v)
                                    return
                                end
                            end
                        end
                    end
                    if grants(effA) then
                        for ti, tgt in ipairs(res.PossibleTargets) do
                            if tonumber(tgt) == me then
                                cast(res, 1, ti - 1, SELF_VOTES)
                                return
                            end
                        end
                    end
                    return
                end
                local rule = RULES[typeName]
                if rule and not isPlayer then cast(res, rule.o, 0, rule.v) end
            end)
        end
    end

    UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_SUBMIT_TURN, {})
    pcall(function() Events.WorldCongressStage1.Remove(__civmcp_wc_handler) end)
    __civmcp_wc_handler = nil
end

__civmcp_wc_handler = handler
Events.WorldCongressStage1.Add(handler)
print("OK:WC_VOTER_REGISTERED")
print("@SENTINEL@")
"""


# --- The policy ---------------------------------------------------------------------------------


@dataclass
class _TurnState:
    """Counters that start again each turn."""

    turn: int = -1
    responses: int = 0
    attempts: dict[tuple, int] = field(default_factory=dict)


class DiplomacyPolicy:
    """A turn handler for diplomacy and the World Congress. Keep one instance for a whole game.

    `run` must be registered as a turn handler, because it starts each turn's bounds. The
    blocker functions only use what it last read, so they never query the game for anything but
    the sessions and deals themselves while the AI may still be taking its turn.
    """

    name = "diplomacy"

    def __init__(self, weights: DiplomacyWeights | None = None) -> None:
        self.weights = weights or DiplomacyWeights()
        self._state = _TurnState()
        self._counterparts: dict[int, Counterpart] = {}
        self._denounced_at: dict[int, int] = {}
        self._refused: dict[tuple, int] = {}  # Signature -> turn it was last refused.
        self._my_id: int | None = None

    # --- Turn handler ---

    async def run(self, game: DiplomacyGame) -> None:
        overview = await game.get_game_overview()
        self._begin_turn(overview.turn)
        self._my_id = overview.player_id
        try:
            await self._read_counterparts(game)
        except ConnectionError:
            raise
        except Exception:
            log.warning("Reading diplomatic standing failed", exc_info=True)
        if not await self.respond(game):
            raise RuntimeError("Diplomacy policy left a proposal unanswered")

    def _begin_turn(self, turn: int) -> None:
        if turn != self._state.turn:  # Also true if the game is reloaded to an earlier turn.
            self._state = _TurnState(turn=turn)
        cutoff = turn - self.weights.refusal_memory_turns
        self._refused = {sig: seen for sig, seen in self._refused.items() if seen >= cutoff}
        while len(self._refused) > self.weights.max_remembered_refusals:
            oldest = min(self._refused, key=lambda sig: self._refused[sig])
            del self._refused[oldest]

    async def _read_counterparts(self, game: DiplomacyGame) -> None:
        met = {}
        for civ in await game.get_diplomacy():
            if not civ.has_met:
                continue
            state = civ.diplomatic_state.upper()
            if state == "DENOUNCED":
                self._denounced_at[civ.player_id] = self._state.turn
            met[civ.player_id] = (civ.is_at_war, state)
        self._counterparts = {
            pid: Counterpart(pid, at_war, state, self._recently_denounced(pid))
            for pid, (at_war, state) in met.items()
        }

    def _recently_denounced(self, player_id: int) -> bool:
        seen = self._denounced_at.get(player_id)
        return seen is not None and self._state.turn - seen <= self.weights.denounce_memory_turns

    def _counterpart(self, player_id: int) -> Counterpart | None:
        found = self._counterparts.get(player_id)
        if found is None:
            return None
        return Counterpart(
            found.player_id, found.at_war, found.state, self._recently_denounced(player_id)
        )

    # --- Proposals ---

    async def respond(self, game: DiplomacyGame) -> bool:
        """Answer every open session and deal, and close each one. True if none is left open."""
        if self._state.turn < 0:  # `run` has not started a turn, so nothing else would reset these.
            self._state = _TurnState()
        sessions = list(await game.get_diplomacy_sessions())
        try:
            deals = {deal.other_player_id: deal for deal in await game.get_pending_deals()}
        except ConnectionError:
            raise
        except Exception:
            log.warning("Reading pending deals failed", exc_info=True)
            deals = {}
        handled: set[int] = set()
        for session in sorted(sessions, key=lambda s: s.other_player_id):
            handled.add(session.other_player_id)
            deal = deals.pop(session.other_player_id, None)
            await self._guarded(
                game, session.other_player_id, self._answer_session(game, session, deal)
            )
        for player_id, deal in sorted(deals.items()):  # A deal that shows no session.
            handled.add(player_id)
            await self._guarded(game, player_id, self._answer_deal(game, player_id, deal, None))
        return await self._close_leftovers(game, handled)

    async def _guarded(self, game: DiplomacyGame, player_id: int, work: Awaitable[None]) -> None:
        """Run one proposal's answer. If it fails, close the session and go on."""
        try:
            await work
        except ConnectionError:
            raise
        except Exception:
            log.exception("Answering player %s failed, closing the session", player_id)
            await self._exit(game, player_id)

    async def _close_leftovers(self, game: DiplomacyGame, handled: set[int]) -> bool:
        """A session still open for a player we just answered is closed, once."""
        try:
            left = [s for s in await game.get_diplomacy_sessions() if s.other_player_id in handled]
        except ConnectionError:
            raise
        except Exception:
            log.warning("Checking for open sessions failed", exc_info=True)
            return False
        for session in left:
            log.warning("Session with player %s was still open, exiting", session.other_player_id)
            await self._exit(game, session.other_player_id)
        if not left:
            return True
        try:
            return not [
                s for s in await game.get_diplomacy_sessions() if s.other_player_id in handled
            ]
        except ConnectionError:
            raise
        except Exception:
            return False

    async def _exit(self, game: DiplomacyGame, player_id: int) -> bool:
        try:
            return action_succeeded(await game.diplomacy_respond(player_id, EXIT))
        except ConnectionError:
            raise
        except Exception:
            log.exception("Closing the session with player %s failed", player_id)
            return False

    def _within_bounds(self, signature: tuple) -> bool:
        """Count this response. False once the proposal or the turn has had its share."""
        state = self._state
        state.responses += 1
        state.attempts[signature] = state.attempts.get(signature, 0) + 1
        within = (
            state.responses <= self.weights.max_responses_per_turn
            and state.attempts[signature] <= self.weights.max_attempts_per_proposal
        )
        if not within:
            log.warning("Closing %s unanswered, its bound for this turn is reached", signature[:2])
        return within

    def _remember_refusal(self, signature: tuple) -> None:
        self._refused[signature] = self._state.turn

    async def _answer_session(self, game: DiplomacyGame, session: Any, deal: Any | None) -> None:
        player_id = session.other_player_id
        if session.deal_summary or deal is not None:
            await self._answer_deal(game, player_id, deal, session)
            return
        if session.is_at_war:  # A war declaration. It cannot be declined, and end_turn exits it.
            log.info("Player %s declared war, closing the session", player_id)
            await self._exit(game, player_id)
            return
        await self._answer_talk(game, session)

    async def _answer_deal(
        self, game: DiplomacyGame, player_id: int, deal: Any | None, session: Any | None
    ) -> None:
        if deal is None:
            # The session shows a deal that the deal query did not return, so it can't be priced.
            summary = session.deal_summary if session is not None else ""
            signature: tuple = ("unpriced", player_id, summary)
            reason = "it could not be read"
            accept = False
        else:
            signature = deal_signature(deal)
            judgement = judge_deal(deal, self.weights)
            accept, reason = judgement.accept, judgement.reason
            if signature in self._refused:
                accept, reason = False, "it was refused before"
        unverified = False
        if accept and deal is not None:
            reason, accept, unverified = await self._verify_items(game, player_id, deal, reason)
        bounded = not self._within_bounds(signature)
        if bounded:
            accept, reason = False, "the bound for this turn is reached"
        log.info(
            "Deal from player %s %s: %s", player_id, "accepted" if accept else "refused", reason
        )
        result = await game.respond_to_deal(player_id, accept)
        if (
            not accept and not bounded and not unverified
        ):  # A bound is no judgement of the offer, so it is not kept.
            self._remember_refusal(signature)
        if not action_succeeded(result):
            log.warning("The game refused our answer to the deal: %s", result)
            await self._exit(game, player_id)

    async def _verify_items(
        self, game: DiplomacyGame, player_id: int, deal: Any, reason: str
    ) -> tuple[str, bool, bool]:
        """Check an acceptable deal against the raw items. Returns reason, accept, unverified."""
        raw: list[str] | None = None
        try:
            output = await game.execute_lua(build_raw_deal_lua(player_id), "ingame")
            raw = parse_raw_deal(output, player_id)
        except ConnectionError:
            raise
        except Exception:
            log.warning("Reading the raw items of the deal failed", exc_info=True)
        log.info("Raw items of the deal from player %s: %s", player_id, raw)
        problem = hidden_items_reason(deal, raw, self.weights)
        if problem is None:
            return reason, True, False
        return problem, False, raw is None  # A failed read says nothing about the offer.

    async def _answer_talk(self, game: DiplomacyGame, session: Any) -> None:
        player_id = session.other_player_id
        counterpart = self._counterpart(player_id)
        text = f"{session.dialogue_text} {session.reason_text}"
        kind = classify_talk(text)
        signature: tuple = ("talk", player_id, kind)
        answer = answer_talk(kind, counterpart, self.weights)
        if signature in self._refused and kind != "friendship":
            answer = NEGATIVE  # A friendship answer depends on standing now, so it is judged again.
        if not self._within_bounds(signature):
            await self._exit(game, player_id)
            return
        for _ in range(self.weights.max_session_rounds):
            if answer == NEGATIVE:
                self._remember_refusal(signature)
            result = await game.diplomacy_respond(player_id, answer)
            if not action_succeeded(result):
                log.warning("The game refused our answer to player %s: %s", player_id, result)
                break
            if "SESSION_CONTINUES" not in result:
                return  # It closed.
            if answer == NEGATIVE:
                break  # One refusal is enough. Do not argue.
            said = _LEADER_SAYS.search(result)
            if said:  # The conversation has moved on, so judge what is being asked now.
                kind = classify_talk(said.group(1))
                answer = answer_talk(kind, counterpart, self.weights)
        # Too many rounds, a refusal, or an error. Never leave it open.
        log.info("Closing the conversation with player %s", player_id)
        await self._exit(game, player_id)

    # --- World Congress ---

    async def _vote_context(self, game: DiplomacyGame, status: Any) -> VoteContext:
        """Favor, vote costs and the met rivals worth voting down."""
        strip: tuple[tuple[int, int], ...] = ()
        try:
            met = {civ.player_id for civ in await game.get_diplomacy() if civ.has_met}
            progress = (await game.get_victory_progress()).players
            ranked = sorted(
                ((p.player_id, p.diplomatic_vp) for p in progress if p.player_id in met),
                key=lambda pair: (-pair[1], pair[0]),
            )
            strip = tuple(pair for pair in ranked if pair[1] >= self.weights.strip_from_vp)
            if self._my_id is None:
                self._my_id = (await game.get_game_overview()).player_id
        except ConnectionError:
            raise
        except Exception:
            log.warning("Reading rivals' Diplomatic Victory Points failed", exc_info=True)
        return VoteContext(self._my_id, status.favor, tuple(status.favor_costs), strip)

    async def queue_voter(self, game: DiplomacyGame, blocker: Blocker) -> bool:
        """Register the voter before `end_turn`. If a session is open already, vote in it."""
        status = await game.get_world_congress()
        if status.is_in_session:
            return await self._vote_now(game, status)
        return await self._register_voter(game, status)

    async def _register_voter(self, game: DiplomacyGame, status: Any) -> bool:
        context = await self._vote_context(game, status)
        strip = tuple(
            (rival, strip_votes_for(points, self.weights)) for rival, points in context.strip
        )
        output = await game.execute_lua(build_voter_lua(strip, self.weights), "ingame")
        if "OK:WC_VOTER_REGISTERED" not in output:
            log.warning(
                "The World Congress voter was not registered, so civ6-mcp's default voter runs. "
                "It can give a rival Diplomatic Victory Points: %s",
                output[:200],
            )
            return False
        return True

    async def vote_in_open_session(self, game: DiplomacyGame, blocker: Blocker) -> bool:
        """Vote on each open resolution by the rules, then submit. The submit always runs."""
        status = await game.get_world_congress()
        if not status.is_in_session:
            return await self._register_voter(game, status)
        return await self._vote_now(game, status)

    async def _vote_now(self, game: DiplomacyGame, status: Any) -> bool:
        # civ6-mcp's end_turn refuses while a session is open unless a voter is registered, even
        # after these votes are submitted. Without this, a live game stuck on its first session.
        try:
            registered = await self._register_voter(game, status)
        except ConnectionError:
            raise
        except Exception:
            # Still vote and submit: an unsubmitted session holds the turn whatever happens here.
            log.exception("Registering the World Congress voter failed")
            registered = False
        context = await self._vote_context(game, status)
        favor = context.favor
        failed = False
        try:
            for resolution in status.resolutions:
                if resolution.is_passed:
                    continue
                vote = choose_vote(
                    resolution,
                    VoteContext(context.my_id, favor, context.favor_costs, context.strip),
                    self.weights,
                )
                if vote is None:
                    continue
                result = await game.vote_world_congress(
                    resolution.resolution_hash, vote.option, vote.target_index, vote.num_votes
                )
                if action_succeeded(result):
                    if vote.num_votes <= len(context.favor_costs):
                        favor -= context.favor_costs[vote.num_votes - 1]
                else:
                    failed = True
                    log.warning("Vote on %s was refused: %s", resolution.name, result)
        except ConnectionError:
            raise
        except Exception:
            failed = True
            log.exception("Voting failed, submitting what was cast")
        finally:
            await submit_world_congress_votes(game)  # An open session holds the turn.
        if failed:
            log.warning("Some World Congress votes were not cast")
        return registered  # False lets the default voter register, which end_turn also accepts.

    def blocker_policies(self) -> Mapping[str, Callable[[Any, Blocker], Awaitable[bool]]]:
        """Policies for `BlockerResolver`. Use the same instance as the turn handler."""

        async def answer(game: Any, blocker: Blocker) -> bool:
            return await self.respond(game)

        return {
            BlockerType.DIPLOMACY_SESSION: answer,
            BlockerType.TRADE_DEAL: answer,
            BlockerType.WORLD_CONGRESS_VOTE: self.queue_voter,
            BlockerType.WORLD_CONGRESS_SESSION: self.vote_in_open_session,
        }


def blocker_policies(
    policy: DiplomacyPolicy | None = None,
) -> Mapping[str, Callable[[Any, Blocker], Awaitable[bool]]]:
    """Policies for `BlockerResolver`. Pass the turn handler's policy so the bounds are shared."""
    return (policy or DiplomacyPolicy()).blocker_policies()
