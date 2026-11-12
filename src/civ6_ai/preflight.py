"""Checks run before turn 1 or a resume, so a broken setup shows at startup, not mid-game.

On 2026-11-12 a stop at turn 102 took no screenshot because the shared `.venv` was out of sync
(Pillow missing), and the only sign was one warning at the moment of the stop. Each check here
returns a `Check` with one clear line. A `FAIL` stops the run; a `WARN` is printed and the run
goes on. Run `uv sync` after pulling to fix a missing dependency.
"""

import importlib
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str

    def line(self) -> str:
        return f"preflight {self.status.value.upper():4} {self.name}: {self.detail}"


# The imports `capture_game_window` needs, by the package that provides them.
SCREENSHOT_IMPORTS = (("win32gui", "pywin32"), ("win32ui", "pywin32"), ("PIL.Image", "Pillow"))


def check_screenshot_imports(
    import_module: Callable[[str], object] = importlib.import_module,
    platform: str | None = None,
) -> Check:
    """Warn if pywin32 or Pillow can't be imported, since screenshots would silently not work."""
    if (platform or sys.platform) != "win32":
        return Check("screenshots", Status.WARN, "screenshots only work on Windows")
    missing: list[str] = []
    for module, package in SCREENSHOT_IMPORTS:
        try:
            import_module(module)
        except ImportError:
            if package not in missing:
                missing.append(package)
    if missing:
        return Check(
            "screenshots",
            Status.WARN,
            f"missing {' and '.join(missing)}, so no screenshots will be taken; run `uv sync`",
        )
    return Check("screenshots", Status.OK, "pywin32 and Pillow import")


def check_patches(patches: Sequence[tuple[str, Callable[[], None]]]) -> Check:
    """Apply each civ6-mcp patch. Any that raises means civ6-mcp changed, which is critical."""
    for name, apply in patches:
        try:
            apply()
        except Exception as error:
            return Check("patches", Status.FAIL, f"{name} did not apply: {error}")
    return Check("patches", Status.OK, f"{len(patches)} civ6-mcp patches applied")


async def check_tuner(connect: Callable[[], Awaitable[None]]) -> Check:
    """Connect to the tuner. It allows one connection, so the caller passes the connection the
    run will use and keeps it open afterwards (closing it itself if a later check fails)."""
    try:
        await connect()
    except Exception as error:
        return Check("tuner", Status.FAIL, f"no answer from the tuner ({error!r}); is Civ open?")
    return Check("tuner", Status.OK, "the tuner answers")


async def check_standard_save(
    get_seed: Callable[[], Awaitable[int]],
    identify: Callable[[int], str | None],
    expect_save: str | None = None,
) -> tuple[Check, str | None]:
    """The open game must be a standard save (and `expect_save`, if given). Returns its name."""
    try:
        seed = await get_seed()
    except Exception as error:
        return Check("save", Status.FAIL, f"could not read the game's seed ({error!r})"), None
    name = identify(seed)
    if name is None:
        return Check("save", Status.FAIL, "the open game isn't a standard save"), None
    if expect_save is not None and name != expect_save:
        return Check("save", Status.FAIL, f"the open game is {name}, not {expect_save}"), None
    return Check("save", Status.OK, f"the open game is {name}"), name


def report(checks: Iterable[Check], emit: Callable[[str], None] = print) -> bool:
    """Print one line per check. True if none failed."""
    ok = True
    for check in checks:
        emit(check.line())
        ok = ok and check.status is not Status.FAIL
    return ok
