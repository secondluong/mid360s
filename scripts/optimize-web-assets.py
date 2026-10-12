#!/usr/bin/env python3
"""Derive small web assets for the on-board viewer.

Nothing here is sized for a 4G uplink: the suit texture is a 4 MB uncompressed
TGA, the fallback card a 1.5 MB PNG, and the three.js bundle another megabyte.
Over the MyView relay those never finish in time and the viewer is left with the
flat card, so ship WebP and gzip derivatives next to the originals. The server
only serves a derivative that is not older than its source.

Re-run after replacing source art or editing the bundled JS:

    python3 scripts/optimize-web-assets.py
"""

from __future__ import annotations

import gzip
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
FIREMAN_DIR = ROOT / "data" / "fireman"
STATIC_DIR = ROOT / "local_viewer" / "static"

SUIT_SRC = FIREMAN_DIR / "消防灭火防护服.tga"
SUIT_OUT = FIREMAN_DIR / "消防灭火防护服.webp"
CARD_SRC = STATIC_DIR / "firefighter.png"
CARD_OUT = STATIC_DIR / "firefighter-card.webp"
CARD_MAX = (512, 768)


def kb(path: Path) -> str:
    return f"{path.stat().st_size / 1024:.0f} KB"


def punch_green(im: Image.Image) -> Image.Image:
    """Key out the green screen, matching the browser-side punchGreen()."""
    rgba = im.convert("RGBA")
    r, g, b, a = rgba.split()
    scale = lambda c: c.point(lambda v: min(255, int(v * 1.35)))  # noqa: E731
    binary = lambda c: c.point(lambda v: 255 if v else 0)  # noqa: E731
    over_r = binary(ImageChops.subtract(g, scale(r)))
    over_b = binary(ImageChops.subtract(g, scale(b)))
    bright = g.point(lambda v: 255 if v > 90 else 0)
    keyed = ImageChops.multiply(ImageChops.multiply(over_r, over_b), bright)
    rgba.putalpha(ImageChops.subtract(a, keyed))
    return rgba


def write_suit() -> bool:
    if not SUIT_SRC.is_file():
        print(f"skip suit: {SUIT_SRC.name} missing")
        return False
    im = Image.open(SUIT_SRC).convert("RGBA")
    # Keep the UV layout at full resolution; the win is compression, not size.
    im.save(SUIT_OUT, "WEBP", quality=88, method=6)
    print(f"suit {SUIT_SRC.name} {kb(SUIT_SRC)} -> {SUIT_OUT.name} {kb(SUIT_OUT)}")
    return True


def write_card() -> bool:
    if not CARD_SRC.is_file():
        print(f"skip card: {CARD_SRC.name} missing")
        return False
    im = punch_green(Image.open(CARD_SRC))
    im.thumbnail(CARD_MAX, Image.LANCZOS)
    im.save(CARD_OUT, "WEBP", quality=86, method=6)
    print(f"card {CARD_SRC.name} {kb(CARD_SRC)} -> {CARD_OUT.name} {kb(CARD_OUT)} {im.size}")
    return True


def gzip_one(src: Path) -> None:
    out = src.with_suffix(src.suffix + ".gz")
    with src.open("rb") as fh, gzip.open(out, "wb", compresslevel=6) as gz:
        shutil.copyfileobj(fh, gz)
    # Match mtime so the server's freshness check passes.
    shutil.copystat(src, out)
    print(f"gzip {src.name} {kb(src)} -> {out.name} {kb(out)}")


def write_gzip() -> bool:
    """Pre-compress the meshes and the JS bundle."""
    sources = sorted(FIREMAN_DIR.glob("*.FBX"))
    sources += [p for p in sorted(STATIC_DIR.glob("*.js")) if not p.name.endswith(".gz")]
    sources += sorted(STATIC_DIR.glob("jsm/**/*.js"))
    if not sources:
        print("skip gzip: nothing to compress")
        return False
    for src in sources:
        gzip_one(src)
    return True


def main() -> int:
    ok = write_suit()
    ok = write_card() or ok
    ok = write_gzip() or ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
