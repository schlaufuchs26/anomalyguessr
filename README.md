# Temporal Detective 🦊

Daily spot-the-anachronism quiz. Each day presents a fixed set of **5**
public-domain historical photographs (pre-1928), each with exactly **one**
modern object planted by AI image editing. Click the spot where something
doesn't belong; score by distance to the true position. Progressive hints are
the safety net for the hard rounds. Everyone gets the same 5 scenes on a given
day (deterministic date-seeded selection).

Conceit: the "Zeitreisen-Schutzstaffel" (time-travel protection squad) has
detected anomalies and you, the Temporal Detective, have to locate each before
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

- One quiz = the daily set of 5 scenes, played in sequence; overall score at
  the end (max 100 per scene, 500 total).
- Click anywhere on the photo. Score: `100 * (1 - distance / 0.6)` in
  normalized image space; a click inside the anomaly's answer radius counts as
  a perfect hit (100). The score always uses original-image coordinates.
- Zoom & pan to inspect fine detail: mouse wheel zooms toward the cursor,
  double-click zooms in (double-click again resets), drag pans when zoomed,
  touch pinch works on mobile.
- `💡 Tipp zeigen` reveals the next of three progressive hints
  (what it's attached to → which half → exact spot + object name). Each hint
  used scales the score by 0.85 / 0.7 / 0.55.
- Scenes carry an internal difficulty tier (dezent / klassisch / auffaellig)
  used for generation and tuning; it is not shown in the UI.
- All source photos are landscape (wider than tall).

## Daily selection

`daily.ts` derives the day's set from the browser's local calendar date
(`YYYY-MM-DD`): the date string hashes to a seed for a deterministic shuffle
(mulberry32), from which the first 5 scenes are taken. Same date → same set and
order for everyone; consecutive days rotate the set.

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
