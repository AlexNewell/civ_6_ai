import asyncio
from dataclasses import replace

import pytest
from civ_mcp import lua as lq

from civ6_ai.blockers import Blocker, BlockerType
from civ6_ai.policies.government import (
    GovernmentPolicy,
    GovernmentWeights,
    blocker_policies,
    government_candidates,
    rank_cards,
)
from civ6_ai.state.models import Policy, PolicySlot
from fixtures import civ_state as fx


def run(coro):
    return asyncio.run(coro)


def info(policy_type, slot_type):
    return lq.PolicyInfo(policy_type, policy_type.title(), "", slot_type)


def slot(index, slot_type, policy=None):
    return lq.PolicySlot(index, slot_type, policy, None)


def appointed(governor_type, city_id=-1, promotions=()):
    promos = [lq.GovernorPromotion(p, p, "") for p in promotions]
    return lq.AppointedGovernor(governor_type, governor_type, city_id, "x", city_id >= 0, 0, promos)


class ScriptedGame(fx.FakeGame):
    """A game with mutable government, slots and governors, which records every action."""

    def __init__(self, government="GOVERNMENT_DIGITAL_DEMOCRACY", slots=(), cards=(), **kwargs):
        points = kwargs.pop("points", 0)
        super().__init__(overview=fx.overview(num_cities=2, turn=kwargs.pop("turn", 50)), **kwargs)
        self.government = government
        self.slots = list(slots)
        self.cards = list(cards)
        self.points = points
        self.appointed: list[lq.AppointedGovernor] = []
        self.to_appoint = []
        self.unlocked = set()  # Governments the game accepts.
        self.refuse: dict[str, str] = {}  # Item -> the reply instead of success.
        self.actions: list[tuple] = []
        self.civics = 3
        self.can_appoint = True

    async def get_policies(self):
        return lq.GovernmentStatus(
            government_name=self.government,
            government_type=self.government,
            slots=list(self.slots),
            available_policies=list(self.cards),
        )

    async def get_governors(self):
        return lq.GovernorStatus(
            points_available=self.points,
            points_spent=0,
            can_appoint=self.can_appoint and self.points > 0,
            appointed=list(self.appointed),
            available_to_appoint=[lq.GovernorInfo(t, t, "") for t in self.to_appoint],
        )

    async def get_tech_civics(self):
        return replace(fx.research_status(), completed_civic_count=self.civics)

    async def change_government(self, government_type):
        self.actions.append(("government", government_type))
        if government_type in self.refuse:
            return self.refuse[government_type]
        if government_type not in self.unlocked:
            return f"Error: GOV_LOCKED|{government_type} is not unlocked"
        self.government = government_type
        return f"GOVERNMENT_CHANGED|{government_type}|x"

    async def set_policies(self, assignments):
        self.actions.append(("policies", dict(assignments)))
        ((index, card),) = assignments.items()
        if card in self.refuse:
            return self.refuse[card]
        self.slots = [
            slot(s.slot_index, s.slot_type, card if s.slot_index == index else s.current_policy)
            for s in self.slots
        ]
        return "POLICIES_SET|Policies updated. Use get_policies to verify."

    async def appoint_governor(self, governor_type):
        self.actions.append(("appoint", governor_type))
        if governor_type in self.refuse:
            return self.refuse[governor_type]
        self.points -= 1
        self.to_appoint.remove(governor_type)
        self.appointed.append(appointed(governor_type))
        return f"APPOINTED|{governor_type} (x)"

    async def assign_governor(self, governor_type, city_id):
        self.actions.append(("assign", governor_type, city_id))
        if (governor_type, city_id) in self.refuse:
            return self.refuse[(governor_type, city_id)]
        self.appointed = [
            appointed(g.governor_type, city_id if g.governor_type == governor_type else -1)
            if g.governor_type == governor_type
            else g
            for g in self.appointed
        ]
        return f"ASSIGNED|{governor_type} to {city_id}"

    async def promote_governor(self, governor_type, promotion_type):
        self.actions.append(("promote", governor_type, promotion_type))
        if promotion_type in self.refuse:
            return self.refuse[promotion_type]
        self.points -= 1
        self.appointed = [
            appointed(g.governor_type, g.assigned_city_id, [])
            if g.governor_type == governor_type
            else g
            for g in self.appointed
        ]
        return f"PROMOTED|{governor_type} with {promotion_type}"


# --- Government ---------------------------------------------------------------------------------


def test_candidates_are_only_better_and_best_first():
    weights = GovernmentWeights()
    top = government_candidates("GOVERNMENT_CHIEFDOM", weights)
    assert top[0] == "GOVERNMENT_DIGITAL_DEMOCRACY"
    assert "GOVERNMENT_CHIEFDOM" not in top
    assert government_candidates("GOVERNMENT_DIGITAL_DEMOCRACY", weights) == []
    # A small step is not worth a change.
    assert "GOVERNMENT_MERCHANT_REPUBLIC" not in government_candidates(
        "GOVERNMENT_MONARCHY", weights
    )


def test_changes_to_the_best_unlocked_government():
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM", slots=[])
    game.unlocked = {"GOVERNMENT_CLASSICAL_REPUBLIC", "GOVERNMENT_OLIGARCHY"}
    run(GovernmentPolicy().run(game))
    assert game.government == "GOVERNMENT_CLASSICAL_REPUBLIC"
    changes = [a for a in game.actions if a[0] == "government"]
    assert changes[-1] == ("government", "GOVERNMENT_CLASSICAL_REPUBLIC")


def test_never_flips_back_or_changes_again_soon():
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM")
    game.unlocked = {"GOVERNMENT_CLASSICAL_REPUBLIC", "GOVERNMENT_MONARCHY"}
    policy = GovernmentPolicy()
    run(policy.run(game))
    assert game.government == "GOVERNMENT_MONARCHY"
    game.actions.clear()
    game.unlocked.add("GOVERNMENT_CHIEFDOM")
    for turn in range(51, 70):  # Inside the minimum gap.
        game.data["overview"] = fx.overview(turn=turn)
        run(policy.run(game))
    assert not [a for a in game.actions if a[0] == "government"]
    assert game.government == "GOVERNMENT_MONARCHY"


def test_does_not_change_to_a_worse_or_equal_government_even_after_the_gap():
    game = ScriptedGame(government="GOVERNMENT_MONARCHY")
    game.unlocked = {"GOVERNMENT_CHIEFDOM", "GOVERNMENT_OLIGARCHY", "GOVERNMENT_MERCHANT_REPUBLIC"}
    run(GovernmentPolicy().run(game))
    assert game.government == "GOVERNMENT_MONARCHY"
    tried = {a[1] for a in game.actions}  # Only better governments are even asked for.
    assert tried == set(government_candidates("GOVERNMENT_MONARCHY", GovernmentWeights()))


def test_locked_governments_are_not_retried_until_a_civic_completes():
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM")
    policy = GovernmentPolicy()
    run(policy.run(game))
    first = len(game.actions)
    assert first == len(government_candidates("GOVERNMENT_CHIEFDOM", GovernmentWeights()))
    run(policy.run(game))
    run(policy.run(game))
    assert len(game.actions) == first
    game.civics += 1
    run(policy.run(game))
    assert len(game.actions) > first


def test_a_refused_candidate_falls_through_to_the_next():
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM")
    game.unlocked = {"GOVERNMENT_CLASSICAL_REPUBLIC", "GOVERNMENT_MONARCHY"}
    game.refuse["GOVERNMENT_MONARCHY"] = "Error: something else"
    run(GovernmentPolicy().run(game))
    assert game.government == "GOVERNMENT_CLASSICAL_REPUBLIC"


def test_vague_text_is_not_a_change():
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM")
    game.unlocked = {"GOVERNMENT_MONARCHY"}
    game.refuse["GOVERNMENT_MONARCHY"] = "Action completed (no response)."
    policy = GovernmentPolicy()
    run(policy.run(game))
    assert policy._changes == 0


def test_change_count_is_capped():
    weights = GovernmentWeights(max_government_changes=1, min_turns_between_changes=0)
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM")
    game.unlocked = {"GOVERNMENT_CLASSICAL_REPUBLIC"}
    policy = GovernmentPolicy(weights)
    run(policy.run(game))
    game.unlocked.add("GOVERNMENT_DIGITAL_DEMOCRACY")
    run(policy.run(game))
    assert game.government == "GOVERNMENT_CLASSICAL_REPUBLIC"


def test_unreadable_government_is_left_alone():
    game = ScriptedGame(government="NONE")
    game.unlocked = {"GOVERNMENT_MONARCHY"}
    run(GovernmentPolicy().run(game))
    assert game.government == "NONE"
    assert not game.actions


def test_slots_are_not_filled_on_the_turn_of_a_change():
    game = ScriptedGame(
        government="GOVERNMENT_CHIEFDOM",
        slots=[slot(0, "SLOT_ECONOMIC")],
        cards=[info("POLICY_A", "SLOT_ECONOMIC")],
    )
    game.unlocked = {"GOVERNMENT_MONARCHY"}
    policy = GovernmentPolicy()
    run(policy.run(game))
    assert not [a for a in game.actions if a[0] == "policies"]
    game.data["overview"] = fx.overview(turn=51)
    run(policy.run(game))
    assert [a for a in game.actions if a[0] == "policies"]


# --- Policy cards -------------------------------------------------------------------------------


def test_fills_each_empty_slot_with_the_best_fitting_card():
    game = ScriptedGame(
        slots=[
            slot(0, "SLOT_MILITARY", "POLICY_DISCIPLINE"),
            slot(1, "SLOT_ECONOMIC"),
            slot(2, "SLOT_MILITARY"),
        ],
        cards=[
            info("POLICY_DISCIPLINE", "SLOT_MILITARY"),
            info("POLICY_URBAN_PLANNING", "SLOT_ECONOMIC"),
            info("POLICY_GOD_KING", "SLOT_ECONOMIC"),
            info("POLICY_AGOGE", "SLOT_MILITARY"),
        ],
    )
    run(GovernmentPolicy().run(game))
    assert [s.current_policy for s in game.slots] == [
        "POLICY_DISCIPLINE",
        "POLICY_URBAN_PLANNING",
        "POLICY_AGOGE",
    ]


def test_wildcard_slot_does_not_take_the_card_a_fixed_slot_needs():
    game = ScriptedGame(
        slots=[slot(0, "SLOT_WILDCARD"), slot(1, "SLOT_ECONOMIC")],
        cards=[
            info("POLICY_URBAN_PLANNING", "SLOT_ECONOMIC"),
            info("POLICY_AGOGE", "SLOT_MILITARY"),
        ],
    )
    run(GovernmentPolicy().run(game))
    assert [s.current_policy for s in game.slots] == ["POLICY_AGOGE", "POLICY_URBAN_PLANNING"]


def test_a_fixed_slot_never_gets_a_card_of_another_type():
    game = ScriptedGame(
        slots=[slot(0, "SLOT_ECONOMIC")], cards=[info("POLICY_AGOGE", "SLOT_MILITARY")]
    )
    run(GovernmentPolicy().run(game))
    assert not game.actions
    assert game.slots[0].current_policy is None


def test_a_refused_card_falls_through_without_stranding_other_slots():
    game = ScriptedGame(
        slots=[slot(0, "SLOT_ECONOMIC"), slot(1, "SLOT_MILITARY")],
        cards=[
            info("POLICY_URBAN_PLANNING", "SLOT_ECONOMIC"),
            info("POLICY_GOD_KING", "SLOT_ECONOMIC"),
            info("POLICY_AGOGE", "SLOT_MILITARY"),
        ],
    )
    game.refuse["POLICY_URBAN_PLANNING"] = "Error: CANNOT_SLOT|rejected"
    run(GovernmentPolicy().run(game))
    assert [s.current_policy for s in game.slots] == ["POLICY_GOD_KING", "POLICY_AGOGE"]


def test_silent_failure_and_vague_text_are_refusals():
    game = ScriptedGame(
        slots=[slot(0, "SLOT_ECONOMIC")],
        cards=[info("POLICY_A", "SLOT_ECONOMIC"), info("POLICY_B", "SLOT_ECONOMIC")],
    )
    game.refuse["POLICY_A"] = "POLICIES_SET|ok\nWARN:SILENT_FAILURE - engine rejected"
    game.refuse["POLICY_B"] = "Action completed (no response)."
    run(GovernmentPolicy().run(game))
    assert game.slots[0].current_policy is None
    assert len(game.actions) == 2  # Each tried once, no retry loop.


def test_attempts_per_slot_are_bounded():
    cards = [info(f"POLICY_C{i}", "SLOT_ECONOMIC") for i in range(10)]
    game = ScriptedGame(slots=[slot(0, "SLOT_ECONOMIC")], cards=cards)
    game.refuse.update({c.policy_type: "Error: no" for c in cards})
    run(GovernmentPolicy().run(game))
    assert len(game.actions) == 4


def test_no_empty_slots_means_no_calls_and_card_in_use_is_not_reused():
    game = ScriptedGame(
        slots=[slot(0, "SLOT_ECONOMIC", "POLICY_A"), slot(1, "SLOT_ECONOMIC")],
        cards=[info("POLICY_A", "SLOT_ECONOMIC")],
    )
    run(GovernmentPolicy().run(game))
    assert not game.actions


def test_card_order_is_deterministic_and_ignores_input_order():
    weights = GovernmentWeights()
    eco = PolicySlot(0, "SLOT_ECONOMIC", None)
    cards = [Policy("POLICY_B", "B", "SLOT_ECONOMIC"), Policy("POLICY_A", "A", "SLOT_ECONOMIC")]
    assert rank_cards(eco, cards, weights) == rank_cards(eco, cards[::-1], weights)
    assert rank_cards(eco, cards, weights)[0].policy_type == "POLICY_A"
    priorities = (("B", 5.0),)
    heavy = GovernmentWeights(policy_priorities=priorities)
    assert rank_cards(eco, cards, heavy)[0].policy_type == "POLICY_B"


# --- Governors ----------------------------------------------------------------------------------


def test_points_are_spent_on_appointments_in_priority_order_and_placed():
    game = ScriptedGame(points=2)
    game.to_appoint = ["GOVERNOR_THE_DEFENDER", "GOVERNOR_THE_EDUCATOR", "GOVERNOR_THE_MERCHANT"]
    run(GovernmentPolicy().run(game))
    assert game.points == 0
    assert [a[1] for a in game.actions if a[0] == "appoint"] == [
        "GOVERNOR_THE_EDUCATOR",
        "GOVERNOR_THE_MERCHANT",
    ]
    placed = {a[1]: a[2] for a in game.actions if a[0] == "assign"}
    assert set(placed) == {"GOVERNOR_THE_EDUCATOR", "GOVERNOR_THE_MERCHANT"}
    assert len(set(placed.values())) == 2  # One governor per city.


def test_educator_goes_to_the_highest_science_city():
    cities = [
        fx.city_info(city_id=1, science=2.0),
        fx.city_info(city_id=2, science=9.0),
    ]
    game = ScriptedGame(points=1, cities=cities)
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR"]
    run(GovernmentPolicy().run(game))
    assert ("assign", "GOVERNOR_THE_EDUCATOR", 2) in game.actions


def test_placed_governors_are_never_moved():
    game = ScriptedGame()
    game.appointed = [appointed("GOVERNOR_THE_EDUCATOR", 65536)]
    run(GovernmentPolicy().run(game))
    assert not game.actions


def test_promotes_when_nothing_is_left_to_appoint_and_falls_through_promotions():
    game = ScriptedGame(points=1)
    game.appointed = [appointed("GOVERNOR_THE_EDUCATOR", 65536, ["PROMO_LOCKED", "PROMO_OK"])]
    game.refuse["PROMO_LOCKED"] = "Error: PREREQ_NOT_MET|needs one"
    run(GovernmentPolicy().run(game))
    assert game.points == 0
    assert [a[2] for a in game.actions if a[0] == "promote"] == ["PROMO_LOCKED", "PROMO_OK"]


def test_refused_appointment_falls_through_to_the_next_and_then_to_promotion():
    game = ScriptedGame(points=1)
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR", "GOVERNOR_THE_BUILDER"]
    game.refuse["GOVERNOR_THE_EDUCATOR"] = "Error: CANNOT_APPOINT"
    run(GovernmentPolicy().run(game))
    assert game.points == 0
    assert ("appoint", "GOVERNOR_THE_BUILDER") in game.actions


def test_accepted_but_unapplied_appointment_stops_instead_of_looping():
    game = ScriptedGame(points=1)
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR"]

    async def pending(governor_type):
        game.actions.append(("appoint", governor_type))
        return "APPOINT_REQUESTED|x - verify with get_governors()"

    game.appoint_governor = pending
    run(GovernmentPolicy().run(game))
    assert len([a for a in game.actions if a[0] == "appoint"]) == 1


def test_an_appointment_that_keeps_the_point_is_skipped_for_a_promotion_and_not_retried():
    # Run 3, turn 318: appointing Ibrahim kept the point every time, and the turn stayed held.
    game = ScriptedGame(points=1)
    game.to_appoint = ["GOVERNOR_IBRAHIM"]
    game.appointed = [appointed("GOVERNOR_THE_EDUCATOR", 65536, ["PROMO_OK"])]

    async def kept(governor_type):
        game.actions.append(("appoint", governor_type))
        return f"APPOINTED|{governor_type} (x)"

    game.appoint_governor = kept
    policy = GovernmentPolicy()
    run(policy.run(game))
    assert game.points == 0
    assert ("promote", "GOVERNOR_THE_EDUCATOR", "PROMO_OK") in game.actions
    game.points = 1
    game.appointed = [appointed("GOVERNOR_THE_EDUCATOR", 65536, ["PROMO_NEXT"])]
    run(policy.run(game))
    assert [a for a in game.actions if a[0] == "appoint"] == [("appoint", "GOVERNOR_IBRAHIM")]


def test_vague_text_is_not_an_appointment():
    game = ScriptedGame(points=1)
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR"]
    game.refuse["GOVERNOR_THE_EDUCATOR"] = "Action completed (no response)."
    run(GovernmentPolicy().run(game))
    assert game.points == 1


def test_refused_city_falls_through_to_the_next():
    cities = [fx.city_info(city_id=1, science=9.0), fx.city_info(city_id=2, science=2.0)]
    game = ScriptedGame(points=1, cities=cities)
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR"]
    game.refuse[("GOVERNOR_THE_EDUCATOR", 1)] = "Error: CANNOT_ASSIGN"
    run(GovernmentPolicy().run(game))
    assert ("assign", "GOVERNOR_THE_EDUCATOR", 2) in game.actions


def test_points_with_nothing_to_spend_are_left_without_an_endless_loop():
    game = ScriptedGame(points=2)
    run(GovernmentPolicy().run(game))
    assert not game.actions


# --- Failure isolation and weights --------------------------------------------------------------


def test_one_step_failing_does_not_stop_the_others():
    game = ScriptedGame(
        points=1, slots=[slot(0, "SLOT_ECONOMIC")], cards=[info("POLICY_A", "SLOT_ECONOMIC")]
    )
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR"]

    async def broken(assignments):
        raise ValueError("boom")

    game.set_policies = broken
    with pytest.raises(RuntimeError, match="policy cards"):
        run(GovernmentPolicy().run(game))
    assert game.points == 0


def test_connection_errors_propagate():
    game = ScriptedGame(slots=[slot(0, "SLOT_ECONOMIC")], cards=[info("POLICY_A", "SLOT_ECONOMIC")])

    async def gone(assignments):
        raise ConnectionError

    game.set_policies = gone
    with pytest.raises(ConnectionError):
        run(GovernmentPolicy().run(game))


def test_weights_are_validated():
    for bad in (
        {"min_rank_gain": -1.0},
        {"default_policy": float("nan")},
        {"max_government_changes": True},
        {"governor_focus": (("EDUCATOR", "nonsense"),)},
        {"policy_priorities": (("X", float("inf")),)},
    ):
        with pytest.raises(ValueError):
            GovernmentWeights(**bad)


# --- Blocker policies ---------------------------------------------------------------------------


def test_blocker_policies_cover_slots_governors_and_government():
    policy = GovernmentPolicy()
    table = blocker_policies(policy)
    assert set(table) == {
        BlockerType.FILL_CIVIC_SLOT,
        BlockerType.GOVERNOR_APPOINTMENT,
        BlockerType.GOVERNOR_OPPORTUNITY,
        BlockerType.GOVERNOR_IDLE,
        BlockerType.CONSIDER_GOVERNMENT_CHANGE,
    }
    game = ScriptedGame(
        points=1, slots=[slot(0, "SLOT_ECONOMIC")], cards=[info("POLICY_A", "SLOT_ECONOMIC")]
    )
    game.to_appoint = ["GOVERNOR_THE_EDUCATOR"]
    blocker = Blocker(BlockerType.FILL_CIVIC_SLOT)
    assert run(table[BlockerType.FILL_CIVIC_SLOT](game, blocker)) is True
    assert run(table[BlockerType.GOVERNOR_APPOINTMENT](game, blocker)) is True
    assert run(table[BlockerType.GOVERNOR_IDLE](game, blocker)) is True


def test_slot_blocker_reports_unfilled_so_the_default_runs():
    game = ScriptedGame(slots=[slot(0, "SLOT_ECONOMIC")], cards=[])
    table = blocker_policies()
    assert run(table[BlockerType.FILL_CIVIC_SLOT](game, Blocker("x"))) is False


def test_government_blocker_shares_the_policys_bounds():
    policy = GovernmentPolicy()
    game = ScriptedGame(government="GOVERNMENT_CHIEFDOM")
    game.unlocked = {"GOVERNMENT_MONARCHY"}
    table = blocker_policies(policy)
    assert run(table[BlockerType.CONSIDER_GOVERNMENT_CHANGE](game, Blocker("x"))) is True
    assert game.government == "GOVERNMENT_MONARCHY"
    game.unlocked.add("GOVERNMENT_DIGITAL_DEMOCRACY")
    run(policy.run(game))  # Same instance, inside the gap.
    assert game.government == "GOVERNMENT_MONARCHY"


# --- Repeats (issue 139) ------------------------------------------------------------------------


def test_a_card_that_does_not_stick_is_not_set_a_third_time_in_the_same_slot():
    game = ScriptedGame(
        slots=[slot(0, "SLOT_ECONOMIC")],
        cards=[info("POLICY_A", "SLOT_ECONOMIC"), info("POLICY_B", "SLOT_ECONOMIC")],
    )
    policy = GovernmentPolicy()
    for _ in range(5):
        game.slots = [slot(0, "SLOT_ECONOMIC")]  # The game empties the slot between turns.
        run(policy.run(game))
    sets = [a[1][0] for a in game.actions if a[0] == "policies"]
    assert sets.count("POLICY_A") == 2 and sets.count("POLICY_B") == 2
    assert len(sets) == 4  # Both cards used up, then no more calls.


def test_a_card_that_stays_is_set_once():
    game = ScriptedGame(slots=[slot(0, "SLOT_ECONOMIC")], cards=[info("POLICY_A", "SLOT_ECONOMIC")])
    policy = GovernmentPolicy()
    for _ in range(4):
        run(policy.run(game))
    assert len([a for a in game.actions if a[0] == "policies"]) == 1


def test_a_government_change_resets_the_card_counts():
    game = ScriptedGame(
        government="GOVERNMENT_CHIEFDOM",
        slots=[slot(0, "SLOT_ECONOMIC")],
        cards=[info("POLICY_A", "SLOT_ECONOMIC")],
    )
    policy = GovernmentPolicy()
    policy._card_sets[(0, "POLICY_A")] = 5
    game.unlocked = {"GOVERNMENT_MONARCHY"}
    run(policy.run(game))
    assert not policy._card_sets


def test_waiting_governor_with_no_free_city_is_not_a_warning_or_a_call(caplog):
    game = ScriptedGame(cities=[fx.city_info(city_id=1), fx.city_info(city_id=2)])
    game.appointed = [
        appointed("GOVERNOR_THE_EDUCATOR", 1),
        appointed("GOVERNOR_THE_BUILDER", 2),
        appointed("GOVERNOR_THE_MERCHANT"),
    ]
    with caplog.at_level("WARNING"):
        run(GovernmentPolicy().run(game))
    assert not game.actions
    assert "unfinished" not in caplog.text


def test_a_governor_that_still_reads_as_waiting_is_not_sent_again_and_again():
    game = ScriptedGame(cities=[fx.city_info(city_id=1), fx.city_info(city_id=2)])
    game.appointed = [appointed("GOVERNOR_THE_EDUCATOR")]

    async def no_effect(governor_type, city_id):
        game.actions.append(("assign", governor_type, city_id))
        return "ASSIGNED|x"

    game.assign_governor = no_effect
    policy = GovernmentPolicy()
    for _ in range(6):
        run(policy.run(game))
    assert len([a for a in game.actions if a[0] == "assign"]) == 4  # 2 cities, 2 sends each.
