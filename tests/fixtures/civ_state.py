"""Synthetic game data built from civ6-mcp's own types, plus a fake `GameState` to read it.

Everything here is invented. Nothing was recorded from a game. Positions use the standard Small
map width of 74, and `ME` is the player under test.
"""

from civ_mcp import lua as lq

ME = 0
ROME = 1
UNMET = 3
BARBARIANS = 63


def city_info(**changes) -> lq.CityInfo:
    base = dict(
        city_id=65536,
        name="Alpha",
        x=10,
        y=10,
        population=5,
        food=12.0,
        production=8.5,
        gold=6.0,
        science=4.0,
        culture=2.0,
        faith=1.0,
        housing=7.0,
        amenities=4,
        turns_to_grow=6,
        food_surplus=3.2,
        food_stored=14.0,
        growth_threshold=30,
        currently_building="BUILDING_MONUMENT",
        production_turns_left=4,
        defense_strength=12,
        garrison_hp=200,
        garrison_max_hp=200,
        wall_hp=0,
        wall_max_hp=0,
        attack_targets=["UNIT_ARCHER@40,40"],  # Omniscient upstream, so never read.
        districts=["DISTRICT_CITY_CENTER@10,10", "DISTRICT_CAMPUS@11,10"],
        pillaged_districts=[],
        pillaged_buildings=[],
        loyalty=100.0,
        loyalty_max=100.0,
        loyalty_per_turn=0.0,
        turns_to_loyalty_flip=0,
        garrison_unit="UNIT_WARRIOR",
        unimproved_resources=["IRON@12,10", "HORSES@8,9"],
        pillaged_improvements=[],
        buildings=["PALACE", "MONUMENT"],
    )
    return lq.CityInfo(**{**base, **changes})


def unit_info(**changes) -> lq.UnitInfo:
    base = dict(
        unit_id=131073,
        unit_index=1,
        name="Warrior",
        unit_type="UNIT_WARRIOR",
        x=10,
        y=11,
        moves_remaining=2.0,
        max_moves=2.0,
        health=100,
        max_health=100,
        combat_strength=8,
    )
    return lq.UnitInfo(**{**base, **changes})


def threat_info(**changes) -> lq.ThreatInfo:
    base = dict(
        unit_type="UNIT_WARRIOR",
        x=12,
        y=11,
        hp=100,
        max_hp=100,
        combat_strength=8,
        ranged_strength=0,
        distance=2,
        owner_id=BARBARIANS,
        owner_name="Barbarians",
    )
    return lq.ThreatInfo(**{**base, **changes})


def civ_info(**changes) -> lq.CivInfo:
    base = dict(
        player_id=ROME,
        civ_name="Rome",
        leader_name="Trajan",
        has_met=True,
        is_at_war=False,
        diplomatic_state="Neutral",
        relationship_score=5,
        grievances=0,
        access_level=1,
        has_delegation=True,
        has_embassy=False,
        they_have_delegation=False,
        they_have_embassy=False,
        available_actions=["PROPOSE_TRADE"],
        military_strength=40,
        num_cities=9,  # Counts cities never seen, so never read.
        defensive_pacts=[4],  # Third-party pacts, so never read.
        visible_cities=[
            lq.VisibleCity("Roma", 20, 10, 6, 100.0, 0.0, True, 18),
            lq.VisibleCity("Fogged", 30, 30, 4, 90.0, 0.0, False, 10),
        ],
        agendas=[
            lq.AgendaInfo("Historical", "Builder of Roads", "Likes roads."),
            lq.AgendaInfo("Random", "???", "Unknown."),
        ],
    )
    return lq.CivInfo(**{**base, **changes})


def unmet_civ() -> lq.CivInfo:
    return civ_info(player_id=UNMET, civ_name="Egypt", leader_name="Cleopatra", has_met=False)


def overview(**changes) -> lq.GameOverview:
    base = dict(
        turn=42,
        player_id=ME,
        civ_name="America",
        leader_name="Teddy Roosevelt",
        gold=120.0,
        gold_per_turn=6.5,
        science_yield=9.0,
        culture_yield=5.0,
        faith=12.0,
        current_research="Bronze Working",
        current_civic="Code of Laws",
        num_cities=2,
        num_units=5,
        score=60,
        diplomatic_favor=3,
        favor_per_turn=1,
        explored_land=80,
        total_land=900,  # Whole map, so never read.
        religions_founded=2,  # Includes unmet civs, so never read.
        total_population=9,
        era_name="Ancient",
        era_score=4,
        era_dark_threshold=8,
        era_golden_threshold=14,
        max_turns=330,
        gold_income=14.0,
        total_maintenance=7.5,
        game_speed="GAMESPEED_QUICK",
    )
    return lq.GameOverview(**{**base, **changes})


def _progress(player_id: int, name: str, **changes) -> lq.VictoryPlayerProgress:
    base = dict(
        player_id=player_id,
        name=name,
        score=50,
        science_vp=0,
        science_vp_needed=50,
        diplomatic_vp=0,
        tourism=0,
        military_strength=30,
        techs_researched=8,
        civics_completed=3,
        religion_cities=0,
        science_yield=7.0,
        culture_yield=4.0,
        gold_yield=99.0,  # Net income, which no UI shows, so never read.
        num_cities=9,
    )
    return lq.VictoryPlayerProgress(**{**base, **changes})


def victory() -> lq.VictoryProgress:
    return lq.VictoryProgress(
        players=[
            _progress(ME, "America", score=60, techs_researched=10),
            _progress(ROME, "Rome", score=55, military_strength=44),
        ],
        capitals_held={"Rome": False},
        religion_majority={"Rome": "Judaism"},
    )


def research_status() -> lq.TechCivicStatus:
    mining = lq.TechOption(
        "Mining", "TECH_MINING", 50, 20, 4, True, "Build a mine", "Mine, Quarry", "", "Ancient"
    )
    writing = lq.TechOption(
        "Writing", "TECH_WRITING", 60, 0, 6, False, "", "Library", "Pottery, Mining", "Ancient"
    )
    return lq.TechCivicStatus(
        current_research="Bronze Working",
        current_research_turns=5,
        current_civic="None",
        current_civic_turns=0,
        available_techs=[mining, writing],
        available_civics=[
            lq.CivicOption("Code of Laws", "CIVIC_CODE_OF_LAWS", 20, 0, 3, False, "", "", "Ancient")
        ],
        completed_tech_count=10,
        completed_civic_count=2,
        locked_techs=[lq.LockedTech("Currency", "TECH_CURRENCY", ["Mining"], "Ancient", False, "")],
        locked_civics=[],
    )


def government() -> lq.GovernmentStatus:
    return lq.GovernmentStatus(
        government_name="Chiefdom",
        government_type="GOVERNMENT_CHIEFDOM",
        slots=[
            lq.PolicySlot(0, "SLOT_MILITARY", "POLICY_DISCIPLINE", "Discipline"),
            lq.PolicySlot(1, "SLOT_ECONOMIC", None, None),
        ],
        available_policies=[
            lq.PolicyInfo("POLICY_GOD_KING", "God King", "+1 faith.", "SLOT_ECONOMIC"),
        ],
    )


def governors() -> lq.GovernorStatus:
    return lq.GovernorStatus(
        points_available=1,
        points_spent=0,
        can_appoint=True,
        appointed=[
            lq.AppointedGovernor("GOVERNOR_THE_EDUCATOR", "Pingala", -1, "Unassigned", False, 3),
        ],
        available_to_appoint=[lq.GovernorInfo("GOVERNOR_THE_BUILDER", "Magnus", "Steward")],
    )


def trade() -> lq.TradeRouteStatus:
    return lq.TradeRouteStatus(capacity=2, active_count=1)


def pantheon() -> lq.PantheonStatus:
    return lq.PantheonStatus(
        has_pantheon=False,
        current_belief=None,
        current_belief_name=None,
        faith_balance=12.0,
        available_beliefs=[lq.BeliefInfo("BELIEF_GOD_OF_THE_FORGE", "God of the Forge", "+25%.")],
    )


def envoys() -> lq.EnvoyStatus:
    return lq.EnvoyStatus(
        tokens_available=1,
        city_states=[
            lq.CityStateInfo(20, "Kabul", "MILITARISTIC", 1, -1, "", True),
            lq.CityStateInfo(21, "Geneva", "SCIENTIFIC", 3, ROME, "Rome", False),
        ],
    )


def resources() -> tuple:
    stockpiles = [lq.ResourceStockpile("Iron", 3, 10, 1, 0, 0)]
    owned = [lq.OwnedResource("IRON", "strategic", False, 12, 10)]  # HORSES@8,9 is not here.
    return stockpiles, owned, [], {"Silk": 1}


def tile(**changes) -> lq.TileInfo:
    base = dict(
        x=20,
        y=10,
        terrain="TERRAIN_GRASS",
        feature=None,
        resource="IRON",
        is_hills=False,
        is_river=True,
        is_coastal=False,
        improvement="IMPROVEMENT_MINE",
        owner_id=ROME,
        visibility="visible",
        is_fresh_water=True,
        yields=(1, 2, 0, 0, 0, 0),
        units=["Rome WARRIOR", "Rome SUBMARINE"],
        resource_class="strategic",
        district="DISTRICT_CAMPUS",
        owner_name="Rome",
        route_type=1,
        movement_cost=1,
    )
    return lq.TileInfo(**{**base, **changes})


class FakeGame:
    """Implements the `GameReads` protocol from canned data, and records the calls made."""

    def __init__(self, **overrides):
        self.calls: list[str] = []
        self.data = {
            "overview": overview(),
            "cities": [city_info(), city_info(city_id=65537, name="Beta", x=30, y=20)],
            "units": [unit_info()],
            "research": research_status(),
            "threats": [threat_info()],
            "civs": [civ_info(), unmet_civ()],
            "victory": victory(),
            "tiles": {(20, 10): [tile()]},
            "production": [lq.ProductionOption("BUILDING", "Library", 90, 9, 180)],
            "builder_tasks": [],
            "strategic": lq.StrategicMapData([], []),
            "settle": [],
            **overrides,
        }

    async def get_game_overview(self):
        self.calls.append("get_game_overview")
        return self.data["overview"]

    async def get_cities(self):
        self.calls.append("get_cities")
        return self.data["cities"], []

    async def get_units(self):
        self.calls.append("get_units")
        return self.data["units"]

    async def get_tech_civics(self):
        self.calls.append("get_tech_civics")
        return self.data["research"]

    async def get_empire_resources(self):
        self.calls.append("get_empire_resources")
        return resources()

    async def get_threat_scan(self):
        self.calls.append("get_threat_scan")
        return self.data["threats"]

    async def get_diplomacy(self):
        self.calls.append("get_diplomacy")
        return self.data["civs"]

    async def get_victory_progress(self):
        self.calls.append("get_victory_progress")
        return self.data["victory"]

    async def get_policies(self):
        self.calls.append("get_policies")
        return government()

    async def get_governors(self):
        self.calls.append("get_governors")
        return governors()

    async def get_trade_routes(self):
        self.calls.append("get_trade_routes")
        return trade()

    async def get_pantheon_status(self):
        self.calls.append("get_pantheon_status")
        return pantheon()

    async def get_city_states(self):
        self.calls.append("get_city_states")
        return envoys()

    async def list_city_production(self, city_id):
        self.calls.append("list_city_production")
        return self.data["production"]

    async def get_builder_tasks(self):
        self.calls.append("get_builder_tasks")
        return self.data["builder_tasks"], []

    async def get_map_area(self, center_x, center_y, radius=2):
        self.calls.append("get_map_area")
        return self.data["tiles"].get((center_x, center_y), [])

    async def get_strategic_map(self):
        self.calls.append("get_strategic_map")
        return self.data["strategic"]

    async def get_global_settle_scan(self):
        self.calls.append("get_global_settle_scan")
        return self.data["settle"]
