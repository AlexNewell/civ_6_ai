"""Screenshots of the game window, so a later triage can see what the screen showed.

A game gets at most `MAX_PER_GAME` of them. The count is the number of files already saved for the
game id, so it holds across sessions (a game spans several) with no ledger of its own.
Screenshots can show player names, so they go under the git-ignored `runs/`.
"""

import logging
import re
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)

MAX_PER_GAME = 20
# The last few slots are kept for turns that stop the run, so minor errors can't use them all up.
RESERVED_FOR_STOPS = 3
WINDOW_TITLE = "civilization vi"
SHOT_WIDTH = 1280
JPEG_QUALITY = 80
# A frame whose brightest and darkest pixels differ by less than this is a blank capture.
BLANK_SPREAD = 8

# Returns the JPEG bytes of the game window, or None if it can't be captured.
Capture = Callable[[], bytes | None]


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40] or "shot"


class Screenshotter:
    def __init__(
        self,
        directory: Path,
        game_id: str,
        cap: int = MAX_PER_GAME,
        capture: Capture | None = None,
    ) -> None:
        self._dir = directory
        self._game_id = _safe(game_id)
        self._cap = cap
        self._capture = capture if capture is not None else capture_game_window
        self._last_turn: int | None = None

    def count(self) -> int:
        """How many screenshots this game already has, from earlier sessions too."""
        return len(list(self._dir.glob(f"{self._game_id}_T*.jpg")))

    def probe_count(self) -> int:
        """How many of this game's screenshots are probe shots."""
        return len(list(self._dir.glob(f"{self._game_id}_T*_probe-*.jpg")))

    def probe_room(self, probe_cap: int) -> int:
        """Probe shots still allowed: under the probe cap and never into the stop reserve."""
        return min(probe_cap - self.probe_count(), self._cap - self.count() - RESERVED_FOR_STOPS)

    def take_probe(self, turn: int, reason: str, probe_cap: int) -> Path | None:
        """Save a probe screenshot. Unlike `take` it ignores and leaves the once-a-turn mark, so
        a stop on the same turn still gets its own shot."""
        try:
            if self.probe_room(probe_cap) <= 0:
                return None
            data = self._capture()
            if not data:
                return None
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{self._game_id}_T{turn:03d}_{_safe(reason)}.jpg"
            path.write_bytes(data)
            log.info("Probe screenshot saved: %s", path.name)
            return path
        except Exception:
            log.exception("Probe screenshot failed")
            return None

    def take(self, turn: int, reason: str, stop: bool = False) -> Path | None:
        """Save one screenshot. None if over the cap, already shot this turn, or it failed.

        `stop` is for a turn that ends the run, which may use the reserved last slots.
        """
        try:
            if turn == self._last_turn:
                return None
            room = self._cap - self.count()
            if room <= 0 or (not stop and room <= RESERVED_FOR_STOPS):
                return None
            data = self._capture()
            if not data:
                return None
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{self._game_id}_T{turn:03d}_{_safe(reason)}.jpg"
            path.write_bytes(data)
            self._last_turn = turn
            log.info("Screenshot saved: %s", path.name)
            return path
        except Exception:
            log.exception("Screenshot failed")
            return None


def capture_game_window() -> bytes | None:
    """JPEG bytes of the game's client area, 1280 px wide. Windows only; None if it can't."""
    try:
        import ctypes

        import win32gui
        import win32ui
        from PIL import Image
    except ImportError:
        log.warning("Screenshots need pywin32 and Pillow on Windows")
        return None

    windows: list[int] = []

    def visit(hwnd: int, _: object) -> None:
        if win32gui.IsWindowVisible(hwnd) and WINDOW_TITLE in win32gui.GetWindowText(hwnd).lower():
            windows.append(hwnd)

    win32gui.EnumWindows(visit, None)
    if not windows or win32gui.IsIconic(windows[0]):
        log.warning("No visible game window to capture")
        return None
    hwnd = windows[0]
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    window_dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        memory.SelectObject(bitmap)
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT: the DirectX frame, not a black window.
        ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 3)
        info = bitmap.GetInfo()
        image = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bitmap.GetBitmapBits(True), "raw", "BGRX"
        )
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

    low, high = image.convert("L").getextrema()
    if high - low < BLANK_SPREAD:
        log.warning("Game window capture came back blank")
        return None
    image = image.resize((SHOT_WIDTH, round(image.height * SHOT_WIDTH / image.width)))
    out = BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()
