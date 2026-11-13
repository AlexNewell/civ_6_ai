import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from civ6_ai.construction import ProductionRefused
from civ6_ai.monitors import CityLost, EventBus
from civ6_ai.probes import ProbeShots, parse_kinds, reason_class_of
from civ6_ai.screenshots import MAX_PER_GAME, RESERVED_FOR_STOPS, Screenshotter

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import probe_report  # noqa: E402


class FakeLua:
    def __init__(self, targets="TARGETS|3,4;5,6|2", fail_on=None, mode_ok=True):
        self.calls: list[str] = []
        self.targets = targets
        self.fail_on = fail_on
        self.mode_ok = mode_ok

    async def __call__(self, lua: str) -> list[str]:
        self.calls.append(lua)
        if self.fail_on and self.fail_on in lua:
            raise RuntimeError("lua failed")
        if "UI.GetInterfaceMode" in lua:
            return [f"MODE|{str(self.mode_ok).lower()}", "---END---"]
        if "TARGETS|" in lua:
            return [self.targets, "---END---"]
        return ["---END---"]


def refused(turn=10, city="Ur", item="DISTRICT_CAMPUS", reply="ERR:NO_PLACEMENT|x", **extra):
    return ProductionRefused(
        turn, 1, city, 5, 6, item=item, category="DISTRICT", reply=reply, is_refusal=True, **extra
    )


def make(tmp_path, kinds=("placement",), capture=lambda: b"jpg", lua=None, **kw):
    shots = Screenshotter(tmp_path, "g", capture=capture)
    lua = lua or FakeLua()
    probes = ProbeShots(shots, lua, kinds=list(kinds), dwell=0, **kw)
    bus = EventBus()
    probes.subscribe(bus)
    return probes, bus, shots, lua


def run(probes):
    asyncio.run(probes.run(None))


def jpgs(tmp_path):
    return sorted(tmp_path.glob("*.jpg"))


def sidecar(shot):
    return json.loads(shot.with_suffix(".json").read_text(encoding="utf-8"))


def test_off_by_default(tmp_path):
    probes, bus, _, lua = make(tmp_path, kinds=())
    bus.publish(refused())
    run(probes)
    assert jpgs(tmp_path) == [] and lua.calls == []


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        parse_kinds("placement,nope")
    assert parse_kinds("placement, city_lost") == ["placement", "city_lost"]


def test_placement_shot_and_sidecar(tmp_path):
    probes, bus, _, lua = make(tmp_path)
    bus.publish(refused())
    run(probes)
    (shot,) = jpgs(tmp_path)
    assert "probe-placement-Ur-DISTRICT_CAMPUS" in shot.name
    data = sidecar(shot)
    assert data["turn"] == 10 and data["city"] == "Ur" and data["item"] == "DISTRICT_CAMPUS"
    assert data["reply"].startswith("ERR:NO_PLACEMENT") and data["reason_class"] == "placement"
    assert data["targets"] == [[3, 4], [5, 6]] and data["target_count"] == 2
    assert "Map.GetPlot(5,6)" in lua.calls[0] and "UI.LookAtPlot" in lua.calls[0]
    assert not any("SetInterfaceMode" in c for c in lua.calls)  # No overlay by default.


def test_declared_reason_class_wins():
    @dataclass(frozen=True, kw_only=True)
    class Declared(ProductionRefused):
        reason_class: str = "no_valid_tile"

    event = Declared(1, 1, "Ur", 5, 6, item="X", category="DISTRICT", reply="r", is_refusal=True)
    assert reason_class_of(event) == "no_valid_tile"
    assert reason_class_of(refused()) == "placement"
    assert reason_class_of(refused(reply="ERR:CANNOT_START|x")) == "start"


def test_non_placement_refusal_ignored(tmp_path):
    probes, bus, *_ = make(tmp_path)
    bus.publish(refused(reply="ERR:INSUFFICIENT|gold"))
    run(probes)
    assert jpgs(tmp_path) == []


def test_city_lost_probe(tmp_path):
    probes, bus, *_ = make(tmp_path, kinds=("city_lost",))
    bus.publish(CityLost(20, 3, "Eridu", 9, 9))
    run(probes)
    (shot,) = jpgs(tmp_path)
    assert "probe-city_lost-Eridu" in shot.name


def test_one_shot_per_turn(tmp_path):
    probes, bus, *_ = make(tmp_path)
    bus.publish(refused(item="DISTRICT_CAMPUS"))
    bus.publish(refused(item="DISTRICT_HARBOR"))
    run(probes)
    assert len(jpgs(tmp_path)) == 1


def test_dedup_per_city_item_reason(tmp_path):
    probes, bus, *_ = make(tmp_path)
    for event in (
        refused(turn=10),
        refused(turn=11),  # Same city, item and reason class: skipped.
        refused(turn=12, city="Uruk"),
        refused(turn=13, reply="ERR:CANNOT_START|x"),
    ):
        bus.publish(event)
        run(probes)
    assert len(jpgs(tmp_path)) == 3


def test_probe_cap(tmp_path):
    probes, bus, *_ = make(tmp_path, cap=2)
    for turn in range(10, 15):
        bus.publish(refused(turn=turn, item=f"DISTRICT_X{turn}"))
        run(probes)
    assert len(jpgs(tmp_path)) == 2


def test_never_uses_stop_reserve(tmp_path):
    probes, bus, *_ = make(tmp_path)
    for i in range(MAX_PER_GAME - RESERVED_FOR_STOPS):
        (tmp_path / f"g_T{i:03d}_other.jpg").write_bytes(b"x")
    bus.publish(refused())
    run(probes)
    assert len(jpgs(tmp_path)) == MAX_PER_GAME - RESERVED_FOR_STOPS


def test_probe_does_not_block_a_stop_shot_on_same_turn(tmp_path):
    probes, bus, shots, _ = make(tmp_path)
    bus.publish(refused(turn=10))
    run(probes)
    assert shots.take(10, "blocked", stop=True) is not None


def test_blocked_turn_gets_no_other_probe(tmp_path):
    probes, bus, *_ = make(tmp_path, kinds=("placement", "blocked"))

    @dataclass(frozen=True)
    class Blocked(CityLost):
        kind = "blocked"

    bus.publish(refused(turn=10))
    bus.publish(Blocked(10, 1, "Ur", 5, 6))
    run(probes)
    names = [p.name for p in jpgs(tmp_path)]
    assert len(names) == 1 and "probe-blocked" in names[0]


def test_capture_failure_never_raises(tmp_path):
    def boom():
        raise RuntimeError("no window")

    probes, bus, *_ = make(tmp_path, capture=boom)
    bus.publish(refused())
    run(probes)  # Must not raise.
    assert jpgs(tmp_path) == []


def test_lua_failure_never_raises(tmp_path):
    probes, bus, *_ = make(tmp_path, lua=FakeLua(fail_on="LookAtPlot"))
    bus.publish(refused())
    run(probes)
    assert jpgs(tmp_path) == []


def test_overlay_restored_when_capture_throws(tmp_path):
    def boom():
        raise RuntimeError("no window")

    probes, bus, _, lua = make(tmp_path, capture=boom, overlay=True)
    bus.publish(refused())
    run(probes)
    opened = [i for i, c in enumerate(lua.calls) if "DISTRICT_PLACEMENT" in c]
    restored = [i for i, c in enumerate(lua.calls) if "InterfaceModeTypes.SELECTION" in c]
    assert opened and restored and restored[-1] > opened[0]


def test_overlay_restore_verified_and_recorded(tmp_path):
    probes, bus, *_ = make(tmp_path, overlay=True)
    bus.publish(refused())
    run(probes)
    (shot,) = jpgs(tmp_path)
    assert sidecar(shot)["overlay"] == "restored"


def test_overlay_unconfirmed_restore_retries_and_flags(tmp_path):
    lua = FakeLua(mode_ok=False)
    probes, bus, *_ = make(tmp_path, overlay=True, lua=lua)
    bus.publish(refused())
    run(probes)
    assert sum("UI.GetInterfaceMode" in c for c in lua.calls) == 2
    (shot,) = jpgs(tmp_path)
    assert sidecar(shot)["overlay"] == "restore_unconfirmed"


def test_probe_time_logged(tmp_path, caplog):
    probes, bus, *_ = make(tmp_path)
    bus.publish(refused())
    with caplog.at_level("INFO", logger="civ6_ai.probes"):
        run(probes)
    assert any("took" in r.message for r in caplog.records) and len(probes.seconds) == 1


def test_report_lists_probe_shots(tmp_path):
    probes, bus, *_ = make(tmp_path)
    bus.publish(refused())
    run(probes)
    table = probe_report.markdown(probe_report.load(tmp_path, "g"))
    assert "DISTRICT_CAMPUS" in table and "Ur" in table and "placement" in table
    assert probe_report.markdown([]) == "No probe screenshots found."
