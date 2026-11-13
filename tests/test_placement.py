import asyncio
import logging
from types import SimpleNamespace

import pytest
from civ_mcp import lua as lq

from civ6_ai import district_advisor, placement
from civ6_ai.placement import (
    ELSEWHERE,
    PLACEMENT,
    UNKNOWN,
    UNLOCK,
    Diagnosis,
    OwnedTile,
    PlacementReads,
    PlanBook,
    Planner,
    PlanTile,
    SavingTarget,
    classify,
    decide,
)
from civ6_ai.policies.gold import GoldPolicy
from civ6_ai.policies.production import (
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    RefusalMemory,
    choose_for_city,
)
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx


def run(coro):
    return asyncio.run(coro)


def city(city_id=65536, **changes):
    return build_city(fx.city_info(city_id=city_id, **changes), (), set())


class Conn:
    """A connection that answers each kind of placement Lua with canned lines."""

    def __init__(self, sig="1:5", unlocks=None, diag=None, plan=()):
        self.sig = sig
        self.unlocks = unlocks or {}
        self.diag = diag
        self.plan = list(plan)
        self.calls: list[str] = []

    async def execute_write(self, lua):
        if "PSIG" in lua:
            self.calls.append("sig")
            lines = [f"PSIG|{c}|{h}|9" for c, h in self._sigs().items()]
            lines += [f"PUNLOCK|{n}|{int(v)}" for n, v in self.unlocks.items()]
            return lines
        if "PDIAG" in lua:
            self.calls.append("diag")
            return [self.diag] if self.diag else []
        if "PTILE" in lua:
            self.calls.append("plan")
            return list(self.plan)
        raise AssertionError(lua)

    def _sigs(self):
        return self.sig if isinstance(self.sig, dict) else {0: self.sig.split(":")[0]}


def reads_for(**kwargs):
    conn = Conn(**kwargs)
    return conn, PlacementReads(SimpleNamespace(conn=conn))


def sig_reads(value):
    """Reads whose city 0 (65536 % 65536) has this signature."""
    conn = Conn(sig={0: value})
    return conn, PlacementReads(SimpleNamespace(conn=conn))


# ---- the water-district patch ----


@pytest.fixture
def restore_query(monkeypatch):
    monkeypatch.setattr(
        lq, "build_district_advisor_query", lq.build_district_advisor_query, raising=True
    )


def test_the_water_filter_keeps_water_for_water_districts(restore_query):
    district_advisor.fix_district_advisor()
    lua = lq.build_district_advisor_query(1, "DISTRICT_HARBOR")
    assert "if plot and (waterOk or not plot:IsWater())" in lua
    assert "not plot:IsWater() and not plot:IsImpassable()" not in lua
    assert 'dType == "DISTRICT_HARBOR"' in lua and "dist.Aquatic" in lua and "dist.Coast" in lua
    # The mountain and impassable tests stay, and the game's own target list is still the source.
    assert "not plot:IsMountain()" in lua and "GetOperationTargets" in lua


def test_the_water_patch_applies_once_and_names_every_water_district(restore_query):
    district_advisor.fix_district_advisor()
    once = lq.build_district_advisor_query
    district_advisor.fix_district_advisor()
    assert lq.build_district_advisor_query is once
    for name in district_advisor.WATER_DISTRICTS:
        assert name in once(1, "DISTRICT_CAMPUS")


def test_the_water_patch_raises_when_the_pinned_lua_changes(monkeypatch):
    monkeypatch.setattr(lq, "build_district_advisor_query", lambda c, d: "local x = 1")
    with pytest.raises(RuntimeError):
        district_advisor.fix_district_advisor()
    with pytest.raises(RuntimeError):
        district_advisor.widen_water_filter("if plot and not plot:IsWater() then")


def test_the_pinned_advisor_still_has_the_filter_and_the_wonder_advisor_has_none():
    original = getattr(lq.build_district_advisor_query, "__wrapped__", None)
    text = lq.build_district_advisor_query(1, "DISTRICT_CAMPUS") if original is None else ""
    if text and "waterOk" not in text:
        assert district_advisor._FILTER in text
    assert "IsWater" not in lq.build_wonder_advisor_query(1, "BUILDING_PYRAMIDS")


# ---- signature, reasons and the memory ----


def test_a_changed_signature_asks_again_and_an_unchanged_one_does_not():
    mem, c = RefusalMemory(), city()
    mem.refused(c, "DISTRICT_HARBOR", 10, "ERR:NO_PLACEMENT|x", PLACEMENT, "abc:9")
    _, same = sig_reads(["abc", "9"][0])
    assert run(mem.blocked(c, "DISTRICT_HARBOR", 11, same))
    assert not run(mem.blocked(c, "DISTRICT_HARBOR", 15, same))  # The short re-ask only.
    _, changed = sig_reads("def")
    assert not run(mem.blocked(c, "DISTRICT_HARBOR", 11, changed))


def test_a_signature_is_one_read_per_turn_however_many_cities_ask():
    conn, reads = reads_for(sig={0: "a", 1: "b"})
    run(reads.signature(65536))
    run(reads.signature(65537))
    run(reads.signature(65536))
    assert conn.calls == ["sig"]


def test_the_signature_query_covers_plots_features_improvements_and_districts():
    lua = placement.build_signatures_query(("TECH_POTTERY",))
    for needle in ("GetOwner() == me", "GetFeatureType", "GetImprovementType", "GetDistrictType"):
        assert needle in lua
    assert '"TECH_POTTERY"' in lua
    assert "TECH_X; os.exit" not in placement.build_signatures_query(("TECH_X; os.exit",))


def test_parse_signatures_ignores_noise():
    sigs, unlocks = placement.parse_signatures(
        ["Omitting feature label", "PSIG|1|77|9", "PSIG|x|y", "PUNLOCK|TECH_A|1", "PUNLOCK|B|0"]
    )
    assert sigs == {1: "77:9"} and unlocks == {"TECH_A": True, "B": False}


CS = "Error: CANNOT_START|BUILDING_X cannot start. Hint: X may require a district."


@pytest.mark.parametrize(
    ("diagnosis", "reason", "needs"),
    [
        (Diagnosis(False, missing_tech="TECH_MASONRY"), UNLOCK, "TECH_MASONRY"),
        (Diagnosis(False, missing_civic="CIVIC_X"), UNLOCK, "CIVIC_X"),
        (Diagnosis(False, missing_district="DISTRICT_CAMPUS"), placement.DISTRICT, None),
        (Diagnosis(False), ELSEWHERE, None),
        (Diagnosis(True), PLACEMENT, None),
        (None, UNKNOWN, None),
    ],
)
def test_each_cannot_start_reason(diagnosis, reason, needs):
    assert classify(CS, diagnosis) == (reason, needs)


def test_other_replies_are_classified():
    assert classify("ERR:NO_PLACEMENT|x", None)[0] == PLACEMENT
    assert classify("Error: SILENT_FAILURE|x", None)[0] == placement.OTHER
    pillaged = "Error: CANNOT_START|X Prerequisite district is pillaged: D"
    assert classify(pillaged, None)[0] == placement.DISTRICT


def test_the_diagnosis_is_parsed_from_the_game_lines():
    parsed = placement.parse_diagnosis(["noise", "PDIAG|0|TECH_A||DISTRICT_B"])
    assert parsed == Diagnosis(False, "TECH_A", "", "DISTRICT_B")
    assert parsed.reason == UNLOCK and parsed.needs == "TECH_A"
    assert placement.parse_diagnosis(["nothing"]) is None


def test_the_diagnosis_query_checks_the_games_own_rules():
    lua = placement.build_diagnosis_query(65536, "BUILDING", "BUILDING_PYRAMIDS")
    for needle in ("CanProduce", "PrereqTech", "PrereqCivic", "PrereqDistrict"):
        assert needle in lua
    assert "PrereqDistrict" not in placement.build_diagnosis_query(1, "DISTRICT", "DISTRICT_X")


def test_an_unlock_refusal_is_asked_again_when_the_tech_lands():
    mem, c = RefusalMemory(), city()
    mem.refused(c, "BUILDING_X", 10, CS, UNLOCK, "5:9", "TECH_MASONRY")
    assert mem.watched() == ("TECH_MASONRY",)
    _, waiting = reads_for(sig={0: "5"}, unlocks={"TECH_MASONRY": False})
    assert run(mem.blocked(c, "BUILDING_X", 12, waiting))
    _, landed = reads_for(sig={0: "5"}, unlocks={"TECH_MASONRY": True})
    assert not run(mem.blocked(c, "BUILDING_X", 12, landed))


def test_a_wonder_built_elsewhere_is_dropped_for_the_game():
    mem, c = RefusalMemory(), city()
    assert mem.refused(c, "BUILDING_X", 10, CS, ELSEWHERE, "5:9") == -1
    _, changed = sig_reads("999")
    assert run(mem.blocked(c, "BUILDING_X", 400, changed))


def test_an_unknown_reason_waits_three_turns_then_only_a_change_ends_it():
    mem, c = RefusalMemory(), city()
    _, same = sig_reads("5")
    waits = []
    turn = 10
    for _ in range(3):
        waits.append(mem.refused(c, "BUILDING_X", turn, CS, UNKNOWN, "5:9"))
        assert run(mem.blocked(c, "BUILDING_X", turn + 1, same))
        turn += 4
    assert waits == [3, 3, -1]  # The third refusal ends the quick retries: only a change now.
    _, changed = sig_reads("6")
    assert not run(mem.blocked(c, "BUILDING_X", turn, changed))


def test_with_no_signature_any_refusal_waits_the_short_time_only():
    mem, c = RefusalMemory(), city()
    assert mem.refused(c, "DISTRICT_HARBOR", 10, "ERR:NO_PLACEMENT|x", PLACEMENT, None) == 3
    assert run(mem.blocked(c, "DISTRICT_HARBOR", 12, None))
    assert not run(mem.blocked(c, "DISTRICT_HARBOR", 13, None))


def test_refusals_are_kept_per_city_and_accepted_clears_them():
    mem, one = RefusalMemory(), city()
    other = city(city_id=65537)
    mem.refused(one, "X", 10, "ERR:NO_PLACEMENT|x", PLACEMENT, "5:9")
    assert not run(mem.blocked(other, "X", 11, None))
    mem.accepted(one, "X")
    assert not mem.has(one, "X")


# ---- the decision ----


def tile(x=1, cost=60, adj=4, disp=0):
    return PlanTile(x, 1, cost, adj, disp)


def test_with_no_owned_tile_an_affordable_tile_is_bought():
    assert decide([tile(cost=60)], [], 100, 5).action == "buy_then_place"


def test_with_no_owned_tile_an_unaffordable_one_is_saved_for_when_income_closes_the_gap():
    plan = decide([tile(cost=100)], [], 60, 10)
    assert plan.action == "save" and plan.cost == 100
    assert decide([tile(cost=100)], [], 20, 5) is None  # 16 turns away.
    assert decide([tile(cost=100)], [], 20, 0) is None


def test_a_tile_beyond_the_cap_is_neither_bought_nor_saved_for():
    assert decide([tile(cost=placement.NO_TILE_COST_CAP + 1)], [], 10_000, 50) is None


def test_a_tile_that_cannot_be_bought_yet_waits_for_the_border():
    plan = decide([tile(cost=-1)], [], 500, 5)
    assert plan.action == "wait_for_border" and plan.cost == 0
    assert decide([], [], 500, 5) is None


def test_an_owned_tile_of_equal_or_better_value_means_no_buy():
    owned = [OwnedTile(5, 5, 4)]
    assert decide([tile(adj=4)], owned, 500, 5) is None
    assert decide([tile(adj=3)], owned, 500, 5) is None


def test_a_gain_under_two_is_not_worth_a_buy_and_two_is():
    owned = [OwnedTile(5, 5, 3)]
    assert decide([tile(adj=4)], owned, 500, 5) is None
    assert decide([tile(adj=5, cost=40)], owned, 500, 5).action == "buy_then_place"


def test_net_value_counts_displacement_on_both_sides():
    # The owned tile is a plain +3; the buyable +5 sits on a luxury resource (-3): net +2, no buy.
    assert decide([tile(adj=5, disp=3)], [OwnedTile(5, 5, 3)], 500, 5) is None
    # A +3 owned tile that costs a luxury (net 0) loses to a clean +3 tile.
    plan = decide([tile(adj=3, cost=40)], [OwnedTile(5, 5, 3, displacement=3)], 500, 5)
    assert plan is not None
    assert plan.action == "buy_then_place"


def test_the_gold_is_capped_per_point_gained_and_never_saved_for_a_gain():
    owned = [OwnedTile(5, 5, 2)]
    cap = placement.GOLD_PER_ADJACENCY_POINT * 2
    assert decide([tile(adj=4, cost=cap)], owned, 500, 5).action == "buy_then_place"
    assert decide([tile(adj=4, cost=cap + 1)], owned, 500, 5) is None
    assert decide([tile(adj=4, cost=40)], owned, 30, 50) is None  # Not affordable: no saving.


def test_the_caps_scale_with_game_speed(monkeypatch):
    monkeypatch.setattr(placement, "GAME_SPEED_COST_SCALE", 3.0)
    assert placement.gold_cap(2) == 150 and placement.gold_cap(None) == 600


# ---- the planner ----


PLAN_LINES = ["PTILE|7|7|60|5|0", "PTILE|8|7|-1|6|0"]


class BuyGame:
    def __init__(self, conn, reply="OK:TILE_PURCHASED|(7,7)|cost:60"):
        self.conn = conn
        self.reply = reply
        self.bought: list[tuple] = []

    async def purchase_tile(self, city_id, x, y):
        self.bought.append((city_id, x, y))
        return self.reply


def consider(planner, game, reads, c, item="DISTRICT_CAMPUS", offered=(), category="DISTRICT"):
    return run(planner.consider(game, reads, c, category, item, list(offered)))


def planner_at(turn=100, budget=200, income=5.0):
    p = Planner()
    p.begin_turn(turn, budget, income)
    return p


def setup(plan=PLAN_LINES, reply="OK:TILE_PURCHASED|(7,7)|cost:60"):
    conn = Conn(sig={0: "1"}, plan=plan)
    game = BuyGame(conn, reply)
    return game, PlacementReads(game)


def test_no_owned_tile_buys_then_the_caller_can_place():
    game, reads = setup()
    p, c = planner_at(), city()
    assert consider(p, game, reads, c)
    assert game.bought == [(65536, 7, 7)]
    assert p.budget == 140


def test_a_better_buyable_tile_is_bought_for_an_adjacency_district():
    game, reads = setup(["PTILE|7|7|60|5|0", "POWN|5|5|0|1"])
    owned = [OwnedTile(5, 5, 2)]
    assert consider(planner_at(), game, reads, city(), offered=owned)
    game, reads = setup(["PTILE|7|7|60|3|0", "POWN|5|5|0|1"])
    assert not consider(planner_at(), game, reads, city(), offered=owned)
    assert game.bought == []


def test_an_owned_tile_means_no_buy_for_a_district_with_no_adjacency_yield():
    game, reads = setup(["PTILE|7|7|20|0|0", "POWN|5|5|0|1"])
    assert not consider(
        planner_at(), game, reads, city(), item="DISTRICT_AQUEDUCT", offered=[OwnedTile(5, 5, 0)]
    )
    assert game.conn.calls == []  # Not even a read.


def test_a_wonder_with_an_owned_tile_is_never_bought_for():
    game, reads = setup()
    assert not consider(
        planner_at(),
        game,
        reads,
        city(),
        item="BUILDING_PYRAMIDS",
        offered=[OwnedTile(5, 5, 0)],
        category="BUILDING",
    )
    assert game.bought == []


def test_a_tile_reserved_for_another_item_is_not_available():
    game, reads = setup()
    p, c = planner_at(), city()
    p.reserve(c.city_id, "DISTRICT_THEATER", 5, 5)
    # The only owned tile is reserved for the Theater, so the Campus may buy.
    assert consider(p, game, reads, c, offered=[OwnedTile(5, 5, 2)])
    # Its own reservation does not count against it.
    game, reads = setup(["PTILE|7|7|60|9|0", "POWN|5|5|0|1"])
    p.reserve(c.city_id, "DISTRICT_CAMPUS", 5, 5)
    assert not consider(
        replace_planner(p), game, reads, city(city_id=65537), offered=[OwnedTile(5, 5, 8)]
    )


def replace_planner(p):
    p.begin_turn(p.turn + 1, 500, 5.0)
    return p


def test_a_reservation_lapses():
    p, c = planner_at(), city()
    p.reserve(c.city_id, "DISTRICT_THEATER", 5, 5)
    p.begin_turn(p.turn + placement.RESERVE_TURNS + 1, 200, 5.0)
    assert p._available(c.city_id, "DISTRICT_CAMPUS", [OwnedTile(5, 5, 2)])


def test_one_tile_per_city_per_turn_and_a_cap_per_item():
    game, reads = setup()
    p, c = planner_at(), city()
    assert consider(p, game, reads, c)
    assert not consider(p, game, reads, c, item="DISTRICT_HOLY_SITE")
    for turn in range(101, 110):
        p.begin_turn(turn, 500, 5.0)
        consider(p, game, reads, c)
    assert len(game.bought) == placement.MAX_PLAN_BUYS


def test_no_buy_in_the_last_forty_turns():
    game, reads = setup()
    p = Planner(last_turn=300)
    p.begin_turn(261, 500, 5.0)
    assert not consider(p, game, reads, city())
    p.begin_turn(260, 500, 5.0)
    assert consider(p, game, reads, city())


def test_gold_is_never_taken_below_the_budget():
    game, reads = setup()
    assert not consider(planner_at(budget=59), game, reads, city())
    assert game.bought == []


def test_the_policys_budget_is_the_gold_above_the_reserve_and_zero_under_threat():
    from civ6_ai.policies.gold import reserve_for

    empire = SimpleNamespace(gold=300.0, num_cities=2, gold_per_turn=5.0)
    policy = CityProductionPolicy()
    assert policy._plan_budget(empire) == 300 - reserve_for(2, 5.0)
    policy.defence = SimpleNamespace(any=True)
    assert policy._plan_budget(empire) == 0


def test_a_rules_mismatch_with_the_game_blocks_the_buy_and_is_logged(caplog):
    caplog.set_level(logging.INFO)
    for lines in (["PTILE|7|7|60|5|0", "PMISS|4|4"], ["PTILE|7|7|60|5|0", "POWN|5|5|0|0"]):
        game, reads = setup(lines)
        assert not consider(planner_at(), game, reads, city(), offered=[OwnedTile(5, 5, 1)])
        assert game.bought == []
    assert "action=rules_mismatch" in caplog.text


def test_a_refused_purchase_is_logged_and_not_counted(caplog):
    caplog.set_level(logging.INFO)
    game, reads = setup(reply="Error: not enough gold")
    p = planner_at()
    assert not consider(p, game, reads, city())
    assert "action=purchase_refused" in caplog.text
    assert p._buys == {}


def test_a_buy_the_game_will_not_place_on_stops_further_buys_until_the_signature_changes(caplog):
    caplog.set_level(logging.INFO)
    conn = Conn(sig={0: "1"}, plan=PLAN_LINES)
    game = BuyGame(conn)
    reads = PlacementReads(game)
    p, c = planner_at(), city()
    assert consider(p, game, reads, c)
    assert not run(p.placed(reads, c, "DISTRICT_CAMPUS", [OwnedTile(1, 1, 0)]))
    assert "action=buy_failed_place" in caplog.text
    p.begin_turn(110, 500, 5.0)
    reads = PlacementReads(game)
    assert not consider(p, game, reads, c)  # Same signature: no second tile.
    conn.sig = {0: "2"}
    p.begin_turn(120, 500, 5.0)
    assert consider(p, game, PlacementReads(game), c)


def test_a_placed_buy_is_confirmed():
    game, reads = setup()
    p, c = planner_at(), city()
    consider(p, game, reads, c)
    assert run(p.placed(reads, c, "DISTRICT_CAMPUS", [OwnedTile(7, 7, 0)]))


def test_two_cities_never_claim_one_tile():
    game, reads = setup()
    p = planner_at()
    assert consider(p, game, reads, city())
    p.begin_turn(100, 500, 5.0)  # The claim lasts the turn it was made, so keep it.
    p._claimed = {(7, 7): 65536}
    other = city(city_id=65537)
    assert not consider(p, game, PlacementReads(game), other, item="DISTRICT_THEATER")
    assert len(game.bought) == 1


def test_a_tile_another_city_is_saving_for_is_left_alone():
    game, reads = setup(["PTILE|7|7|300|5|0"])
    p = planner_at(budget=100, income=50)
    p.book.add(SavingTarget(65537, "Other", "DISTRICT_CAMPUS", 7, 7, 300, 100))
    assert not consider(p, game, reads, city())
    assert p.book.targets.keys() == {(65537, "DISTRICT_CAMPUS")}


def test_an_unaffordable_tile_becomes_a_saving_target_and_logs_the_plan(caplog):
    caplog.set_level(logging.INFO)
    game, reads = setup(["PTILE|7|7|100|5|0"])
    p = planner_at(budget=60, income=10)
    assert not consider(p, game, reads, city())
    assert (65536, "DISTRICT_CAMPUS") in p.book.targets
    assert "placement_plan city=Alpha item=DISTRICT_CAMPUS tile=(7,7) action=save cost=100" in (
        caplog.text
    )


def test_a_wait_for_border_is_logged_once(caplog):
    caplog.set_level(logging.INFO)
    game, reads = setup(["PTILE|8|7|-1|6|0"])
    p, c = planner_at(), city()
    consider(p, game, reads, c)
    consider(p, game, reads, c)
    assert caplog.text.count("action=wait_for_border") == 1


def test_without_a_connection_nothing_is_bought():
    p = planner_at()
    game = SimpleNamespace(purchase_tile=None)
    assert not consider(p, game, PlacementReads(game), city())


def test_the_plan_query_tests_the_games_rules():
    lua = placement.build_plan_query(65536, "DISTRICT", "DISTRICT_HARBOR", ((5, 5),))
    for needle in (
        "District_ValidTerrains",
        "District_ValidFeatures",
        "GetPlotPurchaseCost",
        "RequiresRiver",
        "AdjacentDistrict",
        "GetOperationTargets" if False else "GetCommandTargets",
        '["5,5"] = true',
        "PMISS",
    ):
        assert needle in lua
    wonder = placement.build_plan_query(65536, "BUILDING", "BUILDING_PYRAMIDS")
    assert "Building_ValidTerrains" in wonder and "Building_ValidFeatures" in wonder


def test_the_plan_is_parsed_and_stray_lines_are_ignored():
    read = placement.parse_plan(
        ["junk", "PTILE|1|2|50|3|1", "PTILE|bad", "POWN|5|5|2|1", "POWN|6|6|0|0", "PMISS|9|9"]
    )
    assert read.tiles == [PlanTile(1, 2, 50, 3, 1)] and read.tiles[0].net == 2
    assert read.displacement == {(5, 5): 2, (6, 6): 0}
    assert read.unpredicted == [(6, 6)] and read.missed == [(9, 9)] and not read.agrees


# ---- the gold policy buys or saves for the plan ----


class TileGame(fx.FakeGame):
    def __init__(self, gold=500, per_turn=10.0, **data):
        data.setdefault("threats", [])
        data.setdefault("overview", fx.overview(gold=gold, gold_per_turn=per_turn, num_cities=2))
        super().__init__(**data)
        self.bought: list[tuple] = []

    async def purchase_tile(self, city_id, x, y):
        self.bought.append((city_id, x, y))
        return f"OK:TILE_PURCHASED|({x},{y})|cost:1"

    async def get_purchasable_tiles(self, city_id):
        return []

    async def purchase_item(self, *a, **k):
        return "Error"


def target(cost=60, city_id=65536, item="DISTRICT_CAMPUS"):
    return SavingTarget(city_id, "Alpha", item, 7, 7, cost, 100)


def gold_run(game, book, defence=None):
    policy = GoldPolicy(plans=book, defence=defence)
    asyncio.run(policy.run(game))


def test_gold_buys_the_planned_tile_when_it_fits_and_forgets_it(caplog):
    caplog.set_level(logging.INFO)
    book = PlanBook()
    book.add(target())
    game = TileGame(overview=fx.overview(turn=100, gold=500, gold_per_turn=10, num_cities=2))
    gold_run(game, book)
    assert game.bought == [(65536, 7, 7)] and not book.targets
    assert "action=bought cost=60" in caplog.text


def test_gold_saves_for_a_planned_tile_it_cannot_yet_afford(caplog):
    caplog.set_level(logging.INFO)
    book = PlanBook()
    book.add(target(cost=130))
    game = TileGame(overview=fx.overview(turn=100, gold=100, gold_per_turn=20, num_cities=2))
    gold_run(game, book)
    assert game.bought == []
    assert "saving toward tile (7,7) for DISTRICT_CAMPUS in Alpha" in caplog.text


def test_gold_buys_one_planned_tile_per_city_a_turn():
    book = PlanBook()
    book.add(target(item="DISTRICT_CAMPUS"))
    book.add(target(item="DISTRICT_THEATER"))
    game = TileGame(overview=fx.overview(turn=100, gold=900, gold_per_turn=10, num_cities=2))
    gold_run(game, book)
    assert len(game.bought) == 1


def test_gold_leaves_planned_tiles_while_a_city_is_threatened():
    book = PlanBook()
    book.add(target())
    game = TileGame(overview=fx.overview(turn=100, gold=900, gold_per_turn=10, num_cities=2))
    from civ6_ai.policies.defence import CityThreat, DefenceState

    defence = DefenceState()
    defence.cities = {65536: CityThreat.__new__(CityThreat)}
    gold_run(game, book, defence=defence)
    assert book.current(100 + placement.PLAN_MAX_AGE)
    assert not book.current(100 + placement.PLAN_MAX_AGE + 1)


# ---- a signature change does not make a district win ----


def lq_option(kind, item, cost=60, turns=5):
    return lq.ProductionOption(kind, item, cost, turns, -1)


class PickGame(fx.FakeGame):
    def __init__(self, options, districts=(), cities=None, conn=None):
        super().__init__(
            cities=cities or [fx.city_info(currently_building="nothing", buildings=["PALACE"])],
            production=options,
            overview=fx.overview(num_cities=1),
            threats=[],
        )
        self.set_calls: list[str] = []
        self.advisor_calls = 0
        self.districts = list(districts)
        self.conn = conn

    async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
        self.set_calls.append(item_name)
        return f"PRODUCING|{item_name}|5"

    async def get_district_advisor(self, city_id, district_type):
        self.advisor_calls += 1
        return self.districts


def choose(game, memory, reads, options=None):
    c = city()
    options = options or run(_read_options(game))
    return run(
        choose_for_city(
            game,
            c,
            options,
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=memory,
            reads=reads,
        )
    )


async def _read_options(game):
    from civ6_ai.state import read_production

    return await read_production(game, 65536)


def test_a_signature_change_with_a_stronger_option_still_picks_that_option_and_makes_no_call():
    memory, c = RefusalMemory(), city()
    memory.refused(c, "DISTRICT_HARBOR", 90, "ERR:NO_PLACEMENT|x", PLACEMENT, "1:9")
    conn = Conn(sig={0: "2"})  # Changed.
    # A Library is worth far more than a Harbor to this city, so it wins and the Harbor is not
    # even looked at: no signature read, no advisor call.
    game = PickGame(
        [
            lq_option("BUILDING", "BUILDING_LIBRARY", turns=2),
            lq_option("DISTRICT", "DISTRICT_HARBOR", turns=40),
        ],
        conn=conn,
    )
    picked = choose(game, memory, PlacementReads(game))
    assert picked.item in {"BUILDING_LIBRARY", "UNIT_WARRIOR"} or picked.category != "DISTRICT"
    assert game.advisor_calls == 0 and conn.calls == []


def test_a_winning_district_with_no_tile_falls_through_to_the_next_option_the_same_turn():
    memory = RefusalMemory()
    conn = Conn(sig={0: "1"})
    game = PickGame(
        [
            lq_option("DISTRICT", "DISTRICT_HARBOR", turns=1),
            lq_option("BUILDING", "BUILDING_LIBRARY", turns=9),
        ],
        districts=[],
        conn=conn,
    )
    picked = choose(game, memory, PlacementReads(game))
    assert picked is not None and picked.item == "BUILDING_LIBRARY"
    assert game.set_calls == ["BUILDING_LIBRARY"]
    assert memory.has(city(), "DISTRICT_HARBOR")
    # And asked about only once while nothing changes.
    before = game.advisor_calls
    choose(game, memory, PlacementReads(game))
    assert game.advisor_calls == before


def test_a_changed_signature_makes_the_item_eligible_but_gives_it_no_priority():
    memory, c = RefusalMemory(), city()
    memory.refused(c, "DISTRICT_HARBOR", 90, "ERR:NO_PLACEMENT|x", PLACEMENT, "1:9")
    options = [
        lq_option("DISTRICT", "DISTRICT_HARBOR", turns=1),
        lq_option("BUILDING", "BUILDING_LIBRARY", turns=9),
    ]
    ctx = ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8)
    from civ6_ai.policies.production import rank_production

    want = rank_production(c, [_choice(o) for o in options], ctx, ProductionWeights())
    game = PickGame(options, districts=[lq.DistrictPlacement(5, 5, {}, 1, "Plains")])
    game.conn = Conn(sig={0: "2"})
    picked = choose(game, memory, PlacementReads(game))
    assert picked.item == want[0].item  # Exactly what the scoring alone says.


def _choice(opt):
    from civ6_ai.state import ProductionChoice

    return ProductionChoice(opt.category, opt.item_name, opt.cost, opt.turns)


def test_a_city_in_the_middle_of_a_build_is_not_switched_by_a_signature_change():
    memory, c = RefusalMemory(), city()
    memory.refused(c, "DISTRICT_HARBOR", 90, "ERR:NO_PLACEMENT|x", PLACEMENT, "1:9")
    conn = Conn(sig={0: "2"})
    busy = fx.city_info(currently_building="BUILDING_LIBRARY")
    game = PickGame([lq_option("DISTRICT", "DISTRICT_HARBOR")], cities=[busy], conn=conn)
    policy = CityProductionPolicy()
    policy.refusals = memory
    run(policy.assign(game))
    assert game.set_calls == [] and game.advisor_calls == 0 and conn.calls == []


def test_a_refusal_event_carries_its_reason():
    events = []
    memory = RefusalMemory()
    game = PickGame(
        [
            lq_option("DISTRICT", "DISTRICT_HARBOR", turns=1),
            lq_option("BUILDING", "BUILDING_LIBRARY", turns=9),
        ],
        districts=[],
        conn=Conn(sig={0: "1"}),
    )
    c = city()
    run(
        choose_for_city(
            game,
            c,
            run(_read_options(game)),
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=memory,
            emit=events.append,
            reads=PlacementReads(game),
        )
    )
    refused = [e for e in events if e.kind == "production_refused"]
    assert refused and refused[0].reason_class == PLACEMENT and refused[0].backoff_turns > 0


def test_a_cannot_start_is_diagnosed_once_and_remembered_with_its_unlock():
    events = []
    memory = RefusalMemory()
    conn = Conn(sig={0: "1"}, diag="PDIAG|0|TECH_MASONRY||")

    class Refusing(PickGame):
        async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
            self.set_calls.append(item_name)
            if item_name == "BUILDING_X":
                return "Error: CANNOT_START|BUILDING_X cannot start."
            return f"PRODUCING|{item_name}|5"

    game = Refusing(
        [lq_option("BUILDING", "BUILDING_X", turns=1), lq_option("BUILDING", "BUILDING_LIBRARY")],
        conn=conn,
    )
    run(
        choose_for_city(
            game,
            city(),
            run(_read_options(game)),
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=memory,
            emit=events.append,
            reads=PlacementReads(game),
        )
    )
    refused = [e for e in events if e.kind == "production_refused"]
    if refused:  # BUILDING_X may rank below the monument; when tried it is diagnosed.
        assert refused[0].reason_class == UNLOCK
        assert memory.watched() == ("TECH_MASONRY",)
        assert conn.calls.count("diag") == 1


def test_the_district_chain_buys_then_places_on_the_new_tile(caplog):
    caplog.set_level(logging.INFO)
    conn = Conn(sig={0: "1"}, plan=["PTILE|7|7|60|5|0"])

    class Chain(PickGame):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.bought = []

        async def purchase_tile(self, city_id, x, y):
            self.bought.append((x, y))
            self.districts = [lq.DistrictPlacement(x, y, {}, 5, "Plains")]
            return f"OK:TILE_PURCHASED|({x},{y})|cost:60"

        async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
            self.set_calls.append((item_name, x, y))
            return f"PRODUCING|{item_name}|5"

    game = Chain([lq_option("DISTRICT", "DISTRICT_HARBOR", turns=2)], districts=[], conn=conn)
    planner = planner_at()
    c = city()
    picked = run(
        choose_for_city(
            game,
            c,
            run(_read_options(game)),
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=RefusalMemory(),
            reads=PlacementReads(game),
            planner=planner,
        )
    )
    assert picked.item == "DISTRICT_HARBOR"
    assert game.bought == [(7, 7)]
    assert game.set_calls == [("DISTRICT_HARBOR", 7, 7)]
    assert "action=buy_then_place cost=60" in caplog.text
    assert planner._reserved[c.city_id][(7, 7)][0] == "DISTRICT_HARBOR"


def test_the_chain_logs_buy_failed_place_when_the_game_still_has_no_tile(caplog):
    caplog.set_level(logging.INFO)
    conn = Conn(sig={0: "1"}, plan=["PTILE|7|7|60|5|0"])

    class NoTake(PickGame):
        async def purchase_tile(self, city_id, x, y):
            return f"OK:TILE_PURCHASED|({x},{y})|cost:60"

    game = NoTake([lq_option("DISTRICT", "DISTRICT_HARBOR", turns=2)], districts=[], conn=conn)
    planner = planner_at()
    picked = run(
        choose_for_city(
            game,
            city(),
            run(_read_options(game)),
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=RefusalMemory(),
            reads=PlacementReads(game),
            planner=planner,
        )
    )
    assert picked is None or picked.item != "DISTRICT_HARBOR"
    assert "action=buy_failed_place" in caplog.text


def test_a_saving_target_is_dropped_when_an_owned_tile_becomes_available():
    game, reads = setup(["PTILE|7|7|150|5|0"])
    p, c = planner_at(budget=100, income=50), city()
    assert not consider(p, game, reads, c, item="DISTRICT_HARBOR")
    assert (c.city_id, "DISTRICT_HARBOR") in p.book.targets
    # Border growth supplies an owned tile as good as the bought one would be.
    game, reads = setup(["PTILE|7|7|150|5|0", "POWN|5|5|0|1"])
    assert not consider(p, game, reads, c, item="DISTRICT_HARBOR", offered=[OwnedTile(5, 5, 5)])
    assert p.book.targets == {}


def test_starting_the_item_drops_its_saving_target():
    conn = Conn(sig={0: "1"})
    game = PickGame([lq_option("DISTRICT", "DISTRICT_HARBOR", turns=1)], conn=conn)
    game.districts = [lq.DistrictPlacement(5, 5, {}, 1, "Plains")]
    planner = planner_at()
    planner.book.add(SavingTarget(65536, "Alpha", "DISTRICT_HARBOR", 7, 7, 100, 100))
    run(
        choose_for_city(
            game,
            city(),
            run(_read_options(game)),
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=RefusalMemory(),
            reads=PlacementReads(game),
            planner=planner,
        )
    )
    assert planner.book.targets == {}


def test_the_item_is_placed_on_the_tile_that_was_bought_not_the_best_raw_adjacency():
    # The owned +5 tile has displacement 3 (net 2); the bought +4 tile is clean (net 4).
    conn = Conn(sig={0: "1"}, plan=["PTILE|7|7|50|4|0", "POWN|5|5|3|1"])

    class Chain(PickGame):
        async def purchase_tile(self, city_id, x, y):
            self.districts = [
                lq.DistrictPlacement(5, 5, {}, 5, "Plains"),
                lq.DistrictPlacement(x, y, {}, 4, "Plains"),
            ]
            return f"OK:TILE_PURCHASED|({x},{y})|cost:50"

        async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
            self.set_calls.append((item_name, x, y))
            return f"PRODUCING|{item_name}|5"

    game = Chain(
        [lq_option("DISTRICT", "DISTRICT_HARBOR", turns=1)],
        districts=[lq.DistrictPlacement(5, 5, {}, 5, "Plains")],
        conn=conn,
    )
    run(
        choose_for_city(
            game,
            city(),
            run(_read_options(game)),
            ProductionContext(turn=100, num_cities=4, builders=4, recon=1, army=8),
            ProductionWeights(),
            memory=RefusalMemory(),
            reads=PlacementReads(game),
            planner=planner_at(),
        )
    )
    assert game.set_calls == [("DISTRICT_HARBOR", 7, 7)]


def test_gold_does_not_buy_planned_tiles_while_saving_for_an_upgrade():
    book = PlanBook()
    book.add(target(cost=50))
    unit = fx.unit_info(
        unit_id=1,
        unit_index=1,
        can_upgrade=True,
        upgrade_target="UNIT_SWORDSMAN",
        upgrade_cost=200,
    )
    game = TileGame(
        units=[unit],
        overview=fx.overview(turn=100, gold=150, gold_per_turn=20, num_cities=2),
    )
    gold_run(game, book)
    assert game.bought == []


def test_plan_steps_are_published_as_events():
    events = []
    game, reads = setup(["PTILE|7|7|100|5|0"])
    p, c = Planner(emit=events.append), city()
    p.begin_turn(100, 60, 10.0)
    consider(p, game, reads, c)
    assert [(e.kind, e.action, e.tile_x, e.tile_y, e.cost) for e in events] == [
        ("placement_plan", "save", 7, 7, 100)
    ]
    game, reads = setup(["PTILE|7|7|60|5|0"])
    p.begin_turn(101, 500, 10.0)
    consider(p, game, reads, c, item="DISTRICT_HARBOR")
    run(p.placed(reads, c, "DISTRICT_HARBOR", []))
    assert [e.action for e in events][-2:] == ["buy_then_place", "buy_failed_place"]
    assert events[-1].item == "DISTRICT_HARBOR" and events[-1].category == "DISTRICT"


def test_a_placement_refusal_is_asked_again_after_five_turns_but_only_three_times_a_signature():
    mem, c = RefusalMemory(), city()
    _, same = sig_reads("5")
    turn, waits = 10, []
    for _ in range(3):
        waits.append(
            mem.refused(c, "DISTRICT_HARBOR", turn, "ERR:NO_PLACEMENT|x", PLACEMENT, "5:9")
        )
        assert run(mem.blocked(c, "DISTRICT_HARBOR", turn + 4, same))
        turn += 5
        if len(waits) < 3:
            assert not run(mem.blocked(c, "DISTRICT_HARBOR", turn, same))
    assert waits == [5, 5, -1]
    assert run(mem.blocked(c, "DISTRICT_HARBOR", turn + 100, same))


def test_the_turn_limit_comes_from_the_game_else_the_speed():
    assert placement.last_turn_for("GAMESPEED_QUICK", 0) == 330
    assert placement.last_turn_for("GAMESPEED_QUICK", 250) == 250
    assert placement.last_turn_for("", 0) == placement.DEFAULT_LAST_TURN
    assert placement.speed_scale("GAMESPEED_QUICK") == 0.67
    assert placement.speed_scale("unknown") == placement.GAME_SPEED_COST_SCALE


def test_the_planner_takes_its_limit_and_cost_scale_from_the_game_speed():
    game, reads = setup()
    p = Planner()
    p.begin_turn(295, 500, 5.0, "GAMESPEED_QUICK", 0)
    assert p.last_turn == 330 and p.scale == 0.67
    assert not consider(p, game, reads, city())  # 35 turns left of 330: too late.
    p.begin_turn(285, 500, 5.0, "GAMESPEED_QUICK", 0)
    assert consider(p, game, reads, city())


def test_quick_speed_shrinks_the_gold_caps():
    assert placement.gold_cap(2, 0.67) == int(50 * 0.67)
    owned = [OwnedTile(5, 5, 2)]
    assert decide([tile(adj=4, cost=40)], owned, 500, 5, 1.0).action == "buy_then_place"
    assert decide([tile(adj=4, cost=40)], owned, 500, 5, 0.67) is None


def test_a_tile_the_border_claims_within_four_turns_is_waited_for_not_bought():
    game, reads = setup(["PTILE|7|7|60|5|0", "PBORDER|3|7|7"])
    p = planner_at()
    assert not consider(p, game, reads, city())
    assert game.bought == []


def test_a_tile_the_border_claims_later_or_another_tile_is_bought():
    game, reads = setup(["PTILE|7|7|60|5|0", "PBORDER|5|7|7"])
    assert consider(planner_at(), game, reads, city())
    game, reads = setup(["PTILE|7|7|60|5|0", "PBORDER|2|9|9"])
    assert consider(planner_at(), game, reads, city())


def test_an_unconfirmed_border_api_is_logged_and_does_not_stop_the_buy(caplog):
    caplog.set_level(logging.INFO)
    game, reads = setup(["PTILE|7|7|60|5|0", "PBORDER|?"])
    assert consider(planner_at(), game, reads, city())
    assert "action=border_unknown" in caplog.text


def test_the_plan_query_reads_the_border_with_pcall():
    lua = placement.build_plan_query(65536, "DISTRICT", "DISTRICT_CAMPUS")
    assert "GetTurnsUntilExpansion" in lua and "GetNextPlot" in lua and "PBORDER" in lua
    assert placement.parse_plan(["PBORDER|3|7|8"]).border == (3, 7, 8)
    assert placement.parse_plan(["PBORDER|?"]).border is None
