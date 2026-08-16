"""Generate the cogitobase diagrams from one definition.

The sketch look is computed, not drawn. Every edge is a cubic whose control points are
displaced perpendicular to the line, drawn twice with different displacement, and the
corners overshoot — the three things that separate a hand-drawn box from a rectangle.
The displacement comes from a seeded generator, so re-running this file reproduces the
same drawing rather than a new one.

Type stays Roboto (BRANDING.md §2 admits no script face), so the hand belongs to the
linework and the labels stay legible at feed size.

    pip install fonttools font-roboto resvg-py pillow
    python assets/diagrams.py
"""
import math
from pathlib import Path

from brand import (BASE, BLUE_200, BLUE_500, BLUE_700, GRAY_400, INK, PAPER,
                   _font, svg, text_path, text_width)

OUT = Path(__file__).resolve().parent

# The palette in BRANDING.md §3 is exhaustive, so a box is highlighted by TINTING a
# documented blue rather than by introducing a lighter one. Every fill here resolves to
# one of the seven listed hex values.
HERO = (BLUE_200, 0.34)   # the one box a diagram is about
SOFT = (BASE, 1.0)        # ordinary nodes
STROKE = 2.6


# --- the hand -----------------------------------------------------------------
class Hand:
    """A seeded stream of small displacements — the entire source of the sketch look.

    Seeded per diagram rather than per shape, so two boxes with the same coordinates
    still differ, while the file as a whole redraws identically.
    """

    def __init__(self, seed):
        self.s = seed & 0x7FFFFFFF

    def _next(self):
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s / 0x7FFFFFFF

    def off(self, amp):
        return (self._next() * 2 - 1) * amp


def _edge(hand, x1, y1, x2, y2, amp):
    """One line as a cubic bowed off its own axis.

    The bow is capped against the line's length: a long connector may wander, a 40 px
    box edge may not, or short edges read as broken rather than drawn.
    """
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length
    a = amp * min(1.0, length / 180)
    c1x, c1y = x1 + dx * 0.30 + px * hand.off(a), y1 + dy * 0.30 + py * hand.off(a)
    c2x, c2y = x1 + dx * 0.68 + px * hand.off(a), y1 + dy * 0.68 + py * hand.off(a)
    return (f'M{x1 + hand.off(a * 0.5):.1f},{y1 + hand.off(a * 0.5):.1f} '
            f'C{c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} '
            f'{x2 + hand.off(a * 0.5):.1f},{y2 + hand.off(a * 0.5):.1f}')


def stroke(hand, pts, color=INK, width=STROKE, amp=2.4, passes=2, dash=None,
           opacity=1.0):
    """A polyline drawn `passes` times, each pass displaced on its own."""
    d = " ".join(_edge(hand, *pts[i], *pts[i + 1], amp)
                 for _ in range(passes) for i in range(len(pts) - 1))
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-linecap="round" opacity="{opacity}"{da}/>')


def box(hand, x, y, w, h, fill=SOFT, color=INK, width=STROKE, amp=2.2,
        over=7.0, dash=None):
    """A sketched rectangle: a flat fill, then four edges that overshoot the corners.

    The overshoot is what reads as a pen lifted at the corner instead of a join, and
    the fill is a plain quad so the interior stays calm behind type.
    """
    o = over
    parts = []
    if fill:
        colour, alpha = fill
        parts.append(f'<path d="M{x:.1f},{y:.1f} L{x + w:.1f},{y:.1f} '
                     f'L{x + w:.1f},{y + h:.1f} L{x:.1f},{y + h:.1f} Z" '
                     f'fill="{colour}" fill-opacity="{alpha}"/>')
    for (ax, ay), (bx, by) in ((((x - o, y), (x + w + o, y))),
                               (((x + w, y - o), (x + w, y + h + o))),
                               (((x + w + o, y + h), (x - o, y + h))),
                               (((x, y + h + o), (x, y - o)))):
        parts.append(stroke(hand, [(ax, ay), (bx, by)], color, width, amp, dash=dash))
    return "\n  ".join(parts)


def arrow(hand, pts, color=INK, width=STROKE, head=15.0, dash=None, both=False):
    """A connector with a two-stroke head, angled off the last segment."""
    parts = [stroke(hand, pts, color, width, amp=3.0, dash=dash)]
    for tip, tail in ([(pts[-1], pts[-2])] + ([(pts[0], pts[1])] if both else [])):
        a = math.atan2(tip[1] - tail[1], tip[0] - tail[0])
        for turn in (2.55, -2.55):
            parts.append(stroke(hand, [tip, (tip[0] + head * math.cos(a + turn),
                                             tip[1] + head * math.sin(a + turn))],
                                color, width, amp=1.0, passes=1))
    return "\n  ".join(parts)


def dot(x, y, r=7.0, color=INK):
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{color}"/>'


# --- type ---------------------------------------------------------------------
MEDIUM, REGULAR = "Roboto-Medium.ttf", "Roboto-Regular.ttf"
_LOADED = {}


def font(name):
    if name not in _LOADED:
        _LOADED[name] = _font(name)
    return _LOADED[name]


def line(text, size, cx, baseline, color=INK, face=REGULAR, anchor="middle"):
    ttf = font(face)
    w = text_width(ttf, text, size)
    x = cx - w / 2 if anchor == "middle" else (cx - w if anchor == "end" else cx)
    d, _ = text_path(ttf, text, size, x, baseline)
    return f'<path d="{d}" fill="{color}"/>' if d else ""


def wrap(text, size, max_w, face=REGULAR):
    ttf, lines, cur = font(face), [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and text_width(ttf, trial, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def block(text, size, cx, top, max_w, color=GRAY_400, face=REGULAR, lead=1.34):
    """Centred, wrapped body copy. Returns (svg, height) so callers can stack."""
    rows = wrap(text, size, max_w, face)
    out = [line(r, size, cx, top + i * size * lead, color, face) for i, r in enumerate(rows)]
    return "\n  ".join(out), len(rows) * size * lead


def node(hand, x, y, w, h, title, body="", fill=SOFT, title_size=30,
         body_size=21, color=INK):
    """A sketched box with a centred title and optional wrapped body."""
    parts = [box(hand, x, y, w, h, fill)]
    cx = x + w / 2
    if body:
        rows = wrap(body, body_size, w - 34)
        total = title_size + 10 + len(rows) * body_size * 1.32
        base = y + (h - total) / 2 + title_size * 0.80
        parts.append(line(title, title_size, cx, base, color, MEDIUM))
        for i, r in enumerate(rows):
            parts.append(line(r, body_size, cx, base + 20 + (i + 1) * body_size * 1.32 - 8,
                              GRAY_400))
    else:
        parts.append(line(title, title_size, cx, y + h / 2 + title_size * 0.35,
                          color, MEDIUM))
    return "\n  ".join(p for p in parts if p)


def head(title, sub, w, y=76):
    """The title block every diagram carries, so a shared image explains itself."""
    parts = [line(title, 42, w / 2, y, INK, MEDIUM)]
    if sub:
        parts.append(line(sub, 25, w / 2, y + 44, GRAY_400))
    return "\n  ".join(parts)


def frame(w, h, body):
    """Diagrams sit on the Paper wash, never a flat fill (BRANDING.md §3)."""
    defs = ('<linearGradient id="wash" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{BASE}"/>'
            f'<stop offset="0.55" stop-color="{PAPER}"/>'
            f'<stop offset="1" stop-color="{BASE}"/></linearGradient>')
    return svg(w, h, f'<rect width="{w}" height="{h}" fill="url(#wash)"/>\n  {body}',
               defs, "cogitobase diagram")


# --- 1. architecture ----------------------------------------------------------
# One box per client, each spelled as its own vendor spells it, and each one INSTALL.md §6
# gives connection steps for. Two products sharing a box would claim a kinship they don't
# have, and a name with no section behind it promises support the docs never deliver.
CLIENTS = ["Claude Code", "OpenCode", "Antigravity", "Cursor"]


def architecture():
    W, H = 1760, 1090
    hand = Hand(20260814)
    p = [head("cogitobase architecture",
              "every client reads and writes one vault on hardware you run", W)]

    p.append(line("AI clients", 24, 175, 196, GRAY_400))
    for i, name in enumerate(CLIENTS):
        y = 220 + i * 98
        p.append(node(hand, 60, y, 230, 68, name, title_size=25))
        p.append(stroke(hand, [(294, y + 34), (330, y + 34), (330, 390)],
                        BLUE_500, 2.2, amp=2.0))
    p.append(dot(330, 390, 7, BLUE_500))
    p.append(arrow(hand, [(330, 390), (398, 390)], BLUE_500))
    p.append(line("MCP over HTTPS", 20, 175, 634, GRAY_400))

    p.append(node(hand, 406, 325, 224, 130, "reverse proxy",
                  "TLS termination · bearer token", title_size=29))
    p.append(arrow(hand, [(634, 390), (676, 390), (676, 320), (726, 320)], BLUE_500))

    # The server boundary. Everything inside it runs on the operator's own host, which
    # is the claim the whole diagram exists to make.
    p.append(box(hand, 686, 176, 758, 656, fill=SOFT, color=BLUE_700, width=2.2,
                 dash="14 11", over=4))
    p.append(line("your server · docker compose", 23, 706, 212, BLUE_700, MEDIUM,
                  anchor="start"))

    p.append(node(hand, 726, 250, 680, 122, "MCP server",
                  "Streamable HTTP · bearer auth · rate limit · SSRF filter",
                  fill=HERO, title_size=32))

    p.append(node(hand, 1126, 450, 280, 122, "Qdrant", "vector index", title_size=30))
    p.append(arrow(hand, [(1266, 376), (1266, 446)]))
    p.append(line("embed · query", 20, 1288, 420, GRAY_400, anchor="start"))

    p.append(box(hand, 726, 450, 356, 342, fill=(PAPER, 1.0), color=GRAY_400, width=2.0,
                 dash="10 8", over=4))
    p.append(line("vault-data/", 22, 744, 484, GRAY_400, MEDIUM, anchor="start"))
    for i, (title, body) in enumerate([("Markdown notes", "YAML · [[wikilinks]]"),
                                       ("skills", "shared procedures"),
                                       ("media", "PDFs · images")]):
        p.append(node(hand, 748, 508 + i * 98, 312, 72, title, body,
                      title_size=24, body_size=18))
    p.append(arrow(hand, [(830, 376), (830, 446)], both=True))
    p.append(line("read · write", 20, 808, 420, GRAY_400, anchor="end"))
    p.append(arrow(hand, [(1090, 700), (1180, 590)], GRAY_400, 2.2, dash="9 7"))
    p.append(line("indexed", 19, 1156, 690, GRAY_400, anchor="start"))

    # The one hop that leaves the operator's host. Set apart by a blue outline rather
    # than a warm fill, because the palette admits no amber (BRANDING.md §3) — and this
    # is the box PRIVACY.md turns on, so it must not read as just another node.
    p.append(box(hand, 1500, 250, 220, 122, fill=(PAPER, 1.0), color=BLUE_700))
    p.append(line("Gemini API", 27, 1610, 302, BLUE_700, MEDIUM))
    p.append(line("embeddings · Mem0", 21, 1610, 336, GRAY_400))
    p.append(arrow(hand, [(1440, 300), (1496, 300)], BLUE_700))
    txt, _ = block("note text and search queries — the only hop off your host",
                   19, 1610, 404, 230)
    p.append(txt)

    p.append(node(hand, 726, 906, 300, 116, "Obsidian", "opens the same folder",
                  title_size=28))
    p.append(arrow(hand, [(876, 902), (876, 798)], both=True))
    p.append(line("same files", 19, 898, 856, GRAY_400, anchor="start"))

    p.append(node(hand, 1086, 906, 320, 116, "private git repo",
                  "SSH deploy key · optional", title_size=28))
    p.append(arrow(hand, [(1246, 902), (1246, 838)], GRAY_400, 2.4, dash="9 7"))
    p.append(line("async mirror", 19, 1268, 876, GRAY_400, anchor="start"))

    p.append(stroke(hand, [(70, 900), (130, 900)], INK, 2.4))
    p.append(line("request path", 19, 142, 906, GRAY_400, anchor="start"))
    p.append(stroke(hand, [(70, 940), (130, 940)], GRAY_400, 2.4, dash="9 7"))
    p.append(line("derived or asynchronous", 19, 142, 946, GRAY_400, anchor="start"))
    p.append(stroke(hand, [(70, 980), (130, 980)], BLUE_700, 2.4))
    p.append(line("leaves your host", 19, 142, 986, GRAY_400, anchor="start"))
    return frame(W, H, "\n  ".join(p))


# --- 2. features --------------------------------------------------------------
FEATURES = [
    ("one central brain",
     "Identity, rules and knowledge live once. Every client you connect reads the "
     "same thing, so switching tools costs nothing."),
    ("plain Markdown",
     "Notes are files with YAML frontmatter and [[wikilinks]]. Point Obsidian at the "
     "folder and it opens — no export, no proprietary store."),
    ("search by meaning",
     "Notes, PDFs and images land in one vector index, retrieved semantically rather "
     "than by keyword."),
    ("agents write back",
     "Notes, decisions and daily logs are written by the agent against rules the "
     "server enforces on every call."),
    ("skills, shared",
     "Write a procedure once. The catalog is pushed to every session, the body is "
     "pulled only when a task matches it."),
    ("yours to run",
     "One container stack, your TLS, your token. Single-tenant by design and "
     "AGPL-3.0 licensed."),
]


def features():
    W, H = 1600, 900
    hand = Hand(20260815)
    p = [head("what cogitobase gives you", "one MCP server, six things it changes", W)]
    tw, th, gap = 460, 244, 40
    x0 = (W - (3 * tw + 2 * gap)) / 2
    for i, (title, body) in enumerate(FEATURES):
        x = x0 + (i % 3) * (tw + gap)
        y = 224 + (i // 3) * (th + gap)
        fill = HERO if i == 0 else SOFT
        p.append(box(hand, x, y, tw, th, fill))
        p.append(line(title, 34, x + tw / 2, y + 66, INK, MEDIUM))
        p.append(stroke(hand, [(x + tw / 2 - 34, y + 90), (x + tw / 2 + 34, y + 90)],
                        BLUE_500, 3.0, amp=1.6))
        txt, _ = block(body, 23, x + tw / 2, y + 136, tw - 62)
        p.append(txt)
    p.append(line("cogitobase 1.0.0 · self-hosted MCP server · github.com/mdorloechter/cogitobase",
                  21, W / 2, H - 42, GRAY_400))
    return frame(W, H, "\n  ".join(p))


# --- 3. why it exists ---------------------------------------------------------
def why():
    W, H = 1280, 700
    hand = Hand(20260816)
    p = [head("why", "the same three clients, before and after", W, y=72)]
    p.append(stroke(hand, [(W / 2, 168), (W / 2, H - 96)], BLUE_200, 2.4, amp=3.0,
                    dash="12 10"))

    p.append(line("without a shared brain", 30, 320, 214, INK, MEDIUM))
    # Both halves list the same clients in the same order: the picture only argues if the
    # left and right columns are the same three tools.
    for i, name in enumerate(CLIENTS[:3]):
        y = 252 + i * 118
        p.append(node(hand, 96, y, 232, 82, name, title_size=25))
        p.append(node(hand, 372, y + 8, 176, 66, "?", fill=SOFT, title_size=30,
                      color=GRAY_400))
        p.append(arrow(hand, [(332, y + 41), (368, y + 41)], GRAY_400, 2.2, dash="8 7"))
    txt, _ = block("Three tools, three sets of notes and rules. Every session starts "
                   "from nothing and what it learns dies with it.", 22, 320, 630, 480)
    p.append(txt)

    p.append(line("with cogitobase", 30, 960, 214, INK, MEDIUM))
    for i, name in enumerate(CLIENTS[:3]):
        y = 252 + i * 118
        p.append(node(hand, 700, y, 232, 82, name, title_size=25))
        p.append(stroke(hand, [(936, y + 41), (988, y + 41), (988, 370)], BLUE_500, 2.4))
    p.append(dot(988, 370, 7, BLUE_500))
    p.append(arrow(hand, [(988, 370), (1032, 370)], BLUE_500))
    p.append(node(hand, 1040, 288, 152, 164, "one", "vault, rules and skills",
                  fill=HERO, title_size=30, body_size=19))
    txt, _ = block("One vault on your own server. Update it once and every client is "
                   "already current.", 22, 960, 630, 480)
    p.append(txt)
    return frame(W, H, "\n  ".join(p))


# --- 4. the write path --------------------------------------------------------
def write_path():
    W, H = 1660, 660
    hand = Hand(20260817)
    p = [head("what a write actually does",
              "the file is the source of truth — everything after it is derived", W)]
    steps = [("write_note", "the agent names its target"),
             ("validated", "type, summary, sources — rejected with a reason"),
             ("file on disk", "Markdown in vault-data/, nothing else moved"),
             ("chunked · embedded", "cut on paragraphs, not byte offsets"),
             ("searchable", "one point per chunk in Qdrant")]
    bw, gap = 280, 62
    x0 = (W - (5 * bw + 4 * gap)) / 2
    for i, (title, body) in enumerate(steps):
        x = x0 + i * (bw + gap)
        p.append(node(hand, x, 250, bw, 168, title, body,
                      fill=HERO if i == 2 else SOFT, title_size=27,
                      body_size=20))
        if i:
            p.append(arrow(hand, [(x - gap + 6, 334), (x - 8, 334)], BLUE_500))
    # The mirror hangs off the file, not off the chain: it is a copy of what landed on
    # disk, and it is what a reader would otherwise mistake for a further step.
    disk_cx = x0 + 2 * (bw + gap) + bw / 2
    p.append(arrow(hand, [(disk_cx, 422), (disk_cx, 508)], GRAY_400, 2.4, dash="9 7"))
    p.append(node(hand, disk_cx - 170, 512, 340, 92, "git mirror",
                  "async, optional, your repo", title_size=25, body_size=19))
    txt, _ = block("The index can be rebuilt from the files at any time. The files "
                   "cannot be rebuilt from the index — which is why they are what git "
                   "carries and what Obsidian opens.", 22, W / 2, 190, 1180)
    p.append(txt)
    return frame(W, H, "\n  ".join(p))


# --- 5. how a skill reaches the model -----------------------------------------
def skills():
    W, H = 1500, 700
    hand = Hand(20260818)
    p = [head("how a skill reaches the model",
              "the catalog is pushed, the body is pulled", W)]

    p.append(node(hand, 90, 250, 300, 130, "session starts",
                  "any connected client", title_size=28))
    p.append(arrow(hand, [(394, 315), (466, 315)], BLUE_500))
    p.append(node(hand, 474, 226, 400, 178, "get_core_context",
                  "identity, rules, and one line per skill — name and when to use it",
                  fill=HERO, title_size=30, body_size=20))
    p.append(line("small, every session", 20, 674, 434, GRAY_400))

    p.append(arrow(hand, [(878, 315), (966, 315)], BLUE_500))
    p.append(node(hand, 974, 226, 436, 178, "a task matches one",
                  "get_skill returns that body, and only that one", title_size=28,
                  body_size=20))
    p.append(line("large, on demand", 20, 1192, 434, GRAY_400))

    txt, _ = block("A catalog of names costs a few hundred characters and can ride "
                   "along in every session. Six full procedures cannot. Pushing the "
                   "index and pulling the body is what keeps the context window free "
                   "for your actual task.", 23, W / 2, 516, 1180)
    p.append(txt)
    p.append(stroke(hand, [(W / 2 - 300, 596), (W / 2 + 300, 596)], BLUE_200, 2.6,
                    amp=3.0))
    p.append(line("write_skill persists a procedure once — every client gets it",
                  22, W / 2, 644, INK))
    return frame(W, H, "\n  ".join(p))


DIAGRAMS = {
    "diagram-architecture": (architecture, 1760),
    "diagram-features": (features, 1600),
    "diagram-why": (why, 1280),
    "diagram-write-path": (write_path, 1660),
    "diagram-skills": (skills, 1500),
}


def main():
    import resvg_py  # noqa: PLC0415 - only needed to generate

    for name, (build, px) in DIAGRAMS.items():
        source = build()
        (OUT / f"{name}.svg").write_text(source, encoding="utf-8")
        out = OUT / f"{name}.png"
        out.write_bytes(bytes(resvg_py.svg_to_bytes(svg_string=source, width=px)))
        print(f"{out.name:28} {px:>5}px  {out.stat().st_size / 1024:6.1f} KB")


if __name__ == "__main__":
    main()
