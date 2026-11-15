import pytest

from civ6_ai.policies import valuation as v
from civ6_ai.policies.production import ProductionWeights

W = ProductionWeights()


def test_horizon_is_capped_discounted_and_ends_with_the_game():
    assert v.horizon(100, 330) == v.horizon(200, 330)  # Both beyond the cap.
    assert 0 < v.horizon(100, 330) < v.HORIZON_CAP
    assert v.horizon(320, 330) < v.horizon(300, 330) < v.horizon(200, 330)
    assert v.horizon(330, 330) == 0 and v.horizon(340, 330) == 0


def test_floor_falls_as_spare_currency_idles_and_has_a_minimum():
    assert v.floor(0, 50) == pytest.approx(v.ROI_FLOOR)
    assert v.floor(500, 50) < v.floor(100, 50) < v.ROI_FLOOR
    assert v.floor(10**9, 50) == v.ROI_FLOOR_MIN
    assert v.floor(0, -5) == pytest.approx(v.ROI_FLOOR)


def test_buy_now_beats_saving_follows_the_two_item_rule():
    # B is nearly affordable: only an item at least as good per gold as B goes first.
    assert not v.buy_now_beats_saving(50, 100, 100, 1)  # Worse per gold, B one gold away.
    assert v.buy_now_beats_saving(100, 100, 100, 1)  # Equal per gold.
    # B is far off: even a modest item may go first.
    assert v.buy_now_beats_saving(50, 100, 100, 1000)
    assert v.buy_now_beats_saving(10, 0, 100, 5)  # Free is always fine.


def test_a_building_not_in_production_table_is_valued_from_the_extra_table():
    zoo = v.catalogue_valuation("BUILDING", "BUILDING_ZOO", W)
    unknown = v.catalogue_valuation("BUILDING", "BUILDING_MADE_UP", W)
    assert zoo.rate == pytest.approx(3.0) and unknown.rate == 1.0


def test_science_building_is_worth_more_than_a_culture_one_at_the_default_weights():
    lab = v.catalogue_valuation("BUILDING", "BUILDING_RESEARCH_LAB", W)
    zoo = v.catalogue_valuation("BUILDING", "BUILDING_ZOO", W)
    assert lab.rate > zoo.rate


def priced(label, cost, rate, turn=100, last=330):
    return v.price(label, cost, v.Valuation(rate), turn, last)


def test_pick_target_takes_the_best_return_not_the_cheapest():
    items = [priced("cheap", 300, 1.0), priced("best", 600, 5.0), priced("dear", 1200, 5.0)]
    target, wait = v.pick_target(items, 100, 100.0, 100, 330)
    assert target.label == "best" and wait == pytest.approx(5.0)


def test_pick_target_needs_income_and_time_left():
    items = [priced("a", 600, 5.0)]
    assert v.pick_target(items, 100, 0.0, 100, 330) is None
    assert v.pick_target(items, 100, 100.0, 325, 330) is None  # 5 turns to go, 5 needed.
    assert v.pick_target(items, 100, 1.0, 100, 330) is None  # 500 turns away.


def test_pick_target_skips_items_below_the_floor_and_affordable_ones():
    weak = [priced("weak", 5000, 1.0), priced("owned", 50, 9.0)]
    assert v.pick_target(weak, 100, 100.0, 100, 330) is None


def test_defence_is_always_bought_and_never_a_target():
    item = v.price("d", 500, v.Valuation(3.0, always=True), 100, 330)
    assert item.roi == v.DEFENCE_ROI
    assert v.pick_target([item], 0, 100.0, 100, 330) is None


def test_great_person_value_follows_class_weight_era_and_the_end_of_the_game():
    from civ6_ai.policies.secondary import SecondaryWeights
    from test_policy_faith import person

    sw = SecondaryWeights()
    scientist = v.great_person_valuation(person(cls="Great Scientist"), sw, 100, 330)
    general = v.great_person_valuation(person(cls="Great General"), sw, 100, 330)
    later = v.great_person_valuation(person(era_name="Modern"), sw, 100, 330)
    assert scientist.fixed > general.fixed
    assert later.fixed > scientist.fixed
    assert scientist.at(325, 330) < scientist.at(100, 330) / 3


def test_a_great_person_target_is_valued_when_it_is_reached_not_today():
    from civ6_ai.policies.secondary import SecondaryWeights
    from test_policy_faith import person

    worth = v.great_person_valuation(person(), SecondaryWeights(), 315, 330)
    assert worth.at(315, 330, wait=6) < worth.at(315, 330)
    item = v.price("gp", 600, worth, 315, 330)
    target, _ = v.pick_target([item], 0, 100.0, 315, 330)
    assert target.value < item.value
