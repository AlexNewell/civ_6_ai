import asyncio
import json
import math
from dataclasses import replace

import pytest

from civ6_ai import bot
from civ6_ai.monitors import EventBus
from civ6_ai.orchestrator import TurnLog
from civ6_ai.policies.gold import GoldPolicy
from civ6_ai.policies.military import MilitaryPolicy, MilitaryWeights
from civ6_ai.policies.production import (
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    rank_production,
)
from civ6_ai.policies.research import ResearchPolicy, ResearchWeights, choose_tech
from civ6_ai.policies.secondary import SecondaryPolicy, SecondaryWeights
from civ6_ai.state import ProductionChoice
from civ6_ai.state.reader import build_city
from civ6_ai.strategy.apply import PolicyDocumentHandler, apply_resolved
from civ6_ai.strategy.policy_document import (
    _DOMAINS,
    _POSTURE_PROFILES,
    _VICTORY_PROFILES,
    DEFAULT_DOCUMENT,
    MAX_TARGET_CITIES,
    Expansion,
    PolicyDocument,
    PolicyDocumentError,
    Posture,
    VictoryTarget,
    accept_document,
    parse_policy_document,
    parse_policy_json,
    resolve,
    settable_weights,
)
from fixtures import civ_state as fx


def run(coro):
    return asyncio.run(coro)


def rejected(data):
    with pytest.raises(PolicyDocumentError) as error:
        parse_policy_document(data)
    return error.value.problems


# Parsing


def test_empty_document_is_the_default():
    assert parse_policy_document({}) == DEFAULT_DOCUMENT


def test_defaults_resolve_to_every_policys_own_defaults():
    resolved = resolve(DEFAULT_DOCUMENT)
    assert resolved.production == ProductionWeights()
    assert resolved.research == ResearchWeights()
    assert resolved.secondary == SecondaryWeights()
    assert resolved.military == MilitaryWeights()
    assert resolved.gold == ProductionWeights()


def test_full_document_parses():
    doc = parse_policy_document(
        {
            "version": 1,
            "victory": "science",
            "posture": "expand",
            "expansion": {"target_cities": 9},
            "weights": {"production": {"faith": 2, "wonders": 0}, "military": {"heal_below": 0.5}},
        }
    )
    assert doc.victory is VictoryTarget.SCIENCE
    assert doc.posture is Posture.EXPAND
    assert doc.expansion == Expansion(target_cities=9)
    assert dict(doc.weights)["production"] == (("faith", 2.0), ("wonders", 0.0))


def test_round_trip_through_json():
    doc = parse_policy_document(
        {"victory": "culture", "posture": "defend", "weights": {"research": {"speed": 8}}}
    )
    assert parse_policy_json(doc.to_json()) == doc
    assert parse_policy_json(DEFAULT_DOCUMENT.to_json()) == DEFAULT_DOCUMENT


@pytest.mark.parametrize(
    "data",
    [
        [],
        "science",
        None,
        {"victory": "peace"},
        {"victory": None},
        {"posture": 3},
        {"version": 2},
        {"colour": "red"},
        {"expansion": 5},
        {"expansion": {"target_cities": 0}},
        {"expansion": {"target_cities": MAX_TARGET_CITIES + 1}},
        {"expansion": {"target_cities": 2.5}},
        {"expansion": {"target_cities": "6"}},
        {"expansion": {"target_cities": True}},
        {"expansion": {"cities": 6}},
        {"weights": []},
        {"weights": {"navy": {"speed": 1}}},
        {"weights": {"production": 3}},
        {"weights": {"production": {"nonsense": 1}}},
        {"weights": {"production": {"faith": "high"}}},
        {"weights": {"production": {"faith": True}}},
        {"weights": {"production": {"faith": -1}}},
        {"weights": {"production": {"faith": math.inf}}},
        {"weights": {"production": {"faith": math.nan}}},
        {"weights": {"production": {"faith": 1e9}}},
        {"weights": {"production": {"target_cities": 3.5}}},
        {"weights": {"military": {"heal_below": 1.5}}},
        {"weights": {"research": {"priorities": []}}},
    ],
)
def test_invalid_documents_are_rejected(data):
    assert rejected(data)


def test_every_problem_is_listed_with_its_field():
    problems = rejected(
        {"victory": "peace", "posture": "x", "weights": {"production": {"faith": -1, "zzz": 1}}}
    )
    text = "\n".join(problems)
    assert len(problems) == 4
    for where in ("victory", "posture", "weights.production.faith", "weights.production.zzz"):
        assert where in text


def test_bad_json_text_is_rejected():
    with pytest.raises(PolicyDocumentError):
        parse_policy_json("{not json")


def test_accept_document_keeps_the_current_one_when_invalid():
    current = parse_policy_document({"victory": "culture"})
    kept, problems = accept_document({"victory": "peace"}, current)
    assert kept is current and problems
    new, problems = accept_document({"victory": "science"}, current)
    assert new.victory is VictoryTarget.SCIENCE and not problems


def test_integer_fields_accept_whole_floats():
    doc = parse_policy_document({"weights": {"production": {"target_cities": 8.0}}})
    assert dict(dict(doc.weights)["production"])["target_cities"] == 8


# Resolving


def test_every_profile_names_real_float_fields():
    for profiles in (*_VICTORY_PROFILES.values(), *_POSTURE_PROFILES.values()):
        for domain, table in profiles.items():
            allowed = settable_weights(domain)
            for name in table:
                assert allowed[name] is float, (domain, name)


def test_every_victory_and_posture_resolves():
    for victory in VictoryTarget:
        for posture in Posture:
            resolve(PolicyDocument(victory=victory, posture=posture))


def test_domains_cover_every_resolved_field():
    assert set(_DOMAINS) == {"production", "research", "secondary", "military", "gold"}


def test_victory_target_scales_the_serving_weights():
    science = resolve(PolicyDocument(victory=VictoryTarget.SCIENCE))
    assert science.production.science > ProductionWeights().science
    assert science.secondary.gp_scientist > SecondaryWeights().gp_scientist
    culture = resolve(PolicyDocument(victory=VictoryTarget.CULTURE))
    assert culture.production.culture > ProductionWeights().culture
    assert culture.production.science == ProductionWeights().science
    religion = resolve(PolicyDocument(victory=VictoryTarget.RELIGIOUS))
    assert religion.secondary.faith > SecondaryWeights().faith


def test_posture_moves_military_and_city_target():
    default = ProductionWeights()
    expand = resolve(PolicyDocument(posture=Posture.EXPAND)).production
    defend = resolve(PolicyDocument(posture=Posture.DEFEND)).production
    conquer = resolve(PolicyDocument(posture=Posture.CONQUER)).production
    assert expand.target_cities > default.target_cities > defend.target_cities
    assert expand.expansion > default.expansion > defend.expansion
    assert conquer.military > default.military
    assert resolve(PolicyDocument(posture=Posture.DEFEND)).military.heal_below > 0.7


def test_explicit_expansion_replaces_the_posture_target():
    doc = PolicyDocument(posture=Posture.EXPAND, expansion=Expansion(target_cities=3))
    assert resolve(doc).production.target_cities == 3
    assert resolve(doc).gold.target_cities == 3


def test_posture_target_is_kept_in_bounds():
    low = ProductionWeights(target_cities=1)
    assert low.target_cities == 1
    doc = parse_policy_document({"weights": {"production": {"target_cities": 1}}})
    assert resolve(replace(doc, posture=Posture.DEFEND)).production.target_cities == 1


def test_explicit_weights_beat_profiles():
    doc = parse_policy_document(
        {"victory": "science", "weights": {"production": {"science": 1.0}, "gold": {"faith": 3}}}
    )
    resolved = resolve(doc)
    assert resolved.production.science == 1.0
    assert resolved.production.faith == ProductionWeights().faith  # gold domain is separate
    assert resolved.gold.faith == 3.0
    assert resolved.gold.science > 1.0


# Behaviour: changing a weight changes what a policy does

MENU = [
    ProductionChoice("UNIT", "UNIT_SETTLER", 80, 8),
    ProductionChoice("BUILDING", "BUILDING_LIBRARY", 90, 9),
    ProductionChoice("BUILDING", "BUILDING_MONUMENT", 50, 5),
    ProductionChoice("UNIT", "UNIT_BUILDER", 50, 5),
    ProductionChoice("UNIT", "UNIT_WARRIOR", 40, 4),
]


def best_build(weights, num_cities=4):
    city = build_city(fx.city_info(population=5, buildings=["PALACE"]), (), set())
    ctx = ProductionContext(turn=60, num_cities=num_cities, builders=4, recon=1, army=6)
    return rank_production(city, MENU, ctx, weights)[0].item


def test_city_target_changes_whether_a_settler_is_built():
    capped = resolve(parse_policy_document({"expansion": {"target_cities": 4}})).production
    roomy = resolve(parse_policy_document({"expansion": {"target_cities": 10}})).production
    assert best_build(capped) != "UNIT_SETTLER"
    assert best_build(roomy) == "UNIT_SETTLER"


def test_posture_changes_the_city_target_behaviour():
    # Four cities: the default target of six still wants a settler, a defensive posture does not.
    assert best_build(resolve(DEFAULT_DOCUMENT).production) == "UNIT_SETTLER"
    defend = resolve(PolicyDocument(posture=Posture.DEFEND)).production
    assert best_build(defend) != "UNIT_SETTLER"


def test_a_research_weight_changes_the_tech_chosen():
    from civ_mcp import lua as lq

    from civ6_ai.state import reader

    def tech(name, turns, boosted=False):
        return lq.TechOption(name, f"TECHNOLOGY_{name.upper()}", 50, 0, turns, boosted, "", "")

    status = replace(
        fx.research_status(),
        current_research="None",
        available_techs=[tech("Quick", 2), tech("Boosted", 4, boosted=True)],
        locked_techs=[],
    )
    research = reader.build_research(status)
    low = resolve(parse_policy_document({"weights": {"research": {"eureka": 0}}})).research
    high = resolve(parse_policy_document({"weights": {"research": {"eureka": 9}}})).research
    assert choose_tech(research, replace(low, priorities=()))[0].name == "Quick"
    assert choose_tech(research, replace(high, priorities=()))[0].name == "Boosted"


def test_a_military_weight_reaches_the_policy_settings():
    policy = MilitaryPolicy()
    doc = parse_policy_document({"weights": {"military": {"heal_below": 0.9}}})
    apply_resolved(resolve(doc), military=policy)
    assert policy.settings.weights.heal_below == 0.9
    assert policy.settings.max_garrison == MilitaryPolicy().settings.max_garrison


# Applying to policies and the turn loop


def policies():
    return {
        "production": CityProductionPolicy(),
        "research": ResearchPolicy(),
        "secondary": SecondaryPolicy(),
        "military": MilitaryPolicy(),
        "gold": GoldPolicy(),
    }


def test_apply_sets_every_policys_weights():
    targets = policies()
    doc = parse_policy_document({"victory": "culture", "posture": "conquer"})
    resolved = resolve(doc)
    apply_resolved(resolved, **targets)
    assert targets["production"].weights == resolved.production
    assert targets["research"].weights == resolved.research
    assert targets["secondary"].weights == resolved.secondary
    assert targets["military"].settings.weights == resolved.military
    assert targets["gold"].weights == resolved.gold


def test_handler_applies_a_file_and_follows_changes(tmp_path):
    path = tmp_path / "policy.json"
    targets = policies()
    handler = PolicyDocumentHandler(path, **targets)
    run(handler.run(None))  # No file yet: nothing changes.
    assert targets["production"].weights == ProductionWeights()

    path.write_text(json.dumps({"expansion": {"target_cities": 11}}), encoding="utf-8")
    run(handler.run(None))
    assert targets["production"].weights.target_cities == 11
    assert handler.document.expansion.target_cities == 11

    path.write_text(
        json.dumps({"expansion": {"target_cities": 12, "pad": "x" * 5}}), encoding="utf-8"
    )
    run(handler.run(None))
    assert handler.problems  # Refused: unknown field.
    assert targets["production"].weights.target_cities == 11

    path.write_text(json.dumps({"expansion": {"target_cities": 7}}), encoding="utf-8")
    run(handler.run(None))
    assert targets["production"].weights.target_cities == 7
    assert not handler.problems


def test_handler_keeps_weights_when_the_file_is_broken_or_removed(tmp_path):
    path = tmp_path / "policy.json"
    targets = policies()
    handler = PolicyDocumentHandler(path, **targets)
    path.write_text('{"posture": "defend"}', encoding="utf-8")
    assert handler.refresh()
    applied = targets["production"].weights
    path.write_text("{oops", encoding="utf-8")
    assert not handler.refresh()
    assert handler.problems
    assert targets["production"].weights == applied
    path.unlink()
    assert not handler.refresh()
    assert targets["production"].weights == applied


def test_handler_does_not_reparse_an_unchanged_file(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("{}", encoding="utf-8")
    handler = PolicyDocumentHandler(path, **policies())
    assert handler.refresh()
    assert not handler.refresh()


def test_bad_file_from_the_start_leaves_policies_on_defaults(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"victory": "peace"}', encoding="utf-8")
    targets = policies()
    handler = PolicyDocumentHandler(path, **targets)
    assert not handler.refresh()
    assert handler.document == DEFAULT_DOCUMENT
    assert targets["production"].weights == ProductionWeights()


class WiredGame(fx.FakeGame):
    pass


def test_bot_runs_the_document_handler_first_after_monitors(tmp_path):
    path = tmp_path / "policy.json"
    orchestrator = bot.build_orchestrator(
        WiredGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"), policy_document=path
    )
    names = [handler.name for handler in orchestrator._handlers]
    assert names[:2] == ["monitors", "policy_document"]


def test_bot_without_a_document_has_no_handler(tmp_path):
    orchestrator = bot.build_orchestrator(WiredGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"))
    assert "policy_document" not in [handler.name for handler in orchestrator._handlers]


def test_bot_document_changes_the_wired_policies(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"expansion": {"target_cities": 13}}', encoding="utf-8")
    orchestrator = bot.build_orchestrator(
        WiredGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"), policy_document=path
    )
    handlers = {handler.name: handler for handler in orchestrator._handlers}
    run(handlers["policy_document"].run(None))
    production = next(h for h in orchestrator._handlers if isinstance(h, CityProductionPolicy))
    gold = next(h for h in orchestrator._handlers if isinstance(h, GoldPolicy))
    assert production.weights.target_cities == 13
    assert gold.weights.target_cities == 13
