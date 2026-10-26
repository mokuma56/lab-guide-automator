"""Add lab-guide style callouts to screenshots, with originals kept for rollback.

Callout style matches the hand-annotated sections: a white oval speech bubble with a red
outline and red label (usually "Click"), a tail pointing at the target, and an optional
red box around the target.

    uv run python3 screenshot_tools/annotate.py apply <specs.json>   # back up originals, draw
    uv run python3 screenshot_tools/annotate.py list                 # annotated screenshots
    uv run python3 screenshot_tools/annotate.py restore <file.png>   # put one original back
    uv run python3 screenshot_tools/annotate.py restore --all        # put every original back

specs.json maps a screenshot file name to a list of callouts:
    {"id-users.png": [{"box": [x1, y1, x2, y2], "target": [x, y], "bubble": [cx, cy], "text": "Click"}]}
Coordinates are in a 2000-pixel-wide frame and scaled to each image's real width.

Drawing always starts from the backed-up original, so re-running apply replaces earlier
callouts instead of stacking new ones on top.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SS_DIR = Path(__file__).resolve().parent.parent / "data" / "screenshots"
ORIGINALS = SS_DIR / "_originals"
FRAME = 2000
RED = (230, 30, 30)
WHITE = (255, 255, 255)


def _font(size: int):
    for path in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw_callouts(im: Image.Image, callouts: list[dict]) -> Image.Image:
    im = im.convert("RGB")
    k = im.width / FRAME
    lw = max(3, round(3 * k))
    d = ImageDraw.Draw(im)
    for c in callouts:
        if c.get("box"):
            d.rectangle([v * k for v in c["box"]], outline=RED, width=lw)
    for c in callouts:
        f = _font(round(30 * k))
        text = c.get("text", "Click")
        tw, th = d.textbbox((0, 0), text, font=f)[2:]
        rx, ry = tw / 2 + 28 * k, th / 2 + 26 * k
        cx, cy = (v * k for v in c["bubble"])
        tx, ty = (v * k for v in c["target"])
        # Tail: a narrow triangle from the bubble edge to the target point
        ang = math.atan2(ty - cy, tx - cx)
        p1 = (cx + rx * math.cos(ang - 0.32), cy + ry * math.sin(ang - 0.32))
        p2 = (cx + rx * math.cos(ang + 0.32), cy + ry * math.sin(ang + 0.32))
        d.polygon([p1, (tx, ty), p2], fill=WHITE)
        d.line([p1, (tx, ty), p2], fill=RED, width=lw, joint="curve")
        # Bubble on top, then open its outline where the tail joins
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=WHITE, outline=RED, width=lw)
        d.arc([cx - rx, cy - ry, cx + rx, cy + ry], start=math.degrees(ang - 0.3),
              end=math.degrees(ang + 0.3), fill=WHITE, width=lw + 2)
        d.text((cx - tw / 2, cy - th / 2 - 2 * k), text, fill=RED, font=f)
    return im


def apply(specs: dict[str, list[dict]]) -> None:
    ORIGINALS.mkdir(exist_ok=True)
    for name, callouts in specs.items():
        src, backup = SS_DIR / name, ORIGINALS / name
        if not backup.exists():
            shutil.copy2(src, backup)        # first time only: never overwrite a kept original
        draw_callouts(Image.open(backup), callouts).save(src)
        print(f"annotated {name}")


def restore(name: str | None) -> None:
    names = [p.name for p in ORIGINALS.glob("*.png")] if name is None else [name]
    for n in names:
        backup = ORIGINALS / n
        if not backup.exists():
            print(f"no original kept for {n}")
            continue
        shutil.copy2(backup, SS_DIR / n)
        backup.unlink()
        print(f"restored {n}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "apply" and len(sys.argv) == 3:
        apply(json.loads(Path(sys.argv[2]).read_text()))
    elif cmd == "list":
        for p in sorted(ORIGINALS.glob("*.png")):
            print(p.name)
    elif cmd == "restore" and len(sys.argv) == 3:
        restore(None if sys.argv[2] == "--all" else sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
