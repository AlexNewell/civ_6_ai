"""Track every war we are in, and offer peace, with no model calls.

`WarPolicy` is a turn handler. Register it right after `DiplomacyPolicy`. It moves no unit and
spends nothing. Keep one instance for a whole game.

Tracking, each turn:

- `get_diplomacy` lists the civs we have met, with whether each is at war with us.
- `get_city_states` lists each city-state with its suzerain (`suzerain_id`, -1 for none).
- civ6-mcp has no read of a city-state's own war state. So a city-state counts as at war with us
  when its suzerain is. This is the rule from issue 88 (a city-state joins its suzerain's wars),
  and Run 3 could not confirm it. A city-state at war with us on its own, with a suzerain we are
  at peace with, cannot be seen by this read.
- A log line is written when a war starts and when it ends, with the turn it was first seen. A
  one-line summary is written only when the set of wars changes.
- A suzerain change is logged once, after the new suzerain has held for `suzerain_settle_turns`
  turns. A city-state that flips and flips back inside that time is not logged (Run 6 logged 49
  lines for one city-state flipping between 'none' and the same rival). The count is kept in the
  state file, so a restart does not reset it. A rival (another civ, not us) becoming suzerain is
  flagged as an early warning, and more strongly if that civ is at war with us, because the
  city-state then joins that war. The war itself is still tracked from the suzerain read each
  turn, so a short flip changes a city-state's war at once.
- State is kept in memory, and also in `state_path` (a small JSON file) when one is given. A
  restarted run reads it back, so each war keeps the turn it was first seen. A war that began
  while the bot was down is then first seen on the first turn after, and the log says "first
  seen". Because the turns between are unknown, a war first seen after a gap of more than one
  turn is flagged "while the bot was down" (`War.late`): its true start is some turn between the
  two, and it is offered peace at once instead of waiting out the war age rule. Without a file,
  or with one from a later turn (another game or an older save), a restarted run finds the wars
  again as "start unknown", because civ6-mcp does not read when a war began.

Peace, each turn, for each civ at war with us:

- The bot has no planned attack (`MilitaryPolicy` only defends and clears camps), so the rule is
  simple: offer peace whenever the game allows it. A war first seen fewer than `min_war_age` turns
  ago is left alone, because the game refuses peace early in a war (civ6-mcp words it as a 10-turn
  war cooldown). A war with an unknown start is tried at once.
- `propose_peace` checks the war state right after sending the offer, but the AI answers a little
  later, so a `REJECTED` answer can still turn into peace on the next turn (Run 4, turn 233 to
  234: logged as refused, and the war was gone at the next read). A war that ends within two turns
  of a refused offer is logged as ending after that offer.
- At most one offer per opponent every `offer_interval` turns, counting offers the game refused
  with an error. At most `max_offers_per_turn` offers per turn.
- Every offer and its answer is logged. `propose_peace` answers `ACCEPTED|...`, `REJECTED|...`,
  or `Error: CODE|text` (`NOT_AT_WAR`, `CANNOT_MAKE_PEACE`, `NO_SESSION`). Any other text, or an
  exception, is logged and counts as a refusal. `propose_peace` closes its own session, so no
  diplomacy session is left open by this policy.
- Peace with a suzerain is not assumed to end its city-states' wars. The turn after it, each
  city-state that was counted at war is asked once for peace. `Error: NOT_AT_WAR` confirms it is
  at peace, and the answer is logged either way. This is bounded by the same interval.

Other policies read `at_war_with(player_id)`, and `wars()` for the whole table.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

NO_SUZERAIN = -1


class WarGame(Protocol):
    """The part of civ6-mcp's `GameState` this policy uses."""

    async def get_game_overview(self) -> Any: ...

    async def get_diplomacy(self) -> list[Any]: ...

    async def get_city_states(self) -> Any: ...

    async def propose_peace(self, other_player_id: int) -> str: ...


@dataclass(frozen=True)
class WarSettings:
    offer_interval: int = 5  # Turns between offers to the same opponent.
    min_war_age: int = 10  # The game's war cooldown. An unknown start skips it.
    max_offers_per_turn: int = 4
    suzerain_settle_turns: int = 3  # Turns a new suzerain must hold before the change is logged.


@dataclass(frozen=True)
class War:
    """One opponent at war with us. `via` is the suzerain's id for a city-state, else None."""

    player_id: int
    name: str
    started: int | None  # Turn first seen at war, None if the war was found on a first read.
    via: int | None = None
    late: bool = False  # First seen after a gap while the bot was down, so the start is a guess.


@dataclass
class WarPolicy:
    settings: WarSettings = field(default_factory=WarSettings)
    name: str = "wars"
    state_path: Path | None = None  # Where wars are kept between runs. None keeps them in memory.
    _wars: dict[int, War] = field(default_factory=dict, init=False, repr=False)
    _suzerains: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _offered: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _checked: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _names: dict[int, str] = field(default_factory=dict, init=False, repr=False)
    # city-state id -> (suzerain id it moved to, turn first seen there), not yet logged.
    _pending: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _refused: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _down_since: int | None = field(default=None, init=False, repr=False)
    _last_turn: int | None = field(default=None, init=False, repr=False)
    _summary: str | None = field(default=None, init=False, repr=False)

    # ----- query for other policies -----

    def at_war_with(self, player_id: int) -> bool:
        """True if the last run found us at war with this civ, or with this city-state."""
        return player_id in self._wars

    def wars(self) -> Mapping[int, War]:
        """A copy of the wars found by the last run, keyed by player id."""
        return dict(self._wars)

    # ----- turn handler -----

    async def run(self, game: WarGame) -> None:
        overview = await game.get_game_overview()
        turn = overview.turn
        if self._last_turn is not None and turn < self._last_turn:
            log.info("wars: turn went back from %s to %s, starting over", self._last_turn, turn)
            self._reset()
        if self._last_turn is None:
            self._load(turn)
        self._last_turn = turn
        try:
            majors = await game.get_diplomacy()
            states = await self._city_states(game, turn)
        except ConnectionError:
            raise
        except Exception:
            log.warning("wars: reading wars failed, keeping the last known wars", exc_info=True)
            return
        if not majors:
            return  # An empty read may be a timeout, so nothing is learned from it.
        before = dict(self._wars)
        first = self._summary is None
        self._update(turn, overview.player_id, majors, states, first)
        self._log_changes(turn, before)
        self._save(turn)
        try:
            await self._make_peace(game, turn, before)
        except ConnectionError:
            raise
        except Exception:
            log.warning("wars: peace step failed", exc_info=True)

    def _load(self, turn: int) -> None:
        """Read back the wars of an earlier run of this game. Anything doubtful is left out."""
        if self.state_path is None:
            return
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            if saved["turn"] > turn:
                log.info(
                    "wars: saved wars are from turn %s, after turn %s, so not used",
                    saved["turn"],
                    turn,
                )
                return
            wars = {int(w["player_id"]): War(**w) for w in saved["wars"]}
            suzerains = {int(pid): int(boss) for pid, boss in saved["suzerains"].items()}
            pending = {
                int(pid): (int(p[0]), int(p[1])) for pid, p in saved.get("pending", {}).items()
            }
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("wars: could not read %s, starting without it", self.state_path)
            return
        self._wars = wars
        self._suzerains = suzerains
        self._pending = pending
        if turn - saved["turn"] > 1:
            self._down_since = saved["turn"]
        self._summary = self._summary_of(wars)
        log.info(
            "wars: turn %s: %s war(s) read back from turn %s: %s",
            turn,
            len(wars),
            saved["turn"],
            self._summary,
        )

    def _save(self, turn: int) -> None:
        """Write the wars so a restart keeps their start turns. A failure only costs that."""
        if self.state_path is None:
            return
        saved = {
            "turn": turn,
            "wars": [asdict(war) for war in self._wars.values()],
            "suzerains": {str(pid): boss for pid, boss in self._suzerains.items()},
            "pending": {str(pid): list(p) for pid, p in self._pending.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_name(self.state_path.name + ".tmp")
            temp.write_text(json.dumps(saved, sort_keys=True), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError:
            log.warning("wars: could not save %s", self.state_path, exc_info=True)

    @staticmethod
    def _summary_of(wars: Mapping[int, War]) -> str:
        names = ", ".join(f"{w.name}{'*' if w.via is not None else ''}" for w in wars.values())
        return names or "none"

    def _reset(self) -> None:
        self._wars.clear()
        self._suzerains.clear()
        self._pending.clear()
        self._refused.clear()
        self._down_since = None
        self._offered.clear()
        self._checked.clear()
        self._summary = None

    async def _city_states(self, game: WarGame, turn: int) -> list[Any]:
        try:
            status = await game.get_city_states()
        except ConnectionError:
            raise
        except Exception:
            log.warning("wars: reading city-states failed", exc_info=True)
            return []
        tokens = getattr(status, "tokens_available", None)
        if isinstance(tokens, int):  # Same read as above, logged for the effectiveness checks.
            log.info("envoy_tokens turn %d: %d available", turn, tokens)
        return list(getattr(status, "city_states", None) or [])

    # ----- tracking -----

    def _update(
        self, turn: int, me: int, majors: list[Any], states: list[Any], first: bool
    ) -> None:
        # A war found on the first read has no known start. After a read-back state it isn't first.
        seen = None if first else turn
        late = self._down_since is not None
        self._down_since = None
        wars: dict[int, War] = {}
        for civ in majors:
            self._names[civ.player_id] = civ.civ_name
            if civ.has_met and civ.is_at_war:
                wars[civ.player_id] = self._wars.get(civ.player_id) or War(
                    civ.player_id, civ.civ_name, seen, late=late
                )
        for cs in states:
            self._names[cs.player_id] = cs.name
            self._note_suzerain(turn, me, cs, wars)
            boss = wars.get(cs.suzerain_id) if cs.suzerain_id != NO_SUZERAIN else None
            if boss is not None:
                old = self._wars.get(cs.player_id)
                keep = old is not None and old.via == boss.player_id
                wars[cs.player_id] = (
                    old
                    if keep and old
                    else War(cs.player_id, cs.name, seen, boss.player_id, late=late)
                )
        self._wars = wars

    def _label(self, pid: int, me: int) -> str:
        if pid == NO_SUZERAIN:
            return "none"
        return "us" if pid == me else f"{self._names.get(pid, 'player')} ({pid})"

    def _note_suzerain(self, turn: int, me: int, cs: Any, wars: dict[int, War]) -> None:
        new = cs.suzerain_id
        old = self._suzerains.get(cs.player_id)
        if old is None:
            self._suzerains[cs.player_id] = new  # The first read sets the baseline.
            return
        if new == old:
            self._pending.pop(cs.player_id, None)  # It flipped back: nothing to report.
            return
        target, since = self._pending.get(cs.player_id, (new, turn))
        if target != new:
            target, since = new, turn  # It moved on again: count from now.
        self._pending[cs.player_id] = (target, since)
        if turn - since < self.settings.suzerain_settle_turns:
            return
        del self._pending[cs.player_id]
        self._suzerains[cs.player_id] = new
        text = (
            f"wars: turn {turn}: {cs.name} suzerain changed from "
            f"{self._label(old, me)} to {self._label(new, me)} (held since turn {since})"
        )
        if new in (NO_SUZERAIN, me):
            log.info(text)
        elif new in wars:
            log.warning("%s. Early warning: a rival at war with us is now its suzerain", text)
        else:
            log.warning("%s. Early warning: a rival is now its suzerain", text)

    def _log_changes(self, turn: int, before: Mapping[int, War]) -> None:
        for pid, war in self._wars.items():
            if pid in before:
                continue
            known = "start unknown" if war.started is None else f"first seen turn {war.started}"
            if war.late:
                known += " (declared while the bot was down, so some turn before that)"
            kind = "city-state" if war.via is not None else "civ"
            via = (
                f" via suzerain {self._names.get(war.via, war.via)}" if war.via is not None else ""
            )
            log.info(
                "wars: turn %s: war with %s %s (%s)%s, %s", turn, kind, war.name, pid, via, known
            )
        for pid, war in before.items():
            if pid not in self._wars:
                age = "age unknown" if war.started is None else f"lasted {turn - war.started} turns"
                offer = self._refused.pop(pid, None)
                after = ""
                if offer is not None and turn - offer <= 2:
                    after = f", after our peace offer on turn {offer} (the game answers late)"
                log.info(
                    "wars: turn %s: war with %s (%s) ended, %s%s", turn, war.name, pid, age, after
                )
        summary = self._summary_of(self._wars)
        if summary != self._summary:
            log.info(
                "wars: turn %s: at war with %s (* = city-state, via its suzerain)", turn, summary
            )
            self._summary = summary

    # ----- peace -----

    def _due(self, table: dict[int, int], pid: int, turn: int) -> bool:
        last = table.get(pid)
        return last is None or turn < last or turn - last >= self.settings.offer_interval

    async def _make_peace(self, game: WarGame, turn: int, before: Mapping[int, War]) -> None:
        s = self.settings
        offers = 0
        majors = [w for w in self._wars.values() if w.via is None]
        for war in sorted(majors, key=lambda w: (w.started is None, w.started or 0)):
            if war.started is not None and not war.late and turn - war.started < s.min_war_age:
                log.info(
                    "wars: turn %s: no peace offer to %s, war is %s turns old, game waits %s",
                    turn,
                    war.name,
                    turn - war.started,
                    s.min_war_age,
                )
                continue
            if not self._due(self._offered, war.player_id, turn):
                continue
            if offers >= s.max_offers_per_turn:
                log.info(
                    "wars: turn %s: no peace offer to %s, %s offers made", turn, war.name, offers
                )
                continue
            offers += 1
            self._offered[war.player_id] = turn
            await self._offer(game, turn, war, "peace offer")
        # City-states counted at war last turn, whose suzerain is at peace now: ask what is true.
        for pid, old in before.items():
            if old.via is None or pid in self._wars or old.via in self._wars:
                continue
            if offers >= s.max_offers_per_turn or not self._due(self._checked, pid, turn):
                continue
            offers += 1
            self._checked[pid] = turn
            suzerain = self._names.get(old.via, old.via)
            log.info(
                "wars: turn %s: suzerain %s is at peace, checking %s", turn, suzerain, old.name
            )
            await self._offer(game, turn, old, "peace check")

    async def _offer(self, game: WarGame, turn: int, war: War, what: str) -> None:
        log.info("wars: turn %s: %s to %s (%s)", turn, what, war.name, war.player_id)
        try:
            text = str(await game.propose_peace(war.player_id)).strip()
        except ConnectionError:
            raise
        except Exception:
            log.warning("wars: turn %s: %s to %s raised", turn, what, war.name, exc_info=True)
            return
        gap = self.settings.offer_interval
        if text.startswith("ACCEPTED"):
            log.info("wars: turn %s: %s accepted peace: %s", turn, war.name, text)
        elif text.startswith("REJECTED"):
            self._refused[war.player_id] = turn
            log.info("wars: turn %s: %s refused peace, next offer in %s turns", turn, war.name, gap)
        elif "NOT_AT_WAR" in text:
            log.info("wars: turn %s: %s is not at war with us: %s", turn, war.name, text)
        elif text.startswith("Error"):
            log.warning("wars: turn %s: peace with %s not possible: %s", turn, war.name, text)
        else:
            log.warning(
                "wars: turn %s: unrecognised peace answer for %s: %r", turn, war.name, text[:200]
            )
