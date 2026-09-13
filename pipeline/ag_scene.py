#!/usr/bin/env python3
"""AnomalyGuessr scene lookup by short handle (ticket #1413).

A scene's canonical id is long (source slug + hash + anomaly label) and is
the key for filenames, URLs and trace sidecars. The short handle ("AG-137",
assigned by ``ag_queue.assign_short_ids``) is what a human says out loud, so
this script is the one step from the handle Evan reads off a gallery card to
the scene, its source, its trace and its moderation state::

    ag_scene.py find AG-137          # pretty JSON for one scene
    ag_scene.py list                 # the whole handle -> id map

``find`` accepts the long id too (case-insensitive for the handle), and exit
code 2 means "unknown handle"; the output names the trace sidecar and the
image in the library so the caller does not have to rebuild those paths.
"""

import argparse
import json
import sys
from pathlib import Path

import ag_queue


def scene_view(data_dir: Path, scene: dict) -> dict:
    """The lookup payload: identity + provenance + trace + moderation."""
    eid = scene["id"]
    fb = ag_queue.load_feedback(data_dir)
    rejected = eid in fb.get("rejected", {}) or eid in fb.get("excluded", {})
    accepted = eid in fb.get("accepted", {})
    moderation = ("rejected" if rejected
                  else "accepted" if accepted else "unmoderated")
    trace = data_dir / "traces" / f"{eid}.json"
    lib = data_dir / "library" / eid
    return {
        "shortId": scene.get("shortId", ""),
        "id": eid,
        "title": scene.get("title", ""),
        "place": scene.get("place", ""),
        "year": scene.get("year", ""),
        "anomaly": scene.get("anomaly", ""),
        "family": scene.get("family", ""),
        "added": scene.get("added"),
        "shown": scene.get("shown"),
        "moderation": moderation,
        "comments": fb.get("comments", {}).get(eid, []),
        "sourceUrl": scene.get("sourceUrl", ""),
        "source": scene.get("source", {}),
        # The API resolves the handle itself; the URL keeps the canonical id
        # so it works on an API that predates the handle support.
        "traceUrl": f"/anomalyguessr/api/traces/{eid}",
        "tracePath": str(trace),
        "hasTrace": trace.exists(),
        "libraryImage": str(lib / f"{eid}.jpg"),
        "libraryImageExists": (lib / f"{eid}.jpg").exists(),
    }


def cmd_find(data_dir: Path, handle: str) -> int:
    state = ag_queue.load_state(data_dir)
    scene = ag_queue.find_scene(state, handle)
    if scene is None:
        print(f"unknown scene handle: {handle!r}", file=sys.stderr)
        return 2
    print(json.dumps(scene_view(data_dir, scene), indent=2,
                     ensure_ascii=False))
    return 0


def cmd_list(data_dir: Path) -> int:
    state = ag_queue.load_state(data_dir)
    fb = ag_queue.load_feedback(data_dir)
    rejected = set(fb.get("rejected", {})) | set(fb.get("excluded", {}))
    accepted = set(fb.get("accepted", {}))
    scenes = sorted(state["scenes"].values(),
                    key=lambda s: (str(s.get("added") or ""),
                                   str(s.get("id") or "")))
    for scene in scenes:
        eid = scene["id"]
        state_word = ("rejected" if eid in rejected
                      else "accepted" if eid in accepted else "unmoderated")
        print(f"{scene.get('shortId') or 'AG-?'}\t{scene.get('added', '?')}\t"
              f"{state_word}\t{eid}")
    print(f"{len(scenes)} scene(s); next {ag_queue.SHORT_ID_PREFIX}"
          f"{ag_queue.next_short_number(state)}")
    return 0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="ag_scene.py")
    ap.add_argument("--data", type=Path, default=ag_queue.default_data_dir())
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_find = sub.add_parser("find", help="one scene by short handle or long id")
    p_find.add_argument("handle")
    sub.add_parser("list", help="every scene's handle, oldest added first")
    args = ap.parse_args(argv)
    if args.cmd == "find":
        return cmd_find(args.data, args.handle)
    return cmd_list(args.data)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
