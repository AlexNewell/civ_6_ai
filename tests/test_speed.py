from civ6_ai.metrics.speed import DEV_BAR_SECONDS, GOAL_SECONDS, summarise


def test_no_turns_gives_none():
    assert summarise([]) is None


def test_decision_time_excludes_end_turn_wait():
    summary = summarise([(50.0, 20.0), (40.0, 30.0), (90.0, 30.0)])
    assert summary is not None
    assert summary.turns == 3
    assert summary.decision.mean == (30 + 10 + 60) / 3
    assert summary.decision.median == 30
    assert summary.decision.max == 60
    assert summary.end_turn.max == 30
    assert summary.total.max == 90


def test_p90_is_nearest_rank():
    summary = summarise([(float(n), 0.0) for n in range(1, 11)])
    assert summary is not None
    assert summary.decision.p90 == 9
    single = summarise([(7.0, 0.0)])
    assert single is not None and single.decision.p90 == 7


def test_negative_decision_time_is_clamped():
    summary = summarise([(10.0, 10.5)])
    assert summary is not None
    assert summary.decision.max == 0


def test_verdict_against_both_targets():
    over_bar = summarise([(DEV_BAR_SECONDS + 5, 0.0)])
    between = summarise([(GOAL_SECONDS + 5, 0.0)])
    under = summarise([(GOAL_SECONDS - 1, 0.0)])
    assert over_bar is not None and over_bar.over_dev_bar and over_bar.over_goal
    assert "over the 60s development bar" in over_bar.lines()[0]
    assert between is not None and not between.over_dev_bar and between.over_goal
    assert "over the 10s goal" in between.lines()[0]
    assert under is not None and not under.over_goal
    assert "under the 10s goal" in under.lines()[0]
