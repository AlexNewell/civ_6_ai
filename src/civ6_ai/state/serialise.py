"""Compact city JSON for LLM prompts, with a hard size limit.

The Cheap tier in `docs/decisions.md` assumes 1,500 input tokens a call. A city gets 400, which
leaves about 600 for instructions and the reply format, 300 for an empire summary and 200 for
the choices on offer.

Tokens are estimated offline as UTF-8 bytes / 3, rounded up. The usual 4 characters a token
suits English prose. This JSON is mostly digits and punctuation, which tokenise worse, so 3 is
the cautious figure. The budget governor still needs a measured count before a real call.

Keys are short and fixed, so a prompt should explain them once:

- `y`: yields per turn, in `YIELD_ORDER`
- `grow`: food surplus, turns to grow
- `house`, `amen`: housing, and `City.amenities_total`
- `build`: item, turns left (absent when idle)
- `def`: defence strength, garrison hp, garrison max, wall hp, wall max
- `loy`: loyalty, loyalty per turn, turns to flip (absent at full, stable loyalty)
- `gar`: garrison unit
- `dist`, `bld`: districts and buildings
- `pil`: anything pillaged
- `res`: unimproved resources
- `thr`: threats as `type@x,y:hp`
- `cut`: lists removed to fit the limit, lowest value first
"""

import json
import math

from civ6_ai.state.models import City

CITY_TOKEN_LIMIT = 400
BYTES_PER_TOKEN = 3
MAX_NAME_LENGTH = 32
YIELD_ORDER = ("food", "production", "gold", "science", "culture", "faith")
_CUT_ORDER = ("bld", "res", "dist", "pil", "thr")
_PREFIXES = ("BUILDING_", "DISTRICT_", "IMPROVEMENT_", "PROJECT_", "UNIT_")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN)


def _short(item: str) -> str:
    return item.removeprefix(next((p for p in _PREFIXES if item.startswith(p)), ""))


def _number(value: float) -> int | float:
    rounded = round(value, 1)
    return int(rounded) if rounded == int(rounded) else rounded


def city_payload(city: City) -> dict:
    """Every field worth sending, with empty ones left out."""
    payload = {
        "id": city.city_id,
        "name": city.name[:MAX_NAME_LENGTH],
        "xy": [city.x, city.y],
        "pop": city.pop,
        "y": [_number(getattr(city, name)) for name in YIELD_ORDER],
        "grow": [_number(city.food_surplus), city.turns_to_grow],
        "house": _number(city.housing),
        "amen": city.amenities_total,
        "def": [
            city.defense_strength,
            city.garrison_hp,
            city.garrison_max_hp,
            city.wall_hp,
            city.wall_max_hp,
        ],
    }
    if city.is_capital:
        payload["cap"] = 1
    if city.building is not None:
        payload["build"] = [_short(city.building), city.build_turns_left]
    if city.loyalty < city.loyalty_max or city.loyalty_per_turn != 0:
        payload["loy"] = [
            _number(city.loyalty),
            _number(city.loyalty_per_turn),
            city.turns_to_flip,
        ]
    if city.garrison_unit:
        payload["gar"] = _short(city.garrison_unit)
    pillaged = [
        *(_short(d.district_type) for d in city.districts if d.pillaged),
        *city.pillaged_buildings,
        *(f"{i.name}@{i.x},{i.y}" for i in city.pillaged_improvements),
    ]
    lists = {
        "dist": [_short(d.district_type) for d in city.districts],
        "bld": list(city.buildings),
        "pil": pillaged,
        "res": [f"{r.name}@{r.x},{r.y}" for r in city.unimproved_resources],
        "thr": [f"{_short(t.unit_type)}@{t.x},{t.y}:{t.hp}" for t in city.threats],
    }
    payload.update({key: value for key, value in lists.items() if value})
    return payload


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def serialise_city(city: City, limit: int = CITY_TOKEN_LIMIT) -> str:
    """Return JSON within `limit` estimated tokens, removing whole lists if it must."""
    payload = city_payload(city)
    text = _dumps(payload)
    cut = []
    for key in _CUT_ORDER:
        if estimate_tokens(text) <= limit:
            break
        if key in payload:
            del payload[key]
            cut.append(key)
            text = _dumps({**payload, "cut": cut})
    return text
