"""Typed views of the game, holding only what the player can legally see.

Views are immutable and carry no reference to the game, so a policy can be tested from a
fixture. Everything here comes from `reader.py`, and `fog.py` records why each read is allowed.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """An enemy a unit can attack now."""

    unit_type: str
    x: int
    y: int
    hp: int


@dataclass(frozen=True)
class Threat:
    """A foreign military unit on a tile the player can see."""

    owner_id: int
    owner: str
    unit_type: str
    x: int
    y: int
    hp: int
    max_hp: int
    combat_strength: int
    ranged_strength: int
    distance: int  # To the nearest of our cities and units.
    hostile: bool  # Barbarian, or an owner we are at war with. False means at peace.
    is_city_state: bool = False


@dataclass(frozen=True)
class Unit:
    unit_id: int  # Composite id, used by promotions and upgrades.
    unit_index: int  # Used by move and attack commands.
    unit_type: str
    name: str
    x: int
    y: int
    moves: float
    max_moves: float
    hp: int
    max_hp: int
    combat_strength: int = 0  # The base value, before health and promotions.
    ranged_strength: int = 0
    charges: int = 0  # Builder, great person or religious unit charges.
    targets: tuple[Target, ...] = ()
    can_upgrade: bool = False
    upgrade_to: str | None = None
    upgrade_cost: int = 0
    valid_improvements: tuple[str, ...] = ()  # Only filled for a builder or engineer with moves.
    religion: str | None = None

    @property
    def is_combat(self) -> bool:
        return self.combat_strength > 0 or self.ranged_strength > 0

    @property
    def is_hurt(self) -> bool:
        return self.hp < self.max_hp

    @property
    def can_move(self) -> bool:
        return self.moves > 0


@dataclass(frozen=True)
class District:
    district_type: str
    x: int
    y: int
    pillaged: bool = False


@dataclass(frozen=True)
class TileItem:
    """A resource or improvement on a tile, with its name unprefixed, such as `IRON`."""

    name: str
    x: int
    y: int


@dataclass(frozen=True)
class City:
    city_id: int
    name: str
    x: int
    y: int
    pop: int
    food: float
    production: float
    gold: float
    science: float
    culture: float
    faith: float
    food_surplus: float
    food_stored: float
    growth_threshold: int
    turns_to_grow: int
    housing: float
    amenities_total: int  # Provided plus needed, not net. See `fog.py`.
    building: str | None  # None means the city is idle.
    build_turns_left: int
    defense_strength: int
    garrison_hp: int
    garrison_max_hp: int
    wall_hp: int
    wall_max_hp: int
    loyalty: float
    loyalty_max: float
    loyalty_per_turn: float
    turns_to_flip: int
    is_capital: bool = False
    garrison_unit: str | None = None
    districts: tuple[District, ...] = ()
    buildings: tuple[str, ...] = ()
    pillaged_buildings: tuple[str, ...] = ()
    pillaged_improvements: tuple[TileItem, ...] = ()
    unimproved_resources: tuple[TileItem, ...] = ()
    threats: tuple[Threat, ...] = ()  # Hostile, in range of the city, nearest first.

    @property
    def is_idle(self) -> bool:
        return self.building is None

    @property
    def is_threatened(self) -> bool:
        return bool(self.threats)

    @property
    def has_walls(self) -> bool:
        return self.wall_max_hp > 0


@dataclass(frozen=True)
class ResearchOption:
    """A tech or civic that can be started now."""

    item_type: str  # `TECH_MINING` or `CIVIC_CODE_OF_LAWS`.
    name: str
    cost: int
    progress_pct: int
    turns: int
    boosted: bool
    boost_desc: str
    prereqs: tuple[str, ...]
    era: str
    unlocks: tuple[str, ...] = ()  # Techs only.


@dataclass(frozen=True)
class LockedResearch:
    """A tech or civic within two eras that still needs prerequisites."""

    item_type: str
    name: str
    missing: tuple[str, ...]  # Localised names.
    era: str
    boosted: bool
    boost_desc: str


@dataclass(frozen=True)
class Research:
    current_tech: str | None  # Localised name. None means nothing is being researched.
    current_tech_turns: int | None
    current_civic: str | None
    current_civic_turns: int | None
    techs: tuple[ResearchOption, ...]
    civics: tuple[ResearchOption, ...]
    techs_done: int
    civics_done: int
    locked_techs: tuple[LockedResearch, ...] = ()
    locked_civics: tuple[LockedResearch, ...] = ()


@dataclass(frozen=True)
class Stockpile:
    name: str
    amount: int
    cap: int
    per_turn: int
    demand: int
    imported: int


@dataclass(frozen=True)
class PolicySlot:
    index: int
    slot_type: str
    policy: str | None  # None means empty.


@dataclass(frozen=True)
class Policy:
    policy_type: str
    name: str
    slot_type: str


@dataclass(frozen=True)
class Governor:
    governor_type: str
    city_id: int | None
    established: bool
    turns_to_establish: int
    promotions: tuple[str, ...] = ()  # Not yet taken.


@dataclass(frozen=True)
class Belief:
    belief_type: str
    name: str


@dataclass(frozen=True)
class CityState:
    """A city-state the player has met."""

    player_id: int
    name: str
    kind: str
    envoys_sent: int
    suzerain_id: int | None
    suzerain: str | None
    can_send_envoy: bool


@dataclass(frozen=True)
class Empire:
    turn: int
    player_id: int
    civ: str
    leader: str
    score: int
    gold: float
    gold_per_turn: float
    gold_income: float
    maintenance: float
    science: float
    culture: float
    faith: float
    favor: int
    favor_per_turn: int  # civ6-mcp's estimate: government, alliances and suzerainties only.
    num_cities: int
    num_units: int
    population: int
    explored_land: int
    era: str
    era_score: int
    dark_threshold: int
    golden_threshold: int
    game_speed: str
    max_turns: int
    government: str
    policy_slots: tuple[PolicySlot, ...]
    unlocked_policies: tuple[Policy, ...]
    governor_points: int
    can_appoint_governor: bool
    governors: tuple[Governor, ...]
    appointable_governors: tuple[str, ...]
    trade_capacity: int
    trade_active: int
    stockpiles: tuple[Stockpile, ...]
    luxuries: tuple[tuple[str, int], ...]
    has_pantheon: bool
    pantheon: str | None
    pantheon_options: tuple[Belief, ...]
    envoy_tokens: int
    city_states: tuple[CityState, ...]
    science_vp: int = 0
    diplomatic_vp: int = 0
    tourism: int = 0

    @property
    def empty_policy_slots(self) -> tuple[PolicySlot, ...]:
        return tuple(slot for slot in self.policy_slots if slot.policy is None)

    @property
    def free_trade_routes(self) -> int:
        return max(0, self.trade_capacity - self.trade_active)


@dataclass(frozen=True)
class RivalCity:
    """A rival city on a tile the player can see now."""

    name: str
    x: int
    y: int
    pop: int
    loyalty: float
    loyalty_per_turn: float
    has_walls: bool
    defense_strength: int


@dataclass(frozen=True)
class Rival:
    """A major civ the player has met. Unmet civs never appear."""

    player_id: int
    civ: str
    leader: str
    at_war: bool
    diplomatic_state: str
    relationship_score: int
    grievances: int
    access_level: int
    has_delegation: bool
    has_embassy: bool
    they_have_delegation: bool
    they_have_embassy: bool
    alliance_type: str | None = None
    alliance_level: int = 0
    available_actions: tuple[str, ...] = ()
    agendas: tuple[str, ...] = ()  # Only those the player's access level reveals.
    visible_cities: tuple[RivalCity, ...] = ()
    score: int = 0
    military_strength: int = 0
    science: float = 0.0
    culture: float = 0.0
    techs: int = 0
    tourism: int = 0
    science_vp: int = 0
    diplomatic_vp: int = 0
    religion_cities: int = 0
    holds_capital: bool = True
    majority_religion: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """One read of the core views."""

    empire: Empire
    cities: tuple[City, ...]
    units: tuple[Unit, ...]
    research: Research
    rivals: tuple[Rival, ...]
    threats: tuple[Threat, ...]


@dataclass(frozen=True)
class ProductionChoice:
    category: str  # UNIT, BUILDING, DISTRICT or PROJECT.
    item: str
    cost: int
    turns: int
    gold_cost: int | None = None  # None means it can't be bought.
    is_repair: bool = False
    repair_x: int | None = None
    repair_y: int | None = None


@dataclass(frozen=True)
class BuilderJob:
    priority: str  # urgent, high or normal.
    x: int
    y: int
    improvement: str
    resource: str | None
    resource_class: str | None
    city: str
    nearest_builder_id: int | None
    distance: int | None
    is_repair: bool = False


@dataclass(frozen=True)
class Tile:
    """A known tile. Fields a fogged tile can't legally show are None."""

    x: int
    y: int
    visible: bool  # False means revealed earlier but fogged now.
    terrain: str
    feature: str | None
    resource: str | None
    resource_class: str | None
    hills: bool
    river: bool
    coastal: bool
    owner_id: int | None = None
    owner: str | None = None
    improvement: str | None = None
    pillaged: bool | None = None
    district: str | None = None
    route_type: int | None = None
    movement_cost: int | None = None
    fresh_water: bool | None = None
    yields: tuple[int, ...] | None = None  # Food, production, gold, science, culture, faith.
    foreign_units: tuple[str, ...] = ()
    own_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class FogEdge:
    """How far the revealed map reaches from a city, along six rays."""

    city: str
    x: int
    y: int
    distances: tuple[int, ...]  # -1 means revealed for all of the 15 tiles scanned.


@dataclass(frozen=True)
class KnownResource:
    """A luxury or strategic resource on revealed land that looks unclaimed."""

    resource: str
    resource_class: str
    x: int
    y: int


@dataclass(frozen=True)
class SettleSite:
    x: int
    y: int
    food: int
    production: int
    water: str  # fresh, coast or none.
    resources: tuple[str, ...]
    defense: int
    luxuries: int
    strategics: int


@dataclass(frozen=True)
class Exploration:
    fog_edges: tuple[FogEdge, ...]
    unclaimed_resources: tuple[KnownResource, ...]
    settle_sites: tuple[SettleSite, ...]
