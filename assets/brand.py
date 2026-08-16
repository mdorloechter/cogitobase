"""Generate every cogitobase brand asset from one definition.

The marks are computed, not drawn: the enso is a brush stroke built by offsetting a
circle by its own local pressure, and the wordmark is emitted as glyph outlines, so an
SVG here carries no font dependency and no raster artefacts. Run this instead of editing
an export by hand — the exports are derived files, this file is the source.

    pip install fonttools font-roboto resvg-py pillow
    python assets/brand.py

Roboto is Apache-2.0 (Google). Only its outlines are embedded, which the licence allows.
"""
import math
from pathlib import Path

from fontTools.misc.transform import Transform
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).resolve().parent

# --- palette -----------------------------------------------------------------
# Deliberately narrow: one ink, two blues, one tint, one gray. See BRANDING.md §3.
INK = "#16222E"        # wordmark and headings
BLUE_700 = "#2E5F8F"   # the deep end of the enso, the hub node
BLUE_500 = "#5C8FC4"   # the light end of the enso, the leaf nodes
BLUE_200 = "#BFD4E8"   # the links between nodes
GRAY_400 = "#8794A1"   # secondary type
PAPER = "#F4F8FC"      # the banner wash, never a flat fill
BASE = "#FFFFFF"       # negative space

# Dark-surface variant: the same relationships, lifted so the mark keeps its contrast
# on anything darker than the tint. Not a recolour — a second set of values.
DARK = {BLUE_700: "#8FBBE6", BLUE_500: "#5E93C8", BLUE_200: "#3F5B78",
        INK: "#EAF1F8", GRAY_400: "#9AAAB9"}


# --- type --------------------------------------------------------------------
def _font(name):
    from importlib.resources import files  # noqa: PLC0415 - only needed to generate
    return TTFont(files("font_roboto") / "files" / name)


def text_path(ttf, s, size, x, baseline, tracking=0.0):
    """Outline `s` at `size` with its baseline at `baseline`, starting at `x`."""
    scale = size / ttf["head"].unitsPerEm
    cmap, glyphs, hmtx = ttf.getBestCmap(), ttf.getGlyphSet(), ttf["hmtx"]
    parts, pen_x = [], x
    for ch in s:
        gname = cmap.get(ord(ch))
        if gname is None:
            continue
        pen = SVGPathPen(glyphs, ntos=lambda v: f"{v:.1f}")
        glyphs[gname].draw(TransformPen(pen, Transform(scale, 0, 0, -scale, pen_x, baseline)))
        if d := pen.getCommands():
            parts.append(d)
        pen_x += hmtx[gname][0] * scale + tracking * size
    return " ".join(parts), pen_x - x - tracking * size


def text_width(ttf, s, size, tracking=0.0):
    scale = size / ttf["head"].unitsPerEm
    cmap, hmtx = ttf.getBestCmap(), ttf["hmtx"]
    w = sum(hmtx[g][0] * scale + tracking * size
            for ch in s if (g := cmap.get(ord(ch))))
    return w - tracking * size


# --- the enso ----------------------------------------------------------------
def _smoothstep(a, b, t):
    t = min(1.0, max(0.0, t))
    return a + (b - a) * t * t * (3 - 2 * t)


# Brush pressure along the stroke. Lands as a hairline, gains weight through the
# sweep, lifts off to a point. Both ends ramp over a long span; a short ramp reads
# as a blunt hook rather than a brush leaving the paper.
PRESSURE = [(0.00, 0.02), (0.22, 0.72), (0.46, 1.00),
            (0.68, 0.82), (0.88, 0.42), (1.00, 0.03)]


def _pressure(t):
    for (t0, v0), (t1, v1) in zip(PRESSURE, PRESSURE[1:]):
        if t0 <= t <= t1:
            return _smoothstep(v0, v1, (t - t0) / (t1 - t0))
    return PRESSURE[-1][1]


def enso(cx, cy, r, w, a0, a1, wobble=True, steps=220):
    """A tapered brush arc from `a0` to `a1` degrees, as a filled outline.

    A stroked arc cannot taper, so the outline is walked twice: once offset outward
    by half the local brush width and once inward. A slight radius wobble keeps the
    result from reading as a machine-drawn ring.
    """
    outer, inner = [], []
    for i in range(steps + 1):
        t = i / steps
        a = math.radians(a0 + (a1 - a0) * t)
        half = w * _pressure(t) / 2
        rr = r * (1 + 0.011 * math.sin(2.7 * a + 1.1)
                  + 0.005 * math.sin(5.3 * a + 0.3)) if wobble else r
        ux, uy = math.cos(a), math.sin(a)
        outer.append((cx + ux * (rr + half), cy + uy * (rr + half)))
        inner.append((cx + ux * (rr - half), cy + uy * (rr - half)))
    pts = outer + inner[::-1]
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"


# The aperture holds three linked notes and the hub they share. The hub sits on the
# opening, because the circle is open: things go in and out of it. Offsets are given
# in the 512 grid of logo.svg and scale with the mark.
NODES = [(-76, -58, 17), (-50, 74, 14), (68, 4, 24)]
LINKS = [(0, 2), (1, 2), (0, 1)]
R, W = 172, 34          # centre-line radius and peak brush width, in the 512 grid
SWEEP = (38, 322)       # the opening faces east, so the mark reads as a lowercase c
RADIUS = R + W / 2      # what the mark actually occupies


def mark(cx, cy, scale, dark=False, node_scale=1.0):
    """The mark — enso plus constellation — scaled about (cx, cy)."""
    c = (lambda x: DARK[x]) if dark else (lambda x: x)
    out = [f'<path d="{enso(cx, cy, R * scale, W * scale, *SWEEP)}" '
           f'fill="url(#enso{"D" if dark else ""})"/>']
    pts = [(cx + dx * scale, cy + dy * scale, rr * scale * node_scale)
           for dx, dy, rr in NODES]
    out += [f'<line x1="{pts[i][0]:.1f}" y1="{pts[i][1]:.1f}" x2="{pts[j][0]:.1f}" '
            f'y2="{pts[j][1]:.1f}" stroke="{c(BLUE_200)}" '
            f'stroke-width="{8 * scale:.1f}" stroke-linecap="round"/>'
            for i, j in LINKS]
    out += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rr:.1f}" '
            f'fill="{c(BLUE_700) if n == len(pts) - 1 else c(BLUE_500)}"/>'
            for n, (x, y, rr) in enumerate(pts)]
    return "\n  ".join(out)


def gradient(dark=False):
    c = (lambda x: DARK[x]) if dark else (lambda x: x)
    return (f'<linearGradient id="enso{"D" if dark else ""}" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{c(BLUE_500)}"/>'
            f'<stop offset="1" stop-color="{c(BLUE_700)}"/></linearGradient>')


def svg(w, h, body, defs="", label="cogitobase"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-label="{label}">\n'
            f'  <defs>{defs}</defs>\n  {body}\n</svg>\n')


# --- assets ------------------------------------------------------------------
WORD = "cogitobase"
TAG = "the single source of truth for your AI workspace"
SUB = "private · self-hosted · central"
LABEL = f"cogitobase — {TAG}"


def logo(dark=False):
    """The mark alone: square, transparent, no frame. The frame belongs to the host."""
    return svg(512, 512, mark(256, 256, 1.0, dark=dark), gradient(dark))


def icon():
    """A 32 px icon is not a downscale. Redrawn: no taper worth the name, no wobble,
    one node instead of three, because a constellation at 32 px is a smudge."""
    body = (f'<path d="{enso(256, 256, 162, 96, 44, 316, wobble=False)}" '
            f'fill="{BLUE_700}"/>\n'
            f'  <circle cx="256" cy="256" r="58" fill="{BLUE_500}"/>')
    return svg(512, 512, body)


def _lockup(word_size, dark=False):
    """Mark and wordmark side by side, measured rather than positioned by hand."""
    medium = _font("Roboto-Medium.ttf")
    track = 0.004
    ww = text_width(medium, WORD, word_size, track)
    scale = word_size / 190          # the mark reads level with the wordmark's height
    d = 2 * RADIUS * scale
    gap = word_size * 0.46
    h = d * 1.34
    word, _ = text_path(medium, WORD, word_size, d + gap,
                        h / 2 + word_size * 0.30, track)
    w = d + gap + ww
    body = (mark(d / 2, h / 2, scale, dark=dark)
            + f'\n  <path d="{word}" fill="{DARK[INK] if dark else INK}"/>')
    return svg(round(w), round(h), body, gradient(dark), LABEL)


def lockup(dark=False):
    return _lockup(150, dark=dark)


def wordmark(dark=False):
    """Type only, for places that already show the mark."""
    medium = _font("Roboto-Medium.ttf")
    size, track = 150, 0.004
    w = text_width(medium, WORD, size, track)
    h = size * 1.28
    word, _ = text_path(medium, WORD, size, 0, h / 2 + size * 0.30, track)
    return svg(round(w), round(h),
               f'<path d="{word}" fill="{DARK[INK] if dark else INK}"/>', "", "cogitobase")


def banner(w, h, word_size, dark=False):
    """The hero: mark, wordmark, tagline, qualifiers — centred as one optical block.

    Every measurement is derived, so a change to the wordmark size or the tagline text
    re-centres the whole composition instead of drifting off centre.
    """
    medium, regular = _font("Roboto-Medium.ttf"), _font("Roboto-Regular.ttf")
    tag_size, sub_size = word_size * 0.285, word_size * 0.225
    wt, ws = 0.002, 0.030
    widths = [text_width(medium, WORD, word_size, 0.004),
              text_width(regular, TAG, tag_size, wt),
              text_width(regular, SUB, sub_size, ws)]
    block = max(widths)
    scale = word_size / 152
    d = 2 * RADIUS * scale
    gap = word_size * 0.62
    x0 = (w - (d + gap + block)) / 2
    text_x = x0 + d + gap
    # Stack the three lines, then centre the ink they cover — ascender of the wordmark
    # down to the descender of the last line — on the mark's own centre line.
    lead1, lead2 = word_size * 0.50, word_size * 0.42
    top_to_base = word_size * 0.75
    depth = lead1 + lead2 + sub_size * 0.21
    base = h / 2 - (depth - top_to_base) / 2
    word, _ = text_path(medium, WORD, word_size, text_x, base, 0.004)
    tag, _ = text_path(regular, TAG, tag_size, text_x + 2, base + lead1, wt)
    sub, _ = text_path(regular, SUB, sub_size, text_x + 2, base + lead1 + lead2, ws)
    c = (lambda x: DARK[x]) if dark else (lambda x: x)
    wash = ("#0E1722", "#131F2C") if dark else (PAPER, "#FFFFFF")
    defs = (gradient(dark)
            + f'<linearGradient id="wash" x1="0" y1="0" x2="1" y2="1">'
              f'<stop offset="0" stop-color="{wash[0]}"/>'
              f'<stop offset="0.45" stop-color="{wash[1]}"/>'
              f'<stop offset="1" stop-color="{wash[0]}"/></linearGradient>')
    body = (f'<rect width="{w}" height="{h}" fill="url(#wash)"/>\n  '
            + mark(x0 + d / 2, h / 2, scale, dark=dark, node_scale=1.12)
            + f'\n  <path d="{word}" fill="{c(INK)}"/>'
            + f'\n  <path d="{tag}" fill="{c(GRAY_400)}"/>'
            + f'\n  <path d="{sub}" fill="{c(BLUE_500)}"/>')
    return svg(w, h, body, defs, LABEL)


# name -> (svg source, png widths). The banner is exported at 2x its layout width so
# it stays sharp on a HiDPI screen at the width the README displays it.
ASSETS = {
    "logo": (lambda: logo(), [512, 256, 128, 64]),
    "logo-dark": (lambda: logo(dark=True), [512]),
    "icon": (lambda: icon(), [48, 32, 16]),
    "wordmark": (lambda: wordmark(), [800]),
    "lockup": (lambda: lockup(), [800]),
    "lockup-dark": (lambda: lockup(dark=True), [800]),
    "header": (lambda: banner(1600, 460, 140), [1600]),
    "social-preview": (lambda: banner(1280, 640, 124), [1280]),
}


def main():
    from PIL import Image  # noqa: PLC0415 - only needed to generate
    import resvg_py        # noqa: PLC0415

    for name, (build, widths) in ASSETS.items():
        source = build()
        (OUT / f"{name}.svg").write_text(source, encoding="utf-8")
        for i, px in enumerate(widths):
            out = OUT / (f"{name}.png" if i == 0 else f"{name}-{px}.png")
            out.write_bytes(bytes(resvg_py.svg_to_bytes(svg_string=source, width=px)))
            print(f"{out.name:24} {out.stat().st_size / 1024:6.1f} KB")
    # The .ico carries every size a browser or OS may ask for, each rendered at that
    # size from the simplified icon rather than resampled down from one big one.
    frames = [Image.open(OUT / f"icon{'' if px == 48 else f'-{px}'}.png") for px in (48, 32, 16)]
    frames[0].save(OUT / "favicon.ico", sizes=[(48, 48), (32, 32), (16, 16)],
                   append_images=frames[1:])
    print(f"{'favicon.ico':24} {(OUT / 'favicon.ico').stat().st_size / 1024:6.1f} KB")


if __name__ == "__main__":
    main()
