#!/usr/bin/env python3
"""Machine-readable AnomalyGuessr anomaly catalog (ticket #1169).

The human-facing catalog lives in `wiki/entries/anomalyguessr-anomalies.md`;
the deterministic generator (`pipeline/ag_generate.py`) cannot parse prose, so
this module is the structured mirror. Keep the two in sync when a recipe
works or fails: the wiki page stays the reference for the reasoning (risk
notes, run notes, era table), this file is the data the pipeline uses.

Since the #1372 rebuild the generator no longer *picks* from this catalog: a
creative model proposes an anomaly per photo. The catalog now serves three
jobs (all documented below):

- ``INSPIRATION``: a few-shot example list for the proposal prompt, so the
  model sees the shape of a good anomaly without being limited to it.
- curated text: when the model's label matches a catalog entry, that entry's
  vetted ``explanation``/``references`` are reused instead of the model's.
- ``family_of``: the variety bucket (ship-time distinctness, ag_queue) and
  the one-time ``backfill_family`` resolver for older scene labels.

An entry mirrors one catalog row:

    label        English `anomaly` value written into the scene entry
    type         "object" | "person" | "future"
    family       variety bucket for the >=6-distinct-labels rule (drinks
                 container = one family, all persons = one family, robots =
                 one family)
    settings     where the element fits ("market", "street", "station",
                 "harbor", "crowd"); an empty tuple = any setting
    recipe       placement recipe key (see RECIPES in ag_generate.py)
    size         real-world size written into the prompt (objects) or the
                 rendered-height budget (person/future)
    min_year     earliest plausible decade for an anachronism; the element
                 may only be planted when min_year > photo year (era rule,
                 #1136/#1122). None = no era check (fictional future, #1161).
                 The #1372 generator does not enforce this any more (a model
                 judges the era); the field documents the era anchor.
    risk         "low" | "medium" | "high" (documentation)
    scale_max    optional numeric budget (fraction of image height) that the
                 old scale gate enforced; the #1372 checker owns scale now,
                 so this stays as the catalog's numeric note (ticket #1328).
                 Must agree with the largest percent number in the entry's
                 prose `scale`.
    explanation  English sentence for the scene's `explanation` field
    references   [{label, url}] for every factual claim in the explanation.
                 A reference must be a deep link to a page (or section) that
                 states the claim, ideally a primary or authoritative source
                 (patent, museum, manufacturer history, archival record), not
                 a portal, search page or repository root. Every dated token
                 in the explanation (year, decade, century) has to appear on
                 one of the cited pages; where no such source exists, the
                 sentence must not assert the date (ticket #1624, checked by
                 pipeline/ag_references.py and the audit
                 pipeline/ag_reference_audit.py).
    tells        person/future only: the 1-2 modern tells in the prompt
    noun         short English noun for the prompt/hint text

Only time-travel-readable anomalies are listed: real later-era objects and
persons, or clearly futuristic technology (robots/gadgets, #1161). No UFOs,
no fantasy creatures.
"""

import re

# Placement recipes (prompt phrase, hint phrase). The #1372 generator puts
# the model's own placement sentence into the edit prompt, so these are the
# reference wording for the wiki + the catalog's own notes.
RECIPES = {
    "ground": (
        "on the ground at the very bottom edge of the photo, half hidden "
        "behind a large foreground object such as a cart wheel, crate, "
        "barrel or sack, in its shadow, like a forgotten piece of litter at "
        "the scene's edge; not on a table, not centered, not in the open",
        "at the bottom edge of the photo, half hidden behind a foreground "
        "object",
    ),
    "stall": (
        "on a stall among the wares, half hidden behind the goods, in "
        "shadow; not in the foreground, not centered",
        "among the goods on one of the stalls",
    ),
    "ground_mid": (
        "in the middle distance on the ground, half hidden behind a crate, "
        "barrel, basket or coil of rope, in shadow",
        "in the middle distance, partly hidden behind a crate or barrel",
    ),
    "wall": (
        "against a wall or post in the background, partly behind other "
        "objects or people; not in the foreground",
        "in the background, leaning against a wall or post",
    ),
    "deck": (
        "on the deck or quay edge among the crates and coiled rope, in "
        "shadow, half hidden; not in the foreground",
        "on the deck or quay, half hidden among crates and rope",
    ),
    "bench": (
        "on the platform ground beside a bench or seat, half hidden behind "
        "a bench leg or luggage, in shadow",
        "on the ground near a bench or seat, partly hidden",
    ),
    "person_mid": (
        "mid-ground within the crowd or group, slightly behind or between "
        "other people, in a natural pose such as walking, standing and "
        "talking, or looking at something off-frame; not centered, not "
        "looking at the camera, not in the foreground, not the largest "
        "figure in the frame",
        "in the middle of the crowd, half behind other people",
    ),
    "person_far": (
        "in the far background among the crowd, half hidden behind taller "
        "people, seen from behind or in profile; small, not in the "
        "foreground",
        "in the background among the crowd, partly hidden by other people",
    ),
}

# Type 1: planted modern objects. label/size/recipe from the wiki tables;
# min_year from the era anchors section.
_OBJECTS = [
    # Markets, stalls, shops, bazaars
    dict(label="Plastic bottle (clear PET)", type="object", family="drinks",
         settings=("market", "street", "harbor"), recipe="ground",
         size="a clear PET water bottle, 20-30 cm tall", min_year=1970,
         risk="low",
         explanation="Clear PET plastic bottles date from Nathaniel Wyeth's "
                     "1973 patent and only came into common use later, "
                     "decades after this photograph was taken.",
         references=[{"label": "Nathaniel Wyeth (PET bottle patent, 1973)",
                      "url": "https://en.wikipedia.org/wiki/Nathaniel_Wyeth_(inventor)"}]),
    dict(label="Paper coffee cup with lid", type="object", family="drinks",
         settings=("market", "street"), recipe="stall",
         size="a white paper takeaway coffee cup with a lid, 10-12 cm tall",
         # The lid, not the cup, is the anachronism: the paper cup is old
         # (about 1912) but the disposable drink-through lid was patented in
         # 1967. Anchoring the labeled artifact at the cup's year made the
         # audit call a lidded cup in 1910 merely "same-year" (ticket #1417).
         min_year=1967, risk="low",
         explanation="The paper cup is old, but the disposable lid is not: "
                     "the first coffee-cup-lid patent was filed in 1967, so a "
                     "lidded takeaway cup cannot appear in this photograph.",
         references=[{"label": "Coffee cup (lid patents from 1967)",
                      "url": "https://en.wikipedia.org/wiki/Coffee_cup"},
                     {"label": "Paper cup",
                      "url": "https://en.wikipedia.org/wiki/Paper_cup"}]),
    dict(label="Drink can (matte aluminium)", type="object", family="drinks",
         settings=("market", "street", "harbor"), recipe="ground",
         size="a matte aluminium drink can, about 12 cm tall", min_year=1950,
         risk="high",
         explanation="Aluminium beverage cans only appeared in 1958; one "
                     "could not have been in this photograph.",
         references=[{"label": "History of aluminium (drinks cans from 1958)",
                      "url": "https://en.wikipedia.org/wiki/History_of_aluminium"}]),
    dict(label="Plastic bag (white, with handles)", type="object", family="plastic",
         settings=("market", "street"), recipe="ground_mid",
         size="a white plastic carrier bag with handles, 40-50 cm",
         min_year=1960, risk="medium",
         explanation="Lightweight plastic carrier bags are a product of the "
                     "1960s and later, so none could appear in this photo.",
         references=[{"label": "Plastic shopping bag",
                      "url": "https://en.wikipedia.org/wiki/Plastic_shopping_bag"}]),
    dict(label="Polystyrene cup", type="object", family="drinks",
         settings=("market", "street"), recipe="stall",
         size="a white polystyrene cup, 10-12 cm tall", min_year=1950,
         risk="medium",
         explanation="Expanded polystyrene foam cups were first mass-produced "
                     "in 1960, so one cannot belong to this photograph.",
         references=[{"label": "Museum of Design in Plastics: the EPS cup",
                      "url": "https://www.modip.ac.uk/blog/2020/09/dart-and-humble-disposable-eps-cup"}]),
    dict(label="Chip bag with logo", type="object", family="packaging",
         settings=("market", "street"), recipe="stall",
         size="a printed snack bag, 25-30 cm", min_year=1950, risk="medium",
         explanation="Printed foil snack bags with brand logos are a "
                     "20th-century product, not yet in use here.",
         references=[{"label": "Potato chip",
                      "url": "https://en.wikipedia.org/wiki/Potato_chip"}]),
    dict(label="Carton drink box", type="object", family="packaging",
         settings=("market", "street"), recipe="stall",
         size="a laminated carton drink box, about 20 cm", min_year=1950,
         risk="medium",
         explanation="Laminated carton drink boxes go back to Tetra Pak's "
                     "first filling machine in 1952, after this photograph "
                     "was taken.",
         references=[{"label": "Tetra Pak (first carton 1952)",
                      "url": "https://en.wikipedia.org/wiki/Tetra_Pak"}]),
    dict(label="Barcode price tag", type="object", family="print",
         settings=("market",), recipe="stall",
         size="a small printed price tag with a barcode, 6-8 cm", min_year=1975,
         risk="high",
         explanation="Barcodes only came into retail use in the 1970s, so a "
                     "barcode tag cannot belong to this scene.",
         references=[{"label": "Barcode",
                      "url": "https://en.wikipedia.org/wiki/Barcode"}]),
    # Streets, sidewalks, squares
    dict(label="Traffic cone (orange)", type="object", family="street-furniture",
         settings=("street", "market"), recipe="ground_mid",
         size="an orange traffic cone, 45-70 cm tall", min_year=1940,
         scale="roughly 2-3 percent of the image height, never more than 4 percent",
         scale_max=0.04, risk="medium",
         explanation="Traffic cones are a road-safety marker; the moulded "
                     "cones in use today were first made in the 1940s, after "
                     "this scene.",
         references=[{"label": "The History of Traffic Cones",
                      "url": "https://transportationhistory.org/2020/04/20/national-work-zone-awareness-week-nwzaw-the-history-of-traffic-cones/"}]),
    dict(label="Modern bicycle", type="object", family="street-furniture",
         settings=("street", "market"), recipe="wall",
         size="a modern road bicycle, about 110 cm tall", min_year=1975,
         scale="roughly 8-12 percent of the image height, never a foreground object",
         scale_max=0.12, risk="medium",
         explanation="Derailleur-geared modern bicycles are a later-20th-"
                     "century development that this photo predates.",
         references=[{"label": "Bicycle",
                      "url": "https://en.wikipedia.org/wiki/Bicycle"}]),
    dict(label="Blue plastic tarp", type="object", family="plastic",
         settings=("street", "harbor", "market"), recipe="ground_mid",
         size="a blue plastic tarpaulin, 1-2 m across", min_year=1950,
         scale="roughly 3-5 percent of the image height, never more than 6 percent",
         scale_max=0.06, risk="medium",
         explanation="Woven polyethylene tarpaulins only appeared in the "
                     "1950s, after this photograph.",
         references=[{"label": "The History of the Modern Tarp",
                      "url": "https://tarps.com/blogs/news/where-did-tarps-come-from"}]),
    dict(label="Modern advertising poster", type="object", family="print",
         settings=("street", "market"), recipe="wall",
         size="a modern printed advertising poster, 50-80 cm", min_year=1960,
         scale="roughly 3-5 percent of the image height, never a foreground object",
         scale_max=0.05, risk="medium",
         explanation="Offset-printed advertising posters with modern "
                     "typography belong to a later era than this photo.",
         references=[{"label": "Poster",
                      "url": "https://en.wikipedia.org/wiki/Poster"}]),
    dict(label="Caution tape", type="object", family="street-furniture",
         settings=("street",), recipe="wall",
         size="a roll of yellow-black barrier tape", min_year=1950,
         scale="a thin ribbon, at most 2 percent of the image height",
         scale_max=0.02, risk="high",
         explanation="Plastic barricade tape is a 1960s safety product that "
                     "did not exist when this photo was taken.",
         references=[{"label": "A Brief History of Police Tape",
                      "url": "https://www.atlasobscura.com/articles/a-brief-history-of-police-tape-or-why-humans-ignore-warnings"}]),
    dict(label="E-scooter", type="object", family="vehicle",
         settings=("street",), recipe="wall",
         size="a shared electric kick scooter, about 110 cm tall", min_year=2015,
         scale="roughly 6-10 percent of the image height, a background object",
         scale_max=0.10, risk="high",
         explanation="Electric kick scooters only appeared in the 2010s, over "
                     "a century after this photograph.",
         references=[{"label": "Motorized scooter",
                      "url": "https://en.wikipedia.org/wiki/Motorized_scooter"}]),
    # Stations, travel, transport
    dict(label="Wheeled suitcase", type="object", family="luggage",
         settings=("station", "street"), recipe="bench",
         size="a wheeled suitcase, 60-75 cm tall", min_year=1970,
         scale="roughly 3-5 percent of the image height, never a foreground object",
         scale_max=0.05, risk="medium",
         explanation="Rolling suitcases with wheels only became common in the "
                     "1970s, after this photograph was taken.",
         references=[{"label": "Suitcase",
                      "url": "https://en.wikipedia.org/wiki/Suitcase"}]),
    dict(label="Modern daypack", type="object", family="luggage",
         settings=("station", "street", "market"), recipe="bench",
         size="a modern nylon daypack, 45-55 cm", min_year=1960,
         scale="roughly 2-4 percent of the image height",
         scale_max=0.04, risk="medium",
         explanation="Lightweight nylon daypacks with zips are a later-20th-"
                     "century product, not yet in use here.",
         references=[{"label": "Backpack",
                      "url": "https://en.wikipedia.org/wiki/Backpack"}]),
    dict(label="Over-ear headphones", type="object", family="electronics",
         settings=("station", "street"), recipe="bench",
         size="over-ear headphones, about 20 cm", min_year=1960, risk="high",
         scale="roughly 2-3 percent of the image height", scale_max=0.03,
         explanation="Over-ear headphones are an electronic audio product "
                     "from a later era than this photograph.",
         references=[{"label": "Headphones",
                      "url": "https://en.wikipedia.org/wiki/Headphones"}]),
    # Harbors, waterfront, boats
    dict(label="Shipping container", type="object", family="container",
         settings=("harbor",), recipe="ground_mid",
         size="a corrugated steel shipping container, about 2.5 m tall",
         min_year=1950, risk="medium",
         scale="roughly 6-10 percent of the image height, a background element at the quay",
         scale_max=0.10,
         explanation="Standardized steel shipping containers only appeared in "
                     "the 1950s, after this photograph.",
         references=[{"label": "Intermodal container",
                      "url": "https://en.wikipedia.org/wiki/Intermodal_container"}]),
    dict(label="Plastic cooler box", type="object", family="container",
         settings=("harbor",), recipe="deck",
         size="a plastic cooler box, 50-60 cm", min_year=1950, risk="medium",
         scale="roughly 2-4 percent of the image height", scale_max=0.04,
         explanation="Moulded plastic cooler boxes are a 1950s product and "
                     "cannot belong to this scene.",
         references=[{"label": "Cooler (portable cooler, 1950s)",
                      "url": "https://en.wikipedia.org/wiki/Cooler"}]),
    dict(label="Nylon rope (colored)", type="object", family="cordage",
         settings=("harbor",), recipe="deck",
         size="a coil of brightly colored synthetic rope, 30-40 cm across",
         min_year=1940, risk="medium",
         scale="roughly 2-3 percent of the image height", scale_max=0.03,
         explanation="Nylon, first made in 1935, only replaced natural-fibre "
                     "rope later; it cannot belong to this scene.",
         references=[{"label": "Nylon (first made 1935)",
                      "url": "https://en.wikipedia.org/wiki/Nylon"}]),
    # Household / consumer goods (ticket #1328: the catalog was thin outside
    # drinks/luggage, so those families dominated every run).
    dict(label="Disposable lighter", type="object", family="household",
         settings=("market", "street"), recipe="stall",
         size="a small disposable cigarette lighter, about 8 cm", min_year=1970,
         risk="medium",
         explanation="Disposable lighters are a later-20th-century "
                     "mass-market product and cannot belong to this scene.",
         references=[{"label": "Lighter (disposable lighters, 20th century)",
                      "url": "https://en.wikipedia.org/wiki/Lighter"}]),
    dict(label="Folding nylon umbrella", type="object", family="household",
         settings=("street", "market", "station"), recipe="ground_mid",
         size="a folded nylon umbrella, about 60 cm long", min_year=1950,
         risk="medium",
         explanation="Lightweight nylon umbrellas with steel ribs are a "
                     "1950s product, later than this photo.",
         references=[{"label": "Umbrella (nylon folding umbrella, 1950s)",
                      "url": "https://en.wikipedia.org/wiki/Umbrella"}]),
    dict(label="Plastic bucket", type="object", family="container",
         settings=("market", "harbor", "street"), recipe="ground_mid",
         size="a moulded plastic bucket, about 30 cm tall", min_year=1950,
         risk="medium",
         explanation="Moulded plastic buckets belong to the plastics age: "
                     "cheap plastic containers only spread in the 1950s, "
                     "after this photograph.",
         references=[{"label": "The Age of Plastic (Science Museum)",
                      "url": "https://www.sciencemuseum.org.uk/objects-and-stories/chemistry/age-plastic-parkesine-pollution"}]),
    dict(label="Stackable plastic crate", type="object", family="container",
         settings=("market", "harbor"), recipe="ground_mid",
         size="a stackable plastic crate, about 30 cm tall", min_year=1950,
         risk="medium",
         explanation="Stackable plastic crates go back to the injection-"
                     "moulded milk crate of the 1950s, not yet in use in "
                     "this photo.",
         references=[{"label": "Milk crate (injection-moulded, 1950s)",
                      "url": "https://en.wikipedia.org/wiki/Milk_crate"}]),
    # Sports and electronics: distinct silhouettes, none of them a bottle.
    dict(label="Bicycle helmet", type="object", family="sports",
         settings=("street", "station"), recipe="ground_mid",
         size="a hard-shell bicycle helmet, about 25 cm across", min_year=1975,
         risk="medium",
         explanation="Hard-shell bicycle helmets only became common in the "
                     "1970s, decades after this photograph.",
         references=[{"label": "Bicycle helmet",
                      "url": "https://en.wikipedia.org/wiki/Bicycle_helmet"}]),
    dict(label="Hula hoop", type="object", family="sports",
         settings=("street", "market"), recipe="ground_mid",
         size="a plastic hula hoop, about 80 cm across", min_year=1958,
         risk="medium",
         explanation="The plastic hula hoop was a 1958 craze; it could not "
                     "have been in this scene.",
         references=[{"label": "Hula hoop",
                      "url": "https://en.wikipedia.org/wiki/Hula_hoop"}]),
    dict(label="Portable transistor radio", type="object", family="electronics",
         settings=("market", "station", "street"), recipe="stall",
         size="a small portable transistor radio, about 20 cm wide",
         min_year=1954, risk="medium",
         explanation="Portable transistor radios only appeared in the "
                     "mid-1950s, later than this photograph.",
         references=[{"label": "Transistor radio",
                      "url": "https://en.wikipedia.org/wiki/Transistor_radio"}]),
    dict(label="Pocket calculator", type="object", family="electronics",
         settings=("market", "station"), recipe="stall",
         size="a pocket calculator, about 15 cm wide", min_year=1971,
         risk="medium",
         explanation="Pocket calculators are a 1970s electronic device that "
                     "did not exist when this photo was taken.",
         references=[{"label": "Calculator",
                      "url": "https://en.wikipedia.org/wiki/Calculator"}]),
    dict(label="Compact digital camera", type="object", family="electronics",
         settings=("station", "street", "market"), recipe="bench",
         size="a compact digital camera, about 10 cm wide", min_year=1990,
         risk="medium",
         explanation="Digital cameras only became consumer products in the "
                     "1990s, a century after this photograph.",
         references=[{"label": "Digital camera",
                      "url": "https://en.wikipedia.org/wiki/Digital_camera"}]),
    dict(label="Credit card", type="object", family="money",
         settings=("market", "station"), recipe="stall",
         size="a printed plastic payment card, about 9 cm wide", min_year=1950,
         risk="high",
         explanation="Plastic payment cards only appeared in the 1950s; "
                     "Bank of America's BankAmericard came in 1958, so one "
                     "cannot belong to this scene.",
         references=[{"label": "Credit card (BankAmericard, 1958)",
                      "url": "https://en.wikipedia.org/wiki/Credit_card"}]),
    dict(label="Steel vacuum flask", type="object", family="container",
         settings=("market", "station", "street"), recipe="stall",
         size="a steel vacuum flask, about 25 cm tall", min_year=1904,
         risk="medium",
         explanation="The vacuum flask was only patented in 1904, after this "
                     "photograph was taken.",
         references=[{"label": "Vacuum flask",
                      "url": "https://en.wikipedia.org/wiki/Vacuum_flask"}]),
    # Harbor/waterfront, which had only three entries before.
    dict(label="Synthetic life jacket", type="object", family="clothing",
         settings=("harbor",), recipe="deck",
         size="a bright orange synthetic life jacket, about 50 cm",
         min_year=1960, risk="medium",
         explanation="Bright foam-filled synthetic life jackets are a "
                     "mid-20th-century safety product, not yet in use here.",
         references=[{"label": "All about life jackets (synthetic foams)",
                      "url": "https://www.martide.com/en/blog/all-about-life-jackets"}]),
]

# Numeric twin of the prompt's scale budget (ticket #1328): the maximum
# rendered height fraction the scale gate accepts before it rejects a scene.
# Entries whose prompt budget is the small-object default carry no
# `scale_max`; the per-type defaults below match the prompt wording
# (DEFAULT_OBJECT_SCALE "never more than 3 percent", DEFAULT_PERSON_SCALE
# "8-15 percent"). test_ag_catalog checks that the numeric agrees with the
# largest percent number in an entry's prose `scale`.
DEFAULT_OBJECT_SCALE_MAX = 0.03
DEFAULT_PERSON_SCALE_MAX = 0.15


def scale_max(entry: dict) -> float:
    if entry["type"] == "person" or entry["recipe"].startswith("person"):
        return entry.get("scale_max", DEFAULT_PERSON_SCALE_MAX)
    return entry.get("scale_max", DEFAULT_OBJECT_SCALE_MAX)


# Type 2: time-traveler person (a NEW person among the existing people).
_PERSONS = [
    dict(label="Time traveler: young man in a suit", type="person",
         family="person", settings=("street", "market", "station", "crowd"),
         recipe="person_mid",
         size="roughly 8-15 percent of the image height", min_year=1960,
         risk="medium", noun="young man in a period-appropriate suit",
         tells="modern athletic sneakers visible under the trousers and "
               "sunglasses pushed up in his hair",
         explanation="Modern athletic sneakers and plastic sunglasses are "
                     "20th-century products; a man wearing them does not "
                     "belong in this photograph.",
         references=[{"label": "Sneakers",
                      "url": "https://en.wikipedia.org/wiki/Sneakers"},
                     {"label": "Sunglasses",
                      "url": "https://en.wikipedia.org/wiki/Sunglasses"}]),
    dict(label="Time traveler: young woman with modern accessories",
         type="person", family="person",
         settings=("street", "market", "crowd"), recipe="person_mid",
         size="roughly 8-15 percent of the image height", min_year=1960,
         risk="medium", noun="young woman in period-appropriate dress",
         tells="modern sunglasses pushed into her hair and a small nylon "
               "shoulder bag with plastic clips",
         explanation="Her plastic sunglasses and nylon bag with plastic "
                     "fittings are later-20th-century items, not yet in use "
                     "here.",
         references=[{"label": "Sunglasses",
                      "url": "https://en.wikipedia.org/wiki/Sunglasses"},
                     {"label": "Nylon",
                      "url": "https://en.wikipedia.org/wiki/Nylon"}]),
    dict(label="Time traveler: tourist", type="person", family="person",
         settings=("street", "crowd"), recipe="person_far",
         size="roughly 8-15 percent of the image height", min_year=1970,
         risk="medium", noun="casual tourist in a light jacket",
         tells="a baseball cap with a printed logo and sunglasses pushed up "
               "on it",
         explanation="Baseball caps with printed logos and plastic sunglasses "
                     "belong to a later era than this photograph.",
         references=[{"label": "Baseball cap",
                      "url": "https://en.wikipedia.org/wiki/Baseball_cap"}]),
    dict(label="Time traveler: schoolgirl with headphones", type="person",
         family="person", settings=("street", "market", "station"),
         recipe="person_mid", size="roughly 8-15 percent of the image height",
         min_year=1960, risk="medium",
         noun="schoolgirl in a period dress and coat",
         tells="over-ear headphones around her neck and modern sneakers",
         explanation="Over-ear headphones for personal listening only became "
                     "common in the 1960s, later than this scene's era.",
         references=[{"label": "Headphones (personal listening, 1960s)",
                      "url": "https://en.wikipedia.org/wiki/Headphones"}]),
]

# Type 3: fictional-future / robot time travelers (ticket #1161). No era
# check: the element is from no real era, but it must read as future
# technology and follow the blend/scale rules.
_FUTURES = [
    dict(label="Robot time traveler", type="future", family="robot",
         settings=("street", "market", "crowd"), recipe="person_mid",
         size="roughly 8-15 percent of the image height", min_year=None,
         risk="high", noun="clearly futuristic humanoid robot",
         tells="sleek metallic limbs and an exposed mechanical head, with an "
               "otherwise era-appropriate silhouette",
         explanation="This is a robot from a fictional future, so it could "
                     "not exist when this photograph was taken.",
         references=[{"label": "Robot",
                      "url": "https://en.wikipedia.org/wiki/Robot"},
                     {"label": "Humanoid robot",
                      "url": "https://en.wikipedia.org/wiki/Humanoid_robot"}]),
    dict(label="Futuristic gadget", type="future", family="robot",
         settings=("market", "street", "station", "harbor"),
         recipe="ground_mid",
         size="a sleek metallic handheld device, about 20 cm", min_year=None,
         scale="about 2 percent of the image height (roughly 20-30 pixels on a 1200-pixel-tall image), never more than 3 percent",
         scale_max=0.03,
         risk="high", noun="sleek metallic device of unknown future design",
         tells="a smooth metal-and-glass body with a small unlit indicator, "
               "no buttons or seams of its era",
         explanation="This device is advanced technology from a fictional "
                     "future and cannot belong to this photograph.",
         references=[{"label": "Robot",
                      "url": "https://en.wikipedia.org/wiki/Robot"}]),
]

CATALOG = _OBJECTS + _PERSONS + _FUTURES


def by_type(kind: str) -> list:
    return [e for e in CATALOG if e["type"] == kind]


def labels() -> list:
    return [e["label"] for e in CATALOG]


def entry_for_label(label) -> dict | None:
    """The catalog entry whose label is exactly ``label``, or None.

    Exact match only: the generator reuses a matched entry's curated
    explanation and references, and a near match would attach the wrong
    citation. Case-insensitive because the models capitalize freely.
    """
    if not isinstance(label, str) or not label:
        return None
    low = label.strip().lower()
    for e in CATALOG:
        if e["label"].lower() == low:
            return e
    return None


def references_for_family(family: str) -> list:
    """Curated references of the first catalog entry in a family, [] if none.

    Fallback for a model-proposed anomaly whose own references are unusable:
    a same-family catalog entry's citations are a reasonable stand-in for the
    era claim. The generator logs when this path fires (trace sidecar).
    """
    for e in CATALOG:
        if e["family"] == family:
            return [dict(r) for r in e["references"]]
    return []


# Few-shot inspiration for the creative proposal call (#1372). Not a
# mandate: the model invents an anomaly for the actual photo, these lines
# only show the shape of a good one. The three kinds the game uses are
# represented (later-era object, time-traveler person, fictional-future
# technology), plus a hint at the plausible-era anchor each needs.
INSPIRATION = (
    "Plastic bottle (clear PET); a later-era object, transparent PET bottles "
    "became common in the 1970s",
    "Paper coffee cup with lid; a later-era object, disposable lidded cups "
    "are a 20th-century convenience",
    "Wheeled suitcase; a later-era object, rolling suitcases only became "
    "common in the 1970s",
    "Portable transistor radio; a later-era object, the first ones appeared "
    "in the mid-1950s",
    "Time traveler: young man in a suit; a modern person among the crowd "
    "with modern sneakers as the only tell",
    "Robot time traveler; a fictional-future humanoid, clearly not from this "
    "era and not a fantasy creature",
)


def inspiration_lines(n: int = 6) -> list:
    return list(INSPIRATION[:max(0, int(n))])


# label -> family for the current catalog. The ship step reads the family
# stored in each scene entry (ticket #1232); this map is the resolver used to
# backfill entries that predate the field.
FAMILIES_BY_LABEL = {e["label"]: e["family"] for e in CATALOG}

# Retired anomaly labels still present in the queue's back catalogue. The
# catalog was rewritten once (English-only rename, #1169), so older scenes
# carry labels that no longer exist; they keep their historical family so the
# one-time backfill (ag_queue.backfill_family) resolves them instead of
# leaving them without a variety bucket. Add a row here when a backfill
# reports an unknown label; keep the map free of labels the catalog still has.
FAMILY_ALIASES = {
    "Plastic bottle": "drinks",
    "Plastic bottle (PET)": "drinks",
    "Plastic water bottle (PET)": "drinks",
    "Paper coffee cup": "drinks",
    "Drink can": "drinks",
    "Plastic bag": "plastic",
    "Tetra Pak carton": "packaging",
    "Chip bag (modern snack bag)": "packaging",
    "Smartphone": "electronics",
    "Digital watch": "electronics",
    "Cooler box": "container",
    "Roller suitcase": "luggage",
    # UFOs (removed from the catalog 2026-09-10, #1161) are visually distinct
    # from the robot family, so they get their own retired bucket instead of
    # colliding with the future-tech anomalies.
    "Flying saucer (UFO)": "ufo",
    "UFO (flying saucer)": "ufo",
}

# Element vocabulary for the proposal-time gates (ticket #1473). The model
# invents labels the fixed catalog cannot list ("Disposable plastic ballpoint
# pen"), so a keyword table buckets the common element nouns into a variety
# family and records the settings that element can plausibly sit in.
# ``family_of`` uses it as a fallback, which makes the collision check catch
# reworded repeats ("ballpoint pen" vs "Disposable plastic ballpoint pen");
# ``settings_for_label`` feeds the setting-fit gate. Order matters where
# keywords overlap (first family wins): "cable" is cordage, not electronics.
FAMILY_KEYWORDS = (
    ("person", ("street", "market", "station", "crowd"),
     ("time traveler", "person", "man", "woman", "boy", "girl", "child",
      "tourist", "visitor")),
    ("robot", ("street", "market", "station", "crowd"),
     ("robot", "droid", "android", "humanoid", "cyborg")),
    ("vehicle", ("street", "market", "station", "harbor"),
     ("scooter", "bicycle", "bike", "motorcycle", "car", "automobile",
      "truck", "van", "bus", "tram", "tractor", "motor", "engine")),
    ("drinks", ("market", "street", "station", "harbor"),
     ("bottle", "cup", "mug", "glass", "can", "flask", "thermos",
      "tumbler")),
    ("packaging", ("market", "street", "station"),
     ("wrapper", "packet", "crisp", "snack", "foil", "polystyrene",
      "styrofoam", "carton", "tetrapak")),
    ("plastic", ("market", "street", "harbor"),
     ("bag", "sack", "tarpaulin", "tarp", "film", "wrap", "sheeting")),
    ("cordage", ("harbor", "street", "market", "station"),
     ("rope", "cord", "string", "wire", "cable", "sling", "tie", "strap",
      "zip", "clip", "clamp", "chain")),
    ("luggage", ("station", "street", "market"),
     ("suitcase", "backpack", "daypack", "rucksack", "baggage", "luggage")),
    ("clothing", ("street", "market", "crowd", "station"),
     ("jacket", "shirt", "jeans", "shoe", "sneaker", "hoodie", "headband",
      "wristband", "bracelet", "cap", "hat", "sunglasses")),
    ("stationery", ("market", "street", "station", "crowd"),
     ("ballpoint", "pen", "pencil", "marker", "biro", "crayon", "chalk",
      "notebook", "stationery", "eraser")),
    ("container", ("harbor", "market", "station"),
     ("container", "crate", "cooler", "barrel", "box", "bin", "basket",
      "tin")),
    ("print", ("market", "street", "station"),
     ("poster", "flyer", "barcode", "label", "sticker", "sign", "graffiti",
      "advertisement", "leaflet")),
    ("electric", ("street", "station", "market", "harbor"),
     ("led", "solar", "lamp", "bulb", "light", "battery", "generator",
      "panel", "antenna", "speaker", "charger", "powerbank")),
    ("electronics", ("market", "street", "station", "harbor", "crowd"),
     ("smartphone", "phone", "camera", "headphone", "earbud", "earphone",
      "radio", "laptop", "computer", "tablet", "watch", "screen", "gopro",
      "drone", "quadcopter", "television", "gps", "sensor")),
)

# Setting vocabulary for the setting-fit gate (ticket #1473): the words in a
# scene's text (source title/description, the proposal's title) that mark one
# of the catalog's settings. A word is matched as a substring of the lowered
# text, so "Seaside ... Harbor" reads as harbor and a duel or boxing scene
# reads as none.
SETTING_KEYWORDS = (
    ("harbor", ("harbor", "harbour", "port", "quay", "dock", "pier", "ship",
                "boat", "steamship", "sea", "seaside", "coast", "waterfront",
                "marina", "vessel", "navire", "bateau", "porto")),
    ("station", ("station", "gare", "bahnhof", "platform", "railway",
                 "railroad", "train", "tracks", "tram")),
    ("market", ("market", "marketplace", "markt", "marché", "marche",
                "bazaar", "bazar", "halle", "halles", "stall", "fair",
                "shop", "store", "vendor")),
    ("crowd", ("crowd", "procession", "parade", "festival", "ceremony",
               "celebration", "demonstration", "foule", "spectators",
               "audience")),
    ("street", ("street", "rue", "strasse", "straße", "road", "boulevard",
                "avenue", "square", "bridge", "alley", "sidewalk",
                "pavement", "plaza")),
)

# Elements below this rendered height fraction (of the image height) are
# inherently too small for a spot-the-anachronism search, whatever placement
# they get (ticket #1473). The checker's requirement 3 states the same 2
# percent as a floor; the proposal gate refuses such an element up front.
MIN_ELEMENT_SCALE = 0.02

# Head nouns that name an inherently tiny element even without a catalog
# entry (ticket #1473). A pen, sticker or earbud is only findable when the
# player already knows where it is, so the proposal is re-asked for a bigger
# element instead of shipping a search target that cannot be searched.
SMALL_ELEMENT_KEYWORDS = (
    "ballpoint", "pen", "pencil", "marker", "biro", "crayon", "coin",
    "stamp", "button", "pin", "needle", "sticker", "earbud", "usb", "sim",
    "badge",
)


def _words(label) -> set:
    """The label's lowercase words, for the keyword matches below."""
    return set(re.findall(r"[a-z0-9]+", str(label or "").lower()))


def _keyword_in(keyword: str, words: set) -> bool:
    """True when every word of ``keyword`` is one of ``words``."""
    parts = keyword.split()
    return bool(parts) and all(p in words for p in parts)


def family_of(label) -> str:
    """Variety bucket of an anomaly label, "" when unknown.

    Exact catalog label first, then the retired-label aliases, then two
    prefix rules that absorb the historical time-traveler labels (the label
    text changed with every prompt revision, but "Time traveler: ..." is
    always the person family, ticket #1232). Ticket #1473 adds a keyword
    fallback (``FAMILY_KEYWORDS``) so a model-invented label
    ("Disposable plastic ballpoint pen") buckets with its reworded sibling
    ("ballpoint pen") instead of reading as a new element.
    """
    if not isinstance(label, str) or not label:
        return ""
    if label in FAMILIES_BY_LABEL:
        return FAMILIES_BY_LABEL[label]
    if label in FAMILY_ALIASES:
        return FAMILY_ALIASES[label]
    if label.startswith("Time traveler"):
        return "person"
    if label.startswith("Robot "):
        return "robot"
    words = _words(label)
    if words:
        for family, _settings, keywords in FAMILY_KEYWORDS:
            if any(_keyword_in(k, words) for k in keywords):
                return family
    return ""


def settings_for_label(label) -> tuple:
    """The settings an element fits, () when neither catalog nor keywords know.

    Exact catalog labels use their own entry's settings; otherwise the
    family's union across catalog entries (and, for a keyword-only family,
    the table's tuple). Used by the setting-fit gate (ticket #1473).
    """
    entry = entry_for_label(label)
    if entry is not None:
        return tuple(entry["settings"])
    family = family_of(label)
    if not family:
        return ()
    out = []
    for e in CATALOG:
        if e["family"] == family:
            out += [s for s in e["settings"] if s not in out]
    for fam, settings, _kw in FAMILY_KEYWORDS:
        if fam == family:
            out += [s for s in settings if s not in out]
    return tuple(out)


def settings_in_text(*texts) -> tuple:
    """The settings a scene's text names, in the fixed order above."""
    text = " ".join(str(t or "") for t in texts).lower()
    return tuple(name for name, words in SETTING_KEYWORDS
                 if any(w in text for w in words))


def inherently_small(label) -> bool:
    """True when the element is too small to be a fair search target (#1473).

    A known catalog entry is judged by its own numeric scale budget
    (``scale_max``); a model-invented label by the small-element noun list.
    """
    entry = entry_for_label(label)
    if entry is not None:
        return scale_max(entry) < MIN_ELEMENT_SCALE
    words = _words(label)
    return any(_keyword_in(k, words) for k in SMALL_ELEMENT_KEYWORDS)
