import pytest
from civ_mcp import game_state
from civ_mcp import lua as lq
from civ_mcp.lua import cities as mcp_cities

from civ6_ai import city_attack


def test_the_civ6_mcp_builder_leaves_a_literal_brace_in_its_los_message():
    """The bug this replaces: `{target_x}` is sent to Lua as an (empty) table constructor."""
    assert "{target_x}" in mcp_cities.build_city_attack(1, 5, 6)


def test_the_replacement_has_no_unfilled_python_placeholders():
    lua = city_attack.build_city_attack(65538, 12, 34)
    for name in ("{target_x}", "{target_y}", "{cx}", "{SENTINEL}", "{_bail"):
        assert name not in lua
    assert "Line of sight to (12,34) is blocked from (" in lua
    assert '"ERR:NO_LOS|Line of sight to (12,34)' in lua
    assert "pre_hp:" in lua and "OK:CITY_RANGE_ATTACK|" in lua
    assert lua.rstrip().endswith('print("---END---")')


def test_braces_in_the_lua_are_only_table_constructors():
    lua = city_attack.build_city_attack(1, 5, 6)
    assert "local params = {}" in lua
    assert "= {5}" not in lua and "{target" not in lua


def test_only_integers_reach_the_lua():
    with pytest.raises(ValueError):
        city_attack.build_city_attack(1, 5, "6; os.exit()")  # type: ignore[arg-type]
    lua = city_attack.build_city_attack("65538", 5.0, 6)  # type: ignore[arg-type]
    assert "65538 % 65536" in lua


def test_a_target_missing_from_the_command_targets_does_not_stop_the_strike():
    lua = city_attack.build_city_attack(1, 5, 6)
    # The only way out before RequestCommand are the bails, and CanStartCommand decides.
    before = lua.split("CityManager.RequestCommand")[0]
    assert "if not inTargets then" not in before
    assert "if not canAttack then" in before


def test_fix_city_attack_replaces_the_builder_game_state_calls(monkeypatch):
    monkeypatch.setattr(lq, "build_city_attack", mcp_cities.build_city_attack)
    assert game_state.lq is lq
    city_attack.fix_city_attack()
    assert lq.build_city_attack is city_attack.build_city_attack
    assert "{target_x}" not in lq.build_city_attack(1, 2, 3)


def test_fix_city_attack_raises_if_civ6_mcp_changed(monkeypatch):
    monkeypatch.delattr(lq, "build_city_attack")
    with pytest.raises(RuntimeError):
        city_attack.fix_city_attack()
