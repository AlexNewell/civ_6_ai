"""Site quality (issue 324): the breakdown, the centre resource, and the report."""

import asyncio
import json
from dataclasses import replace

import pytest
from civ_mcp import lua as lq

import site_report
from civ6_ai.policies.settling import (
    SettlingSettings,
    breakdown_total,
    score_site,
    score_site_breakdown,
)
from civ6_ai.site_quality import enrich_site, format_breakdown, founded_site_scores
from civ6_ai.state.models import SettleSite
from civ6_ai.state.reader import build_tile
from fixtures.civ_state import FakeGame, city_info, tile

PLAIN = SettleSite(10, 10, 20, 10, "none", (), 0, 0, 0)
RICH = SettleSite(10, 10, 20, 10, "fresh", ("L:SILK", "S:IRON"), 3, 1, 1)
SETTINGS = SettlingSettings()


@pytest.mark.parametrize("site", [PLAIN, RICH, replace(RICH, water="coast")])
def test_breakdown_sums_to_score_site_with_default_weights(site):
    enriched = replace(site, centre_resource="S:COPPER", inner_yield=9, outer_yield=20)
    breakdown = score_site_breakdown(enriched, SETTINGS, [(12, 10)], (12, 10))
    assert breakdown_total(breakdown) == score_site(site, SETTINGS)


def test_original_components_are_raw_and_weighted():
    breakdown = score_site_breakdown(RICH, SETTINGS)
    assert breakdown["food"] == (20, 40)
    assert breakdown["luxuries"] == (1, 4)
    assert breakdown["water"] == (2, 5)


def test_inner_and_outer_ring_weights():
    site = replace(PLAIN, inner_yield=8, outer_yield=10)
    s = replace(SETTINGS, inner_ring_weight=2.0, outer_ring_weight=0.5)
    breakdown = score_site_breakdown(site, s)
    assert breakdown["inner_ring"] == (8, 16)
    assert breakdown["outer_ring"] == (10, 5)
    assert breakdown_total(breakdown) == score_site(site, s) + 21


def test_water_grade_orders_fresh_over_coast_over_none():
    s = replace(SETTINGS, water_grade_weight=1.0)
    graded = [
        score_site_breakdown(replace(PLAIN, water=w), s)["water_grade"][1]
        for w in ("fresh", "coast", "none")
    ]
    assert graded == [2, 1, 0]


def test_resource_under_the_centre_is_penalised_when_strategic_or_luxury():
    s = replace(SETTINGS, centre_resource_penalty=20.0)
    copper = score_site_breakdown(replace(PLAIN, centre_resource="S:COPPER"), s)
    assert copper["centre_resource"] == (1, -20)
    assert score_site_breakdown(replace(PLAIN, centre_resource="L:SILK"), s)["centre_resource"] == (
        1,
        -20,
    )
    assert score_site_breakdown(replace(PLAIN, centre_resource="B:WHEAT"), s)[
        "centre_resource"
    ] == (0, 0)
    assert score_site_breakdown(PLAIN, s)["centre_resource"] == (0, 0)


def test_capital_distance_penalises_distance_from_the_ideal():
    s = replace(SETTINGS, capital_distance_weight=3.0, capital_ideal_distance=6)
    near = score_site_breakdown(PLAIN, s, capital=(12, 10))
    assert near["capital_distance"] == (2, -12)
    exact = score_site_breakdown(PLAIN, s, capital=(16, 10))
    assert exact["capital_distance"] == (6, 0)
    assert score_site_breakdown(PLAIN, s)["capital_distance"] == (0, 0)  # No capital known.


def test_overlap_counts_shared_workable_tiles():
    s = replace(SETTINGS, overlap_penalty=1.5)
    same = score_site_breakdown(PLAIN, s, [(10, 10)])["overlap"]
    assert same == (37, -55.5)  # A radius-3 area is 37 tiles.
    far = score_site_breakdown(PLAIN, s, [(30, 10)])["overlap"]
    assert far == (0, 0)
    mid = score_site_breakdown(PLAIN, s, [(14, 10)])["overlap"][0]
    assert 0 < mid < 37
    assert score_site_breakdown(PLAIN, s)["overlap"] == (0, 0)


def ring_tile(x, y, **changes):
    return build_tile(tile(x=x, y=y, resource=None, resource_class=None, **changes))


def test_enrich_reads_centre_resource_and_rings():
    tiles = [
        build_tile(tile(x=10, y=10, resource="COPPER", resource_class="strategic")),
        ring_tile(11, 10, yields=(2, 1, 0, 0, 0, 0)),  # Inner ring.
        ring_tile(12, 10, yields=(1, 1, 0, 0, 0, 0)),  # Two out.
        ring_tile(13, 10, yields=(3, 0, 0, 0, 0, 0)),  # Three out.
        ring_tile(14, 10, yields=(5, 5, 0, 0, 0, 0)),  # Four out: not counted.
    ]
    site = enrich_site(PLAIN, tiles)
    assert site.centre_resource == "S:COPPER"
    assert (site.inner_yield, site.outer_yield) == (3, 5)


def test_enrich_with_no_resource_under_the_centre():
    site = enrich_site(PLAIN, [ring_tile(10, 10)])
    assert site.centre_resource is None


def test_format_breakdown_ends_with_the_total():
    text = format_breakdown(score_site_breakdown(RICH, SETTINGS))
    assert text.splitlines()[-1].endswith(f"{score_site(RICH, SETTINGS):7.1f}")


def test_founded_site_scores_use_the_chosen_candidate():
    decisions = [
        {"action": "move", "x": 1, "y": 1},
        {
            "action": "found",
            "x": 5,
            "y": 5,
            "target": None,
            "here_score": None,
            "candidates": [{"x": 5, "y": 5, "site_score": 90.0}],
        },
        {
            "action": "found",
            "x": 1,
            "y": 1,
            "target": [7, 7],
            "here_score": 10.0,
            "candidates": [{"x": 7, "y": 7, "site_score": 110.0}],
        },
        {"action": "found", "x": 2, "y": 2, "target": None, "here_score": 40.0, "candidates": []},
        {"action": "found_failed", "x": 3, "y": 3},
    ]
    assert founded_site_scores(decisions) == [90.0, 110.0, 40.0]


def test_site_report_prints_a_breakdown_from_fake_reads():
    around = [tile(x=20, y=10, resource="COPPER", resource_class="strategic")]
    game = FakeGame(tiles={(20, 10): around}, cities=[city_info(x=14, y=10)], settle=[])
    text = asyncio.run(site_report.report_site(game, 20, 10, [(14, 10)], 74))
    assert "site (20, 10)" in text
    assert "not on the survey list" in text
    assert "centre_resource" in text


def test_candidate_log_carries_the_breakdown(caplog):
    from civ6_ai.policies.settling import SettlingPolicy
    from test_policy_settling import make_game, run, settler
    from test_policy_settling import site as settle_site

    caplog.set_level("INFO")
    run(SettlingPolicy(), make_game([settler()], sites=[settle_site(14, 10)]))
    line = next(r.getMessage() for r in caplog.records if "settle_decision" in r.getMessage())
    candidate = json.loads(line.split("settle_decision ", 1)[1])["candidates"][0]
    assert set(candidate["breakdown"]) >= {"food", "inner_ring", "centre_resource", "overlap"}
    assert sum(w for _, w in candidate["breakdown"].values()) == candidate["site_score"]
    _ = lq
