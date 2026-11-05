import sys
from pathlib import Path

import pytest
from civ_mcp import game_launcher

import run_game
from civ6_ai import saves

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows folders only")


@windows_only
def test_documents_dir_exists():
    assert saves.documents_dir().is_dir()


@windows_only
def test_civ6_mcp_is_pointed_at_the_real_documents_folder(monkeypatch):
    monkeypatch.setattr(game_launcher, "SINGLE_SAVE_DIR", "unset")
    monkeypatch.setattr(game_launcher, "SAVE_DIR", "unset")
    monkeypatch.setattr(saves, "documents_dir", lambda: Path("moved", "Documents"))
    saves.use_real_save_dirs()
    assert Path(game_launcher.SINGLE_SAVE_DIR).parts[:2] == ("moved", "Documents")
    assert Path(game_launcher.SINGLE_SAVE_DIR).name == "Single"
    assert Path(game_launcher.SAVE_DIR).name == "auto"


def test_standard_save_is_identified_by_seed():
    manifest = {"saves": [{"name": "STD_T1_01", "recorded": {"game_seed": "-5"}}]}
    assert run_game.identify_save(manifest, -5) == "STD_T1_01"
    assert run_game.identify_save(manifest, 5) is None
