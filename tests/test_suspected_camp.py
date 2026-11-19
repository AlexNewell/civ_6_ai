"""Issue 293: a camp nobody has seen is inferred from where barbarians first appear."""

import json
import logging

from civ6_ai.policies.military import MilitaryPolicy
from civ6_ai.state.models import Threat
from civ6_ai.state.reader import build_city, build_unit
from civ6_ai.threat_map import Ally, ThreatMap
from fixtures.civ_state import city_info
from test_policy_military import MilitaryGame, run, scout, warrior


def hostile(kind="UNIT_COURSER", x=16, y=10):
    return Threat(63, "Barbarians", kind, x, y, 100, 100, 20, 0, 3, True)


def map_with_city():
    tm = ThreatMap()
    tm.allies = [Ally("city", 10, 10, 10.0)]
    return tm


def feed(tm, sightings):
    """Each (turn, x, y) is a new unit coming into view that turn, the last one gone by the next."""
    for turn, x, y in sightings:
        tm.see_threats(turn, [hostile(x=x, y=y)])
        tm.sources = []  # Forgotten, so the next sighting is an arrival, not the same unit.


EAST = [(20, 16, 10), (23, 17, 11), (27, 16, 9)]


def test_arrivals_from_one_area_on_several_turns_make_a_suspected_camp_beyond_them():
    tm = map_with_city()
    feed(tm, EAST)
    sus = tm.suspected_camp()
    assert sus is not None
    assert sus.arrivals == 3 and sus.turns == 3 and sus.city == (10, 10)
    assert sus.x > 16 and abs(sus.y - 10) <= 1  # On from the arrivals, away from the city.


def test_too_few_or_one_turns_arrivals_are_no_suspicion():
    tm = map_with_city()
    feed(tm, EAST[:2])
    assert tm.suspected_camp() is None
    tm = map_with_city()
    tm.see_threats(20, [hostile(x=16, y=10), hostile(x=17, y=11), hostile(x=16, y=9)])
    assert tm.suspected_camp() is None  # Three at once is a raid, not a trail.


def test_a_unit_seen_again_close_by_is_not_a_new_arrival():
    tm = map_with_city()
    for turn, x in ((20, 16), (21, 15), (22, 14), (23, 13)):
        tm.see_threats(turn, [hostile(x=x)])
    assert len(tm.arrivals) == 1


def test_arrivals_near_a_known_camp_or_far_from_our_cities_do_not_count():
    tm = map_with_city()
    tm.see_tiles(
        19, [__import__("test_threat_map").camp(x=18, y=10)]
    )  # A camp seen, so it explains these.
    feed(tm, EAST)
    assert tm.suspected_camp() is None
    tm = map_with_city()
    feed(tm, [(20, 40, 10), (21, 41, 11), (22, 40, 9)])  # Far from every city.
    assert tm.suspected_camp() is None


def test_a_unit_of_ours_beside_the_guess_clears_the_suspicion():
    tm = map_with_city()
    feed(tm, EAST)
    sus = tm.suspected_camp()
    tm.see_own(
        30, [build_unit(warrior(uid=1, x=sus.x - 1, y=sus.y))], [build_city(city_info(), [], [])]
    )
    assert tm.suspected_camp() is None


def test_a_camp_found_and_removed_takes_its_arrivals_with_it():
    tm = map_with_city()
    feed(tm, EAST)
    tc = __import__("test_threat_map")
    tm.see_tiles(30, [tc.camp(x=19, y=10)])
    assert tm.suspected_camp() is None  # Explained by the camp.
    tm.see_tiles(31, [tc.camp(x=19, y=10, improvement=None)])
    assert tm.arrivals == []  # Cleared: it no longer stands for anything.


def policy_for(tm, units, **kw):
    game = MilitaryGame(units, [], cities=[city_info()], **kw)
    policy = MilitaryPolicy()
    policy.threat_map = tm
    return policy, game


def suspecting_map():
    tm = map_with_city()
    feed(tm, EAST)
    return tm


def test_a_spare_unit_walks_toward_the_suspected_camp_and_stops_short_of_it():
    tm = suspecting_map()
    sus = tm.suspected_camp()
    policy, game = policy_for(tm, [warrior(uid=1, x=10, y=10), warrior(uid=2, x=11, y=9)])
    run(policy, game)
    (decision,) = [d for d in policy.decisions if d.action == "probe"]
    assert decision.unit_id == 2 and "not to attack" in decision.reason
    stop = decision.target
    assert stop[0] > 11 and (stop[0] - sus.x) ** 2 + (stop[1] - sus.y) ** 2 >= 4  # Short of it.
    assert ("move", 2, *stop) in game.orders("move")


def test_the_garrison_is_never_sent_and_the_reason_is_logged(caplog):
    policy, game = policy_for(suspecting_map(), [warrior(uid=1, x=10, y=10)])
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not [d for d in policy.decisions if d.action == "probe"]
    lines = [r.getMessage() for r in caplog.records if "military_probe_skip" in r.getMessage()]
    assert json.loads(lines[0].split(" ", 1)[1])["reason"] == "no spare unit"


def test_a_free_scout_is_left_to_settling(caplog):
    units = [warrior(uid=1, x=10, y=10), warrior(uid=2, x=11, y=9), scout(uid=9, x=9, y=9)]
    policy, game = policy_for(suspecting_map(), units)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not [d for d in policy.decisions if d.action == "probe"]
    assert "we have a scout" in caplog.text


def test_no_unit_is_sent_to_a_stop_under_threat(caplog):
    tm = suspecting_map()
    tm.see_threats(40, [hostile(x=13, y=9), hostile(kind="UNIT_SPEARMAN", x=13, y=10)])
    policy, game = policy_for(tm, [warrior(uid=1, x=10, y=10), warrior(uid=2, x=11, y=9)], turn=40)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not [d for d in policy.decisions if d.action == "probe"]
    assert "no safe stop" in caplog.text


def test_no_suspicion_sends_nobody(caplog):
    policy, game = policy_for(map_with_city(), [warrior(uid=1), warrior(uid=2, x=11, y=9)])
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not [d for d in policy.decisions if d.action == "probe"]
    assert "no suspicion" in caplog.text


def test_arrivals_across_the_seam_are_centred_on_that_side_of_the_map():
    tm = ThreatMap()
    tm.allies = [Ally("city", 70, 10, 10.0)]
    feed(tm, [(20, 73, 10), (23, 0, 10), (27, 1, 9)])
    sus = tm.suspected_camp()
    assert sus is not None and (sus.x >= 72 or sus.x <= 5)


def test_a_one_turn_raid_does_not_hide_a_real_trail():
    tm = map_with_city()
    tm.see_threats(15, [
        hostile(x=14, y=9), hostile(kind="UNIT_SPEARMAN", x=14, y=10),
        hostile(kind="UNIT_ARCHER", x=14, y=11), hostile(kind="UNIT_HORSEMAN", x=13, y=10),
    ])  # fmt: skip
    tm.sources = []
    feed(tm, [(20, 4, 10), (23, 4, 11), (27, 3, 10)])
    assert tm.suspected_camp() is not None


def test_the_guess_stays_on_the_map():
    tm = ThreatMap()
    tm.allies = [Ally("city", 10, 38, 10.0)]
    feed(tm, [(20, 10, 43), (23, 10, 44), (27, 10, 44)])
    sus = tm.suspected_camp()
    assert sus is not None and sus.y <= 45
