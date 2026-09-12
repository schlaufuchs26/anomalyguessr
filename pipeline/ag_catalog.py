#!/usr/bin/env python3
"""Machine-readable AnomalyGuessr anomaly catalog (ticket #1169).

The human-facing catalog lives in `wiki/entries/anomalyguessr-anomalies.md`;
the deterministic generator (`pipeline/ag_generate.py`) cannot parse prose, so
this module is the structured mirror the script picks from. Keep the two in
sync when a recipe works or fails: the wiki page stays the reference for the
reasoning (risk notes, run notes, era table), this file is the data the
pipeline actually uses.

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
                 #1136/#1122). None = no era check (fictional future, #1161)
    risk         "low" | "medium" | "high" (documentation; the generator
                 treats every entry the same, the risk notes drive retries)
    explanation  English sentence for the scene's `explanation` field
    references   [{label, url}] for every factual claim in the explanation
    tells        person/future only: the 1-2 modern tells in the prompt
    noun         short English noun for the prompt/hint text

Only time-travel-readable anomalies are listed: real later-era objects and
persons, or clearly futuristic technology (robots/gadgets, #1161). No UFOs,
no fantasy creatures.
"""

# Placement recipes (prompt phrase, hint phrase). Keys are referenced by the
# catalog entries; ag_generate.build_prompt turns them into full instructions.
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
         explanation="Clear PET plastic bottles only came into common use in "
                     "the 1970s, decades after this photograph was taken.",
         references=[{"label": "Polyethylene terephthalate",
                      "url": "https://en.wikipedia.org/wiki/Polyethylene_terephthalate"}]),
    dict(label="Paper coffee cup with lid", type="object", family="drinks",
         settings=("market", "street"), recipe="stall",
         size="a white paper takeaway coffee cup with a lid, 10-12 cm tall",
         min_year=1910, risk="low",
         explanation="Disposable paper cups with lids only became common "
                     "takeaway items in the 20th century and did not exist "
                     "when this photograph was taken.",
         references=[{"label": "Paper cup",
                      "url": "https://en.wikipedia.org/wiki/Paper_cup"}]),
    dict(label="Drink can (matte aluminium)", type="object", family="drinks",
         settings=("market", "street", "harbor"), recipe="ground",
         size="a matte aluminium drink can, about 12 cm tall", min_year=1950,
         risk="high",
         explanation="Aluminium beverage cans only appeared in the 1950s; "
                     "one could not have been in this photograph.",
         references=[{"label": "Beverage can",
                      "url": "https://en.wikipedia.org/wiki/Beverage_can"}]),
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
         explanation="Expanded polystyrene cups only exist since the mid-20th "
                     "century and are out of place in this photograph.",
         references=[{"label": "Polystyrene",
                      "url": "https://en.wikipedia.org/wiki/Polystyrene"}]),
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
         explanation="Laminated carton drink boxes were introduced in the "
                     "mid-20th century, after this photograph was taken.",
         references=[{"label": "Carton",
                      "url": "https://en.wikipedia.org/wiki/Carton"}]),
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
         risk="medium",
         explanation="Traffic cones are a mid-20th-century invention; this "
                     "scene predates them.",
         references=[{"label": "Traffic cone",
                      "url": "https://en.wikipedia.org/wiki/Traffic_cone"}]),
    dict(label="Modern bicycle", type="object", family="street-furniture",
         settings=("street", "market"), recipe="wall",
         size="a modern road bicycle, about 110 cm tall", min_year=1975,
         scale="roughly 8-12 percent of the image height, never a foreground object",
         risk="medium",
         explanation="Derailleur-geared modern bicycles are a later-20th-"
                     "century development that this photo predates.",
         references=[{"label": "Bicycle",
                      "url": "https://en.wikipedia.org/wiki/Bicycle"}]),
    dict(label="Blue plastic tarp", type="object", family="plastic",
         settings=("street", "harbor", "market"), recipe="ground_mid",
         size="a blue plastic tarpaulin, 1-2 m across", min_year=1950,
         scale="roughly 3-5 percent of the image height, never more than 6 percent",
         risk="medium",
         explanation="Woven plastic tarpaulins only became common in the "
                     "mid-20th century, after this photograph.",
         references=[{"label": "Tarpaulin",
                      "url": "https://en.wikipedia.org/wiki/Tarpaulin"}]),
    dict(label="Modern advertising poster", type="object", family="print",
         settings=("street", "market"), recipe="wall",
         size="a modern printed advertising poster, 50-80 cm", min_year=1960,
         scale="roughly 3-5 percent of the image height, never a foreground object",
         risk="medium",
         explanation="Offset-printed advertising posters with modern "
                     "typography belong to a later era than this photo.",
         references=[{"label": "Poster",
                      "url": "https://en.wikipedia.org/wiki/Poster"}]),
    dict(label="Caution tape", type="object", family="street-furniture",
         settings=("street",), recipe="wall",
         size="a roll of yellow-black barrier tape", min_year=1950,
         scale="a thin ribbon, at most 2 percent of the image height",
         risk="high",
         explanation="Plastic barrier tape is a 20th-century safety product "
                     "that did not exist when this photo was taken.",
         references=[{"label": "Barrier tape",
                      "url": "https://en.wikipedia.org/wiki/Barrier_tape"}]),
    dict(label="E-scooter", type="object", family="vehicle",
         settings=("street",), recipe="wall",
         size="a shared electric kick scooter, about 110 cm tall", min_year=2015,
         scale="roughly 6-10 percent of the image height, a background object",
         risk="high",
         explanation="Electric kick scooters only appeared in the 2010s, over "
                     "a century after this photograph.",
         references=[{"label": "Motorized scooter",
                      "url": "https://en.wikipedia.org/wiki/Motorized_scooter"}]),
    # Stations, travel, transport
    dict(label="Wheeled suitcase", type="object", family="luggage",
         settings=("station", "street"), recipe="bench",
         size="a wheeled suitcase, 60-75 cm tall", min_year=1970,
         scale="roughly 3-5 percent of the image height, never a foreground object",
         risk="medium",
         explanation="Rolling suitcases with wheels only became common in the "
                     "1970s, after this photograph was taken.",
         references=[{"label": "Suitcase",
                      "url": "https://en.wikipedia.org/wiki/Suitcase"}]),
    dict(label="Modern daypack", type="object", family="luggage",
         settings=("station", "street", "market"), recipe="bench",
         size="a modern nylon daypack, 45-55 cm", min_year=1960,
         scale="roughly 2-4 percent of the image height",
         risk="medium",
         explanation="Lightweight nylon daypacks with zips are a later-20th-"
                     "century product, not yet in use here.",
         references=[{"label": "Backpack",
                      "url": "https://en.wikipedia.org/wiki/Backpack"}]),
    dict(label="Over-ear headphones", type="object", family="electronics",
         settings=("station", "street"), recipe="bench",
         size="over-ear headphones, about 20 cm", min_year=1960, risk="high",
         scale="roughly 2-3 percent of the image height",
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
         explanation="Standardized steel shipping containers only appeared in "
                     "the 1950s, after this photograph.",
         references=[{"label": "Intermodal container",
                      "url": "https://en.wikipedia.org/wiki/Intermodal_container"}]),
    dict(label="Plastic cooler box", type="object", family="container",
         settings=("harbor",), recipe="deck",
         size="a plastic cooler box, 50-60 cm", min_year=1950, risk="medium",
         scale="roughly 2-4 percent of the image height",
         explanation="Moulded plastic cooler boxes are a mid-20th-century "
                     "product and cannot belong to this scene.",
         references=[{"label": "Cooler",
                      "url": "https://en.wikipedia.org/wiki/Cooler"}]),
    dict(label="Nylon rope (colored)", type="object", family="cordage",
         settings=("harbor",), recipe="deck",
         size="a coil of brightly colored synthetic rope, 30-40 cm across",
         min_year=1940, risk="medium",
         scale="roughly 2-3 percent of the image height",
         explanation="Bright synthetic nylon rope only replaced natural fibre "
                     "rope in the mid-20th century, after this photo.",
         references=[{"label": "Nylon",
                      "url": "https://en.wikipedia.org/wiki/Nylon"}]),
]

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
         explanation="Headphones and modern athletic shoes are 20th-century "
                     "products that did not exist in this scene's era.",
         references=[{"label": "Headphones",
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


def family_of(label) -> str:
    """Variety bucket of an anomaly label, "" when unknown.

    Exact catalog label first, then the retired-label aliases, then two
    prefix rules that absorb the historical time-traveler labels (the label
    text changed with every prompt revision, but "Time traveler: ..." is
    always the person family, ticket #1232). Only the backfill uses this; at
    ship time the family is read from the scene entry, so this cannot drift
    from the Go mirror.
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
    return ""
