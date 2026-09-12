#!/usr/bin/env python3
"""Provenance + entry builder for the #1157 masked-inpainting playtest set.

This is a ONE-OFF helper, not a pipeline component; it is kept here for
provenance (moved from the fuchs repo in ticket #1261) instead of being
deleted. It reads the source metadata captured while downloading the fresh
Commons sources for the ticket's 5 accepted scenes and writes the entry.json
files in the shape pipeline/ag_queue.py validates. The edited images + answer
json live in /tmp/ag1157/out/ (generated via pipeline/ag_mask.py).
"""

import json
import sys
from pathlib import Path

# Captured while downloading each source (Commons API extmetadata), same day.
# fileUrl = Commons file page (canonical), imageUrl = upload.wikimedia URL.
SOURCES = {
    "cairo-citadel-1907": {
        "title": "Open-air market, Cairo Citadel (1907)",
        "fileUrl": "https://commons.wikimedia.org/wiki/File:Open-air_market,_Cairo_Citadel_(1907).jpg",
        "imageUrl": "https://upload.wikimedia.org/wikipedia/commons/9/96/Open-air_market%2C_Cairo_Citadel_%281907%29.jpg",
        "date": "1907",
        "place": "Cairo, Egypt",
        "license": "Public domain",
        "repository": "Wikimedia Commons",
        "description": "Open-air market beneath the Cairo Citadel, with a crowd gathered in the foreground and the Citadel's mosque and minarets in the background.",
    },
    "curb-market-broad-st": {
        "title": "The Curb Market, Broad St",
        "fileUrl": "https://commons.wikimedia.org/wiki/File:The_Curb_Market,_Broad_St_LCCN2007661233.tif",
        "imageUrl": "https://upload.wikimedia.org/wikipedia/commons/c/cf/The_Curb_Market%2C_Broad_St_",
        "date": "ca. 1905",
        "place": "New York City, USA",
        "license": "Public domain",
        "repository": "Wikimedia Commons (Library of Congress)",
        "description": "Stock curb market crowd on Broad Street, New York, densely packed with men in suits and hats; a building with steps and columns on the left.",
    },
    "westlake-public-market": {
        "title": "Westlake Public Market, Seattle (1910)",
        "fileUrl": "https://commons.wikimedia.org/wiki/File:Seattle_-_Westlake_Public_Market,_1910.gif",
        "imageUrl": "https://upload.wikimedia.org/wikipedia/commons/e/e5/Seattle_-_Westlake_Public_Market%2C_1910.gif",
        "date": "1910",
        "place": "Seattle, Washington, USA",
        "license": "Public domain",
        "repository": "Wikimedia Commons",
        "description": "Westlake Public Market in Seattle, 1910: open-air market stalls under a canopy, people gathered on the street with tram tracks in the foreground.",
    },
    "helsinki-herring-market-1941": {
        "title": "Helsinki Herring Market 1941",
        "fileUrl": "https://commons.wikimedia.org/wiki/File:Helsinki_Herring_Market_1941_(5788C;_JOKAHBL3C_B103-1).tif",
        "imageUrl": "https://upload.wikimedia.org/wikipedia/commons/1/1f/Helsinki_Herring_Market_1941_(5788C;_JOKAHBL3C_B103-1).tif",
        "date": "1941",
        "place": "Helsinki, Finland",
        "license": "CC BY 4.0 (Helsinki City Museum)",
        "repository": "Wikimedia Commons (Helsinki City Museum)",
        "description": "Crowd watching the herring market in Helsinki, 1941; a large crowd behind a barrier, open paved square in the foreground.",
    },
    "wallabout-market-brooklyn": {
        "title": "Vast crowd of trucks and horse-drawn carts at the Wallabout Market, Brooklyn, N.Y.",
        "fileUrl": "https://commons.wikimedia.org/wiki/File:Vast_crowd_of_trucks_and_horse-drawn_carts_at_the_Wallabout_Market,_Brooklyn,_N.Y..jpg",
        "imageUrl": "https://upload.wikimedia.org/wikipedia/commons/d/de/Vast_crowd_of_trucks_and_horse-drawn_carts_at_the_Wallabout_Market%2C_Brooklyn%2C_N.Y..jpg",
        "date": "between 1900 and 1920",
        "place": "Brooklyn, New York City, USA",
        "license": "Public domain",
        "repository": "Wikimedia Commons (Library of Congress)",
        "description": "Wallabout Market, Brooklyn: a vast open-air market filled with trucks and horse-drawn carts, people moving between the vehicles and piles of goods.",
    },
}

SCENES = [
    {
        "id": "cairo-citadel-time-traveler",
        "anomaly": "Time traveler: young man in robes with modern athletic sneakers",
        "explanation": "Modern athletic sneakers (with visible swoosh-style branding and thick soles) only became common in the 1960s, so they cannot appear in a 1907 photograph.",
        "references": [{"label": "Sneakers", "url": "https://en.wikipedia.org/wiki/Sneakers"}],
        "answer": {"x": 0.647, "y": 0.532, "r": 0.10},
        "hints": [
            "He stands mid-ground among the crowd, not at the edges of the photo.",
            "Right of the center, around halfway down the picture.",
            "The young man in robes right of the center: look at his feet, he wears modern athletic sneakers.",
        ],
    },
    {
        "id": "curb-market-time-traveler",
        "anomaly": "Time traveler: young man in a suit with modern sneakers",
        "explanation": "Modern sneakers with a distinct white swoosh logo are a later-20th-century design; they could not be worn on Broad Street ca. 1905.",
        "references": [{"label": "Nike, Inc.", "url": "https://en.wikipedia.org/wiki/Nike,_Inc."}],
        "answer": {"x": 0.152, "y": 0.554, "r": 0.10},
        "hints": [
            "He stands at the edge of the crowd, on the steps by the building on the left.",
            "Far left side of the photo, about halfway down.",
            "The man on the steps at the far left, in the dark suit: he wears modern sneakers with a white swoosh.",
        ],
    },
    {
        "id": "westlake-market-coffee-cup",
        "anomaly": "Paper coffee cup with lid",
        "explanation": "Disposable paper coffee cups with plastic lids only became everyday items in the mid-20th century; they cannot lie at a Seattle market stall in 1910.",
        "references": [{"label": "Disposable cup", "url": "https://en.wikipedia.org/wiki/Disposable_cup"}],
        "answer": {"x": 0.42, "y": 0.80, "r": 0.06},
        "hints": [
            "It sits on the ground at the front edge of the market stall.",
            "Lower middle of the photo, near the bottom edge.",
            "On the ground left of the stall's front corner: a small paper coffee cup with a lid.",
        ],
    },
    {
        "id": "helsinki-herring-bottle",
        "anomaly": "Plastic bottle (PET)",
        "explanation": "PET plastic bottles only entered common use in the 1970s; a bottle cannot lie on the Helsinki market square in 1941.",
        "references": [{"label": "Plastic bottle", "url": "https://en.wikipedia.org/wiki/Plastic_bottle"}],
        "answer": {"x": 0.359, "y": 0.635, "r": 0.06},
        "hints": [
            "It lies on the open paved ground in the foreground, away from the crowd.",
            "Left of the center, in the lower half of the photo.",
            "On the pavement left of the centre, near the cart wheel: a small plastic bottle.",
        ],
    },
    {
        "id": "wallabout-market-bottle",
        "anomaly": "Plastic bottle (PET)",
        "explanation": "PET plastic bottles only became common in the 1970s; one cannot lie between the market vehicles of the Wallabout Market ca. 1900-1920.",
        "references": [{"label": "Plastic bottle", "url": "https://en.wikipedia.org/wiki/Plastic_bottle"}],
        "answer": {"x": 0.4955, "y": 0.8125, "r": 0.06},
        "hints": [
            "It lies on the ground between the market vehicles.",
            "Center of the photo, lower area between the carts.",
            "Between the cart wheels near the center: a small crushed plastic bottle.",
        ],
    },
]


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ag1157/out")
    for sc in SCENES:
        src_key = {
            "cairo-citadel-time-traveler": "cairo-citadel-1907",
            "curb-market-time-traveler": "curb-market-broad-st",
            "westlake-market-coffee-cup": "westlake-public-market",
            "helsinki-herring-bottle": "helsinki-herring-market-1941",
            "wallabout-market-bottle": "wallabout-market-brooklyn",
        }[sc["id"]]
        src = SOURCES[src_key]
        entry = {
            "id": sc["id"],
            "title": src["title"],
            "place": src["place"],
            "year": src["date"],
            "credit": f"{src['repository']}, {src['license']}",
            "sourceUrl": src["fileUrl"],
            "source": {
                "repository": src["repository"],
                "fileUrl": src["fileUrl"],
                "originalTitle": src["title"],
                "date": src["date"],
                "place": src["place"],
                "license": src["license"],
                "description": src["description"],
            },
            "anomaly": sc["anomaly"],
            "explanation": sc["explanation"],
            "references": sc["references"],
            "description": src["description"],
            "answer": sc["answer"],
            "hints": sc["hints"],
        }
        (out / f"{sc['id']}.entry.json").write_text(json.dumps(entry, indent=2) + "\n")
        print(f"wrote {sc['id']}.entry.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())