"""Where Civ VI keeps saves on this machine."""

import ctypes
import inspect
import os
import sys
from pathlib import Path

_CSIDL_PERSONAL = 5  # The user's Documents folder, wherever Windows has moved it.
_MAX_PATH = 260
_SAVES_UNDER_DOCUMENTS = Path("My Games", "Sid Meier's Civilization VI", "Saves", "Single")


def documents_dir() -> Path:
    """Return the real Documents folder, which isn't always under the home folder on Windows."""
    buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, _CSIDL_PERSONAL, None, 0, buffer)
    if result != 0:
        raise OSError(f"Windows couldn't find the Documents folder: {result}")
    return Path(buffer.value)


def use_real_save_dirs() -> None:
    """Point civ6-mcp at the folder Civ really saves to, so it can prune its per-turn saves.

    Don't call `GameState.load_game_save` after this. With the right folder, its fallback can
    find a save that Lua can't, and it then restarts the game and clicks through its menus.
    """
    if sys.platform != "win32":
        return
    from civ_mcp import game_launcher

    saves = documents_dir() / _SAVES_UNDER_DOCUMENTS
    game_launcher.SINGLE_SAVE_DIR = os.fspath(saves)
    game_launcher.SAVE_DIR = os.fspath(saves / "auto")


def disable_mcp_saves() -> None:
    """Stop civ6-mcp's `end_turn` writing its per-turn `0_MCP_NNNN` save.

    Civ already autosaves every turn and keeps the last 10 as `AutoSave_*` in the `auto` folder,
    so the extra copy (about 2 MB late in a game, uploaded by a cloud sync service) adds nothing.

    civ6-mcp has no flag for this. Its `end_turn` writes the save only when
    `autosave.saves_work_on_this_platform()` is true, and that function is read nowhere else, so
    it is replaced here with one that answers False. Nothing else in `end_turn` changes.

    Crash recovery: `autosave.get_latest_autosave` looks for `0_MCP_*` first and, finding none,
    falls back to the newest `AutoSave_*` in `game_launcher.SAVE_DIR` (set by
    `use_real_save_dirs`). Our code never loads a save through civ6-mcp. The names it puts in
    its HANG and turn-regression messages (`0_MCP_NNNN`) won't exist, and nothing here reads them.

    Raises RuntimeError if civ6-mcp no longer has the code this relies on, so that a version bump
    can't quietly bring the saves back.
    """
    from civ_mcp import autosave, end_turn

    if not callable(getattr(autosave, "saves_work_on_this_platform", None)):
        raise RuntimeError("civ6-mcp has no autosave.saves_work_on_this_platform to replace")
    source = inspect.getsource(end_turn)
    if "from .autosave import saves_work_on_this_platform" not in source:
        raise RuntimeError(
            "civ6-mcp end_turn no longer gates its save on saves_work_on_this_platform"
        )
    autosave.saves_work_on_this_platform = lambda: False
