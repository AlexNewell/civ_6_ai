import json

import check_saves


def test_parse_info_reads_only_tagged_lines():
    lines = ["noise", "SAVEINFO|turn|1", "SAVEINFO|map_script|a|b.lua"]
    assert check_saves.parse_info(lines) == {"turn": "1", "map_script": "a|b.lua"}


def test_find_differences_reports_changed_and_missing_values():
    expected = {"map_size": "MAPSIZE_SMALL", "game_speed": "GAMESPEED_QUICK"}
    actual = {"map_size": "MAPSIZE_LARGE"}
    assert len(check_saves.find_differences(expected, actual)) == 2
    assert check_saves.find_differences(expected, expected) == []


def test_manifest_has_at_least_three_distinct_recorded_saves():
    saves = json.loads(check_saves.MANIFEST.read_text(encoding="utf-8"))["saves"]
    assert len(saves) >= 3
    assert all(save["name"].startswith("STD_T1_") for save in saves)
    assert len({save["recorded"]["map_seed"] for save in saves}) == len(saves)
    assert len({save["recorded"]["game_seed"] for save in saves}) == len(saves)
