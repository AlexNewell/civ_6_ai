"""Draw one turn of a threat-map dump as a picture, with x and y labelled.

    python scripts/threat_map_view.py runs/<game>_threatmap.jsonl [--turn N] [--pad 6] [--out f.png]

Each hex shows its threat number (blank is zero). Our cities are blue with their name and sit at
zero threat; our combat units are green dots; hostile units are red; a camp is a black X. The x
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
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if turn is None:
        return rows[-1]
    return next(r for r in rows if r["turn"] == turn)


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


def window(record: dict, pad: int) -> tuple[int, int, int, int]:
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


def draw(record: dict, pad: int) -> Image.Image:
    turn = record["turn"]
    m = rebuild(record)
    x0, x1, y0, y1 = window(record, pad)
    size = 30  # Hex radius in pixels.
    w, h = math.sqrt(3) * size, 1.5 * size
    margin = 46
    width = int(margin + (x1 - x0 + 2) * w + w / 2)
    height = int(margin + (y1 - y0 + 1) * h + size + 40)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default()

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
            (m.threat_at(x, y, turn) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)),
            default=1,
        )
        or 1
    )
    cities = {(c[0], c[1]): c[2] for c in record.get("cities", [])}
    own = {(u[0], u[1]) for u in record.get("units", [])}
    hostile = {(s["x"], s["y"]): s for s in record["sources"] if s["kind"] == "unit"}
    camps = {(s["x"], s["y"]) for s in record["sources"] if s["kind"] == "camp"}
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cx, cy = centre(x, y)
            value = m.threat_at(x, y, turn)
            fill = (150, 190, 245) if (x, y) in cities else colour(value, top)
            d.polygon(hexagon(cx, cy), fill=fill, outline=(190, 190, 190))
            if (x, y) in cities:
                d.text((cx - 18, cy - 12), cities[x, y][:7], fill=(0, 0, 90), font=font)
                d.text((cx - 12, cy + 2), "CITY 0", fill=(0, 0, 90), font=font)
            elif value > 0:
                d.text((cx - 6, cy + 8), f"{value:.0f}", fill=(60, 60, 60), font=font)
            if (x, y) in camps:
                d.line([cx - 11, cy - 14, cx + 11, cy + 6], fill=(0, 0, 0), width=3)
                d.line([cx - 11, cy + 6, cx + 11, cy - 14], fill=(0, 0, 0), width=3)
            if (x, y) in hostile:
                d.ellipse([cx - 7, cy - 14, cx + 7, cy], fill=(200, 0, 0))
                d.text(
                    (cx - 9, cy - 28), hostile[x, y]["unit_type"][5:9], fill=(120, 0, 0), font=font
                )
            if (x, y) in own:
                d.ellipse([cx - 7, cy - 4, cx + 7, cy + 10], fill=(0, 150, 0))
    for x in range(x0, x1 + 1):  # x across the top (rows differ by half a tile; label row y1).
        cx, _ = centre(x, y1)
        d.text((cx - 6, 8), str(x), fill=(0, 0, 0), font=font)
    for y in range(y0, y1 + 1):
        _, cy = centre(x0, y)
        d.text((6, cy - 5), str(y), fill=(0, 0, 0), font=font)
    legend = (
        f"turn {turn}   blue = our city (threat 0)   green dot = our combat unit   "
        "red dot = hostile unit   black X = barbarian camp   number = threat"
    )
    d.text((margin, height - 22), legend, fill=(0, 0, 0), font=font)
    return img


def ascii_map(record: dict, pad: int) -> str:
    m = rebuild(record)
    x0, x1, y0, y1 = window(record, pad)
    turn = record["turn"]
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
                v = m.threat_at(x, y, turn)
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
    args = parser.parse_args()
    record = load(args.dump, args.turn)
    if args.out:
        draw(record, args.pad).save(args.out)
        print(f"wrote {args.out}")
    else:
        print(ascii_map(record, args.pad))


if __name__ == "__main__":
    main()
