"""The only route to a paid model call.

The cap holds as long as the `send` function a caller passes in keeps these promises:

- `max_input_tokens` is a true upper bound. Measure it with the API's token counting endpoint.
  `max_output_tokens` is the request's `max_tokens`, which the API enforces and which already
  includes thinking.
- `send` makes exactly one billable request. Build the client with `max_retries=0`, because each
  hidden SDK retry could be billed, and don't enable server-side refusal fallbacks, because
  top-level `usage` only covers the final attempt.
- Requests use standard global inference, the rate in `pricing.toml`. US-only inference costs more.
- `game_id` is the game's id as `scripts/cost_report.py` assigns it, and the ledger lives beside the
  other run logs as `runs/spend_ledger.jsonl`. The report is built from those two things, so a
  call made outside this governor, or under another id, is missing from every cost figure. The
  rules for the id are in docs/cost-tracking.md.
- `send` raises `NotBilledError` only when the API definitely charged nothing. Any other exception
  keeps the call's worst-case cost on the ledger, since a timed-out request can still be billed.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from civ6_ai.budget.ledger import Ledger, LedgerDamagedError, Outcome
from civ6_ai.budget.pricing import CacheTtl, Pricing, Usage

log = logging.getLogger(__name__)


class NotBilledError(Exception):
    """The API rejected the request and charged nothing."""


class FallbackReason(StrEnum):
    CAP_EXHAUSTED = "cap_exhausted"
    API_FAILURE = "api_failure"
    INVALID_REQUEST = "invalid_request"
    LEDGER_ERROR = "ledger_error"


@dataclass(frozen=True)
class BudgetedCall:
    game_id: str
    player_id: str
    tier: str  # What the spend is for, such as "strategist" or "cheap".
    model: str
    max_input_tokens: int
    max_output_tokens: int
    cache_write_ttl: CacheTtl | None = "1h"  # None only if the request sets no cache_control.
    turn: int | None = None  # The game turn, so the cost report can place the spend. Pass it.


@dataclass(frozen=True)
class Completed[T]:
    value: T
    cost_usd: Decimal


@dataclass(frozen=True)
class Fallback:
    """No result: the caller plays this decision deterministically."""

    reason: FallbackReason
    detail: str = ""


class Governor:
    def __init__(self, pricing: Pricing, ledger: Ledger) -> None:
        self._pricing = pricing
        self._ledger = ledger

    def call[T](
        self, call: BudgetedCall, send: Callable[[], tuple[T, Usage]]
    ) -> Completed[T] | Fallback:
        """Run `send` if its worst case fits the player's cap. Never raises for a failed call."""
        try:
            worst_case = self._pricing.worst_case(
                call.model, call.max_input_tokens, call.max_output_tokens, call.cache_write_ttl
            )
        except (KeyError, ValueError) as error:
            return Fallback(FallbackReason.INVALID_REQUEST, repr(error))

        try:
            call_id = self._ledger.reserve_within(
                self._pricing.cap_usd,
                game_id=call.game_id,
                player_id=call.player_id,
                tier=call.tier,
                model=call.model,
                worst_case_usd=worst_case,
                turn=call.turn,
            )
        except (OSError, LedgerDamagedError) as error:
            log.error("Blocking a model call, the ledger is unusable: %r", error)
            return Fallback(FallbackReason.LEDGER_ERROR, repr(error))
        if call_id is None:
            return Fallback(FallbackReason.CAP_EXHAUSTED, f"needs up to {worst_case} USD")

        try:
            value, usage = send()
            cost = self._pricing.cost(call.model, usage)
        except NotBilledError as error:
            self._ledger.close(call_id, Decimal(0), Outcome.UNBILLED, detail=repr(error))
            return Fallback(FallbackReason.API_FAILURE, repr(error))
        except Exception as error:
            log.warning("Model call failed, charging its worst case: %r", error)
            self._ledger.close(call_id, worst_case, Outcome.BILLING_UNKNOWN, detail=repr(error))
            return Fallback(FallbackReason.API_FAILURE, repr(error))

        self._ledger.close(call_id, cost, Outcome.SETTLED, usage)
        return Completed(value, cost)
