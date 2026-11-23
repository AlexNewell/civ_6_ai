import asyncio
import json
import logging

import pytest
from civ_mcp import lua as lq

import replay_builds as rb
from civ6_ai.policies.build_value import (
    LOG_TAG,
    BuildValueSettings,
    DecisionInputs,
    OptionInput,
    discounted_turns,
    log_line,
    options_from_record,
    rerank,
    value_option,
)
from civ6_ai.policies.production import (
    ProductionContext,
    ProductionWeights,
    choose_for_city,
    rank_production,
)
from civ6_ai.state import ProductionChoice
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx

S = BuildValueSettings()


def opt(item, category="BUILDING", cost=60, turns=6, score=1.0, repair=False):
    return OptionInput(item, category, cost, turns, score, repair)


def inputs(**changes):
    base = dict(
        turn=30,
        last_turn=330,
        pop=4,
        yields=(3.0, 4.0, 2.0, 3.0, 2.0, 0.0),
        num_cities=2,
        settler_cap=4,
        sites_ok=True,
        garrisoned=True,
    )
    return DecisionInputs(**{**base, **changes})


def city():
    return build_city(fx.city_info(), (), set())


def test_discounting_matches_the_valuation_horizon_and_the_game_end():
    from civ6_ai.policies.valuation import horizon

    assert discounted_turns(0, None, S) == pytest.approx(horizon(0, 10**6))
    # Three turns of benefit left after a delay of five.
    assert discounted_turns(5, 8, S) == pytest.approx(0.97**5 * (1 - 0.97**3) / 0.03)
    assert discounted_turns(10, 8, S) == 0.0


def test_a_building_is_its_discounted_yield_less_its_production():
    v = value_option(opt("BUILDING_MONUMENT", cost=30, turns=6), inputs(), S)
    benefit = 2.0 * discounted_turns(6, 300, S)  # 2 culture a turn, weight 1.
    assert v.benefit == pytest.approx(benefit)
    assert v.net == pytest.approx(benefit - 30)
    assert v.value == pytest.approx(v.net / 6)


def test_a_build_late_in_the_game_is_a_loss():
    v = value_option(opt("BUILDING_LIBRARY", cost=90), inputs(turn=325), S)
    assert v.value < 0 and v.benefit == 0.0


def test_ranking_by_net_total_is_a_setting():
    a = BuildValueSettings(per_build_turn=False)
    v = value_option(opt("BUILDING_MONUMENT", cost=30, turns=6), inputs(), a)
    assert v.value == pytest.approx(v.net)


@pytest.mark.parametrize(
    "option, why",
    [
        (opt("BUILDING_NO_SUCH_THING"), "no yield profile"),
        (opt("BUILDING_WALLS"), "walls"),
        (opt("BUILDING_BARRACKS"), "no yield profile"),
        (opt("PROJECT_X", "PROJECT"), "project"),
        (opt("UNIT_WARRIOR", "UNIT", 40), "no threat-map reading"),
        (opt("UNIT_TRADER", "UNIT", 40), None),
    ],
)
def test_what_cannot_be_valued_says_so_and_invents_nothing(option, why):
    v = value_option(option, inputs(), S)
    if why is None:
        assert v.value is not None
    else:
        assert v.value is None and why in v.reason


def test_a_wonder_has_no_value():
    from civ6_ai.policies.game_data import WONDERS

    wonder = sorted(WONDERS)[0]
    assert value_option(opt(wonder), inputs(), S).value is None


def test_a_housing_item_is_worth_the_citizens_the_cap_costs():
    capped = inputs(growth={"stance": "grow", "housing_gap": 2, "food_gap": 0, "gap": 3})
    free = inputs(growth={"stance": "grow", "housing_gap": 0, "food_gap": 0, "gap": 3})
    a = value_option(opt("DISTRICT_AQUEDUCT", "DISTRICT", 60, 8), capped, S)
    b = value_option(opt("DISTRICT_AQUEDUCT", "DISTRICT", 60, 8), free, S)
    assert a.benefit > 0 and b.benefit == 0
    assert value_option(opt("DISTRICT_AQUEDUCT", "DISTRICT"), inputs(), S).value is None


def test_food_follows_the_growth_plan():
    held = inputs(growth={"stance": "hold", "housing_gap": 0, "food_gap": 0, "gap": 0})
    short = inputs(growth={"stance": "grow", "housing_gap": 0, "food_gap": 2, "gap": 2})
    a = value_option(opt("BUILDING_GRANARY"), held, S)
    b = value_option(opt("BUILDING_GRANARY"), short, S)
    assert b.benefit > a.benefit > 0


def test_a_holy_site_counts_its_adjacency_when_read():
    unread = value_option(opt("DISTRICT_HOLY_SITE", "DISTRICT"), inputs(), S)
    read = value_option(opt("DISTRICT_HOLY_SITE", "DISTRICT"), inputs(holy_site_adjacency=3), S)
    assert "adjacency 3" in read.reason and read.benefit != unread.benefit


def test_a_settler_is_the_expected_city_and_worthless_without_a_site():
    settler = opt("UNIT_SETTLER", "UNIT", 80, 8)
    ok = value_option(settler, inputs(), S)
    none = value_option(settler, inputs(sites_ok=False), S)
    capped = value_option(settler, inputs(num_cities=4), S)
    assert ok.benefit > 0 and "expected city" in ok.reason
    assert none.benefit == 0 and none.value < 0 and "no safe site" in none.reason
    assert capped.benefit == 0 and "cap" in capped.reason


def test_a_settler_gets_better_with_a_better_city():
    settler = opt("UNIT_SETTLER", "UNIT", 80, 8)
    rich = value_option(settler, inputs(yields=(5, 8, 4, 6, 4, 1)), S)
    poor = value_option(settler, inputs(yields=(2, 2, 1, 1, 1, 0)), S)
    assert rich.value > poor.value


def test_builders_need_jobs():
    builder = opt("UNIT_BUILDER", "UNIT")
    assert value_option(builder, inputs(), S).value is None
    assert value_option(builder, inputs(unimproved=2), S).benefit > 0
    assert value_option(builder, inputs(spare_jobs=0), S).benefit == 0
    assert value_option(builder, inputs(idle_builders=1, spare_jobs=3), S).benefit == 0


def test_a_trader_needs_a_free_route_slot():
    trader = opt("UNIT_TRADER", "UNIT")
    assert value_option(trader, inputs(trade_capacity=1), S).benefit > 0
    assert value_option(trader, inputs(trade_capacity=1, traders=1), S).benefit == 0


def test_a_scout_unblocks_settlers_only_when_no_site_is_known():
    scout = opt("UNIT_SCOUT", "UNIT", 30, 3)
    assert value_option(scout, inputs(sites_ok=False, turn=5), S).benefit > 0
    assert value_option(scout, inputs(turn=5), S).value is None
    assert value_option(scout, inputs(recon=1), S).benefit == 0


READING = {"threat": 20.0, "fog": 0.0, "cover": 10.0, "damage": 15.0, "net": 12.0, "turns": 9.0}


def test_a_defender_is_the_loss_the_threat_map_says_it_avoids():
    sword = opt("UNIT_SWORDSMAN", "UNIT", 80, 5)
    v = value_option(sword, inputs(reading=READING), S)
    calm = value_option(sword, inputs(reading={**READING, "net": 0.0}), S)
    late = value_option(sword, inputs(reading={**READING, "turns": 2.0}), S)
    assert v.benefit > 80 and calm.benefit == 0 and late.benefit == 0
    assert "after the city falls" in late.reason


def test_an_old_pick_with_no_value_is_never_replaced():
    options = [opt("UNIT_WARRIOR", "UNIT", 40, 3), opt("BUILDING_MONUMENT", cost=30)]
    r = rerank(options, inputs(), S)
    assert not r.changed and r.new.item == "UNIT_WARRIOR"


def test_a_changed_pick_always_has_the_higher_value():
    options = [opt("BUILDING_GRANARY", cost=40), opt("BUILDING_LIBRARY", cost=40, turns=6)]
    r = rerank(options, inputs(), S)
    assert r.changed and r.new.item == "BUILDING_LIBRARY"
    assert r.new_value.value > r.old_value.value


def test_dropped_options_are_not_candidates_and_defence_only_keeps_to_defence():
    dropped = OptionInput("BUILDING_LIBRARY", "BUILDING", 40, 6, None)
    r = rerank([opt("BUILDING_GRANARY", cost=40), dropped], inputs(), S)
    assert not r.changed
    r = rerank(
        [opt("UNIT_SWORDSMAN", "UNIT", 80, 5), opt("BUILDING_LIBRARY")],
        inputs(reading=READING),
        S,
        defence_only=True,
    )
    assert r.new.item == "UNIT_SWORDSMAN"


def test_settings_reject_negative_numbers():
    with pytest.raises(ValueError):
        BuildValueSettings(route_yield=-1)


# --- The log line and the replay ---------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def choice(category, item, turns=5, cost=50):
    return ProductionChoice(category, item, cost, turns)


MENU = [
    choice("UNIT", "UNIT_WARRIOR", 4),
    choice("UNIT", "UNIT_SETTLER", 8, 80),
    choice("UNIT", "UNIT_BUILDER", 5),
    choice("UNIT", "UNIT_SCOUT", 3),
    choice("BUILDING", "BUILDING_MONUMENT", 5, 30),
    choice("BUILDING", "BUILDING_LIBRARY", 9, 90),
    choice("BUILDING", "BUILDING_WALLS", 10),
    choice("UNIT", "UNIT_TRADER", 6),
]
CTX = ProductionContext(turn=40, num_cities=2, builders=1, recon=1, army=3, trade_safe=False)


class Game(fx.FakeGame):
    def __init__(self):
        super().__init__(
            cities=[fx.city_info(currently_building="nothing", buildings=["PALACE"])],
            production=[lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 9, -1)],
            overview=fx.overview(num_cities=1),
            threats=[],
        )
        self.set_calls = []

    async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
        self.set_calls.append(item_name)
        return f"PRODUCING|{item_name}|5"


def pick(caplog):
    c = city()
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.production"):
        picked = run(choose_for_city(Game(), c, MENU, CTX, ProductionWeights(), planner=None))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(LOG_TAG)]
    return c, picked, lines


def payload(line):
    return json.loads(line.removeprefix(LOG_TAG + " "))


def test_one_structured_line_per_decision_lists_every_option(caplog):
    c, picked, lines = pick(caplog)
    assert len(lines) == 1
    record = payload(lines[0])
    assert [o[0] for o in record["options"] if o[4] is not None][0] == picked.item
    assert {o[0] for o in record["options"]} == {o.item for o in MENU}  # Trader: dropped, listed.
    trader = next(o for o in record["options"] if o[0] == "UNIT_TRADER")
    assert trader[4] is None
    assert record["inputs"]["pop"] == c.pop and record["city"] == c.name


def test_identical_inputs_give_the_current_picks_unchanged(caplog):
    """The logging reads only held state: the pick is the one `rank_production` makes, and the
    replay's old pick is that same pick."""
    c, picked, lines = pick(caplog)
    expected = rank_production(c, MENU, CTX, ProductionWeights())[0]
    assert picked.item == expected.item
    record = payload(lines[0])
    assert options_from_record(record)[0].item == expected.item
    # With the logger off the pick is the same.
    assert run(choose_for_city(Game(), c, MENU, CTX, ProductionWeights())).item == expected.item
    assert rb.replay_records("x", [record]).decisions == 1


def test_the_line_stays_small(caplog):
    _, _, lines = pick(caplog)
    assert len(lines[0]) < 3000  # 8 options here; 48 offered measure 2.8 KB.


def test_a_record_round_trips_its_inputs(caplog):
    _, _, lines = pick(caplog)
    record = payload(lines[0])
    again = DecisionInputs.from_dict(record["inputs"])
    assert DecisionInputs.from_dict(again.to_dict()) == again
    assert log_line(record) == lines[0].removeprefix(LOG_TAG + " ")


def make_record(items, **ins):
    return {
        "turn": 12,
        "city": "Roma",
        "inputs": inputs(**ins).to_dict(),
        "options": [list(i) for i in items],
    }


def test_the_replay_reports_changes_and_flags_a_displaced_settler():
    record = make_record(
        [
            ("UNIT_SETTLER", "UNIT", 80, 8, 14.0, 0),
            ("BUILDING_LIBRARY", "BUILDING", 40, 3, 5.0, 0),
        ],
        sites_ok=False,
    )
    report = rb.replay_records("r", [record])
    assert len(report.changes) == 1
    assert report.changes[0].flags == ["displaces a settler"]
    text = rb.render("r", report, 10)
    assert "T12 Roma" in text and "UNIT_SETTLER" in text and "displaces a settler" in text


def test_the_replay_counts_options_with_no_value():
    record = make_record(
        [
            ("BUILDING_MONUMENT", "BUILDING", 30, 5, 9.0, 0),
            ("BUILDING_WALLS", "BUILDING", 50, 9, 0.0, 0),
        ]
    )
    report = rb.replay_records("r", [record])
    assert report.options_total == 2 and report.options_unvalued == 1


def test_the_replay_flags_a_defender_displaced_in_a_threatened_city():
    record = make_record(
        [
            ("UNIT_SWORDSMAN", "UNIT", 80, 5, 20.0, 0),
            ("BUILDING_MONUMENT", "BUILDING", 30, 2, 5.0, 0),
        ],
        under_threat=True,
        reading={**READING, "net": 0.0},
        yields=(1, 1, 0, 0, 3, 0),
    )
    report = rb.replay_records("r", [record])
    assert report.changes and report.changes[0].flags == [
        "displaces a defender in a threatened city"
    ]


def test_an_old_log_cannot_be_replayed(tmp_path):
    (tmp_path / "old.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "old_bot.log").write_text(
        "2026-11-22 10:00:00,000 civ6_ai.policies.production INFO Roma options: "
        "BUILDING_LIBRARY 5.0, UNIT_SETTLER 4.0\n",
        encoding="utf-8",
    )
    assert rb.analyse(str(tmp_path / "old")) is None
    assert rb.render("old", None, 10) == "old: no structured options; cannot replay"


def test_the_script_reads_a_run_log(tmp_path, capsys):
    record = make_record(
        [
            ("BUILDING_GRANARY", "BUILDING", 40, 6, 5.0, 0),
            ("BUILDING_LIBRARY", "BUILDING", 40, 6, 4.0, 0),
        ]
    )
    (tmp_path / "new.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "new_bot.log").write_text(
        f"2026-11-22 10:00:00,000 civ6_ai.policies.production INFO "
        f"{LOG_TAG} {json.dumps(record)}\n",
        encoding="utf-8",
    )
    assert rb.main([str(tmp_path / "new")]) == 0
    out = capsys.readouterr().out
    assert "1 decisions, 1 changed picks" in out and "BUILDING_LIBRARY" in out


def test_a_settler_is_net_negative_on_a_young_capital_and_positive_on_a_rich_one():
    """Reported plainly, not tuned away: with one 60-turn horizon the settler (80 production) loses
    at the capital yields measured at turns 10 to 30 (25 to 31) and wins from about 40 (39)."""
    settler = opt("UNIT_SETTLER", "UNIT", 80, 10)
    young = inputs(turn=20, pop=2, yields=(6.0, 8.0, 4.0, 5.0, 3.0, 0.0))  # About 28.5 weighted.
    rich = inputs(turn=40, pop=3, yields=(8.0, 10.0, 6.0, 8.0, 5.0, 1.0))  # About 39.5.
    assert value_option(settler, young, S).net < 0
    assert value_option(settler, rich, S).net > 0


def test_walls_make_the_map_turns_a_wall_breach_not_a_fall_deadline():
    sword = opt("UNIT_SWORDSMAN", "UNIT", 80, 5)
    reading = {**READING, "turns": 2.0}
    assert value_option(sword, inputs(reading=reading), S).benefit == 0
    assert value_option(sword, inputs(reading=reading, has_walls=True), S).benefit > 0


def test_a_camp_unit_is_not_valued_as_a_defender():
    sword = opt("UNIT_SWORDSMAN", "UNIT", 80, 5)
    v = value_option(sword, inputs(reading={**READING, "net": 0.0}, camp_in_reach=True), S)
    assert v.value is None and "camp" in v.reason
    r = rerank([sword, opt("BUILDING_MONUMENT", cost=30)], inputs(camp_in_reach=True), S)
    assert not r.changed


def test_a_defence_only_decision_follows_the_live_guard():
    sword = opt("UNIT_SWORDSMAN", "UNIT", 80, 5)
    monument = opt("BUILDING_MONUMENT", cost=30)
    # The best option is not a defence option: live changes nothing, so there is no pick.
    assert rerank([monument, sword], inputs(reading=READING), S, defence_only=True).old is None
    # A defence option scored zero is refused live too.
    zero = opt("UNIT_SWORDSMAN", "UNIT", 80, 5, score=0.0)
    assert rerank([zero, monument], inputs(reading=READING), S, defence_only=True).old is None
    assert rerank([sword, monument], inputs(reading=READING), S, defence_only=True).old is sword


def test_a_changed_pick_lists_the_assumed_settings_it_rests_on():
    record = make_record(
        [
            ("UNIT_BUILDER", "UNIT", 50, 5, 9.0, 0),
            ("DISTRICT_HOLY_SITE", "DISTRICT", 60, 3, 5.0, 0),
        ],
        unimproved=1,
        holy_site_adjacency=3,
    )
    report = rb.replay_records("r", [record])
    change = report.changes[0]
    joined = " | ".join(change.sensitivity)
    assert "improvement_yield" in joined and "district_unlock_share" in joined
    assert "scout_unblock_share" not in joined  # Not used by either pick.
    assert "x0.5" in joined and "x2" in joined and "pick" in joined
    text = rb.render("r", report, 10)
    assert text.startswith("assumed settings:") and "rests on assumed settings" in text


def test_a_scout_unblocks_a_waiting_settler_and_a_capped_empire():
    scout = opt("UNIT_SCOUT", "UNIT", 30, 3)
    base = dict(sites_ok=False, turn=5, pop=3)
    assert value_option(scout, inputs(held_settlers=1, **base), S).benefit > 0
    assert (
        value_option(scout, inputs(num_cities=4, settlers=1, settler_cap=4, **base), S).benefit > 0
    )


def test_a_defender_that_finishes_after_the_game_ends_adds_nothing():
    sword = opt("UNIT_SWORDSMAN", "UNIT", 80, 5)
    v = value_option(sword, inputs(turn=328, last_turn=330, reading={**READING, "turns": 9.0}), S)
    assert v.benefit == 0
    assert value_option(sword, inputs(reading=READING), S).benefit > 0
