import asyncio

import pytest

from civ6_ai.lens_probe import PROBES, parse_lens_probe, run_lens_probe, summarise

GOOD = """LN|probe|appeal
LN|note|lens_on|false
LN|note|Map.GetContinentPlotsAppeal|function
LN|sample|charming|10|12|3|true
LN|sample|charming|11|12|2|true
LN|bucket|charming|40|2
LN|bucket|disgusting|0|0
LN|note|per_plot_seconds|0.0100
LN|seconds|0.0150
LNDONE
---END---
"""


def test_parses_a_full_reply():
    probe = parse_lens_probe(GOOD)
    assert probe.error == "" and probe.name == "appeal"
    assert probe.notes["lens_on"] == "false"
    assert probe.buckets["charming"].count == 40
    assert probe.buckets["charming"].unrevealed == 2
    assert probe.buckets["charming"].samples[0] == (10, 12, "3", True)
    assert probe.buckets["disgusting"].count == 0
    assert probe.seconds == 0.015


def test_a_reply_without_its_end_marker_is_an_error():
    assert "incomplete" in parse_lens_probe("LN|probe|water\nLN|bucket|coastal|3|0\n").error


def test_a_failed_call_is_reported_and_keeps_the_function_type():
    probe = parse_lens_probe(
        "LN|probe|power\nLN|note|City.GetPower|nil\nLN|error|attempt to call\nLNDONE\n"
    )
    assert probe.notes["City.GetPower"] == "nil"
    assert probe.error == "attempt to call"
    assert "ERROR" in summarise(probe)


def test_malformed_lines_are_skipped_and_a_value_may_contain_bars():
    probe = parse_lens_probe(
        "LN|bucket|a|x|y\nLN|sample|a|x|y|1|true\nnoise\nLN|note|k|a|b\nLNDONE\n"
    )
    assert probe.buckets == {}
    assert probe.notes["k"] == "a|b"


def test_summary_lists_notes_and_buckets():
    text = summarise(parse_lens_probe(GOOD))
    for needle in ("appeal", "lens_on: false", "charming: 40 plots, 2 not revealed", "(10,12)=3"):
        assert needle in text


@pytest.mark.parametrize("name", sorted(PROBES))
def test_every_probe_reports_its_name_and_ends_cleanly(name):
    lua = PROBES[name]
    assert f'print("LN|probe|{name}")' in lua
    assert 'print("LNDONE")' in lua
    assert lua.rstrip().endswith('print("---END---")')
    # No call that changes the game.
    for banned in (
        "UILens.SetActive",
        "SetLayerHexes",
        "UI.SetInterfaceMode",
        "RequestOperation",
        "RequestCommand",
    ):
        assert banned not in lua


def test_the_probes_cover_the_calls_the_note_names():
    assert "Map.GetContinentPlotsAppeal" in PROBES["appeal"] and "GetAppeal" in PROBES["appeal"]
    assert "Map.GetContinentPlotsWaterAvailability" in PROBES["water"]
    assert "GetVisibleContinentPlots" in PROBES["continent"]
    assert "CanBeFlooded" in PROBES["hazards"] and "CanSufferEruption" in PROBES["hazards"]
    assert "GetConversionOutcome" in PROBES["loyalty_lens"]
    assert "GetMajorityReligion" in PROBES["religion"]
    assert "GetTourismAt" in PROBES["tourism"]
    assert "GetPlotsCoveredByRegionalPower" in PROBES["power"]
    assert "GetCurrentGovernment" in PROBES["political"]


def test_runs_in_the_ingame_context():
    calls = []

    class FakeGame:
        async def execute_lua(self, code, context):
            calls.append((code, context))
            return GOOD.splitlines()

    probe = asyncio.run(run_lens_probe(FakeGame(), "appeal"))
    assert calls == [(PROBES["appeal"], "ingame")]
    assert probe.buckets["charming"].count == 40
