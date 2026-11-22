"""Issue 280: hostile civilians are seen on tile reads and retaken by a unit that can reach them."""

import json
import logging

from civ6_ai.policies.military import MilitaryPolicy
from civ6_ai.state.reader import build_tile
from civ6_ai.threat_map import ThreatMap, foreign_civilian
from fixtures.civ_state import ROME, city_info, overview, tile
from test_policy_military import MilitaryGame, barbarian, run, settler, warrior


def civ_tile(x=12, y=10, units=("Barbarian SETTLER",), **changes):
    return tile(
        x=x, y=y, units=list(units), district=None, improvement="IMPROVEMENT_MINE", **changes
    )


def seen_map(*tiles, turn=20):
    tm = ThreatMap()
    tm.see_tiles(turn, [build_tile(t) for t in tiles])
    return tm


def setup(units, tm, civ=None, turn=20, **kw):
    civ = civ or civ_tile()
    game = MilitaryGame(units, [], turn=turn, tiles={(civ.x, civ.y): [civ]}, **kw)
    policy = MilitaryPolicy()
    policy.threat_map = tm
    return policy, game


def pair(**changes):  # The garrison, and a second warrior just outside.
    return [warrior(uid=1, x=10, y=10), warrior(uid=2, x=11, y=9, **changes)]


def test_the_map_keeps_a_foreign_civilian_from_a_tile_read_and_forgets_it_when_gone():
    tm = seen_map(civ_tile())
    assert tm.civilians[12, 10].owner == "Barbarian" and tm.civilians[12, 10].unit == "SETTLER"
    tm.see_tiles(21, [build_tile(civ_tile(units=()))])
    assert tm.civilians == {}
    assert foreign_civilian("Rome WARRIOR") is None
    assert foreign_civilian("Sumeria GREAT_GENERAL") is not None


def test_a_civilian_on_a_tile_with_a_soldier_is_kept_as_guarded():
    tm = seen_map(civ_tile(units=("Barbarian SETTLER", "Barbarian WARRIOR")))
    assert tm.civilians[12, 10].guarded
    assert not seen_map(civ_tile()).civilians[12, 10].guarded


def test_a_spare_unit_that_can_reach_a_barbarian_settler_steps_onto_it():
    policy, game = setup(pair(), seen_map(civ_tile()))
    run(policy, game)
    assert ("move", 2, 12, 10) in game.orders("move")
    assert any(d.action == "recapture" and "SETTLER" in d.reason for d in policy.decisions)


def test_the_offense_line_counts_the_recapture(caplog):
    policy, game = setup(pair(), seen_map(civ_tile()))
    with caplog.at_level(logging.INFO):
        run(policy, game)
    line = next(m for m in caplog.messages if m.startswith("military_offense"))
    assert json.loads(line.split(" ", 1)[1])["recaptures"] == 1


def test_a_lone_garrison_is_not_pulled_out_to_recapture():
    policy, game = setup([warrior(uid=1, x=10, y=10)], seen_map(civ_tile()))
    run(policy, game)
    assert not [d for d in policy.decisions if d.action == "recapture"]


def test_a_unit_that_cannot_reach_it_this_turn_stays():
    policy, game = setup(pair(), seen_map(civ_tile(x=19, y=10)), civ=civ_tile(x=19, y=10))
    run(policy, game)
    assert not [d for d in policy.decisions if d.action == "recapture"]


def test_a_tile_inside_a_camps_reach_is_not_safe():
    tm = seen_map(civ_tile())
    tm.see_tiles(
        20, [build_tile(tile(x=13, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", units=[]))]
    )
    policy, game = setup(pair(), tm)
    run(policy, game)
    assert not [d for d in policy.decisions if d.action == "recapture"]


def test_a_civilian_that_is_gone_when_the_tile_is_read_again_is_left():
    policy, game = setup(pair(), seen_map(civ_tile()), civ=civ_tile(units=()))
    run(policy, game)
    assert not [m for m in game.orders("move") if m[2:] == (12, 10)]


def test_a_civ_at_peace_is_not_taken_but_one_we_fight_is():
    peaceful = civ_tile(units=("Rome SETTLER",))
    policy, game = setup(pair(), seen_map(peaceful), civ=peaceful)
    run(policy, game)
    assert not [d for d in policy.decisions if d.action == "recapture"]
    enemy_soldier = barbarian(x=18, y=18)  # Seen hostile: the owner is at war with us.
    object.__setattr__(enemy_soldier, "owner", "Rome")
    policy, game = setup(pair(), seen_map(peaceful), civ=peaceful)
    policy._hostile_owners.add("Rome")
    run(policy, game)
    assert [d for d in policy.decisions if d.action == "recapture"]


def test_a_settler_that_vanishes_beside_a_barbarian_settler_counts_as_captured(caplog):
    tm = seen_map(civ_tile(x=11, y=11))
    policy, game = setup(
        [warrior(uid=1, x=10, y=10), settler(uid=8, x=11, y=11)], tm, civ=civ_tile(x=11, y=11)
    )
    run(policy, game)
    game.data["units"] = [warrior(uid=1, x=10, y=10)]  # The settler is gone.
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert any(m.startswith("military_capture") for m in caplog.messages)
    line = next(m for m in caplog.messages if m.startswith("military_offense"))
    stats = json.loads(line.split(" ", 1)[1])
    assert stats["civilians_captured"] == 1 and "civilians_lost" not in stats


def test_a_settler_that_vanishes_with_no_hostile_civilian_near_is_not_a_capture(caplog):
    policy, game = setup([warrior(uid=1), settler(uid=8, x=11, y=11)], ThreatMap())
    run(policy, game)
    game.data["units"] = [warrior(uid=1)]  # It founded a city, say.
    with caplog.at_level(logging.INFO):
        run(policy, game)
    line = next(m for m in caplog.messages if m.startswith("military_offense"))
    assert json.loads(line.split(" ", 1)[1])["civilians_captured"] == 0
    assert not any(m.startswith("military_capture") for m in caplog.messages)


def test_a_partial_move_is_not_counted_as_a_recapture(caplog):
    policy, game = setup(pair(), seen_map(civ_tile()))
    game.replies["move"] = "MOVING_TO|12,10|from:11,9|now_at:11,10"
    with caplog.at_level(logging.INFO):
        run(policy, game)
    line = next(m for m in caplog.messages if m.startswith("military_offense"))
    assert json.loads(line.split(" ", 1)[1])["recaptures"] == 0
    assert any("short of 12,10" in d.reason for d in policy.decisions)


def test_a_settler_that_founded_a_city_is_not_a_capture_even_beside_a_hostile_civilian(caplog):
    tm = seen_map(civ_tile(x=11, y=11))
    policy, game = setup([warrior(uid=1), settler(uid=8, x=11, y=11)], tm, civ=civ_tile(x=11, y=11))
    run(policy, game)
    game.data["units"] = [warrior(uid=1)]  # The settler founded a city: one more than before.
    game.data["cities"] = [*game.data["cities"], city_info(city_id=65537, name="Beta", x=11, y=11)]
    game.data["overview"] = overview(turn=21, num_cities=2)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not any(m.startswith("military_capture") for m in caplog.messages)


def test_a_builder_that_vanishes_is_not_a_capture(caplog):
    builder = settler(uid=8, x=11, y=11, name="Builder", unit_type="UNIT_BUILDER")
    tm = seen_map(civ_tile(x=11, y=11, units=("Barbarian BUILDER",)))
    policy, game = setup([warrior(uid=1), builder], tm, civ=civ_tile(x=11, y=11))
    run(policy, game)
    game.data["units"] = [warrior(uid=1)]
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not any(m.startswith("military_capture") for m in caplog.messages)


def test_a_civ_at_peace_again_is_no_longer_a_target():
    peaceful = civ_tile(units=("Rome SETTLER",))
    policy, game = setup(pair(), seen_map(peaceful), civ=peaceful)
    policy._hostile_owners.add("Rome")
    game.data["threats"] = [barbarian(x=18, y=18, owner_id=ROME, owner_name="Rome")]
    run(policy, game)
    assert not [d for d in policy.decisions if d.action == "recapture"]
    assert "Rome" not in policy._hostile_owners


def test_a_civilian_swapped_for_a_peaceful_one_at_the_reread_is_left():
    policy, game = setup(pair(), seen_map(civ_tile()), civ=civ_tile(units=("Rome SETTLER",)))
    run(policy, game)
    assert not [m for m in game.orders("move") if m[2:] == (12, 10)]


def test_a_fogged_target_tile_is_not_confirmed_by_the_cache():
    fogged = civ_tile(visibility="revealed")
    policy, game = setup(pair(), seen_map(civ_tile()), civ=fogged)
    run(policy, game)
    assert not [m for m in game.orders("move") if m[2:] == (12, 10)]


def skips(caplog):
    return [
        json.loads(m.split(" ", 1)[1])
        for m in caplog.messages
        if m.startswith("military_recapture")
    ]


def test_a_civilian_a_few_turns_away_is_marched_on():
    far = civ_tile(x=15, y=10)
    policy, game = setup(pair(), seen_map(far), civ=far)
    game.replies["move"] = "MOVING_TO|15,10|from:11,9|now_at:12,10"
    run(policy, game)
    assert ("move", 2, 13, 10) in game.orders("move") or ("move", 2, 13, 9) in game.orders("move")
    assert any("short of 15,10" in d.reason for d in policy.decisions)


def test_a_civilian_too_far_to_march_on_is_logged_as_out_of_reach(caplog):
    far = civ_tile(x=19, y=10)
    policy, game = setup(pair(), seen_map(far), civ=far)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    (skip,) = skips(caplog)
    assert skip["reason"] == "out of reach" and skip["at"] == [19, 10] and skip["nearest"] > 6


def test_no_spare_unit_is_logged(caplog):
    policy, game = setup([warrior(uid=1, x=10, y=10)], seen_map(civ_tile()))
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert [x["reason"] for x in skips(caplog)] == ["no spare unit"]


def test_a_threatened_tile_is_logged(caplog):
    tm = seen_map(civ_tile())
    tm.see_tiles(
        20, [build_tile(tile(x=13, y=10, improvement="IMPROVEMENT_BARBARIAN_CAMP", units=[]))]
    )
    policy, game = setup(pair(), tm)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    (skip,) = skips(caplog)
    assert "reach" in skip["reason"] and skip["threat"] > 4


def test_a_guarded_civilian_is_logged_and_left(caplog):
    guarded = civ_tile(units=("Barbarian SETTLER", "Barbarian COURSER"))
    policy, game = setup(pair(), seen_map(guarded), civ=guarded)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert "soldier" in skips(caplog)[0]["reason"]
    assert not [d for d in policy.decisions if d.action == "recapture"]


def test_a_civilian_on_water_is_logged(caplog):
    class Sea:
        def step_cost(self, x, y, naval):
            return None

    tm = seen_map(civ_tile())
    tm.terrain = Sea()
    policy, game = setup(pair(), tm)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert "water" in skips(caplog)[0]["reason"]


def lost_settler_game():
    tm = seen_map(civ_tile(x=11, y=11))
    policy, game = setup(
        [warrior(uid=1, x=10, y=10), settler(uid=8, x=11, y=11)], tm, civ=civ_tile(x=11, y=11)
    )
    run(policy, game)
    game.data["units"] = [warrior(uid=1, x=10, y=10)]  # The settler is gone.
    tm.civilians.clear()  # Nothing in sight on the turn it vanished.
    return policy, game, tm


def test_a_capture_is_told_when_the_taken_settler_shows_a_turn_later_under_its_captor(caplog):
    policy, game, tm = lost_settler_game()
    with caplog.at_level(logging.INFO):
        run(policy, game)
        assert not any(m.startswith("military_capture ") for m in caplog.messages)
        game.data["overview"] = overview(turn=21, num_cities=1)
        guarded = civ_tile(x=13, y=12, units=("Barbarian SETTLER", "Barbarian COURSER"))
        tm.see_tiles(21, [build_tile(guarded)])
        run(policy, game)
    line = next(m for m in caplog.messages if m.startswith("military_capture "))
    data = json.loads(line.split(" ", 1)[1])
    assert data["unit"] == "UNIT_SETTLER" and data["guarded"] and data["now_at"] == [13, 12]
    assert data["lost"] == 20 and data["turn"] == 21


def test_a_settler_never_seen_again_is_reported_unknown_after_the_window(caplog):
    policy, game, tm = lost_settler_game()
    with caplog.at_level(logging.INFO):
        for turn in range(20, 20 + policy.settings.capture_window + 2):
            game.data["overview"] = overview(turn=turn, num_cities=1)
            run(policy, game)
    assert any(m.startswith("military_capture_unknown") for m in caplog.messages)
    assert not any(m.startswith("military_capture ") for m in caplog.messages)
    assert policy._vanished == {}


def test_a_captor_that_walked_on_is_found_with_a_wider_reach_each_turn(caplog):
    policy, game, tm = lost_settler_game()
    with caplog.at_level(logging.INFO):
        run(policy, game)
        game.data["overview"] = overview(turn=22, num_cities=1)
        far = civ_tile(x=17, y=11)  # Six tiles from where it was lost, two turns on.
        tm.see_tiles(22, [build_tile(far)])
        run(policy, game)
    assert any(m.startswith("military_capture ") for m in caplog.messages)


def test_a_march_that_would_stop_in_a_threatened_tile_is_not_started(caplog):
    far = civ_tile(x=15, y=10)
    tm = seen_map(far)
    tm.see_tiles(
        20, [build_tile(tile(x=11, y=7, improvement="IMPROVEMENT_BARBARIAN_CAMP", units=[]))]
    )
    policy, game = setup(pair(), tm, civ=far)
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert not [m for m in game.orders("move") if m[2:] == (15, 10)]
    assert any("threatened tile" in x["reason"] for x in skips(caplog))


def test_a_march_steps_back_from_a_mountain_on_the_line_to_a_passable_tile():
    class Ground:
        def step_cost(self, x, y, naval):
            return None if (x, y) == (13, 10) else 1.0

    far = civ_tile(x=15, y=10)
    tm = seen_map(far)
    tm.terrain = Ground()
    policy, game = setup(pair(), tm, civ=far)
    run(policy, game)
    moves = [m for m in game.orders("move") if m[1] == 2]
    assert moves and all(m[2:] != (13, 10) for m in moves)


# Issue 408: STD_T1_03 T23, a settler taken and held by a slinger beside our units.
def held_by(captor_x=12, captor_y=10, **changes):
    held = civ_tile(x=captor_x, y=captor_y, units=("Barbarian SETTLER", "Barbarian SLINGER"))
    cap = barbarian("UNIT_SLINGER", x=captor_x, y=captor_y, cs=14, rs=0, **changes)
    return held, cap


def test_an_adjacent_captor_is_attacked_when_the_odds_are_reasonable(caplog):
    from test_policy_military import target

    held, cap = held_by()
    units = [
        warrior(uid=1, x=10, y=10),
        warrior(uid=2, x=11, y=10, health=75, targets=[target(x=12, y=10)]),
    ]
    policy, game = setup(units, seen_map(held), civ=held)
    game.data["threats"] = [cap]
    with caplog.at_level(logging.INFO):
        run(policy, game)
    assert game.orders("attack")
    assert any("freeing" in d.reason for d in policy.decisions if d.action == "attack")
    assert any(m.startswith("military_retake_weigh") for m in caplog.messages)


def test_a_captor_we_would_lose_to_is_left_alone():
    from test_policy_military import target

    held, _ = held_by()
    cap = barbarian("UNIT_SLINGER", x=12, y=10, cs=60, rs=60)
    units = [
        warrior(uid=1, x=10, y=10),
        warrior(uid=2, x=11, y=10, health=30, targets=[target(x=12, y=10)]),
    ]
    policy, game = setup(units, seen_map(held), civ=held)
    game.data["threats"] = [cap]
    run(policy, game)
    assert not game.orders("attack")
