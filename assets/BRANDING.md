# cogitobase brand & design guidelines

This document defines cogitobase's visual identity. Everything in `assets/` is generated:
the marks from `assets/brand.py`, the diagrams from `assets/diagrams.py`. The SVG and PNG
files are derived, so change a generator and re-run it rather than editing an export:

```bash
pip install fonttools font-roboto resvg-py pillow
python assets/brand.py
python assets/diagrams.py
```

## 1. Core identity

- **Name:** cogitobase — **always lowercase, in every position**, including at the start of
  a sentence, in headings, and in the wordmark. Like `npm` or `nginx`, the lowercase
  spelling only reads as deliberate when it is exceptionless; a single capitalised instance
  makes every other one look like a typo. The one exception is an identifier that follows
  its own language's convention — a Prometheus alert name stays CamelCase
  (`CogitobaseGitSyncPaused`), because there the surrounding convention is stronger than
  the wordmark.
- **Where the name comes from:** *cogito* — Latin "I think", from Descartes' *cogito, ergo
  sum* — plus *base*, the store it thinks out of. Worth one line wherever the project
  introduces itself, because a name nobody can decode is a name nobody remembers.
- **Tagline:** the single source of truth for your AI workspace
- **Qualifiers:** private · self-hosted · central — always that order, always the `·`
  separator, never `|`.
- **Core themes:** centralization, second brain, human-centric AI, trust, privacy, elegance.
- **Claim discipline:** "private" means *your data stays in infrastructure you control* —
  the vault, the git mirror, the index. It is not a no-egress claim: note text and search
  queries reach the Gemini API (see `PRIVACY.md`). Never write "fully private", "nothing
  leaves your server" or "zero third parties" in any asset or document.

## 2. Aesthetic & visual style

- **Vibe:** corporate minimalist — trustworthy, calm, enterprise-ready, approachable.
- **Background:** always light. Clean white or a very soft, airy tint. A dark variant of a
  mark exists for dark host surfaces, but the brand's own surfaces are light.
- **Key elements:** a Zen Ensō brush circle, clean network nodes, generous white space.
- **Style:** vector art, flat or a single subtle gradient. No drop shadows, no bevels, no
  3D, no photographic texture.
- **Typeface:** Roboto (Apache-2.0) for every asset — the generator embeds its outlines, so
  no asset depends on an installed font. Wordmark: Roboto Medium, lowercase, tracking
  `0.004em`. Taglines and body: Roboto Regular. No serif, no display, no script face.

## 3. Color palette

| Role | Hex | Use |
|------|-----|-----|
| Ink | `#16222E` | Wordmark, headings, body type. The default for all text. |
| Blue 700 | `#2E5F8F` | The deep end of the Ensō gradient; the hub node; the icon. |
| Blue 500 | `#5C8FC4` | The light end of the gradient; leaf nodes; the qualifier line. |
| Blue 200 | `#BFD4E8` | The links between nodes; hairline structure. |
| Gray 400 | `#8794A1` | Secondary type — taglines, captions. Never body text. |
| Paper | `#F4F8FC` | The banner wash, always against white. Never a flat fill. |
| Base | `#FFFFFF` | Negative space. |

One ink, three blues, one gray, one tint — nothing else. No amber, emerald, indigo or
violet accent, and never neon. Diagrams are brand surfaces too: a mermaid `classDef` in a
document takes its fills from this table.

On a surface darker than Paper, use the `-dark` variant of a mark. It is a second set of
values, not a recolour: Ink becomes `#EAF1F8`, Blue 700 `#8FBBE6`, Blue 500 `#5E93C8`,
Blue 200 `#3F5B78`.

## 4. The mark

An Ensō brush circle whose opening faces east, so the mark reads as the lowercase `c` of
the wordmark. Inside the aperture sit three linked nodes — notes and the hub they share —
with the hub on the opening itself, because the circle is open: things go in and out of it.

The stroke is computed, not drawn. `brand.py` walks the arc and offsets it by half the
local brush pressure, which is why the taper stays smooth at any size and why a slight
radius wobble keeps it from reading as a machine-drawn ring.

`icon.svg` is a **redrawn** small-size mark, not a downscale: no wobble, a heavier stroke,
and one node instead of three, because a constellation below 32 px is a smudge.

## 5. Diagrams

Diagrams are drawn in a sketch style: box edges are cubics bowed off their own axis, drawn
twice, and they overshoot at the corners. That is the same principle as the mark's radius
wobble — a line that reads as drawn rather than plotted invites a reader to follow the
argument instead of taking it as given. `diagrams.py` computes the displacement from a
seeded generator, so a re-run redraws the same picture.

Type stays Roboto: a script face would put the hand in the labels, where legibility at feed
size matters more than character. The sketch is in the linework only.

Conventions that hold across every diagram:

- A solid line is a request path, a dashed line something derived or asynchronous, and a
  Blue 700 outline the boundary where data leaves the operator's host. Each diagram that
  uses all three carries the legend.
- One box per diagram is highlighted, and it is highlighted by tinting Blue 200, never by a
  colour outside §3.
- Every diagram states a title and one line of subtitle, because a diagram is shared on its
  own and has to explain itself away from the document it was written for.

## 6. Logo usage

- **Clear space:** free margin on all four sides equal to 25 % of the mark's height.
  Nothing — type, border, badge — enters it.
- **Minimum size:** 64 px for `logo.svg`, 200 px wide for the lockup. At 48 px and below
  use `icon.svg`; at 16–48 px use `favicon.ico`, which carries a frame drawn for each size.
- **Do:** place a mark on white or Paper; use the SVG or the transparent PNG so the host
  surface shows through; use the `-dark` variant on anything darker than Paper.
- **Don't:** add a frame, a rounded-rectangle plate or a drop shadow; bake a background
  into a file; rotate, skew, outline or recolour the mark; stretch it off 1:1; set the
  wordmark in any case but lowercase; put type inside the icon — the icon is wordless.

## 7. Files & formats

SVG is the master for every mark and PNG is the only export format: lossless, with alpha.
**JPEG is not used** for logos, wordmarks, banners or diagrams — it has no alpha and it
rings around type. Every PNG stays under 300 KB.

| File | Size | Where it goes |
|------|------|---------------|
| `logo.svg` / `.png` | 512, 256, 128, 64 | The mark alone, square, transparent |
| `logo-dark.svg` / `.png` | 512 | The mark on a dark surface |
| `icon.svg` / `.png` | 48, 32, 16 | Small sizes; the redrawn mark |
| `favicon.ico` | 48/32/16 | Browser tab, OS shortcut |
| `wordmark.svg` / `.png` | 800 | Type only, where the mark already appears |
| `lockup.svg` / `.png` | 800 | Mark + wordmark, horizontal |
| `lockup-dark.svg` / `.png` | 800 | The lockup on a dark surface |
| `header.svg` / `.png` | 1600×460 | The README hero |
| `social-preview.svg` / `.png` | 1280×640 | GitHub social preview, Open Graph card |
| `diagram-architecture.svg` / `.png` | 1760×1090 | The stack, and where data leaves the host |
| `diagram-features.svg` / `.png` | 1600×900 | The six things cogitobase changes |
| `diagram-why.svg` / `.png` | 1280×700 | The same clients, with and without a shared vault |
| `diagram-write-path.svg` / `.png` | 1660×660 | A write, from tool call to searchable chunk |
| `diagram-skills.svg` / `.png` | 1500×700 | Why the catalog is pushed and the body pulled |

## 8. Wording

- **"second brain"** is a common noun: lowercase, no quotation marks, in every document,
  prompt and skill. Capitals are for the product name, which is never capitalised.
- **Ensō** carries the macron in prose. Drop it inside a generator prompt.
- One register throughout: plain declarative. No intensifiers ("incredibly powerful",
  "very easy"), no emoji in headings, and no absolute a document elsewhere contradicts —
  a backup is *durable*, never "indestructible".
