import pytest
from civ_mcp import lua as lq

from civ6_ai import game_speed


@pytest.fixture
def restore(monkeypatch):
    monkeypatch.setattr(lq, "build_overview_query", lq.build_overview_query)
    monkeypatch.setattr(game_speed, "_original", None)


def test_the_overview_reads_the_speed_from_the_game_configuration(restore):
    original = lq.build_overview_query
    assert "Game.GetGameSpeedType()" in original()
    game_speed.fix_overview_speed()
    patched = lq.build_overview_query()
    assert "GameConfiguration.GetGameSpeedType()" in patched
    assert "Game.GetGameSpeedType()" not in patched.replace("GameConfiguration.", "")
    game_speed.fix_overview_speed()  # A second call changes nothing.
    assert lq.build_overview_query is game_speed.build_overview_query


def test_the_fix_raises_if_civ6_mcp_changed(restore, monkeypatch):
    monkeypatch.setattr(lq, "build_overview_query", lambda *a, **k: "print('no speed here')")
    with pytest.raises(RuntimeError):
        game_speed.fix_overview_speed()
