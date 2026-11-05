import json
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from civ6_ai.budget import (
    BudgetedCall,
    Completed,
    Fallback,
    FallbackReason,
    Governor,
    Ledger,
    NotBilledError,
    Outcome,
    Usage,
    load_pricing,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/usage_responses.json").read_text())
PRICING = load_pricing()
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

# Worst case with no caching: 10,000 input at $2 and 8,000 output at $10 per million = $0.10.
TENTH_OF_A_DOLLAR = BudgetedCall("g1", "p1", "strategist", SONNET, 10_000, 8_000, None)
TENTH_USAGE = Usage(input_tokens=10_000, output_tokens=8_000)


def governor(ledger: Ledger | None = None) -> tuple[Governor, Ledger]:
    ledger = ledger or Ledger()
    return Governor(PRICING, ledger), ledger


def spend(gov: Governor, times: int, call: BudgetedCall = TENTH_OF_A_DOLLAR) -> None:
    for _ in range(times):
        assert isinstance(gov.call(call, lambda: ("ok", TENTH_USAGE)), Completed)


def must_not_send() -> tuple[str, Usage]:
    raise AssertionError("a blocked call reached the API")


def test_cap_defaults_to_one_dollar():
    assert PRICING.cap_usd == Decimal("1.00")


@pytest.mark.parametrize("call", FIXTURE["calls"], ids=lambda call: call["cost_usd"])
def test_cost_matches_fixture(call):
    cost = PRICING.cost(call["model"], Usage.from_api(call["usage"]))
    assert cost == Decimal(call["cost_usd"])


def test_ledger_totals_match_fixture():
    gov, ledger = governor()
    for call in FIXTURE["calls"]:
        usage = Usage.from_api(call["usage"])
        request = BudgetedCall(
            "g1", call["player_id"], "any", call["model"], usage.prompt_tokens, 2_000
        )
        assert isinstance(gov.call(request, lambda u=usage: ("ok", u)), Completed)
    for player, total in FIXTURE["totals_usd"].items():
        assert ledger.committed("g1", player) == Decimal(total)
    assert len(ledger.entries) == len(FIXTURE["calls"])


def test_call_that_would_pass_the_cap_is_blocked_unsent():
    gov, ledger = governor()
    spend(gov, 9)
    just_over = BudgetedCall("g1", "p1", "strategist", SONNET, 10_001, 8_000, None)
    result = gov.call(just_over, must_not_send)
    assert isinstance(result, Fallback)
    assert result.reason is FallbackReason.CAP_EXHAUSTED
    assert ledger.committed("g1", "p1") == Decimal("0.9")


def test_call_ending_exactly_at_the_cap_is_allowed():
    gov, ledger = governor()
    spend(gov, 10)
    assert ledger.committed("g1", "p1") == PRICING.cap_usd
    assert isinstance(gov.call(TENTH_OF_A_DOLLAR, must_not_send), Fallback)


def test_cap_is_tracked_per_player_and_per_game():
    gov, _ = governor()
    spend(gov, 10)
    for game, player in [("g1", "p2"), ("g2", "p1")]:
        other = BudgetedCall(game, player, "strategist", SONNET, 10_000, 8_000, None)
        assert isinstance(gov.call(other, lambda: ("ok", TENTH_USAGE)), Completed)


def test_worst_case_uses_the_dearest_input_rate():
    # 1,000 input at $4 (1-hour write), not $2, plus 1,000 output at $10 per million.
    assert PRICING.worst_case(SONNET, 1_000, 1_000, "1h") == Decimal("0.014")
    assert PRICING.worst_case(SONNET, 1_000, 1_000, "5m") == Decimal("0.0125")
    assert PRICING.worst_case(SONNET, 1_000, 1_000, None) == Decimal("0.012")


def test_long_prompt_rate_starts_above_the_threshold():
    at_threshold = Usage(input_tokens=100_000, output_tokens=0)
    over = Usage(input_tokens=100_001, output_tokens=0)
    assert PRICING.cost(HAIKU, at_threshold) == Decimal("0.01")
    assert PRICING.cost(HAIKU, over) == Decimal("0.0500005")
    assert PRICING.worst_case(HAIKU, 100_001, 1, None) > PRICING.worst_case(HAIKU, 100_000, 1, None)


def test_cache_writes_without_a_ttl_split_count_at_the_dearer_rate():
    usage = Usage.from_api(
        {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000}
    )
    assert usage.cache_write_1h_tokens == 1_000


def test_unbilled_failure_falls_back_and_refunds_the_reservation():
    gov, ledger = governor()

    def rejected() -> tuple[str, Usage]:
        raise NotBilledError("429")

    result = gov.call(TENTH_OF_A_DOLLAR, rejected)
    assert isinstance(result, Fallback)
    assert result.reason is FallbackReason.API_FAILURE
    assert ledger.committed("g1", "p1") == 0
    assert ledger.entries[0].outcome is Outcome.UNBILLED


def test_ambiguous_failure_falls_back_and_keeps_the_worst_case():
    gov, ledger = governor()

    def timed_out() -> tuple[str, Usage]:
        raise TimeoutError("no response")

    result = gov.call(TENTH_OF_A_DOLLAR, timed_out)
    assert isinstance(result, Fallback)
    assert result.reason is FallbackReason.API_FAILURE
    assert ledger.committed("g1", "p1") == Decimal("0.1")
    assert ledger.entries[0].outcome is Outcome.BILLING_UNKNOWN


def test_malformed_usage_falls_back_and_keeps_the_worst_case():
    gov, ledger = governor()
    result = gov.call(TENTH_OF_A_DOLLAR, lambda: ("ok", Usage.from_api({"output_tokens": 1})))
    assert isinstance(result, Fallback)
    assert ledger.committed("g1", "p1") == Decimal("0.1")


@pytest.mark.parametrize(
    "call",
    [
        BudgetedCall("g1", "p1", "strategist", "not-a-priced-model", 1, 1),
        BudgetedCall("g1", "p1", "strategist", SONNET, -1_000_000, 1),
        BudgetedCall("g1", "p1", "strategist", SONNET, 1, 0),
    ],
)
def test_invalid_request_falls_back_unsent(call):
    gov, _ = governor()
    result = gov.call(call, must_not_send)
    assert isinstance(result, Fallback)
    assert result.reason is FallbackReason.INVALID_REQUEST


def test_understated_input_is_charged_in_full_and_blocks_later_calls():
    gov, ledger = governor()
    huge = Usage(input_tokens=600_000, output_tokens=0)
    assert isinstance(gov.call(TENTH_OF_A_DOLLAR, lambda: ("ok", huge)), Completed)
    assert ledger.committed("g1", "p1") == Decimal("1.2")
    assert isinstance(gov.call(TENTH_OF_A_DOLLAR, must_not_send), Fallback)


def test_concurrent_calls_cannot_overshoot_the_cap():
    gov, ledger = governor()
    results = []

    def slow_send() -> tuple[str, Usage]:
        time.sleep(0.02)
        return "ok", TENTH_USAGE

    def worker() -> None:
        results.append(gov.call(TENTH_OF_A_DOLLAR, slow_send))

    threads = [threading.Thread(target=worker) for _ in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(result, Completed) for result in results) == 10
    assert ledger.committed("g1", "p1") == PRICING.cap_usd


def test_spend_survives_a_restart(tmp_path):
    path = tmp_path / "nested" / "ledger.jsonl"
    spend(governor(Ledger(path))[0], 10)
    gov, ledger = governor(Ledger(path))
    assert ledger.committed("g1", "p1") == PRICING.cap_usd
    assert isinstance(gov.call(TENTH_OF_A_DOLLAR, must_not_send), Fallback)


def test_crash_during_a_call_leaves_the_worst_case_charged(tmp_path):
    path = tmp_path / "ledger.jsonl"
    gov, _ = governor(Ledger(path))

    def crash() -> tuple[str, Usage]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        gov.call(TENTH_OF_A_DOLLAR, crash)
    assert Ledger(path).committed("g1", "p1") == Decimal("0.1")


def test_torn_final_line_keeps_the_worst_case_and_later_writes_stay_readable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    spend(governor(Ledger(path))[0], 1)
    with path.open("rb+") as file:
        file.truncate(path.stat().st_size - 20)  # Cut into the close event.
    spend(governor(Ledger(path))[0], 1)
    reloaded = Ledger(path)
    assert reloaded.committed("g1", "p1") == Decimal("0.2")
    assert isinstance(
        Governor(PRICING, reloaded).call(TENTH_OF_A_DOLLAR, lambda: ("ok", TENTH_USAGE)), Completed
    )


def test_corrupt_ledger_blocks_calls_instead_of_trusting_partial_history(tmp_path):
    path = tmp_path / "ledger.jsonl"
    spend(governor(Ledger(path))[0], 1)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(["not json", *lines]) + "\n")
    result = governor(Ledger(path))[0].call(TENTH_OF_A_DOLLAR, must_not_send)
    assert isinstance(result, Fallback)
    assert result.reason is FallbackReason.LEDGER_ERROR


def test_unwritable_ledger_blocks_the_call(tmp_path):
    path = tmp_path / "ledger.jsonl"
    gov, ledger = governor(Ledger(path))
    path.mkdir()  # Writing to a directory fails, like a full disk would.
    result = gov.call(TENTH_OF_A_DOLLAR, must_not_send)
    assert isinstance(result, Fallback)
    assert result.reason is FallbackReason.LEDGER_ERROR
    assert ledger.committed("g1", "p1") == 0


def test_failed_settlement_write_still_returns_the_paid_result(tmp_path):
    path = tmp_path / "ledger.jsonl"
    gov, ledger = governor(Ledger(path))

    def send_then_lose_the_disk() -> tuple[str, Usage]:
        path.unlink()
        path.mkdir()
        return "ok", TENTH_USAGE

    assert isinstance(gov.call(TENTH_OF_A_DOLLAR, send_then_lose_the_disk), Completed)
    assert ledger.committed("g1", "p1") == Decimal("0.1")
