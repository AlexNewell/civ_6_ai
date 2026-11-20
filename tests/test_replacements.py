"""Replacements read from the game's tables (issue 354)."""

import asyncio

import pytest

from civ6_ai import replacements as rp
from civ6_ai.policies import game_data

REPLY = """BR|BUILDING_MADRASA|BUILDING_UNIVERSITY
BR|BUILDING_STAVE_CHURCH|BUILDING_TEMPLE
BR|BUILDING_PRASAT|BUILDING_TEMPLE
BR|BUILDING_BIBLIOTHEQUE|BUILDING_LIBRARY
BR|BUILDING_SEOWON_ACADEMY|BUILDING_UNIVERSITY
BR|BUILDING_GRAND_BAZAAR|BUILDING_BANK
DR|DISTRICT_SEOWON|DISTRICT_CAMPUS
DR|DISTRICT_HANSA|DISTRICT_INDUSTRIAL_ZONE
DR|DISTRICT_BATH|DISTRICT_AQUEDUCT
UR|UNIT_GREEK_HOPLITE|UNIT_SPEARMAN
UR|UNIT_EGYPTIAN_CHARIOT|UNIT_HEAVY_CHARIOT
bad line
BR||
RPDONE
"""


def test_parse_builds_maps_without_prefixes():
    read = rp.parse_replacements(REPLY)
    assert read.buildings["MADRASA"] == "UNIVERSITY"
    assert len(read.buildings) == 6
    assert read.districts == {
        "SEOWON": "CAMPUS",
        "HANSA": "INDUSTRIAL_ZONE",
        "BATH": "AQUEDUCT",
    }
    assert read.units["UNIT_GREEK_HOPLITE"] == "UNIT_SPEARMAN"


def test_read_matches_hand_tables_and_adds_extras():
    read = rp.parse_replacements(REPLY)
    for key, value in game_data.UNIQUE_BUILDINGS.items():
        if key in read.buildings:
            assert read.buildings[key] == value
    assert "PRASAT" in read.buildings and "PRASAT" not in game_data.UNIQUE_BUILDINGS


def test_partial_read_is_dropped():
    with pytest.raises(ValueError):
        rp.parse_replacements("BR|BUILDING_MADRASA|BUILDING_UNIVERSITY\n")


def test_apply_merges_over_hand_tables(monkeypatch):
    monkeypatch.setattr(game_data, "UNIQUE_BUILDINGS", dict(game_data.UNIQUE_BUILDINGS))
    monkeypatch.setattr(game_data, "UNIQUE_DISTRICTS", dict(game_data.UNIQUE_DISTRICTS))
    monkeypatch.setattr(game_data, "REPLACES", dict(game_data.REPLACES))
    rp.apply_replacements(rp.parse_replacements(REPLY))
    assert game_data.UNIQUE_BUILDINGS["PRASAT"] == "TEMPLE"
    assert game_data.UNIQUE_BUILDINGS["ELECTRONICS_FACTORY"] == "FACTORY"  # Hand entry kept.
    assert game_data.UNIQUE_DISTRICTS["BATH"] == "AQUEDUCT"
    assert game_data.REPLACES["UNIT_GREEK_HOPLITE"] == "UNIT_SPEARMAN"


def test_read_calls_game_and_accepts_list_reply():
    class G:
        async def execute_lua(self, code, context="gamecore"):
            assert "BuildingReplaces" in code and context == "ingame"
            return REPLY.splitlines()

    read = asyncio.run(rp.read_replacements(G()))
    assert len(read.buildings) == 6
