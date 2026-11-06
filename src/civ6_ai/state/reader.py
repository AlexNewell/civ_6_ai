"""Turn civ6-mcp's raw reads into the views in `models.py`.

The `build_*` functions are pure, so they run on fixtures. The async `read_*` functions make the
game calls one at a time, because the tuner serialises them anyway. Each read is a round trip,
so a policy should call the narrowest `read_*` it can. `fog.py` explains every filter here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from civ6_ai.state.fog import BARBARIAN_ID, is_hidden_stealth_unit
from civ6_ai.state.models import (
    Belief,
    BuilderJob,
    City,
    CityState,
    District,
    Empire,
    Exploration,
    FogEdge,
    Governor,
    KnownResource,
    LockedResearch,
    Policy,
    PolicySlot,
    ProductionChoice,
    Research,
    ResearchOption,
    Rival,
    RivalCity,
    SettleSite,
    Snapshot,
    Stockpile,
    Target,
    Threat,
    Tile,
    TileItem,
    Unit,
)

if TYPE_CHECKING:
    from civ_mcp import lua as lq

# Civ VI's Small map. Used to measure distance across the east-west wrap.
STANDARD_MAP_WIDTH = 74
CITY_THREAT_RADIUS = 4  # City attacks reach 2 tiles, and most land units move 2 a turn.
_IDLE_BUILDS = {"", "nothing", "NONE"}
_RESOURCE_CLASSES = {"strategic", "luxury", "bonus"}
_TARGET = re.compile(r"(?P<type>\w+)@(?P<x>-?\d+),(?P<y>-?\d+)\((?P<hp>-?\d+)hp\)")
_TILE_ITEM = re.compile(r"(?P<name>\w+)@(?P<x>-?\d+),(?P<y>-?\d+)")

type EmpireResources = tuple[
    list[lq.ResourceStockpile],
    list[lq.OwnedResource],
    list[lq.NearbyResource],
    dict[str, int],
]


class GameReads(Protocol):
    """The read-only part of civ6-mcp's `GameState` that this layer uses."""

    async def get_game_overview(self) -> lq.GameOverview: ...

    async def get_cities(self) -> tuple[list[lq.CityInfo], list[str]]: ...

    async def get_units(self) -> list[lq.UnitInfo]: ...

    async def get_tech_civics(self) -> lq.TechCivicStatus: ...

    async def get_empire_resources(self) -> EmpireResources: ...

    async def get_threat_scan(self) -> list[lq.ThreatInfo]: ...

    async def get_diplomacy(self) -> list[lq.CivInfo]: ...

    async def get_victory_progress(self) -> lq.VictoryProgress: ...

    async def get_policies(self) -> lq.GovernmentStatus: ...

    async def get_governors(self) -> lq.GovernorStatus: ...

    async def get_trade_routes(self) -> lq.TradeRouteStatus: ...

    async def get_pantheon_status(self) -> lq.PantheonStatus: ...

    async def get_city_states(self) -> lq.EnvoyStatus: ...

    async def list_city_production(self, city_id: int) -> list[lq.ProductionOption]: ...

    async def get_builder_tasks(self) -> tuple[list[lq.BuilderTask], list[lq.BuilderInfo]]: ...

    async def get_map_area(
        self, center_x: int, center_y: int, radius: int = 2
    ) -> list[lq.TileInfo]: ...

    async def get_strategic_map(self) -> lq.StrategicMapData: ...

    async def get_global_settle_scan(self) -> list[lq.SettleCandidate]: ...


def hex_distance(
    x1: int, y1: int, x2: int, y2: int, map_width: int | None = STANDARD_MAP_WIDTH
) -> int:
    """Tiles between two grid positions. Odd rows sit half a tile east, and x wraps."""

    def axial_distance(east: int) -> int:
        q1, q2 = x1 - (y1 - (y1 & 1)) // 2, east - (y2 - (y2 & 1)) // 2
        return max(abs(q1 - q2), abs(y1 - y2), abs(q1 + y1 - q2 - y2))

    if map_width is None:
        return axial_distance(x2)
    return min(axial_distance(x2 + shift * map_width) for shift in (-1, 0, 1))


def _split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _tile_items(entries: Iterable[str]) -> list[TileItem]:
    matches = (_TILE_ITEM.fullmatch(entry) for entry in entries)
    return [TileItem(m["name"], int(m["x"]), int(m["y"])) for m in matches if m]


def _visible_resources(owned: Iterable[lq.OwnedResource]) -> set[tuple[str, int, int]]:
    return {(resource.name, resource.x, resource.y) for resource in owned}


def _at_war_ids(civs: Iterable[lq.CivInfo]) -> set[int]:
    return {civ.player_id for civ in civs if civ.has_met and civ.is_at_war}


def build_threats(scan: Iterable[lq.ThreatInfo], civs: Iterable[lq.CivInfo]) -> tuple[Threat, ...]:
    at_war = _at_war_ids(civs)
    threats = [
        Threat(
            owner_id=info.owner_id,
            owner=info.owner_name,
            unit_type=info.unit_type,
            x=info.x,
            y=info.y,
            hp=info.hp,
            max_hp=info.max_hp,
            combat_strength=info.combat_strength,
            ranged_strength=info.ranged_strength,
            distance=info.distance,
            # civ6-mcp only reports city-state units when at war with them.
            hostile=(
                info.owner_id == BARBARIAN_ID or info.is_city_state or info.owner_id in at_war
            ),
            is_city_state=info.is_city_state,
        )
        for info in scan
        if not is_hidden_stealth_unit(info.unit_type, info.distance)
    ]
    return tuple(sorted(threats, key=lambda threat: threat.distance))


def build_city(
    info: lq.CityInfo,
    threats: Iterable[Threat],
    visible_resources: set[tuple[str, int, int]],
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> City:
    pillaged = set(info.pillaged_districts)
    districts = [
        District(item.name, item.x, item.y, pillaged=item.name in pillaged)
        for item in _tile_items(info.districts)
    ]
    near = [
        (hex_distance(info.x, info.y, threat.x, threat.y, map_width), index, threat)
        for index, threat in enumerate(threats)
        if threat.hostile
    ]
    return City(
        city_id=info.city_id,
        name=info.name,
        x=info.x,
        y=info.y,
        pop=info.population,
        food=info.food,
        production=info.production,
        gold=info.gold,
        science=info.science,
        culture=info.culture,
        faith=info.faith,
        food_surplus=info.food_surplus,
        food_stored=info.food_stored,
        growth_threshold=info.growth_threshold,
        turns_to_grow=info.turns_to_grow,
        housing=info.housing,
        amenities_total=info.amenities,
        building=None if info.currently_building in _IDLE_BUILDS else info.currently_building,
        build_turns_left=info.production_turns_left,
        defense_strength=info.defense_strength,
        garrison_hp=info.garrison_hp,
        garrison_max_hp=info.garrison_max_hp,
        wall_hp=info.wall_hp,
        wall_max_hp=info.wall_max_hp,
        loyalty=info.loyalty,
        loyalty_max=info.loyalty_max,
        loyalty_per_turn=info.loyalty_per_turn,
        turns_to_flip=info.turns_to_loyalty_flip,
        is_capital="PALACE" in info.buildings,
        garrison_unit=info.garrison_unit or None,
        districts=tuple(districts),
        buildings=tuple(info.buildings),
        pillaged_buildings=tuple(b.removeprefix("BUILDING_") for b in info.pillaged_buildings),
        pillaged_improvements=tuple(_tile_items(info.pillaged_improvements)),
        unimproved_resources=tuple(
            item
            for item in _tile_items(info.unimproved_resources)
            if (item.name, item.x, item.y) in visible_resources
        ),
        threats=tuple(
            threat
            for distance, _, threat in sorted(near, key=lambda entry: entry[:2])
            if distance <= CITY_THREAT_RADIUS
        ),
    )


def _build_targets(info: lq.UnitInfo, map_width: int | None) -> tuple[Target, ...]:
    matches = (_TARGET.fullmatch(entry) for entry in info.targets)
    return tuple(
        Target(m["type"], int(m["x"]), int(m["y"]), int(m["hp"]))
        for m in matches
        if m
        and not is_hidden_stealth_unit(
            m["type"], hex_distance(info.x, info.y, int(m["x"]), int(m["y"]), map_width)
        )
    )


def build_unit(info: lq.UnitInfo, map_width: int | None = STANDARD_MAP_WIDTH) -> Unit:
    return Unit(
        unit_id=info.unit_id,
        unit_index=info.unit_index,
        unit_type=info.unit_type,
        name=info.name,
        x=info.x,
        y=info.y,
        moves=info.moves_remaining,
        max_moves=info.max_moves,
        hp=info.health,
        max_hp=info.max_health,
        combat_strength=info.combat_strength,
        ranged_strength=info.ranged_strength,
        charges=info.build_charges,
        targets=_build_targets(info, map_width),
        can_upgrade=info.can_upgrade,
        upgrade_to=info.upgrade_target or None,
        upgrade_cost=info.upgrade_cost,
        valid_improvements=tuple(info.valid_improvements),
        religion=info.religion or None,
    )


def _active_research(name: str, turns: int) -> tuple[str | None, int | None]:
    return (None, None) if name == "None" else (name, turns)


def _locked(items: Iterable[lq.LockedTech] | Iterable[lq.LockedCivic]) -> tuple:
    return tuple(
        LockedResearch(
            item_type=getattr(item, "tech_type", None) or getattr(item, "civic_type", ""),
            name=item.name,
            missing=tuple(item.missing_prereqs),
            era=item.era,
            boosted=item.boosted,
            boost_desc=item.boost_desc,
        )
        for item in items
    )


def build_research(status: lq.TechCivicStatus) -> Research:
    tech, tech_turns = _active_research(status.current_research, status.current_research_turns)
    civic, civic_turns = _active_research(status.current_civic, status.current_civic_turns)
    return Research(
        current_tech=tech,
        current_tech_turns=tech_turns,
        current_civic=civic,
        current_civic_turns=civic_turns,
        techs=tuple(
            ResearchOption(
                item_type=option.tech_type,
                name=option.name,
                cost=option.cost,
                progress_pct=option.progress_pct,
                turns=option.turns,
                boosted=option.boosted,
                boost_desc=option.boost_desc,
                prereqs=_split(option.prereqs),
                era=option.era,
                unlocks=_split(option.unlocks),
            )
            for option in status.available_techs
        ),
        civics=tuple(
            ResearchOption(
                item_type=option.civic_type,
                name=option.name,
                cost=option.cost,
                progress_pct=option.progress_pct,
                turns=option.turns,
                boosted=option.boosted,
                boost_desc=option.boost_desc,
                prereqs=_split(option.prereqs),
                era=option.era,
            )
            for option in status.available_civics
        ),
        techs_done=status.completed_tech_count,
        civics_done=status.completed_civic_count,
        locked_techs=_locked(status.locked_techs or ()),
        locked_civics=_locked(status.locked_civics or ()),
    )


def _own_entry(overview: lq.GameOverview, victory: lq.VictoryProgress):
    return next((p for p in victory.players if p.player_id == overview.player_id), None)


def build_empire(
    overview: lq.GameOverview,
    victory: lq.VictoryProgress,
    government: lq.GovernmentStatus,
    governors: lq.GovernorStatus,
    trade: lq.TradeRouteStatus,
    resources: EmpireResources,
    pantheon: lq.PantheonStatus,
    envoys: lq.EnvoyStatus,
) -> Empire:
    stockpiles, _, _, luxuries = resources
    own = _own_entry(overview, victory)
    return Empire(
        turn=overview.turn,
        player_id=overview.player_id,
        civ=overview.civ_name,
        leader=overview.leader_name,
        score=overview.score,
        gold=overview.gold,
        gold_per_turn=overview.gold_per_turn,
        gold_income=overview.gold_income,
        maintenance=overview.total_maintenance,
        science=overview.science_yield,
        culture=overview.culture_yield,
        faith=overview.faith,
        favor=overview.diplomatic_favor,
        favor_per_turn=overview.favor_per_turn,
        num_cities=overview.num_cities,
        num_units=overview.num_units,
        population=overview.total_population,
        explored_land=overview.explored_land,
        era=overview.era_name,
        era_score=overview.era_score,
        dark_threshold=overview.era_dark_threshold,
        golden_threshold=overview.era_golden_threshold,
        game_speed=overview.game_speed,
        max_turns=overview.max_turns,
        government=government.government_type,
        policy_slots=tuple(
            PolicySlot(slot.slot_index, slot.slot_type, _policy_in(slot))
            for slot in government.slots
        ),
        unlocked_policies=tuple(
            Policy(policy.policy_type, policy.name, policy.slot_type)
            for policy in government.available_policies
        ),
        governor_points=governors.points_available,
        can_appoint_governor=governors.can_appoint,
        governors=tuple(
            Governor(
                governor_type=governor.governor_type,
                city_id=None if governor.assigned_city_id < 0 else governor.assigned_city_id,
                established=governor.is_established,
                turns_to_establish=governor.turns_to_establish,
                promotions=tuple(p.promotion_type for p in governor.available_promotions),
            )
            for governor in governors.appointed
        ),
        appointable_governors=tuple(g.governor_type for g in governors.available_to_appoint),
        trade_capacity=trade.capacity,
        trade_active=trade.active_count,
        stockpiles=tuple(
            Stockpile(s.name, s.amount, s.cap, s.per_turn, s.demand, s.imported) for s in stockpiles
        ),
        luxuries=tuple(sorted(luxuries.items())),
        has_pantheon=pantheon.has_pantheon,
        pantheon=pantheon.current_belief,
        pantheon_options=tuple(
            Belief(belief.belief_type, belief.name) for belief in pantheon.available_beliefs
        ),
        envoy_tokens=envoys.tokens_available,
        city_states=tuple(
            CityState(
                player_id=cs.player_id,
                name=cs.name,
                kind=cs.city_state_type,
                envoys_sent=cs.envoys_sent,
                suzerain_id=None if cs.suzerain_id < 0 else cs.suzerain_id,
                suzerain=None if cs.suzerain_id < 0 else cs.suzerain_name,
                can_send_envoy=cs.can_send_envoy,
            )
            for cs in envoys.city_states
        ),
        science_vp=own.science_vp if own else 0,
        diplomatic_vp=own.diplomatic_vp if own else 0,
        tourism=own.tourism if own else 0,
    )


def _policy_in(slot: lq.PolicySlot) -> str | None:
    return None if slot.current_policy in (None, "NONE") else slot.current_policy


def _visible_cities(civ: lq.CivInfo, tiles: dict[tuple[int, int], bool]) -> tuple[RivalCity, ...]:
    return tuple(
        RivalCity(
            name=city.name,
            x=city.x,
            y=city.y,
            pop=city.population,
            loyalty=city.loyalty,
            loyalty_per_turn=city.loyalty_per_turn,
            has_walls=city.has_walls,
            defense_strength=city.defense_strength,
        )
        for city in civ.visible_cities
        if tiles.get((city.x, city.y), False)
    )


def _known_agendas(civ: lq.CivInfo) -> tuple[str, ...]:
    return tuple(agenda.name for agenda in civ.agendas if agenda.name != "???")


def build_rivals(
    civs: Iterable[lq.CivInfo],
    victory: lq.VictoryProgress,
    visible_tiles: dict[tuple[int, int], bool],
) -> tuple[Rival, ...]:
    progress = {player.player_id: player for player in victory.players}
    rivals = []
    for civ in civs:
        if not civ.has_met:
            continue
        stats = progress.get(civ.player_id)
        religion = victory.religion_majority.get(civ.civ_name, "none")
        rival = Rival(
            player_id=civ.player_id,
            civ=civ.civ_name,
            leader=civ.leader_name,
            at_war=civ.is_at_war,
            diplomatic_state=civ.diplomatic_state,
            relationship_score=civ.relationship_score,
            grievances=civ.grievances,
            access_level=civ.access_level,
            has_delegation=civ.has_delegation,
            has_embassy=civ.has_embassy,
            they_have_delegation=civ.they_have_delegation,
            they_have_embassy=civ.they_have_embassy,
            alliance_type=civ.alliance_type,
            alliance_level=civ.alliance_level,
            available_actions=tuple(civ.available_actions or ()),
            agendas=_known_agendas(civ),
            visible_cities=_visible_cities(civ, visible_tiles),
            military_strength=civ.military_strength,
            holds_capital=victory.capitals_held.get(civ.civ_name, True),
            majority_religion=None if religion == "none" else religion,
        )
        if stats is not None:
            rival = _with_rankings(rival, stats)
        rivals.append(rival)
    return tuple(rivals)


def _with_rankings(rival: Rival, stats: lq.VictoryPlayerProgress) -> Rival:
    from dataclasses import replace

    return replace(
        rival,
        score=stats.score,
        military_strength=stats.military_strength,
        science=stats.science_yield,
        culture=stats.culture_yield,
        techs=stats.techs_researched,
        tourism=stats.tourism,
        science_vp=stats.science_vp,
        diplomatic_vp=stats.diplomatic_vp,
        religion_cities=stats.religion_cities,
    )


def build_production(options: Iterable[lq.ProductionOption]) -> tuple[ProductionChoice, ...]:
    return tuple(
        ProductionChoice(
            category=option.category,
            item=option.item_name,
            cost=option.cost,
            turns=option.turns,
            gold_cost=None if option.gold_cost < 0 else option.gold_cost,
            is_repair=option.is_repair,
            repair_x=option.repair_x,
            repair_y=option.repair_y,
        )
        for option in options
    )


def build_builder_jobs(
    tasks: Iterable[lq.BuilderTask], visible_resources: set[tuple[str, int, int]]
) -> tuple[BuilderJob, ...]:
    jobs = []
    for task in tasks:
        has_resource = task.resource_class in _RESOURCE_CLASSES
        if has_resource and (task.resource, task.x, task.y) not in visible_resources:
            continue
        is_repair = task.improvement == "REPAIR"  # civ6-mcp puts the name in `resource`.
        jobs.append(
            BuilderJob(
                priority=task.priority,
                x=task.x,
                y=task.y,
                improvement=f"IMPROVEMENT_{task.resource}" if is_repair else task.improvement,
                resource=task.resource if has_resource else None,
                resource_class=task.resource_class if has_resource else None,
                city=task.city_name,
                nearest_builder_id=None if task.nearest_builder_id < 0 else task.nearest_builder_id,
                distance=None if task.nearest_builder_id < 0 else task.distance,
                is_repair=is_repair,
            )
        )
    return tuple(jobs)


def _visible_foreign_units(units: Iterable[str] | None) -> tuple[str, ...]:
    # Entries look like "Barbarian WARRIOR" or "Rome SUBMARINE", and the civ can be two words.
    stealth = {name.removeprefix("UNIT_") for name in _stealth_units()}
    return tuple(unit for unit in units or () if unit.rsplit(" ", 1)[-1] not in stealth)


def _stealth_units() -> frozenset[str]:
    from civ6_ai.state.fog import STEALTH_UNITS

    return STEALTH_UNITS


def build_tile(info: lq.TileInfo) -> Tile:
    visible = info.visibility == "visible"
    tile = Tile(
        x=info.x,
        y=info.y,
        visible=visible,
        terrain=info.terrain,
        feature=info.feature,
        resource=info.resource,
        resource_class=info.resource_class,
        hills=info.is_hills,
        river=info.is_river,
        coastal=info.is_coastal,
        own_units=tuple(info.own_units or ()),
    )
    if not visible:
        return tile
    from dataclasses import replace

    return replace(
        tile,
        owner_id=info.owner_id,
        owner=info.owner_name,
        improvement=info.improvement,
        pillaged=info.is_pillaged,
        district=info.district,
        route_type=info.route_type,
        movement_cost=info.movement_cost,
        fresh_water=info.is_fresh_water,
        yields=info.yields,
        foreign_units=_visible_foreign_units(info.units),
    )


def build_exploration(
    strategic: lq.StrategicMapData, candidates: Iterable[lq.SettleCandidate]
) -> Exploration:
    return Exploration(
        fog_edges=tuple(
            FogEdge(edge.city_name, edge.city_x, edge.city_y, tuple(edge.fog_distances))
            for edge in strategic.fog_boundaries
        ),
        unclaimed_resources=tuple(
            KnownResource(item.resource_type, item.resource_class, item.x, item.y)
            for item in strategic.unclaimed_resources
        ),
        settle_sites=tuple(
            SettleSite(
                x=site.x,
                y=site.y,
                food=site.total_food,
                production=site.total_prod,
                water=site.water_type,
                resources=tuple(site.resources),
                defense=site.defense_score,
                luxuries=site.luxury_count,
                strategics=site.strategic_count,
            )
            for site in candidates
        ),
    )


async def _rival_city_visibility(
    game: GameReads, civs: Iterable[lq.CivInfo]
) -> dict[tuple[int, int], bool]:
    """Check each reported city's tile, since `get_diplomacy` only proves it was once revealed."""
    visible = {}
    for civ in civs:
        if civ.has_met:
            for city in civ.visible_cities:
                tiles = await game.get_map_area(city.x, city.y, 0)
                visible[city.x, city.y] = any(t.visibility == "visible" for t in tiles)
    return visible


async def read_threats(game: GameReads) -> tuple[Threat, ...]:
    return build_threats(await game.get_threat_scan(), await game.get_diplomacy())


async def read_cities(
    game: GameReads, map_width: int | None = STANDARD_MAP_WIDTH
) -> tuple[City, ...]:
    cities, _ = await game.get_cities()
    threats = await read_threats(game)
    _, owned, _, _ = await game.get_empire_resources()
    visible = _visible_resources(owned)
    return tuple(build_city(city, threats, visible, map_width) for city in cities)


async def read_units(
    game: GameReads, map_width: int | None = STANDARD_MAP_WIDTH
) -> tuple[Unit, ...]:
    return tuple(build_unit(unit, map_width) for unit in await game.get_units())


async def read_research(game: GameReads) -> Research:
    return build_research(await game.get_tech_civics())


async def read_empire(game: GameReads) -> Empire:
    return build_empire(
        await game.get_game_overview(),
        await game.get_victory_progress(),
        await game.get_policies(),
        await game.get_governors(),
        await game.get_trade_routes(),
        await game.get_empire_resources(),
        await game.get_pantheon_status(),
        await game.get_city_states(),
    )


async def read_rivals(game: GameReads) -> tuple[Rival, ...]:
    civs = await game.get_diplomacy()
    victory = await game.get_victory_progress()
    return build_rivals(civs, victory, await _rival_city_visibility(game, civs))


async def read_snapshot(game: GameReads, map_width: int | None = STANDARD_MAP_WIDTH) -> Snapshot:
    """Read the core views, sharing the calls they have in common."""
    overview = await game.get_game_overview()
    victory = await game.get_victory_progress()
    civs = await game.get_diplomacy()
    resources = await game.get_empire_resources()
    threats = build_threats(await game.get_threat_scan(), civs)
    cities, _ = await game.get_cities()
    visible = _visible_resources(resources[1])
    return Snapshot(
        empire=build_empire(
            overview,
            victory,
            await game.get_policies(),
            await game.get_governors(),
            await game.get_trade_routes(),
            resources,
            await game.get_pantheon_status(),
            await game.get_city_states(),
        ),
        cities=tuple(build_city(city, threats, visible, map_width) for city in cities),
        units=await read_units(game, map_width),
        research=await read_research(game),
        rivals=build_rivals(civs, victory, await _rival_city_visibility(game, civs)),
        threats=threats,
    )


async def read_production(game: GameReads, city_id: int) -> tuple[ProductionChoice, ...]:
    return build_production(await game.list_city_production(city_id))


async def read_builder_jobs(game: GameReads) -> tuple[BuilderJob, ...]:
    tasks, _ = await game.get_builder_tasks()
    _, owned, _, _ = await game.get_empire_resources()
    return build_builder_jobs(tasks, _visible_resources(owned))


async def read_area(game: GameReads, x: int, y: int, radius: int = 2) -> tuple[Tile, ...]:
    return tuple(build_tile(tile) for tile in await game.get_map_area(x, y, radius))


async def read_exploration(game: GameReads) -> Exploration:
    return build_exploration(await game.get_strategic_map(), await game.get_global_settle_scan())
