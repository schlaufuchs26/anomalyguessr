# AnomalyGuessr 🦊

Daily spot-the-anomaly quiz. Each day presents a fixed set of **5**
public-domain historical photographs, each with exactly **one** anomaly
planted by AI image editing: a modern object, a time-traveling person or
futuristic technology brought from a fictional future (e.g. a robot time
traveler). Click the spot where something doesn't belong; score
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
bun index.html     # dev server (HMR, serves index.html + frontend.tsx)
bun run build      # public Pages site into dist/ (minified React bundle + scenes/)
bun run build:dev  # dev-instance dist/ (same, plus the dev-only surfaces)
bun run checks     # format + tsc + biome + knip + tests
bun run test:e2e   # browser layout smoke tests (Playwright, needs a browser)
```

### Public build vs dev build

One frontend source serves two hosts: GitHub Pages (the public game at
anomalyguessr.com) and the dev instance at `fuchs.science/anomalyguessr/`.
The gallery link, the Live and Moderation modes and the queue API calls are
dev-only (ticket #1374): their code is gated on
`process.env.NODE_ENV !== "production"`, which Bun's minifier resolves at
build time.

- `bun run build` (alias `build:pages`, what the deploy workflow runs)
  defines `NODE_ENV=production`, so the dev-only surfaces are compiled out of
  the bundle, and then runs `scripts/check-public-build.ts`, which fails when
  a marker only dev code emits survives in `dist/`.
- `bun run build:dev` defines `NODE_ENV=development` and keeps them; the dev
  clone is built with it (`scripts/deploy-frontends.sh` in the fuchs repo).
- `bun index.html` (the dev server) and `bun test` run outside production,
  so they exercise the dev surfaces too.

A public build must expose only the frontpage and the daily/quiz path. The
gallery page itself lives in the fuchs dashboard (`dashboard/`), served under
`/anomalyguessr/gallery/` on the dev host; this repo never builds it.

`test:e2e` runs real Chromium at a short laptop viewport (1280x757) against
the dev server: it guesses a scene and asserts the HUD stays a bounded
scroller, the original reveals on a correct guess, and the dev moderation box
stays reachable without the page scrolling (regressions #1185 and #1187, which
happy-dom cannot see). CI installs Playwright's Chromium; on a box that ships
Chromium via nix (the fuchs host), set `PLAYWRIGHT_CHROMIUM_PATH`, or rely on
the default `/home/exedev/.nix-profile/bin/chromium` when it exists.

## Repo layout

| Path | What it is |
|------|------------|
| repo root | the React frontend (`frontend.tsx`, `src/`, `scenes/`, `tests/`, `e2e/`) |
| `api/` | the TypeScript/Bun HTTP service behind `/anomalyguessr/api/` (manifest, scenes, moderation, on-demand generation); its own package with its own `bun.lock` |
| `pipeline/` | the Python content pipeline (`ag_generate.py` and friends) plus its `unittest` suites |
| `data/` | runtime data the pipeline and the API share: `state.json`, `feedback.json`, image caches |

`data/` is gitignored: it is a local cache (a few hundred MB of images) and
this repo is public. The fuchs box back it up in its nightly
`scripts/backup.sh` tarball.

```sh
cd api && bun install && bun run checks   # API: format + tsc + biome + tests
python3 -m unittest discover -s pipeline -t pipeline -p 'test_ag_*.py'
```

The content pipeline is two halves (ticket #1372). **Sourcing**
(`pipeline/ag_sources.py`) fills a pool of source photos from Wikimedia
Commons' "Quality images" assessment category, filtered on structured keys
only (license, quality rating, MIME/format, pixel size); its `top-up`
command walks the category from a persisted cursor. **Generation**
(`pipeline/ag_generate.py`) turns one pooled photo into one queued scene with
a four-step model flow: a `deepseek-v4.1-flash` vision call proposes a
subtle time-travel anomaly for that photo, a `gemini-3.1-flash-image` call
applies it, another vision call returns the click target, and a final vision
call checks the scene against the requirements list (one correction edit is
allowed). Each scene gets a trace sidecar under `data/anomalyguessr/traces/`
with the full prompt and answer of the calls that decided its content.

## Stack

React 19 + Bun + TypeScript on the house `frontend-template` setup (ticket
#1172): `frontend.tsx` is the app shell (run state, HUD, navigation,
moderation), `src/PhotoStage.tsx` the photo layer, `src/photoInteractions.ts`
the zoom/pan/click math, and `manifest.ts` / `scoring.ts` / `layout.ts` /
`daily.ts` stay plain TS logic. Tests run on happy-dom + Testing Library
(`tests/app.test.tsx` drives the real click → reveal → compare → end-screen
flows); `e2e/layout.playwright.ts` adds the real-browser layout smoke tests.
GitHub Pages deploys `dist/` on every push to `main`.

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
  phone in a parade crowd, a time-traveling jogger, a robot traveler in a
  station crowd), not a fixed object set. The anomaly type matches the
  scene's density: a person among crowds, an object among clutter.
- All source photos are landscape (wider than tall).

## Daily selection

Two manifest generations share one frontend:

- **Version 1 (legacy pool):** the manifest holds every known scene. The
  frontend derives the day's set from the browser's local calendar date
  (`YYYY-MM-DD`): the date string hashes to a seed for a deterministic shuffle
  (mulberry32), from which the first 5 scenes are taken. Same date → same set
  and order for everyone; consecutive days rotate the set.
- **Version 2 (pipeline daily manifest):** the daily content pipeline
  (`pipeline/`, started by a cron on the fuchs host) ships exactly the day's 5
  scenes in play order inside the manifest (root `date` = the quiz day, `YYYY-MM-DD`). The
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
  managed by the pipeline queue instead (see `data/anomalyguessr/`, untracked);
  `scenes/` then only ever holds the current day's 5.

## Sources & licensing

Base photographs are public domain / CC0 images (any era; the license status
is verified per record rather than assumed from age) sourced via Wikimedia
Commons (DPLA collections incl. Seattle Public Library and University of
Colorado, State Library of Queensland, Fortepan, NYPL, USC Digital Library).
Per-scene provenance + file page link live in the `source` block and are
enforced by the validator. Anomalies are either from a LATER time than the
photo (anachronisms) or futuristic technology from a fictional future
(robot time travelers); flying saucers and pure fantasy creatures are not
used (time-travel framing, ticket #1161). The scene's `explanation` says
which and links a reference. The anomalies are AI edits;
the edited images are new derived works of the base photos.
