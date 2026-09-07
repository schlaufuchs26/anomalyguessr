# Temporal Detective 🦊

Spot-the-anachronism browser game. Every round shows a public-domain historical
photograph (pre-1928) into which exactly **one** modern object was planted with
AI image editing. Click the spot where something doesn't belong; score by
distance to the true position. Progressive hints are the safety net for the
hard rounds.

Conceit: the "Zeitreisen-Schutzstaffel" (time-travel protection squad) has
detected an anomaly and you, the Temporal Detective, have to locate it before
the timeline frays.

Play it: <https://schlaufuchs26.github.io/temporal-detective/>

## How to run

```sh
bun install
bun index.html     # dev server
bun run build      # static site into dist/
bun run checks     # format + tsc + biome + knip + tests
```

## Gameplay

- One scene = one edited photo + one planted anomaly.
- Click anywhere on the photo. Score: `100 * (1 - distance / 0.6)` in
  normalized image space; a click inside the anomaly's answer radius counts as
  a perfect hit (100). The score always uses original-image coordinates.
- Zoom & pan to inspect fine detail: mouse wheel zooms toward the cursor,
  double-click zooms in (double-click again resets), drag pans when zoomed,
  touch pinch works on mobile.
- `💡 Tipp zeigen` reveals the next of three progressive hints
  (what it's attached to → which half → exact spot + object name). Each hint
  used scales the score by 0.85 / 0.7 / 0.55.
- Difficulty tiers label expected subtlety: **Dezent** (small muted object,
  e.g. a digital watch on a wrist), **Klassisch** (e.g. a plastic bottle among
  stall goods), **Auffällig** (e.g. a smartphone in a hand). The badge on each
  scene shows its tier.
- All source photos are landscape (wider than tall).

## Scene manifest format

Scenes live in `scenes/`; `scenes/manifest.json` is loaded at runtime.
Each scene entry:

```json
{
  "id": "jammu-bazaar",
  "title": "Butcher's Bazaar",
  "place": "Jammu, Indien",
  "year": "ca. 1900",
  "credit": "Public Domain, via Wikimedia Commons",
  "sourceUrl": "https://commons.wikimedia.org/wiki/File:...",
  "difficulty": "dezent | klassisch | auffaellig",
  "image": "scenes/jammu-bazaar.jpg",
  "original": "scenes/jammu-bazaar-original.jpg",
  "anomaly": "Digitaluhr",
  "answer": { "x": 0.11, "y": 0.62, "r": 0.03 },
  "hints": [
    "Eine Person trägt es am Körper.",
    "Linke Bildhälfte, unterer Bereich.",
    "Am Handgelenk des Mannes in heller Kleidung links: eine Digitaluhr."
  ]
}
```

- `answer.x/y`: normalized position of the anomaly (0..1, top-left origin)
  **on the edited image** (`image`), not on the original.
- `answer.r`: normalized hit radius for a perfect 100-point click.
- `image` must keep the original's aspect ratio, otherwise the answer
  coordinates no longer match what the player sees.
- Adding a scene = drop `<id>.jpg` + `<id>-original.jpg` into `scenes/`,
  append the manifest entry, done.

## Sources & licensing

All base photographs are public domain / CC0, pre-1928, sourced via Wikimedia
Commons (Library of Congress / DPLA collections, State Library of Queensland,
National Library of Ireland, Fortepan). Per-scene credit + file page link live
in the manifest and are shown in the game's result panel. The anomalies are
AI edits; the edited images are new derived works of public-domain photos.
