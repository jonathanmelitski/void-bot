"""Generates the void_throw_<minutes> reaction emotes: a frisbee with the number of minutes to its right.

    pipenv run pip install pillow
    pipenv run python scripts/make_emotes.py     # writes emotes/void_throw_5.png ... void_throw_180.png

Upload the PNGs under Server Settings -> Emoji; Discord names each emoji after its file name.
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Every 5 minutes up to an hour, every 10 up to two hours, every 15 up to three. The bot rounds every
# session to one of these; keep in step with LOGGED_MINUTES in bot/cogs/throwing.py.
MINUTES = [*range(5, 61, 5), *range(70, 121, 10), *range(135, 181, 15)]

SIZE = 128  # Discord's recommended emoji size
SCALE = 4  # draw big, then shrink, for smooth edges
DISC = "#f2f3f5"
RINGS = "#b5bac1"
TEXT = "#ffffff"
TILT = 18  # degrees the disc is tipped up
OUTLINE = "#1e1f22"  # dark edge keeps the disc and number readable on light and dark themes

FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont:
    for candidate in [path] if path else FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    if path:
        raise SystemExit(f"Couldn't load font {path}")
    return ImageFont.load_default(size)


def draw_disc(d: int, stroke: int) -> Image.Image:
    """A disc in flight, on a d x d transparent square: seen from the side and tilted nose-up."""
    layer = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    w, h, rim = d - 1, int(d * 0.36), int(d * 0.13)  # ellipse width and height, and how deep the rim is
    top = (d - h - rim) // 2
    mid = top + h // 2

    # The rim: the same ellipse dropped down, joined to the top by straight sides.
    draw.ellipse((0, top + rim, w, top + rim + h), fill=RINGS, outline=OUTLINE, width=stroke)
    draw.rectangle((stroke, mid, w - stroke, mid + rim), fill=RINGS)
    for side in (0, w - stroke + 1):
        draw.rectangle((side, mid, side + stroke - 1, mid + rim), fill=OUTLINE)
    # The top, with a ring on it.
    draw.ellipse((0, top, w, top + h), fill=DISC, outline=OUTLINE, width=stroke)
    ix, iy = int(w * 0.24), int(h * 0.26)
    draw.ellipse((ix, top + iy, w - ix, top + h - iy), outline=RINGS, width=max(stroke // 2, 1))
    return layer.rotate(TILT, resample=Image.BICUBIC)


def draw_emote(minutes: int, font_path: str | None) -> Image.Image:
    s = SIZE * SCALE
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = s // 28

    # The frisbee, on the left.
    d = int(s * 0.42)
    x = 0
    image.alpha_composite(draw_disc(d, stroke), (x, (s - d) // 2))

    # The number: as big as fits in the space to the right of the disc.
    text = str(minutes)
    left, right = x + d + stroke // 2, s
    size = int(s * 0.62)
    while True:
        font = load_font(font_path, size)
        edge = size // 12  # thinner outline on smaller digits, so they don't fill in
        box = draw.textbbox((0, 0), text, font=font, stroke_width=edge)
        if box[2] - box[0] <= right - left or size <= 8:
            break
        size -= 4
    draw.text(
        ((left + right) / 2, s / 2), text, font=font, anchor="mm",
        fill=TEXT, stroke_width=edge, stroke_fill=OUTLINE,
    )
    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("emotes"), help="output folder (default: emotes)")
    parser.add_argument("--font", help="path to a .ttf/.otf font for the number")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for minutes in MINUTES:
        draw_emote(minutes, args.font).save(args.out / f"void_throw_{minutes}.png", optimize=True)
    print(f"Wrote {len(MINUTES)} emotes to {args.out}/: {', '.join(map(str, MINUTES))}")


if __name__ == "__main__":
    main()
