"""Draw one turn of a threat-map dump as a picture, with x and y labelled.

    python scripts/threat_map_view.py runs/<game>_threatmap.jsonl [--turn N] [--pad 6] [--out f.png]

Each hex shows its threat number (blank is zero). Our cities are blue with their name and the
threat on their tile; our combat units are green dots; hostile units are red; a camp is a black X. The x
labels run across the top and the y labels down the left, so any tile can be read off. Odd rows sit
half a tile east, as in the game's coordinates. Terrain is not in the dump, so this is threat
only: lay it beside the turn's screenshot for the ground.

Without `--out` it also prints a text version, for a terminal.
"""

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from civ6_ai.threat_map import Source, ThreatMap, ThreatMapSettings


def load(path: Path, turn: int | None) -> dict:
    """The record for `turn` (the last by default), with the ground learned up to then added as
    `_ground`: {(x, y): code}."""
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    record = rows[-1] if turn is None else next(r for r in rows if r["turn"] == turn)
    ground: dict[tuple[int, int], str] = {}
    for row in rows:
        if row["turn"] > record["turn"]:
            break
        for key, code in row.get("terrain_new", {}).items():
            x, y = key.split(",")
            ground[int(x), int(y)] = code
    record["_ground"] = ground
    return record


def threat_lookup(record: dict):
    """A function (x, y) -> threat for the record. A dump with the full heat list (newer ones) is
    read as it is; an older dump keeps only the hottest tiles, so its sources are spread again."""
    if "terrain_new" in record:
        heat = {(x, y): v for x, y, v in record["heat"]}
        return lambda x, y: heat.get((x, y), 0.0)
    m = rebuild(record)
    return lambda x, y: m.threat_at(x, y, record["turn"])


def layer_lookup(record: dict, name: str):
    """A function (x, y) -> value for a layer of the dump ("cover" or "fog"); 0 if it has none."""
    data = {(x, y): v for x, y, v in record.get(name, [])}
    return lambda x, y: data.get((x, y), 0.0)


GROUND_COLOURS = {
    "w": (172, 207, 236),
    "m": (150, 150, 150),
    "h": (208, 188, 142),
    "f": (176, 208, 152),
    "j": (128, 184, 128),
    "s": (170, 192, 172),
    "g": (236, 241, 226),
}


def ground_colour(code: str | None) -> tuple[int, int, int]:
    if code is None:
        return (246, 246, 246)  # Never read.
    return GROUND_COLOURS.get(code[0], (236, 241, 226))


def rebuild(record: dict) -> ThreatMap:
    """The map as it stood, from the dump's sources, so the picture shows every tile, not just
    the hottest ones the dump keeps."""
    m = ThreatMap(ThreatMapSettings())
    for s in record["sources"]:
        m.sources.append(
            Source(
                s["kind"], s["x"], s["y"], s["strength"], s["first_seen"], s["last_seen"],
                s.get("unit_type", ""), s.get("hostile_owner", 63), s.get("naval", False),
            )
        )  # fmt: skip
    return m


def window(
    record: dict, pad: int, box: tuple[int, int, int, int] | None = None
) -> tuple[int, int, int, int]:
    """The tiles drawn: `box` (x0, x1, y0, y1) if given, else everything on the dump plus `pad`."""
    if box is not None:
        return box
    points = [(s["x"], s["y"]) for s in record["sources"]]
    points += [(c[0], c[1]) for c in record.get("cities", [])]
    points += [(u[0], u[1]) for u in record.get("units", [])]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad


def colour(value: float, top: float) -> tuple[int, int, int]:
    if value <= 0:
        return (245, 245, 240)
    t = min(1.0, value / top)
    return (255, int(235 - 170 * t), int(150 - 130 * t))  # Pale yellow to red.


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _label(d: ImageDraw.ImageDraw, at: tuple[float, float], text: str, font, fill) -> None:
    """Centred text with a pale halo, so it reads over any fill."""
    d.text(at, text, fill=fill, font=font, anchor="mm", stroke_width=2, stroke_fill=(255, 255, 255))


def draw(record: dict, pad: int, box: tuple[int, int, int, int] | None = None) -> Image.Image:
    turn = record["turn"]
    threat_at = threat_lookup(record)
    fog_at = layer_lookup(record, "fog")
    cover_at = layer_lookup(record, "cover")

    def danger_at(x: int, y: int) -> float:
        return threat_at(x, y) + fog_at(x, y)  # Threat plus the mild penalty for unseen ground.

    border_floor = 4.0  # Net danger below this (fog alone is at most 3) draws no border.

    ground = record.get("_ground", {})
    x0, x1, y0, y1 = window(record, pad, box)
    size = 44  # Hex radius in pixels.
    w, h = math.sqrt(3) * size, 1.5 * size
    margin = 60
    width = int(margin + (x1 - x0 + 1) * w + w)
    height = int(margin + (y1 - y0 + 1) * h + size + 135)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    big, mid, small = _font(20), _font(17), _font(14)

    def centre(x: int, y: int) -> tuple[float, float]:
        return (
            margin + (x - x0 + 0.5 + (0.5 if y % 2 else 0)) * w,
            margin + (y1 - y + 0.5) * h + size / 2,  # North up: a larger y is drawn higher.
        )

    def hexagon(cx: float, cy: float) -> list[tuple[float, float]]:
        return [
            (cx + size * math.cos(math.radians(60 * i - 30)),
             cy + size * math.sin(math.radians(60 * i - 30)))
            for i in range(6)
        ]  # fmt: skip

    top = (
        max(
            (danger_at(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)),
            default=1,
        )
        or 1
    )
    cities = {(c[0], c[1]): c[2] for c in record.get("cities", [])}
    own = {(u[0], u[1]): u[2] for u in record.get("units", [])}
    hostile = {(s["x"], s["y"]): s for s in record["sources"] if s["kind"] == "unit"}
    camps = {(s["x"], s["y"]) for s in record["sources"] if s["kind"] == "camp"}
    red, green, blue = (170, 0, 0), (0, 110, 0), (0, 0, 120)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cx, cy = centre(x, y)
            net = max(0.0, danger_at(x, y) - cover_at(x, y))
            fill = (120, 160, 235) if (x, y) in cities else ground_colour(ground.get((x, y)))
            d.polygon(hexagon(cx, cy), fill=fill, outline=(150, 150, 150))
            if (
                net >= border_floor
            ):  # Net danger is a red border, thicker the worse; the fill stays ground.
                width_px = max(2, min(9, int(2 + 7 * net / top)))
                d.line([*hexagon(cx, cy), hexagon(cx, cy)[0]], fill=(215, 40, 30), width=width_px)
            if ground.get((x, y), "g").endswith("r"):  # A river: a blue bar across the hex.
                d.line(
                    [cx - size * 0.6, cy + 6, cx + size * 0.6, cy - 6], fill=(60, 110, 200), width=4
                )
    for y in range(y0, y1 + 1):  # Second pass, so no hex paints over a neighbour's label.
        for x in range(x0, x1 + 1):
            cx, cy = centre(x, y)
            cover = cover_at(x, y)
            if (x, y) in cities:
                _label(d, (cx, cy - 12), cities[x, y][:9], mid, blue)
                _label(d, (cx, cy + 12), f"threat {threat_at(x, y):.0f}", small, blue)
            else:
                if (hot := threat_at(x, y)) >= 0.5:  # Red number: threat, upper right.
                    _label(d, (cx + 20, cy - 12), f"{hot:.0f}", mid, red)
                if (haze := fog_at(x, y)) >= 1.0:  # Grey number: fog, upper left.
                    _label(d, (cx - 20, cy - 12), f"{haze:.0f}", small, (120, 120, 120))
                if cover >= 0.5:  # Green number: our cover, lower left.
                    _label(d, (cx - 20, cy + 20), f"{cover:.0f}", mid, green)
            if (x, y) in camps:
                d.line([cx - 16, cy - 20, cx + 16, cy + 8], fill=(0, 0, 0), width=5)
                d.line([cx - 16, cy + 8, cx + 16, cy - 20], fill=(0, 0, 0), width=5)
                _label(d, (cx, cy + 20), "CAMP", mid, (0, 0, 0))
            if (x, y) in hostile:
                src = hostile[x, y]
                name = src["unit_type"].rsplit("_", 1)[-1].title()
                if (src.get("age") or 0) > 0:  # Remembered, not seen this turn: a hollow ring.
                    d.ellipse([cx - 9, cy - 18, cx + 9, cy], outline=(200, 0, 0), width=3)
                    note = f"{name} {src['strength']:.0f} ({src['age']}t ago)"
                else:
                    d.ellipse([cx - 9, cy - 18, cx + 9, cy], fill=(200, 0, 0))
                    note = f"{name} {src['strength']:.0f}"
                _label(d, (cx, cy - 28), note, small, red)
            if (x, y) in own:
                d.ellipse([cx - 9, cy - 2, cx + 9, cy + 16], fill=(0, 150, 0))
                _label(d, (cx, cy + 30), own[x, y].rsplit("_", 1)[-1].title()[:9], small, green)
    for x in range(x0, x1 + 1):  # x across the top (rows differ by half a tile; label row y1).
        cx, _ = centre(x, y1)
        _label(d, (cx, 24), str(x), big, (0, 0, 0))
    for y in range(y0, y1 + 1):
        _, cy = centre(x0, y)
        _label(d, (24, cy), str(y), big, (0, 0, 0))
    lines = [
        f"Turn {turn}.  Hex colour is the ground: blue water, grey mountain, tan hills, "
        "green woods, blue bar = river, blank = never seen.  "
        "Blue hex = our city (its name, and the threat on its tile).",
        "Red number = threat.  Grey number = fog (unseen ground nearby).  Green number = our cover "
        "(combat strength of our units and cities).  "
        "Red border = threat plus fog left after cover, thicker is worse.",
        "Red dot = hostile unit with its strength (hollow = remembered, not seen now).  "
        "Green dot = our combat unit.  Black X = camp.  North up, x increases to the right.",
    ]
    for i, line in enumerate(lines):
        d.text((margin, height - 100 + i * 26), line, fill=(0, 0, 0), font=mid)
    return img


def ascii_map(record: dict, pad: int, box: tuple[int, int, int, int] | None = None) -> str:
    value_at = threat_lookup(record)
    x0, x1, y0, y1 = window(record, pad, box)
    cities = {(c[0], c[1]) for c in record.get("cities", [])}
    camps = {(s["x"], s["y"]) for s in record["sources"] if s["kind"] == "camp"}
    hostile = {(s["x"], s["y"]) for s in record["sources"] if s["kind"] == "unit"}
    own = {(u[0], u[1]) for u in record.get("units", [])}
    lines = ["     " + "".join(f"{x:>4}" for x in range(x0, x1 + 1))]
    for y in range(y1, y0 - 1, -1):
        cells = []
        for x in range(x0, x1 + 1):
            if (x, y) in cities:
                cells.append("  C ")
            elif (x, y) in camps:
                cells.append("  X ")
            elif (x, y) in hostile:
                cells.append("  h ")
            elif (x, y) in own:
                cells.append("  u ")
            else:
                v = value_at(x, y)
                cells.append(f"{v:>3.0f} " if v > 0 else "  . ")
        lines.append(f"{y:>3}  " + ("  " if y % 2 else "") + "".join(cells))
    lines.append("C city (0)  X camp  h hostile unit  u our unit  number = threat  . = none")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path)
    parser.add_argument("--turn", type=int)
    parser.add_argument("--pad", type=int, default=5, help="tiles of margin around what matters")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--box", help="x0,x1,y0,y1: draw exactly these tiles instead of fitting the dump"
    )
    args = parser.parse_args()
    record = load(args.dump, args.turn)
    box = tuple(int(v) for v in args.box.split(",")) if args.box else None
    if not record.get("cities"):
        print(
            "warning: this dump has no cities or units (written before they were recorded), so "
            "the window fits only the sources. Use --box x0,x1,y0,y1 to draw a wider area."
        )
    if args.out:
        draw(record, args.pad, box).save(args.out)
        print(f"wrote {args.out}")
    else:
        print(ascii_map(record, args.pad, box))


if __name__ == "__main__":
    main()
