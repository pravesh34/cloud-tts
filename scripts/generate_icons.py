#!/usr/bin/env python3
"""Generate PWA icons from a simple vector-ish design (no external assets)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "web" / "icons"
BG = (11, 10, 9, 255)
EMBER = (255, 138, 76, 255)
EMBER_LIGHT = (255, 179, 122, 255)
MINT = (143, 227, 192, 255)
SCALE = 4


def draw_icon(size: int, padding: float = 0.0) -> Image.Image:
    canvas = size * SCALE
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    inset = int(canvas * padding)
    radius = int((canvas - 2 * inset) * 0.22)
    d.rounded_rectangle(
        [inset, inset, canvas - inset, canvas - inset],
        radius=radius,
        fill=BG,
        outline=(41, 36, 31, 255),
        width=max(1, int(canvas * 0.008)),
    )

    # Five waveform bars, warm gradient centre, mint accent on the outside.
    centre = canvas / 2
    heights = [0.22, 0.40, 0.56, 0.40, 0.22]
    colors = [MINT, EMBER_LIGHT, EMBER, EMBER_LIGHT, MINT]
    bar_w = canvas * 0.055
    gap = canvas * 0.040
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = centre - total / 2
    for h, color in zip(heights, colors):
        half = canvas * h / 2
        d.rounded_rectangle(
            [x, centre - half, x + bar_w, centre + half],
            radius=bar_w / 2,
            fill=color,
        )
        x += bar_w + gap

    return img.resize((size, size), Image.LANCZOS)


def save(name: str, size: int, padding: float = 0.0) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw_icon(size, padding).save(OUT / name, "PNG", optimize=True)
    print("wrote", OUT / name)


if __name__ == "__main__":
    save("icon-192.png", 192)
    save("icon-512.png", 512)
    save("maskable-512.png", 512, padding=0.12)
    save("apple-touch-icon.png", 180, padding=0.08)
    save("favicon-64.png", 64)
