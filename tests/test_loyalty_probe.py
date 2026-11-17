import asyncio

from civ6_ai.loyalty_probe import (
    LOYALTY_PROBE_LUA,
    parse_loyalty_probe,
    run_loyalty_probe,
    summarise,
)

GOOD = """LP|settlers|1
LP|lens_on|true
LP|exists|function
LP|type|table
LP|plot|10|12|-22|true
LP|plot|11|12|3|true
LP|plot|30|4|-5|false
LP|count|3|-22|3|1
LP|continent|1|2
LP|continent|4|1
LP|seconds|0.0120
LPDONE
---END---
"""


def test_parses_a_full_reply():
    probe = parse_loyalty_probe(GOOD)
    assert probe.error == ""
    assert (probe.settlers, probe.lens_on, probe.exists) == (1, "true", "function")
    assert (probe.count, probe.low, probe.high, probe.unrevealed) == (3, -22, 3, 1)
    assert probe.continents == {1: 2, 4: 1}
    assert probe.plots[0] == (10, 12, -22.0, True)
    assert probe.plots[2][3] is False
    assert probe.seconds == 0.012
    assert probe.clamps_at_20 is False  # -22 is past 20, so no clamp at 20.


def test_a_reply_without_its_end_marker_is_an_error():
    assert "incomplete" in parse_loyalty_probe("LP|settlers|0\nLP|count|1|1|1|0\n").error


def test_a_missing_function_or_a_failed_call_is_reported():
    probe = parse_loyalty_probe("LP|settlers|0\nLP|exists|nil\nLP|error|attempt to call\nLPDONE\n")
    assert probe.exists == "nil"
    assert probe.error == "attempt to call"
    assert probe.count == 0
    assert probe.clamps_at_20 is None
    assert "ERROR" in summarise(probe)


def test_malformed_lines_are_skipped():
    probe = parse_loyalty_probe("LP|plot|x|y|z|true\nLP|continent|a|b\nnoise\nLPDONE\n")
    assert probe.plots == [] and probe.continents == {}


def test_summary_mentions_the_questions_the_note_asks():
    text = summarise(parse_loyalty_probe(GOOD))
    for needle in ("settlers owned: 1", "3 plots", "1 not revealed", "clamped at 20", "continents"):
        assert needle in text


def test_runs_in_the_ingame_context_and_ends_with_the_sentinel():
    calls = []

    class FakeGame:
        async def execute_lua(self, code, context):
            calls.append((code, context))
            return GOOD.splitlines()

    probe = asyncio.run(run_loyalty_probe(FakeGame()))
    assert calls == [(LOYALTY_PROBE_LUA, "ingame")]
    assert probe.count == 3
    assert "Map.GetContinentPlotsLoyalty" in LOYALTY_PROBE_LUA
    assert LOYALTY_PROBE_LUA.rstrip().endswith('print("---END---")')
