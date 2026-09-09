# AnomalyGuessr 🦊

Daily spot-the-anomaly quiz. Each day presents a fixed set of **5**
public-domain historical photographs, each with exactly **one** anomaly
planted by AI image editing: a modern object, a time-traveling person or
something that could not possibly have been there (a fictional creature,
an impossible object). Click the spot where something doesn't belong; score
by distance to the true position. After the guess the game reveals WHY the
anomaly could not have been in the original photo, with a checkable
reference link for every factual claim. Progressive hints are the safety
net for the hard rounds. Everyone gets the same 5 scenes on a given day
(deterministic date-seeded selection).

Conceit: the time-travel protection squad has detected anomalies and you, the
AnomalyGuessr agent, have to locate each before the timeline frays.

Play it: <https://schlaufuchs26.github.io/anomalyguessr/>

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
- `💡 Show hint` reveals the next of three progressive hints
  (what it's attached to → which half → exact spot + object name). Each hint
  used scales the score by 0.85 / 0.7 / 0.55.
- A short context paragraph under the title describes what the photo shows
  and its historical background (place, year, period).
- The planted anomalies are varied and context-fitting (a coffee cup on a
  market stall, a drink can among baskets, a wristwatch on a passer-by, a
  phone in a parade crowd, a time-traveling jogger, a small dragon on a
  rooftop), not a fixed object set. The anomaly type matches the scene's
  density: a person among crowds, an object among clutter.
- All source photos are landscape (wider than tall).

## Daily selection

Two manifest generations share one frontend:

- **Version 1 (legacy pool):** the manifest holds every known scene. The
  frontend derives the day's set from the browser's local calendar date
  (`YYYY-MM-DD`): the date string hashes to a seed for a deterministic shuffle
  (mulberry32), from which the first 5 scenes are taken. Same date → same set
  and order for everyone; consecutive days rotate the set.
- **Version 2 (pipeline daily manifest):** the daily content pipeline (fuchs
  cron, see the game's wiki page) ships exactly the day's 5 scenes in play
  order inside the manifest (root `date` = the quiz day, `YYYY-MM-DD`). The
  frontend plays them in manifest order; no client-side selection. Everyone
  sees the same 5 because they load the same manifest.

## Scene manifest format

Scenes live in `scenes/`; `scenes/manifest.json` is loaded at runtime.
Each scene entry:

```json
{
  "id": "jammu-bazaar",
  "title": "Butcher's Bazaar",
  "place": "Jammu, India",
  "year": "c. 1875-1940",
  "credit": "Public Domain, via Wikimedia Commons",
  "sourceUrl": "https://commons.wikimedia.org/wiki/File:...",
  "source": {
    "repository": "Wikimedia Commons (USC Digital Library; Church of Scotland Foreign Missions Committee)",
    "fileUrl": "https://commons.wikimedia.org/wiki/File:...",
    "originalTitle": "Butcher's Bazaar, Jammu, ca.1875-ca.1940 (imp-cswc-GB-237-CSWC47-LS10-028)",
    "date": "1875/1940",
    "place": "Jammu",
    "license": "Public Domain",
    "description": "Photograph of a butcher's bazaar in the city of Jammu. ..."
  },
  "image": "scenes/jammu-bazaar.jpg",
  "original": "scenes/jammu-bazaar-original.jpg",
  "anomaly": "Digital watch",
  "description": "The Butcher's Bazaar in Jammu, photographed between c. 1875 and c. 1940: a market street with small shops and stalls on both sides (catalogue description of the USC Digital Library).",
  "answer": { "x": 0.11, "y": 0.62, "r": 0.03 },
  "hints": [
    "A person wears it on their body.",
    "Left half, lower area.",
    "On the wrist of the man in light clothing on the left: a digital watch."
  ]
}
```

- `answer.x/y`: normalized position of the anomaly (0..1, top-left origin)
  **on the edited image** (`image`), not on the original.
- `answer.r`: normalized hit radius for a perfect 100-point click.
- `image` must keep the original's aspect ratio, otherwise the answer
  coordinates no longer match what the player sees.
- **Provenance discipline:** `source` holds the photo's catalog metadata
  taken from the source record itself (never inferred from the image). The
  `description` paragraph is written strictly from that metadata and plain
  visible content; no invented dates, names, events or context. Version-2
  manifests require `source` on every scene and a root `date`; the runtime
  validator (`manifest.ts`) enforces both.
- Adding a scene = drop `<id>.jpg` + `<id>-original.jpg` into `scenes/`,
  append the manifest entry, done. For pipeline-managed manifests, scenes are
  managed by the fuchs queue instead (see `data/anomalyguessr/` on the
  fuchs box); `scenes/` then only ever holds the current day's 5.

## Sources & licensing

Base photographs are public domain / CC0 images (any era; the license status
is verified per record rather than assumed from age) sourced via Wikimedia
Commons (DPLA collections incl. Seattle Public Library and University of
Colorado, State Library of Queensland, Fortepan, NYPL, USC Digital Library).
Per-scene provenance + file page link live in the `source` block and are
enforced by the validator. Anomalies are either from a LATER time than the photo (anachronisms) or
impossible in any era (fictional creatures, impossible objects); the scene's
`explanation` says which and links a reference. The anomalies are AI edits;
the edited images are new derived works of the base photos.
