import asyncio

from civ6_ai.preflight import (
    Status,
    check_patches,
    check_screenshot_imports,
    check_standard_save,
    check_tuner,
    report,
)


def _importer(missing: set[str]):
    def import_module(name: str):
        if name in missing:
            raise ImportError(name)

    return import_module


def test_screenshots_ok_when_imports_work():
    assert check_screenshot_imports(_importer(set()), "win32").status is Status.OK


def test_missing_pillow_warns_and_names_uv_sync():
    check = check_screenshot_imports(_importer({"PIL.Image"}), "win32")
    assert check.status is Status.WARN
    assert "Pillow" in check.detail and "uv sync" in check.detail
    assert "pywin32" not in check.detail


def test_missing_pywin32_warns():
    check = check_screenshot_imports(_importer({"win32gui", "win32ui"}), "win32")
    assert check.status is Status.WARN
    assert check.detail.count("pywin32") == 1


def test_screenshots_warn_off_windows():
    assert check_screenshot_imports(_importer(set()), "linux").status is Status.WARN


def test_patches_ok_and_all_applied():
    calls = []
    check = check_patches([("a", lambda: calls.append("a")), ("b", lambda: calls.append("b"))])
    assert check.status is Status.OK and calls == ["a", "b"]


def test_patch_failure_is_critical_and_named():
    def bad():
        raise RuntimeError("civ6-mcp changed")

    check = check_patches([("fix_x", bad)])
    assert check.status is Status.FAIL
    assert "fix_x" in check.detail and "civ6-mcp changed" in check.detail


def test_tuner_ok():
    async def connect():
        return None

    assert asyncio.run(check_tuner(connect)).status is Status.OK


def test_tuner_down_fails():
    async def connect():
        raise ConnectionRefusedError

    check = asyncio.run(check_tuner(connect))
    assert check.status is Status.FAIL and "Civ" in check.detail


def _save(seed, expect=None):
    def identify(s):
        return "STD_T1_01" if s == 1 else None

    async def get_seed():
        if seed is None:
            raise TimeoutError
        return seed

    return asyncio.run(check_standard_save(get_seed, identify, expect))


def test_standard_save_recognised():
    check, name = _save(1)
    assert check.status is Status.OK and name == "STD_T1_01"


def test_non_standard_save_fails():
    check, name = _save(2)
    assert check.status is Status.FAIL and name is None


def test_wrong_expected_save_fails():
    check, _ = _save(1, expect="STD_T1_02")
    assert check.status is Status.FAIL and "STD_T1_02" in check.detail


def test_unreadable_seed_fails():
    assert _save(None)[0].status is Status.FAIL


def test_report_prints_each_line_and_flags_failure():
    lines: list[str] = []
    ok = check_patches([("x", lambda: None)])
    bad = check_patches([("y", lambda: 1 / 0)])
    assert report([ok], lines.append) is True
    assert report([ok, bad], lines.append) is False
    assert lines[0].startswith("preflight OK") and "FAIL" in lines[-1]
