"""What each civ6-mcp read reveals, and what the state layer does about it.

civ6-mcp's Lua has full access to the game, and its filters are not always the ones the player's
own screen applies. `READ_AUDIT` covers every read this layer uses or refuses. It comes from
reading that Lua at the pinned commit and comparing it with the game's own UI scripts.
"""

from dataclasses import dataclass
from enum import StrEnum

BARBARIAN_ID = 63

# Units with the game's CLASS_STEALTH tag, which stay hidden from anything over 1 hex away.
STEALTH_UNITS = frozenset(
    {
        "UNIT_ENGLISH_SEADOG",
        "UNIT_GERMAN_UBOAT",
        "UNIT_NUCLEAR_SUBMARINE",
        "UNIT_OTTOMAN_BARBARY_CORSAIR",
        "UNIT_PRIVATEER",
        "UNIT_SUBMARINE",
    }
)
STEALTH_REVEAL_DISTANCE = 1


def is_hidden_stealth_unit(unit_type: str, distance: int) -> bool:
    """Tile visibility ignores stealth, so civ6-mcp reports these units from afar."""
    return unit_type in STEALTH_UNITS and distance > STEALTH_REVEAL_DISTANCE


class Verdict(StrEnum):
    LEGAL = "legal"  # The player can see all of it.
    FILTERED = "filtered"  # Omniscient parts are removed here.
    FLAGGED = "flagged"  # Used, with a small leak that can't be removed. See the note.
    BANNED = "banned"  # Never called from this layer.


@dataclass(frozen=True)
class ReadAudit:
    verdict: Verdict
    note: str
    dropped: tuple[str, ...] = ()  # civ6-mcp fields that must never reach a view.


READ_AUDIT = {
    "get_game_overview": ReadAudit(
        Verdict.FILTERED,
        "Own empire, plus scores of met civs only. religions_founded includes unmet civs and "
        "total_land counts the whole map.",
        ("religions_founded", "religions_max", "total_land"),
    ),
    "get_cities": ReadAudit(
        Verdict.FILTERED,
        "Own cities. attack_targets has no fog, war or line-of-sight check, so visible threats "
        "replace it. unimproved_resources ignores resource visibility, so it is checked against "
        "get_empire_resources. amenities is GetAmenities + GetAmenitiesNeeded, but the game's UI "
        "treats GetAmenities as the amount provided, so it is not a net figure.",
        ("attack_targets",),
    ),
    "get_units": ReadAudit(
        Verdict.FILTERED,
        "Own units. Targets have no fog check, but are at-war units in melee reach or passing "
        "the game's own ranged-attack check. Distant stealth units are removed. needs_promotion "
        "is always false upstream, because promotions arrive as end_turn blockers.",
        ("needs_promotion",),
    ),
    "get_tech_civics": ReadAudit(Verdict.LEGAL, "Own research and game rules."),
    "get_empire_resources": ReadAudit(
        Verdict.LEGAL,
        "Uses IsResourceVisible, so it is the source of truth for hidden resources. Its nearby "
        "list is not used.",
    ),
    "get_threat_scan": ReadAudit(
        Verdict.FILTERED,
        "Checks tile visibility, which ignores stealth, so stealth units are removed unless "
        "adjacent to a friendly position.",
    ),
    "get_diplomacy": ReadAudit(
        Verdict.FILTERED,
        "Unmet civs are dropped, since each entry shows a live player. num_cities counts cities "
        "never seen. defensive_pacts holds third-party pacts the UI never shows. visible_cities "
        "only tests IsRevealed but returns live values, so each one is confirmed on a tile that "
        "is visible now. A city in the fog is left out until it is seen again.",
        ("num_cities", "defensive_pacts"),
    ),
    "get_victory_progress": ReadAudit(
        Verdict.FILTERED,
        "Met civs only. Kept fields match the diplomacy ribbon and World Rankings. gold_yield is "
        "net income, which neither shows, and demographics aggregates unmet civs. "
        "military_strength includes treasury, unlike the UI's figure, but rival gold is public.",
        (
            "gold_yield",
            "num_cities",
            "civics_completed",
            "demographics",
            "religions_founded",
            "religions_max",
        ),
    ),
    "get_policies": ReadAudit(Verdict.LEGAL, "Own government."),
    "get_governors": ReadAudit(Verdict.LEGAL, "Own governors."),
    "get_trade_routes": ReadAudit(Verdict.LEGAL, "Own routes. Only the counts are used."),
    "get_pantheon_status": ReadAudit(
        Verdict.LEGAL, "Options leave out pantheons taken by any civ, met or not."
    ),
    "get_trade_destinations": ReadAudit(
        Verdict.FLAGGED,
        "Lists cities the game accepts a route to. If it accepts none, civ6-mcp falls back to "
        "every living civ's cities, met or not, and the parser drops the warning that says so. "
        "Foreign destinations are therefore kept only for met civs and city-states, and "
        "religion_in, pressure_in and dest_yields of a foreign city are never used.",
        ("religion_in", "pressure_in"),
    ),
    "get_religion_founding_status": ReadAudit(
        Verdict.LEGAL,
        "Own religion. Religions and pantheon beliefs taken by any civ are left out of the "
        "options, as the game's own founding screen does.",
    ),
    "get_great_people": ReadAudit(
        Verdict.LEGAL,
        "The game's Great People screen: who has claimed each person, and our own points. "
        "Rival points are not read.",
    ),
    "get_gp_advisor": ReadAudit(Verdict.LEGAL, "Own unit and own cities."),
    "get_city_states": ReadAudit(Verdict.LEGAL, "Met city-states only."),
    "list_city_production": ReadAudit(Verdict.LEGAL, "Own city."),
    "get_builder_tasks": ReadAudit(
        Verdict.FILTERED,
        "Checks that the improvement is unlocked, not that the resource is visible, so a mine "
        "can reveal iron before Bronze Working. Jobs on unconfirmed resources are dropped.",
    ),
    "get_map_area": ReadAudit(
        Verdict.FILTERED,
        "Only reports revealed tiles, but a fogged tile returns live values, not what the player "
        "last saw. Owner, improvement, district and route are blanked on fogged tiles. Feature "
        "and resource stay, so a change in the fog can still show. Stealth units are removed, "
        "because their distance from the player isn't known here.",
        ("owner_id", "owner_name", "improvement", "district", "route_type", "movement_cost"),
    ),
    "get_strategic_map": ReadAudit(
        Verdict.FLAGGED,
        "Fog edges are legal. Unclaimed resources use the live owner, so one claimed in the fog "
        "silently drops off the list.",
    ),
    "get_global_settle_scan": ReadAudit(
        Verdict.FLAGGED,
        "Scoring reads every player's cities, so score and loyalty_pressure are dropped. Sites "
        "near unseen cities are still missing, and the top ten were ranked with the hidden "
        "score, so the list itself carries a small leak.",
        ("score", "loyalty_pressure"),
    ),
    "get_rival_snapshot": ReadAudit(
        Verdict.BANNED, "Any met rival's strategic stockpiles, population and net income."
    ),
    "get_diary_snapshot": ReadAudit(
        Verdict.BANNED, "Every player's full state, met or not. For research logging only."
    ),
    "execute_lua": ReadAudit(Verdict.BANNED, "Raw game access bypasses every rule here."),
}
