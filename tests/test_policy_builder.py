import asyncio
import json
import logging
import random

import pytest
from civ_mcp import lua as lq

from civ6_ai.policies import builder as builder_module
from civ6_ai.policies.builder import (
    BuilderPolicy,
    BuilderSettings,
    BuilderWeights,
    is_builder,
    score_job,
)
from civ6_ai.policies.settling import hex_line
from civ6_ai.state import BuilderJob
from fixtures.civ_state import BARBARIANS, FakeGame, overview, resources, threat_info, unit_info

W = BuilderWeights()


def job(priority="normal", x=10, y=10, improvement="IMPROVEMENT_FARM", **changes):
    base = dict(
        priority=priority,
        x=x,
        y=y,
        improvement=improvement,
        resource=None,
        resource_class=None,
        city="Alpha",
        nearest_builder_id=None,
        distance=None,
    )
    return BuilderJob(**{**base, **changes})


STRATEGIC = dict(priority="urgent", resource="IRON", resource_class="strategic")
LUXURY = dict(priority="high", resource="SILK", resource_class="luxury")
BONUS = dict(priority="high", resource="WHEAT", resource_class="bonus")
REPAIR = dict(priority="urgent", is_repair=True)


def test_priority_order_at_equal_distance():
    kinds = [STRATEGIC, REPAIR, LUXURY, BONUS, {}]
    scores = [score_job(job(**kind), 2, 2.0, W) for kind in kinds]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)


def test_distance_costs_turns_not_tiles():
    near = score_job(job(), 2, 2.0, W)  # One turn.
    far = score_job(job(), 6, 2.0, W)  # Three turns.
    assert near - far == pytest.approx(2 * W.turn_cost)


def test_nearby_luxury_never_beats_a_reachable_strategic_resource_by_default():
    assert score_job(job(**STRATEGIC), 10, 2.0, W) > score_job(job(**LUXURY), 0, 2.0, W)


def test_weights_are_plain_inputs():
    flipped = BuilderWeights(luxury=500.0)
    assert score_job(job(**LUXURY), 0, 2.0, flipped) > score_job(job(**STRATEGIC), 0, 2.0, flipped)


class ActionGame(FakeGame):
    """Records builder actions. `replies` overrides what an action answers, by action name."""

    def __init__(self, jobs=(), builders=(), owned=(), **overrides):
        overrides.setdefault("threats", [])
        super().__init__(units=list(builders), overview=overview(turn=5), **overrides)
        self.data["builder_tasks"] = list(jobs)
        self.owned = list(owned)
        self.actions: list[tuple] = []
        self.replies: dict[str, str] = {}

    async def get_empire_resources(self):
        stockpiles, _, rest, extra = resources()
        return stockpiles, self.owned, rest, extra

    async def move_unit(self, unit_index, target_x, target_y):
        self.actions.append(("move", unit_index, target_x, target_y))
        return self.replies.get("move", f"MOVING_TO|{target_x},{target_y}")

    async def improve_tile(self, unit_index, improvement_name):
        self.actions.append(("improve", unit_index, improvement_name))
        return self.replies.get("improve", f"OK:IMPROVING|{improvement_name}|0,0")

    async def repair_improvement(self, unit_index):
        self.actions.append(("repair", unit_index))
        return self.replies.get("repair", "OK:REPAIRING|X at (0,0)")

    async def remove_feature(self, unit_index):
        self.actions.append(("remove_feature", unit_index))
        return self.replies.get("remove_feature", "OK:REMOVING_FEATURE|FEATURE_JUNGLE at 0,0")


def task(priority="normal", x=10, y=10, improvement="IMPROVEMENT_FARM", resource="", cls="none"):
    return lq.BuilderTask(priority, x, y, improvement, resource, cls, "Alpha")


def repair_task(x, y, name="FARM"):
    return lq.BuilderTask("urgent", x, y, "REPAIR", name, "pillaged", "Alpha")


def worker(uid=1, x=10, y=10, **changes):
    base = dict(
        unit_id=uid,
        unit_index=uid,
        unit_type="UNIT_BUILDER",
        name="Builder",
        x=x,
        y=y,
        combat_strength=0,
        build_charges=3,
    )
    return unit_info(**{**base, **changes})


def run(policy, game):
    asyncio.run(policy.run(game))


def test_standing_on_a_job_improves_it():
    game = ActionGame([task(x=10, y=10)], [worker(x=10, y=10)])
    run(BuilderPolicy(), game)
    assert game.actions == [("improve", 1, "IMPROVEMENT_FARM")]


def test_repair_uses_the_repair_action():
    game = ActionGame([repair_task(10, 10)], [worker(x=10, y=10)])
    run(BuilderPolicy(), game)
    assert game.actions == [("repair", 1)]


def test_walks_to_a_distant_job_and_claims_it():
    game = ActionGame([task(x=20, y=10)], [worker(x=10, y=10)])
    policy = BuilderPolicy()
    run(policy, game)
    assert game.actions == [("move", 1, 20, 10)]
    assert policy.decisions[-1].action == "move"


def test_single_builder_takes_strategic_before_repair_before_luxury():
    jobs = [
        task("high", 10, 10, "IMPROVEMENT_PLANTATION", "SILK", "luxury"),
        repair_task(10, 10, "FARM"),
        task("urgent", 10, 10, "IMPROVEMENT_MINE", "IRON", "strategic"),
    ]
    owned = [lq.OwnedResource("IRON", "strategic", False, 10, 10)]
    game = ActionGame(jobs, [worker()], owned)
    run(BuilderPolicy(), game)
    assert game.actions == [("improve", 1, "IMPROVEMENT_MINE")]


def test_repair_beats_luxury_on_different_tiles():
    jobs = [task("high", 10, 10, "IMPROVEMENT_PLANTATION", "SILK", "luxury"), repair_task(11, 10)]
    owned = [lq.OwnedResource("SILK", "luxury", False, 10, 10)]
    game = ActionGame(jobs, [worker(x=10, y=10)], owned)
    run(BuilderPolicy(), game)
    assert game.actions == [("move", 1, 11, 10)]


def test_two_builders_never_share_a_job():
    jobs = [task(x=10, y=10), task(x=14, y=10)]
    game = ActionGame(jobs, [worker(1, x=10, y=10), worker(2, x=10, y=10)])
    run(BuilderPolicy(), game)
    targets = {a[2:] for a in game.actions if a[0] == "move"}
    assert game.actions[0] == ("improve", 1, "IMPROVEMENT_FARM")
    assert targets == {(14, 10)}


def test_nearest_builder_gets_the_job():
    game = ActionGame([task(x=20, y=10)], [worker(1, x=10, y=10), worker(2, x=18, y=10)])
    run(BuilderPolicy(), game)
    assert game.actions == [("move", 2, 20, 10)]


def test_job_held_by_a_builder_out_of_moves_is_not_given_to_another():
    jobs = [task(x=10, y=10), task(x=12, y=10)]
    policy = BuilderPolicy()
    run(policy, ActionGame(jobs, [worker(1, x=10, y=10)]))
    game = ActionGame(jobs, [worker(1, x=10, y=10, moves_remaining=0), worker(2, x=11, y=10)])
    run(policy, game)
    assert game.actions == [("move", 2, 12, 10)]


def test_builder_keeps_its_job_over_a_slightly_better_one():
    policy = BuilderPolicy()
    run(policy, ActionGame([task(x=20, y=10)], [worker(x=10, y=10)]))
    jobs = [task(x=20, y=10), task("high", 11, 10, "IMPROVEMENT_PLANTATION", "SILK", "luxury")]
    owned = [lq.OwnedResource("SILK", "luxury", False, 11, 10)]
    # Luxury next door scores 90 - 4; normal job far away scores 20 - 20 + 15. Luxury wins.
    game = ActionGame(jobs, [worker(x=11, y=10)], owned)
    run(policy, game)
    assert game.actions == [("improve", 1, "IMPROVEMENT_PLANTATION")]


def test_blocked_move_is_a_failure_and_the_next_job_is_tried():
    game = ActionGame([task(x=20, y=10), task(x=14, y=10)], [worker()])
    game.replies["move"] = "MOVING_TO|20,10|now_at:10,10|BLOCKED (enemy)"
    policy = BuilderPolicy()
    run(policy, game)
    moves = [a for a in game.actions if a[0] == "move"]
    assert len(moves) == 2  # Each job once.
    assert [d.action for d in policy.decisions] == ["failed", "failed", "idle"]


def test_refused_job_is_set_aside_then_retried():
    policy = BuilderPolicy()
    game = ActionGame([task(x=10, y=10)], [worker()])
    game.replies["improve"] = "ERR:CANNOT_IMPROVE|nope"
    for turn in (5, 6):
        game.data["overview"] = overview(turn=turn)
        run(policy, game)
    assert len(game.actions) == 2
    game.data["overview"] = overview(turn=7)
    run(policy, game)
    assert len(game.actions) == 2  # Set aside.
    game.data["overview"] = overview(turn=17)
    run(policy, game)
    assert len(game.actions) == 3


def test_error_text_is_a_failure_even_when_it_does_not_start_with_err():
    game = ActionGame([task()], [worker()])
    game.replies["improve"] = "Error: no such improvement"
    policy = BuilderPolicy()
    run(policy, game)
    assert policy.decisions[0].action == "failed"


def test_no_moves_left_after_arriving_is_not_a_failure():
    game = ActionGame([task(x=11, y=10)], [worker()])
    game.replies["move"] = "MOVING_TO|11,10|from:10,10|now_at:11,10"
    game.replies["improve"] = "ERR:CANNOT_IMPROVE|Builder has no moves remaining this turn"
    policy = BuilderPolicy()
    run(policy, game)
    assert [d.action for d in policy.decisions] == ["move"]
    assert policy._failures == {}


def test_feature_in_the_way_is_removed_for_resource_jobs_only():
    owned = [lq.OwnedResource("BANANAS", "bonus", False, 10, 10)]
    jobs = [task("high", 10, 10, "IMPROVEMENT_PLANTATION", "BANANAS", "bonus")]
    game = ActionGame(jobs, [worker()], owned)
    game.replies["improve"] = (
        "ERR:CANNOT_IMPROVE|tile has FEATURE_JUNGLE (use remove_feature first)"
    )
    run(BuilderPolicy(), game)
    assert [a[0] for a in game.actions] == ["improve", "remove_feature"]
    plain = ActionGame([task()], [worker()])
    plain.replies["improve"] = game.replies["improve"]
    run(BuilderPolicy(), plain)
    assert [a[0] for a in plain.actions] == ["improve"]


def test_offered_improvements_veto_a_wrong_job_without_a_call():
    game = ActionGame([task()], [worker(valid_improvements=["IMPROVEMENT_MINE"])])
    policy = BuilderPolicy()
    run(policy, game)
    assert game.actions == []
    assert policy.decisions[0].action == "failed"


def test_empty_offer_list_is_unknown_not_a_veto():
    game = ActionGame([task()], [worker(valid_improvements=[])])
    run(BuilderPolicy(), game)
    assert game.actions == [("improve", 1, "IMPROVEMENT_FARM")]


def test_unbuildable_and_unknown_improvements_are_skipped():
    jobs = [
        task("urgent", 10, 10, "UNKNOWN", "XYZ", "strategic"),
        task("high", 10, 11, "IMPROVEMENT_FISHING_BOATS"),
    ]
    game = ActionGame(jobs, [worker()], [lq.OwnedResource("XYZ", "strategic", False, 10, 10)])
    policy = BuilderPolicy()
    run(policy, game)
    assert game.actions == []
    assert policy.decisions[0].action == "idle"


def test_job_near_a_hostile_unit_is_skipped_for_the_turn():
    game = ActionGame([task(x=12, y=10), task(x=20, y=10)], [worker()])
    game.data["threats"] = [threat_info(x=13, y=10, owner_id=BARBARIANS)]
    run(BuilderPolicy(), game)
    assert game.actions == [("move", 1, 20, 10)]


def test_walking_forever_is_bounded():
    policy = BuilderPolicy(BuilderSettings(max_travel_turns=3, retry_turns=100))
    game = ActionGame([task(x=40, y=10)], [worker()])
    for turn in range(1, 8):
        game.data["overview"] = overview(turn=turn)
        run(policy, game)
    assert len([a for a in game.actions if a[0] == "move"]) == 3  # The fourth turn gives up.
    assert policy.decisions[-1].action == "idle"


def test_working_forever_is_bounded():
    policy = BuilderPolicy(BuilderSettings(max_work_turns=3, retry_turns=100))
    game = ActionGame([task()], [worker()])
    for turn in range(1, 9):
        game.data["overview"] = overview(turn=turn)
        run(policy, game)
    assert len(game.actions) == 3


def test_per_builder_state_is_dropped_when_it_dies():
    policy = BuilderPolicy()
    run(policy, ActionGame([task(x=20, y=10)], [worker(1), worker(2, x=30, y=10)]))
    assert 1 in policy._claims
    run(policy, ActionGame([task(x=20, y=10)], [worker(2, x=30, y=10)]))
    assert 1 not in policy._claims and 1 not in policy._travel and 1 not in policy._work
    run(policy, ActionGame([task(x=20, y=10)], []))
    assert not policy._claims and not policy._travel and not policy._work


def test_claim_is_dropped_when_the_job_is_finished():
    policy = BuilderPolicy()
    run(policy, ActionGame([task(x=10, y=10)], [worker(x=10, y=10)]))
    assert policy._claims
    run(policy, ActionGame([], [worker(x=10, y=10)]))
    assert not policy._claims


def test_unique_builder_units_are_builders(monkeypatch):
    monkeypatch.setitem(builder_module.REPLACES, "UNIT_TEST_BUILDER", "UNIT_BUILDER")
    assert is_builder("UNIT_TEST_BUILDER")
    assert is_builder("UNIT_BUILDER")
    assert not is_builder("UNIT_WARRIOR")
    game = ActionGame([task()], [worker(unit_type="UNIT_TEST_BUILDER")])
    run(BuilderPolicy(), game)
    assert game.actions == [("improve", 1, "IMPROVEMENT_FARM")]


def test_other_units_and_spent_reads_do_nothing():
    game = ActionGame([task()], [unit_info()])
    run(BuilderPolicy(), game)
    assert game.actions == [] and "get_builder_tasks" not in game.calls
    empty = ActionGame([], [worker()])
    policy = BuilderPolicy()
    run(policy, empty)
    assert empty.actions == [] and policy.decisions[0].action == "idle"


def test_resource_job_on_an_unseen_resource_is_never_worked():
    jobs = [task("urgent", 10, 10, "IMPROVEMENT_MINE", "IRON", "strategic")]
    game = ActionGame(jobs, [worker()], [])
    run(BuilderPolicy(), game)
    assert game.actions == []


def test_decisions_are_deterministic_and_logged(caplog):
    jobs = [task(x=14, y=10), task(x=10, y=14), task(x=16, y=10)]

    def play():
        game = ActionGame(jobs, [worker(1), worker(2, x=12, y=10)])
        policy = BuilderPolicy()
        run(policy, game)
        return game.actions, policy.decisions

    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.builder"):
        first = play()
    assert first == play()
    line = next(r.getMessage() for r in caplog.records if "builder_decision" in r.getMessage())
    assert json.loads(line.split(" ", 1)[1])["unit_id"] in (1, 2)


# ----- 100-turn simulation -----


class SimGame(ActionGame):
    """A tiny world: jobs appear, builders walk 2 tiles a turn, and a build takes 3 turns."""

    def __init__(self, seed):
        super().__init__()
        self.rng = random.Random(seed)
        self.turn = 1
        self.jobs: dict[tuple[int, int], dict] = {}
        self.builders = {
            1: dict(x=10, y=10, target=None, work=None),
            2: dict(x=10, y=10, target=None, work=None),
            3: dict(x=12, y=10, target=None, work=None),
        }
        self.moved: set[int] = set()
        self.completed = 0
        for _ in range(4):
            self.spawn()

    def spawn(self):
        for _ in range(20):
            tile = (self.rng.randint(5, 15), self.rng.randint(5, 15))
            if tile not in self.jobs:
                break
        else:
            return
        kind = self.rng.choice(["strategic", "luxury", "bonus", "normal", "repair"])
        self.jobs[tile] = dict(kind=kind, left=3)

    def refresh(self):
        tasks, owned = [], []
        for (x, y), j in sorted(self.jobs.items()):
            kind = j["kind"]
            if kind == "repair":
                tasks.append(repair_task(x, y))
            elif kind == "normal":
                tasks.append(task("normal", x, y))
            else:
                name = {"strategic": "IRON", "luxury": "SILK", "bonus": "WHEAT"}[kind]
                prio = "urgent" if kind == "strategic" else "high"
                tasks.append(task(prio, x, y, "IMPROVEMENT_MINE", name, kind))
                owned.append(lq.OwnedResource(name, kind, False, x, y))
        self.data["builder_tasks"], self.owned = tasks, owned
        units = []
        for uid, b in sorted(self.builders.items()):
            working = b["work"] is not None
            units.append(worker(uid, x=b["x"], y=b["y"], moves_remaining=0.0 if working else 2.0))
        self.data["units"] = units
        self.data["overview"] = overview(turn=self.turn)

    async def move_unit(self, unit_index, target_x, target_y):
        self.actions.append(("move", unit_index, target_x, target_y))
        b = self.builders[unit_index]
        b["target"] = (target_x, target_y)
        steps = hex_line((b["x"], b["y"]), (target_x, target_y), None)[:2]
        if steps:
            b["x"], b["y"] = steps[-1]
        self.moved.add(unit_index)
        return f"MOVING_TO|{target_x},{target_y}|now_at:{b['x']},{b['y']}"

    async def improve_tile(self, unit_index, improvement_name):
        b = self.builders[unit_index]
        if unit_index in self.moved:
            return "ERR:CANNOT_IMPROVE|Builder has no moves remaining this turn"
        b["target"] = b["work"] = (b["x"], b["y"])
        return "OK:IMPROVING"

    async def repair_improvement(self, unit_index):
        return await self.improve_tile(unit_index, "")

    def end_turn(self):
        for b in self.builders.values():
            tile = b["work"]
            if tile in self.jobs:
                self.jobs[tile]["left"] -= 1
                if self.jobs[tile]["left"] <= 0:
                    del self.jobs[tile]
                    self.completed += 1
                    b["work"] = b["target"] = None
        self.moved.clear()
        self.turn += 1
        if self.turn % 2 == 0:
            self.spawn()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_builder_idles_while_work_exists_over_100_turns(seed):
    game = SimGame(seed)
    policy = BuilderPolicy()
    for _ in range(100):
        game.refresh()
        free = [u for u in game.data["units"] if u.moves_remaining > 0]
        before = len(policy.decisions)
        run(policy, game)
        idle = [d for d in policy.decisions[before:] if d.action == "idle"]
        engaged = {b["target"] for b in game.builders.values() if b["target"]}
        unworked = [t for t in game.jobs if t not in engaged]
        assert not (idle and unworked), (game.turn, idle, unworked)
        assert len(policy.decisions) - before == len(free)
        game.end_turn()
    assert game.completed >= 20
    assert not policy._failures


def test_veto_applies_to_every_builder():
    game = ActionGame(
        [task(x=10, y=10)],
        [worker(1, x=10, y=10, valid_improvements=["IMPROVEMENT_MINE"]), worker(2, x=15, y=10)],
    )
    run(BuilderPolicy(), game)
    assert game.actions == []  # Builder 2 does not walk to a refused tile.


def test_claim_survives_a_passing_hostile_unit_but_not_for_ever():
    policy = BuilderPolicy(BuilderSettings(max_hold_turns=2))
    run(policy, ActionGame([task(x=30, y=10)], [worker(x=10, y=10)]))
    claim = policy._claims[1]
    near = [threat_info(x=31, y=10, owner_id=BARBARIANS)]
    game = ActionGame([task(x=30, y=10)], [worker(x=12, y=10)], threats=near)
    for turn in (6, 7):
        game.data["overview"] = overview(turn=turn)
        run(policy, game)
        assert game.actions == [] and policy._claims[1] == claim
    game.data["overview"] = overview(turn=8)
    run(policy, game)
    assert 1 not in policy._claims  # Bounded.
    clear = ActionGame([task(x=30, y=10)], [worker(x=12, y=10)])
    run(policy, clear)
    assert clear.actions == [("move", 1, 30, 10)]


def test_claim_resumes_when_the_threat_leaves():
    policy = BuilderPolicy()
    run(policy, ActionGame([task(x=30, y=10)], [worker(x=10, y=10)]))
    near = [threat_info(x=31, y=10, owner_id=BARBARIANS)]
    run(policy, ActionGame([task(x=30, y=10)], [worker(x=12, y=10)], threats=near))
    assert policy._claims[1][:2] == (30, 10) and policy._holds[1] == 1
    game = ActionGame([task(x=30, y=10)], [worker(x=12, y=10)])
    run(policy, game)
    assert game.actions == [("move", 1, 30, 10)] and 1 not in policy._holds
