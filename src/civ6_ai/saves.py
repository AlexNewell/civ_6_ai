"""Where Civ VI keeps saves on this machine."""

import ctypes
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
