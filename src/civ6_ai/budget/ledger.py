"""Append-only record of model spend, safe across threads, crashes and restarts.

Each call writes an `open` event holding its worst-case cost before the request is sent, then a
`close` event with the real cost. A crash between the two leaves the worst case charged, so a
restarted game can't spend money that may already be gone.
"""

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from civ6_ai.budget.pricing import Usage

log = logging.getLogger(__name__)


class LedgerDamagedError(Exception):
    """Past spend can't be read, so no new call can be proven to fit under the cap."""


class Outcome(StrEnum):
    SETTLED = "settled"  # Billed, from actual API usage.
    UNBILLED = "unbilled"  # The API refused the request, so nothing was charged.
    BILLING_UNKNOWN = "billing_unknown"  # The call failed in a way that may still be billed.


@dataclass(frozen=True)
class LedgerEntry:
    call_id: str
    game_id: str
    player_id: str
    tier: str
    model: str
    worst_case_usd: Decimal
    cost_usd: Decimal
    outcome: Outcome
    usage: Usage | None
    detail: str = ""


class Ledger:
    """Tracks spend per (game, player). With no `path`, it lives in memory only."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._open: dict[str, dict] = {}
        self._entries: list[LedgerEntry] = []
        self._damaged = False
        if path is not None:
            self._load(path)

    @property
    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    def committed(self, game_id: str, player_id: str) -> Decimal:
        """Spend so far, plus the worst case of calls still open."""
        with self._lock:
            return self._committed(game_id, player_id)

    def reserve_within(
        self,
        cap_usd: Decimal,
        *,
        game_id: str,
        player_id: str,
        tier: str,
        model: str,
        worst_case_usd: Decimal,
    ) -> str | None:
        """Open a call if its worst case fits under the cap, returning its id or None."""
        with self._lock:
            if self._damaged:
                raise LedgerDamagedError(str(self._path))
            if self._committed(game_id, player_id) + worst_case_usd > cap_usd:
                return None
            event = {
                "event": "open",
                "call_id": uuid.uuid4().hex,
                "at": datetime.now(UTC).isoformat(),
                "game_id": game_id,
                "player_id": player_id,
                "tier": tier,
                "model": model,
                "worst_case_usd": str(worst_case_usd),
            }
            self._append(event)  # Before the memory update, so a failed write opens nothing.
            self._open[event["call_id"]] = event
            return event["call_id"]

    def close(
        self,
        call_id: str,
        cost_usd: Decimal,
        outcome: Outcome,
        usage: Usage | None = None,
        detail: str = "",
    ) -> None:
        """Record how a call ended. A failed write is logged, leaving the worst case on disk."""
        event = {
            "event": "close",
            "call_id": call_id,
            "at": datetime.now(UTC).isoformat(),
            "cost_usd": str(cost_usd),
            "outcome": outcome.value,
            "usage": asdict(usage) if usage else None,
            "detail": detail,
        }
        with self._lock:
            entry = self._apply_close(event)
            try:
                self._append(event)
            except OSError:
                log.exception("Could not record the end of call %s", call_id)
        if entry.cost_usd > entry.worst_case_usd:
            log.warning(
                "Call %s cost %s, above its declared worst case of %s",
                call_id,
                entry.cost_usd,
                entry.worst_case_usd,
            )

    def _committed(self, game_id: str, player_id: str) -> Decimal:
        key = (game_id, player_id)
        settled = sum(
            (e.cost_usd for e in self._entries if (e.game_id, e.player_id) == key),
            Decimal(0),
        )
        reserved = sum(
            (
                Decimal(e["worst_case_usd"])
                for e in self._open.values()
                if (e["game_id"], e["player_id"]) == key
            ),
            Decimal(0),
        )
        return settled + reserved

    def _apply_close(self, event: dict) -> LedgerEntry:
        opened = self._open.pop(event["call_id"])
        entry = LedgerEntry(
            call_id=event["call_id"],
            game_id=opened["game_id"],
            player_id=opened["player_id"],
            tier=opened["tier"],
            model=opened["model"],
            worst_case_usd=Decimal(opened["worst_case_usd"]),
            cost_usd=Decimal(event["cost_usd"]),
            outcome=Outcome(event["outcome"]),
            usage=Usage(**event["usage"]) if event["usage"] else None,
            detail=event["detail"],
        )
        self._entries.append(entry)
        return entry

    def _append(self, event: dict) -> None:
        if self._path is None:
            return
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def _load(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                return
            raw = path.read_bytes()
            complete = raw[: raw.rfind(b"\n") + 1]
            text = complete.decode("utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                try:
                    event = json.loads(line)
                    if event["event"] == "open":
                        self._open[event["call_id"]] = event
                    else:
                        self._apply_close(event)
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    log.error("Unreadable line %d in spend ledger %s", number, path)
                    self._damaged = True
            if len(complete) < len(raw):
                # A crash tore the last write. A torn open was never sent, and after a torn close
                # the open remains, so its worst case stands. Dropping it keeps the file readable.
                with path.open("rb+") as file:
                    file.truncate(len(complete))
        except OSError:
            log.exception("Could not read spend ledger %s", path)
            self._damaged = True
