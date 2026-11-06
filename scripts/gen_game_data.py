"""Rebuild src/civ6_ai/policies/game_data.py from an installed game's gameplay data.

Usage: python scripts/gen_game_data.py <game install folder>

Reads only. Takes the Base, Expansion1 and Expansion2 data plus the civ and leader packs that
add unique units, and skips scenario, mode and hero files because those units aren't in a
standard game.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SKIP = ("scenario", "_mode", "heroes", "text", "localiz", "removedata", "secretsocieties")
OUT = Path(__file__).parent.parent / "src" / "civ6_ai" / "policies" / "game_data.py"
ROW_ATTRS = (
    "Cost",
    "Combat",
    "RangedCombat",
    "Bombard",
    "Domain",
    "FormationClass",
    "PromotionClass",
)


def data_files(root: Path):
    for path in sorted(
        [*root.glob("Base/Assets/Gameplay/Data/*.xml"), *root.glob("DLC/*/Data/*.xml")]
    ):
        lowered = str(path.relative_to(root)).lower()
        if any(word in lowered for word in SKIP):
            continue
        if "dlc" in lowered and "scenario" in lowered:
            continue
        yield path


def main(root: Path) -> None:
    units: dict[str, dict[str, str]] = {}
    replaces: dict[str, str] = {}
    upgrades: dict[str, str] = {}
    wonders: set[str] = set()
    for path in data_files(root):
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            continue
        for row in tree.iterfind(".//Units/Row"):
            units.setdefault(row.get("UnitType"), {}).update(
                {k: row.get(k) for k in ROW_ATTRS if row.get(k) is not None}
            )
        for row in tree.iterfind(".//Buildings/Row"):
            if row.get("IsWonder") == "true":
                wonders.add(row.get("BuildingType"))
        for row in tree.iterfind(".//UnitReplaces/Row"):
            replaces[row.get("CivUniqueUnitType")] = row.get("ReplacesUnitType")
        for row in tree.iterfind(".//UnitUpgrades/Row"):
            upgrades[row.get("Unit")] = row.get("UpgradeUnit")
    lines = [
        '"""Land combat units and wonders in the standard game, from scripts/gen_game_data.py.',
        "",
        "Do not edit by hand. Each entry is (melee strength, ranged strength, bombard strength,",
        "cost, promotion class). `REPLACES` maps a unique unit to the unit it replaces and",
        "`UPGRADES` maps a unit to the one it upgrades to.",
        '"""',
        "",
        "COMBAT_UNITS: dict[str, tuple[int, int, int, int, str]] = {",
    ]
    for name in sorted(units):
        u = units[name]
        if (
            u.get("Domain") != "DOMAIN_LAND"
            or u.get("FormationClass") == "FORMATION_CLASS_CIVILIAN"
        ):
            continue
        if not (int(u.get("Combat", 0)) or int(u.get("RangedCombat", 0))):
            continue
        promo = u.get("PromotionClass", "").removeprefix("PROMOTION_CLASS_")
        entry = (
            int(u.get("Combat", 0)),
            int(u.get("RangedCombat", 0)),
            int(u.get("Bombard", 0)),
            int(u.get("Cost", 0)),
            promo,
        )
        lines.append(f'    "{name}": {entry!r},'.replace("'", '"'))
    lines += ["}", "", "REPLACES: dict[str, str] = {"]
    lines += [f'    "{k}": "{v}",' for k, v in sorted(replaces.items()) if k in units]
    lines += ["}", "", "UPGRADES: dict[str, str] = {"]
    lines += [f'    "{k}": "{v}",' for k, v in sorted(upgrades.items()) if k in units]
    lines += ["}", "", "WONDERS: frozenset[str] = frozenset({"]
    lines += [f'    "{w}",' for w in sorted(wonders)]
    lines += ["})", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
