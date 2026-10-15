"""
Generate the app icons in static/icons/ — no image library needed.

    ./venv/bin/python scripts/make_icons.py

The mark is the map's own "you are here" dot: a teal point inside a fading
teal ring, on the app's near-black. Shapes are drawn with analytic
anti-aliasing (each pixel's coverage comes from its distance to the edge), and
the PNGs are written by hand with zlib, so this runs with the standard library.

Variants:
- icon-192 / icon-512: rounded square, transparent corners (Android, desktop)
- icon-maskable-512: full-bleed square; the mark sits inside the central
  safe zone, so any mask shape Android applies (circle, squircle) keeps it
- apple-touch-icon (180): full square; iOS rounds the corners itself
- badge-96: white mark on transparent, for Android's status-bar badge
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
BG = (10, 10, 11)
TEAL = (0, 229, 199)
WHITE = (255, 255, 255)


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _over(dst, rgb, alpha):
    """Composite `rgb` at coverage `alpha` over an RGBA pixel."""
    r, g, b, a = dst
    out_a = alpha + a * (1 - alpha)
    if out_a == 0:
        return (0, 0, 0, 0)
    mix = lambda c_src, c_dst: (c_src * alpha + c_dst * a * (1 - alpha)) / out_a
    return (mix(rgb[0], r), mix(rgb[1], g), mix(rgb[2], b), out_a)


def render(size: int, rounded: bool, full_bleed: bool, mono: bool = False, scale: float = 1.0) -> bytes:
    c = size / 2
    corner = size * 0.22
    dot_r, ring_r, ring_w = size * 0.13 * scale, size * 0.29 * scale, size * 0.055 * scale
    rows = []
    for y in range(size):
        row = bytearray([0])                               # PNG filter type 0 per row
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            pixel = (0, 0, 0, 0)
            if not mono:
                if full_bleed:
                    cover = 1.0
                else:                                       # rounded square background
                    dx = max(abs(px - c) - (c - corner), 0)
                    dy = max(abs(py - c) - (c - corner), 0)
                    cover = _clamp(corner - math.hypot(dx, dy) + 0.5) if rounded else 1.0
                pixel = _over(pixel, BG, cover)
            d = math.hypot(px - c, py - c)
            ink = WHITE if mono else TEAL
            ring = _clamp(ring_r + ring_w / 2 - d + 0.5) - _clamp(ring_r - ring_w / 2 - d + 0.5)
            pixel = _over(pixel, ink, ring * (0.9 if mono else 0.45))
            pixel = _over(pixel, ink, _clamp(dot_r - d + 0.5))
            row += bytes(int(round(v)) for v in pixel[:3]) + bytes([int(round(pixel[3] * 255))])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "icon-192.png": render(192, rounded=True, full_bleed=False),
        "icon-512.png": render(512, rounded=True, full_bleed=False),
        # Maskable: the mark shrunk to stay inside the safe zone (radius 40%).
        "icon-maskable-512.png": render(512, rounded=False, full_bleed=True, scale=0.8),
        "apple-touch-icon.png": render(180, rounded=False, full_bleed=True),
        "badge-96.png": render(96, rounded=False, full_bleed=False, mono=True, scale=1.25),
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
        print(f"wrote {name} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()
