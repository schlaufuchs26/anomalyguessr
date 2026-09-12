"""Tests for pipeline/ag_queue.py (ticket #1107).

Run with: python3 scripts/test_ag_queue.py

Synthetic landscape JPEGs are generated with ImageMagick's `convert` (on
PATH in the nix env); no network access is needed. All state lands in temp
dirs.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_queue as q

SCRIPTS_DIR = Path(__file__).resolve().parent


def make_img(path: Path, w=800, h=600, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
        check=True, capture_output=True,
    )


def valid_entry(eid="s1"):
    return {
        "id": eid,
        "title": "Market in Testville",
        "place": "Testville",
        "year": "1900",
        "credit": "Public Domain",
        "sourceUrl": "https://example.test/file",
        "source": {
            "repository": "Example Archive",
            "fileUrl": "https://example.test/file",
            "originalTitle": "Market in Testville",
            "date": "1900",
            "place": "Testville",
            "license": "Public Domain",
            "description": "A market scene.",
        },
        "anomaly": "Plastic bottle",
        "explanation": "Plastic bottles only appeared in the 1970s.",
        "references": [{"label": "Plastic bottle – Wikipedia",
                        "url": "https://en.wikipedia.org/wiki/Plastic_bottle"}],
        "description": "A market in Testville in 1900 (catalogue text).",
        "answer": {"x": 0.3, "y": 0.6, "r": 0.05},
        "hints": ["on a wagon", "on the left", "between the crates: a bottle"],
    }


def make_repo(root: Path, scenes=None):
    """A fake game repo with a v1 manifest + scene images."""
    repo = root / "game"
    (repo / "scenes").mkdir(parents=True)
    scenes = scenes or [valid_entry("a1"), valid_entry("a2")]
    manifest_scenes = []
    for i, s in enumerate(scenes):
        eid = s["id"]
        make_img(repo / "scenes" / f"{eid}.jpg")
        make_img(repo / "scenes" / f"{eid}-original.jpg")
        entry = dict(s)
        entry["image"] = f"scenes/{eid}.jpg"
        entry["original"] = f"scenes/{eid}-original.jpg"
        manifest_scenes.append(entry)
    manifest = {"version": 1, "scenes": manifest_scenes}
    (repo / "scenes" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False))
    return repo


def write_feedback(data_dir: Path, rejected=(), accepted=(), comments=None):
    """Synthetic feedback.json as the dashboard API writes it (#1113/#1163)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fb = {
        "version": 1,
        "accepted": {a: "2026-09-08T08:00:00+02:00" for a in accepted},
        "rejected": {e: "2026-09-08T08:00:00+02:00" for e in rejected},
        "comments": comments or {},
    }
    (data_dir / "feedback.json").write_text(
        json.dumps(fb, ensure_ascii=False))


class ValidateTest(unittest.TestCase):
    def test_accepts_valid_entry(self):
        self.assertEqual(q.validate_entry(valid_entry()), [])

    def test_rejects_bad_id(self):
        e = valid_entry("Bad ID!")
        self.assertTrue(any("id" in x for x in q.validate_entry(e)))
        e = valid_entry("x!")
        self.assertTrue(any("id" in x for x in q.validate_entry(e)))

    def test_requires_source(self):
        e = valid_entry()
        del e["source"]
        self.assertTrue(any("source" in x for x in q.validate_entry(e)))
        e = valid_entry()
        e["source"]["repository"] = ""
        self.assertTrue(
            any("source.repository" in x for x in q.validate_entry(e)))

    def test_requires_explanation_and_references(self):
        e = valid_entry()
        del e["explanation"]
        self.assertTrue(any("explanation" in x for x in q.validate_entry(e)))
        e = valid_entry()
        e["explanation"] = "   "
        self.assertTrue(any("explanation" in x for x in q.validate_entry(e)))
        e = valid_entry()
        del e["references"]
        self.assertTrue(any("references" in x for x in q.validate_entry(e)))
        e = valid_entry()
        e["references"] = []
        self.assertTrue(any("references" in x for x in q.validate_entry(e)))
        e = valid_entry()
        e["references"] = [{"label": "L", "url": "ftp://x"}]
        self.assertTrue(any("http(s)" in x for x in q.validate_entry(e)))
        e = valid_entry()
        e["references"] = [{"label": "", "url": "https://x.test"}]
        self.assertTrue(any("label" in x for x in q.validate_entry(e)))

    def test_rejects_bad_answer(self):
        e = valid_entry()
        e["answer"] = {"x": 1.5, "y": 0.5, "r": 0.9}
        errs = q.validate_entry(e)
        self.assertTrue(any("answer.x" in x for x in errs))
        self.assertTrue(any("answer.r" in x for x in errs))

    def test_requires_three_hints(self):
        e = valid_entry()
        e["hints"] = ["a", "b"]
        self.assertTrue(any("hints" in x for x in q.validate_entry(e)))


class ChooseDayTest(unittest.TestCase):
    def _scenes(self, ids):
        return [{"id": i} for i in ids]

    def test_takes_oldest_first_up_to_limit(self):
        cands = self._scenes(["a", "b", "c", "d", "e", "f", "g"])
        day = q.choose_day(cands, 5)
        self.assertEqual(len(day), 5)
        # composition caps were removed with the difficulty enum (#1142):
        # the ship order is plain oldest-first.
        self.assertEqual([s["id"] for s in day], ["a", "b", "c", "d", "e"])

    def test_stops_at_limit(self):
        cands = self._scenes(["a", "b"])
        self.assertEqual(len(q.choose_day(cands, 5)), 2)


class StateAddTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_add_"))
        self.data = self.tmp / "data"
        self.ed = self.tmp / "ed.jpg"
        self.orig = self.tmp / "orig.jpg"
        make_img(self.ed)
        make_img(self.orig)

    def test_add_copies_images_and_records_date(self):
        scene = q.state_add(self.data, valid_entry("s1"), self.ed, self.orig,
                            "2026-09-08")
        self.assertEqual(scene["added"], "2026-09-08")
        self.assertIsNone(scene["shown"])
        self.assertNotIn("image", scene)
        lib = self.data / "library" / "s1"
        self.assertTrue((lib / "s1.jpg").exists())
        self.assertTrue((lib / "s1-original.jpg").exists())
        state = q.load_state(self.data)
        self.assertIn("s1", state["scenes"])

    def test_add_is_idempotent_per_id(self):
        q.state_add(self.data, valid_entry("s1"), self.ed, self.orig,
                    "2026-09-08")
        q.state_add(self.data, valid_entry("s1"), self.ed, self.orig,
                    "2026-09-08")
        self.assertEqual(len(q.load_state(self.data)["scenes"]), 1)

    def test_add_rejects_invalid_date_and_invalid_entry(self):
        with self.assertRaises(ValueError):
            q.state_add(self.data, valid_entry(), self.ed, self.orig, "09-08")
        bad = valid_entry()
        del bad["source"]
        with self.assertRaises(ValueError):
            q.state_add(self.data, bad, self.ed, self.orig, "2026-09-08")

    def test_add_rejects_portrait_edited(self):
        portrait = self.tmp / "portrait.jpg"
        make_img(portrait, 400, 600)
        with self.assertRaises(ValueError):
            q.state_add(self.data, valid_entry(), portrait, self.orig,
                        "2026-09-08")


class RemoveScenesTest(unittest.TestCase):
    """The repair path (ticket #1338): drop a scene that cannot be right."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_rm_"))
        self.data = self.tmp / "data"
        self.ed = self.tmp / "ed.jpg"
        self.orig = self.tmp / "orig.jpg"
        make_img(self.ed)
        make_img(self.orig)
        q.state_add(self.data, valid_entry("s1"), self.ed, self.orig,
                    "2026-09-08")
        q.state_add(self.data, valid_entry("s2"), self.ed, self.orig,
                    "2026-09-08")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remove_drops_the_scene_and_its_images(self):
        res = q.remove_scenes(self.data, ["s1"])
        self.assertEqual(res["removed"], ["s1"])
        self.assertEqual(res["missing"], [])
        self.assertNotIn("s1", q.load_state(self.data)["scenes"])
        self.assertFalse((self.data / "library" / "s1").exists())
        self.assertTrue((self.data / "library" / "s2" / "s2.jpg").exists())

    def test_remove_reports_unknown_ids_without_failing(self):
        res = q.remove_scenes(self.data, ["nope"])
        self.assertEqual(res, {"removed": [], "missing": ["nope"]})
        self.assertEqual(len(q.load_state(self.data)["scenes"]), 2)

    def test_remove_can_keep_the_images(self):
        q.remove_scenes(self.data, ["s1"], drop_images=False)
        self.assertTrue((self.data / "library" / "s1" / "s1.jpg").exists())


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_init_"))
        self.data = self.tmp / "data"
        self.repo = make_repo(self.tmp, [valid_entry("m1"), valid_entry("m2")])

    def test_init_migrates_and_marks_shown(self):
        n = q.state_init(self.data, self.repo, "2026-09-07")
        self.assertEqual(n, 2)
        state = q.load_state(self.data)
        for s in state["scenes"].values():
            self.assertEqual(s["shown"], "2026-09-07")
            self.assertEqual(s["added"], "2026-09-07")
        # images copied into the library
        self.assertTrue((self.data / "library" / "m1" / "m1.jpg").exists())

    def test_init_idempotent(self):
        q.state_init(self.data, self.repo, "2026-09-07")
        n = q.state_init(self.data, self.repo, "2026-09-07")
        self.assertEqual(n, 0)
        self.assertEqual(len(q.load_state(self.data)["scenes"]), 2)

    def test_init_skips_missing_images(self):
        (self.repo / "scenes" / "m1.jpg").unlink()
        n = q.state_init(self.data, self.repo, "2026-09-07")
        self.assertEqual(n, 1)
        self.assertNotIn("m1", q.load_state(self.data)["scenes"])


class ShipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_ship_"))
        self.data = self.tmp / "data"
        self.repo = make_repo(self.tmp)  # empty v1 repo shell
        # seed the queue with 7 unshown scenes (difficulty enum removed, #1142)
        for i in range(7):
            e = valid_entry(f"n{i}")
            make_img(self.tmp / f"ed{i}.jpg")
            make_img(self.tmp / f"or{i}.jpg")
            q.state_add(self.data, e, self.tmp / f"ed{i}.jpg",
                        self.tmp / f"or{i}.jpg", "2026-09-08")
        # ship-eligibility is a human accept (ticket #1163): accept them all
        write_feedback(self.data, accepted=[f"n{i}" for i in range(7)])

    def test_ship_writes_v2_manifest_with_date_and_copies_images(self):
        res = q.ship(self.data, self.repo, "2026-09-09")
        self.assertTrue(res["shipped"])
        self.assertEqual(len(res["scenes"]), 5)
        manifest = json.loads(
            (self.repo / "scenes" / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["date"], "2026-09-09")
        self.assertEqual(len(manifest["scenes"]), 5)
        for s in manifest["scenes"]:
            self.assertEqual(s["image"], f"scenes/{s['id']}.jpg")
            self.assertEqual(s["original"], f"scenes/{s['id']}-original.jpg")
            self.assertIn("source", s)
            self.assertTrue((self.repo / s["image"]).exists())
        state = q.load_state(self.data)
        self.assertEqual(state["last_shipped"], "2026-09-09")
        # the manifest order is recorded for replays (ticket #1237)
        self.assertEqual(state["last_shipped_ids"],
                         [s["id"] for s in manifest["scenes"]])
        shown = [s for s in state["scenes"].values() if s["shown"] == "2026-09-09"]
        self.assertEqual(len(shown), 5)

    def test_ship_is_idempotent_per_date(self):
        q.ship(self.data, self.repo, "2026-09-09")
        res = q.ship(self.data, self.repo, "2026-09-09")
        self.assertFalse(res["shipped"])
        # same 5 stay marked shown on the same date
        state = q.load_state(self.data)
        self.assertEqual(
            len([s for s in state["scenes"].values()
                 if s["shown"] == "2026-09-09"]), 5)

    def test_ship_removes_orphaned_scene_images(self):
        # pre-existing junk image in the repo scenes dir
        (self.repo / "scenes" / "old-junk.jpg").write_bytes(b"x")
        res = q.ship(self.data, self.repo, "2026-09-09")
        self.assertIn("old-junk.jpg", res["removed"])
        self.assertFalse((self.repo / "scenes" / "old-junk.jpg").exists())

    def test_ship_empty_queue_raises(self):
        empty = self.tmp / "empty"
        with self.assertRaises(ValueError):
            q.ship(empty, self.repo, "2026-09-09")

    def test_ship_ships_partial_when_fewer_than_5(self):
        data = self.tmp / "few"
        e = valid_entry("only")
        make_img(self.tmp / "edx.jpg")
        make_img(self.tmp / "orx.jpg")
        q.state_add(data, e, self.tmp / "edx.jpg", self.tmp / "orx.jpg",
                    "2026-09-08")
        write_feedback(data, accepted=["only"])
        res = q.ship(data, self.repo, "2026-09-10")
        self.assertEqual(len(res["scenes"]), 1)

    def test_ship_commit_bypasses_hook_with_identity(self):
        # a git repo with a failing pre-commit hook + no user config: the
        # robot commit must still land (hook bypassed, identity passed).
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"],
                       check=True)
        hook = self.repo / ".git" / "hooks"
        hook.mkdir(parents=True, exist_ok=True)
        (hook / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (hook / "pre-commit").chmod(0o755)
        res = q.ship(self.data, self.repo, "2026-09-09", commit=True)
        self.assertTrue(res["shipped"])
        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1",
             "--format=%an <%ae> %s"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn("Schlaufuchs <hugo@fuchs.science>", log)
        self.assertIn("ag-pipeline: ship daily set 2026-09-09", log)

    def test_ship_explicit_ids_same_date_redo(self):
        # Same-day redo path (ticket #1107): ship() with ids must replace an
        # already-shipped day's manifest (last_shipped == date) with exactly
        # the given scenes, in order, and mark them shown on that date.
        q.ship(self.data, self.repo, "2026-09-09")  # first ship (5 scenes)
        first = json.loads(
            (self.repo / "scenes" / "manifest.json").read_text())
        # pick two of the six seeded scenes by id (fresh additions)
        state = q.load_state(self.data)
        fresh = sorted(s["id"] for s in state["scenes"].values()
                       if s.get("shown") is None)
        self.assertTrue(len(fresh) >= 1, fresh)
        chosen = fresh[:2]
        res = q.ship(self.data, self.repo, "2026-09-09",
                     ids=[chosen[1], chosen[0]])
        self.assertTrue(res["shipped"])
        self.assertEqual(res["scenes"], [chosen[1], chosen[0]])
        manifest = json.loads(
            (self.repo / "scenes" / "manifest.json").read_text())
        self.assertEqual(manifest["date"], "2026-09-09")
        self.assertEqual([s["id"] for s in manifest["scenes"]],
                         [chosen[1], chosen[0]])
        # the redo's order replaces the recorded ship order (ticket #1237)
        self.assertEqual(q.load_state(self.data)["last_shipped_ids"],
                         [chosen[1], chosen[0]])
        # only the explicit scenes are in the repo now; state marks them shown
        for eid in chosen:
            self.assertEqual(q.load_state(self.data)["scenes"][eid]["shown"],
                             "2026-09-09")
        self.assertEqual(
            len(list((self.repo / "scenes").glob("*.jpg"))), 2 * len(chosen))

    def test_ship_explicit_ids_unknown_rejected(self):
        with self.assertRaises(ValueError):
            q.ship(self.data, self.repo, "2026-09-11", ids=["nope"])

    def test_ship_explicit_ids_reshows_earlier_scene(self):
        # Ticket #1206: an explicit daily set may re-show a scene that was
        # shown on an earlier date (curation / recycling); it stays legal and
        # updates the scene's `shown` to the new date.
        q.ship(self.data, self.repo, "2026-09-09")
        state = q.load_state(self.data)
        shown_on_09 = next(s["id"] for s in state["scenes"].values()
                           if s.get("shown") == "2026-09-09")
        res = q.ship(self.data, self.repo, "2026-09-10", ids=[shown_on_09])
        self.assertTrue(res["shipped"])
        self.assertEqual(res["scenes"], [shown_on_09])
        self.assertEqual(res["recycled"], 1)
        self.assertEqual(
            q.load_state(self.data)["scenes"][shown_on_09]["shown"],
            "2026-09-10")

    def test_ship_explicit_ids_duplicate_in_day_rejected(self):
        # Ticket #1206: the same scene must never appear twice in one daily,
        # even when the caller passes it twice.
        state = q.load_state(self.data)
        eid = next(iter(state["scenes"]))
        with self.assertRaises(ValueError):
            q.ship(self.data, self.repo, "2026-09-11", ids=[eid, eid])


def set_shown(data_dir: Path, mapping: dict):
    """Stamp `shown` per id directly in state.json (test fixture, #1206)."""
    state = q.load_state(data_dir)
    for eid, shown in mapping.items():
        state["scenes"][eid]["shown"] = shown
    q.save_state(data_dir, state)


class RecycleTest(unittest.TestCase):
    """Daily fill recycles the back catalogue when the fresh pool is dry
    (ticket #1206): never-shown scenes first, then least-recently-shown
    first, no duplicate inside a day, only an empty accepted pool errors."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_recycle_"))
        self.data = self.tmp / "data"
        self.repo = make_repo(self.tmp, [])  # empty shell repo

    def _seed(self, specs, accepted=None):
        """specs: (id, added, shown). Adds scenes, stamps shown, accepts all
        unless `accepted` is given (accepted=[] = no accepted scenes)."""
        for eid, added, _shown in specs:
            make_img(self.tmp / f"ed_{eid}.jpg")
            make_img(self.tmp / f"or_{eid}.jpg")
            q.state_add(self.data, valid_entry(eid),
                        self.tmp / f"ed_{eid}.jpg",
                        self.tmp / f"or_{eid}.jpg", added)
        set_shown(self.data, {eid: shown for eid, _a, shown in specs})
        if accepted is None:
            accepted = [eid for eid, _a, _s in specs]
        write_feedback(self.data, accepted=accepted)

    def test_fresh_preferred_over_recycled(self):
        self._seed([
            ("r1", "2026-08-01", "2026-08-05"),
            ("r2", "2026-08-02", "2026-08-06"),
            ("r3", "2026-08-03", "2026-08-07"),
            ("f1", "2026-09-01", None),
            ("f2", "2026-09-02", None),
            ("f3", "2026-09-03", None),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        # three fresh scenes fill the day; the remaining two slots come from
        # the back catalogue, so no fresh scene is displaced by a recycle
        self.assertEqual(res["scenes"], ["f1", "f2", "f3", "r1", "r2"])
        self.assertEqual(res["recycled"], 2)

    def test_lru_fill_order_oldest_shown_first(self):
        self._seed([
            ("s1", "2026-08-01", "2026-08-10"),
            ("s2", "2026-08-01", "2026-08-11"),
            ("s3", "2026-08-01", "2026-08-12"),
            ("s4", "2026-08-01", "2026-08-13"),
            ("s5", "2026-08-01", "2026-08-14"),
            ("s6", "2026-08-01", "2026-08-15"),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        # least recently shown wins; the newest-shown scene stays unused
        self.assertEqual(res["scenes"], ["s1", "s2", "s3", "s4", "s5"])
        self.assertEqual(res["recycled"], 5)

    def test_only_shown_scenes_do_not_error(self):
        # The old "no unshown accepted scenes" error is gone: with only
        # previously-shown scenes the day fills from the back catalogue and
        # ships what is there.
        self._seed([
            ("s1", "2026-08-01", "2026-08-10"),
            ("s2", "2026-08-01", "2026-08-11"),
            ("s3", "2026-08-01", "2026-08-12"),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertTrue(res["shipped"])
        self.assertEqual(res["scenes"], ["s1", "s2", "s3"])
        self.assertEqual(res["recycled"], 3)

    def test_no_duplicate_within_one_day_and_shown_updated(self):
        self._seed([
            ("r1", "2026-08-01", "2026-08-05"),
            ("r2", "2026-08-02", "2026-08-06"),
            ("f1", "2026-09-01", None),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(res["scenes"], ["f1", "r1", "r2"])
        self.assertEqual(len(res["scenes"]), len(set(res["scenes"])))
        state = q.load_state(self.data)
        for eid in res["scenes"]:
            self.assertEqual(state["scenes"][eid]["shown"], "2026-09-10")

    def test_lru_rotation_continues_next_day(self):
        self._seed([
            ("a", "2026-08-01", "2026-08-10"),
            ("b", "2026-08-01", "2026-08-11"),
            ("c", "2026-08-01", "2026-08-12"),
        ])
        q.ship(self.data, self.repo, "2026-09-10", ids=["a", "b"])
        # a + b were just re-shown, so c (now oldest shown) leads the next day
        res = q.ship(self.data, self.repo, "2026-09-11")
        self.assertEqual(res["scenes"][0], "c")

    def test_rejected_and_unmoderated_not_recycled(self):
        self._seed([
            ("r1", "2026-08-01", "2026-08-05"),
            ("r2", "2026-08-02", "2026-08-06"),
            ("r3", "2026-08-03", "2026-08-07"),
        ])
        # r1 rejected, r3 unmoderated: only r2 may fill the day
        write_feedback(self.data, accepted=["r1", "r2"], rejected=["r1"])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(res["scenes"], ["r2"])

    def test_empty_accepted_pool_still_errors(self):
        self._seed([("u1", "2026-08-01", None)], accepted=[])
        with self.assertRaises(ValueError):
            q.ship(self.data, self.repo, "2026-09-10")


class AnomalyVarietyTest(unittest.TestCase):
    """The daily prefers distinct anomaly labels (ticket #1229): walk the
    freshness order, skip a candidate whose label is already in the day,
    then fill the remaining slots from the skipped candidates (repeats
    allowed). Never a hard block, never a duplicate scene id."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_variety_"))
        self.data = self.tmp / "data"
        self.repo = make_repo(self.tmp, [])

    def _seed(self, specs):
        """specs: (id, added, label). Fresh (never shown), all accepted."""
        for eid, added, label in specs:
            e = valid_entry(eid)
            e["anomaly"] = label
            make_img(self.tmp / f"ed_{eid}.jpg")
            make_img(self.tmp / f"or_{eid}.jpg")
            q.state_add(self.data, e, self.tmp / f"ed_{eid}.jpg",
                        self.tmp / f"or_{eid}.jpg", added)
        write_feedback(self.data, accepted=[eid for eid, _a, _l in specs])

    def test_distinct_labels_preferred_over_repeats(self):
        # The three oldest scenes share one label; the day takes the first of
        # them, then skips ahead to the distinct labels, and only then fills
        # the two open slots from the skipped repeats.
        self._seed([
            ("b1", "2026-09-01", "Bottle"),
            ("b2", "2026-09-02", "Bottle"),
            ("b3", "2026-09-03", "Bottle"),
            ("c1", "2026-09-04", "Can"),
            ("r1", "2026-09-05", "Robot"),
            ("b4", "2026-09-06", "Bottle"),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        # pass one in freshness order, then the skipped repeats in order
        self.assertEqual(res["scenes"], ["b1", "c1", "r1", "b2", "b3"])
        self.assertEqual(res["distinct_labels"], 3)
        self.assertEqual(res["pool_distinct_labels"], 3)
        self.assertEqual(res["labels"], ["Bottle", "Can", "Robot",
                                         "Bottle", "Bottle"])

    def test_fewer_than_five_distinct_labels_still_fills(self):
        # Only two labels in the pool: the day still fills five, repeats and
        # all; a starved pool is reported, not an error.
        self._seed([
            ("a1", "2026-09-01", "Bottle"),
            ("a2", "2026-09-02", "Can"),
            ("a3", "2026-09-03", "Bottle"),
            ("a4", "2026-09-04", "Can"),
            ("a5", "2026-09-05", "Bottle"),
            ("a6", "2026-09-06", "Can"),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(len(res["scenes"]), 5)
        self.assertEqual(res["scenes"], ["a1", "a2", "a3", "a4", "a5"])
        self.assertEqual(res["distinct_labels"], 2)
        self.assertEqual(res["pool_distinct_labels"], 2)

    def test_all_distinct_labels_keep_freshness_order(self):
        # With five distinct labels the day is just the five freshest scenes
        # in the #1206 order; variety changes nothing.
        self._seed([(f"f{i}", f"2026-09-0{i}", f"Label {i}")
                    for i in range(1, 8)])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(res["scenes"], ["f1", "f2", "f3", "f4", "f5"])
        self.assertEqual(res["distinct_labels"], 5)
        self.assertEqual(res["pool_distinct_labels"], 7)

    def test_variety_reaches_into_the_back_catalogue(self):
        # A fresh repeat loses its slot to a back-catalogue scene with a new
        # label: the day takes f1 plus the three distinct back-catalogue
        # labels, then fills the last slot with the oldest skipped repeat.
        # f3 stays unshown; the three re-shown scenes are counted recycled.
        self._seed([
            ("f1", "2026-09-01", "Bottle"),
            ("f2", "2026-09-02", "Bottle"),
            ("f3", "2026-09-03", "Bottle"),
            ("r1", "2026-08-01", "Can"),
            ("r2", "2026-08-01", "Robot"),
            ("r3", "2026-08-01", "Statue"),
        ])
        set_shown(self.data, {"r1": "2026-08-10", "r2": "2026-08-11",
                              "r3": "2026-08-12"})
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(res["scenes"], ["f1", "r1", "r2", "r3", "f2"])
        self.assertEqual(res["distinct_labels"], 4)
        self.assertEqual(res["recycled"], 3)
        self.assertIsNone(q.load_state(self.data)["scenes"]["f3"]["shown"])

    def test_no_duplicate_scene_id_and_no_gap(self):
        self._seed([
            ("b1", "2026-09-01", "Bottle"),
            ("b2", "2026-09-02", "Bottle"),
            ("c1", "2026-09-03", "Can"),
            ("d1", "2026-09-04", "Robot"),
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(len(res["scenes"]), len(set(res["scenes"])))
        self.assertEqual(res["scenes"], ["b1", "c1", "d1", "b2"])

    def test_choose_day_and_daily_order_are_deterministic(self):
        cands = [{"id": "b1", "anomaly": "Bottle"},
                 {"id": "b2", "anomaly": "Bottle"},
                 {"id": "c1", "anomaly": "Can"}]
        self.assertEqual(q.choose_day(cands), q.choose_day(cands))
        self.assertEqual([s["id"] for s in q.choose_day(cands)],
                         ["b1", "c1", "b2"])

    def test_daily_order_puts_the_day_first_then_the_rest(self):
        # Gallery order (#1208): the day's set first, then the candidates the
        # variety pass skipped, still in freshness order.
        self._seed([
            ("b1", "2026-09-01", "Bottle"),
            ("b2", "2026-09-02", "Bottle"),
            ("b3", "2026-09-03", "Bottle"),
            ("c1", "2026-09-04", "Can"),
            ("d1", "2026-09-05", "Robot"),
            ("e1", "2026-09-06", "Statue"),
        ])
        state = q.load_state(self.data)
        order = q.daily_order(state, accepted={"b1", "b2", "b3", "c1",
                                               "d1", "e1"})
        self.assertEqual(order, ["b1", "c1", "d1", "e1", "b2", "b3"])

    def test_missing_label_never_blocks(self):
        # Defensive: an empty anomaly label must not dedupe scenes away.
        cands = [{"id": "x", "anomaly": ""}, {"id": "y", "anomaly": ""}]
        self.assertEqual([s["id"] for s in q.choose_day(cands)], ["x", "y"])


class FamilyVarietyTest(unittest.TestCase):
    """The daily prefers distinct anomaly families, not just labels (ticket
    #1232): a bottle and a can are different labels but one family, so the
    second same-family scene waits while a distinct family (even a recycled
    back-catalogue scene) takes the slot. Family repeats are still allowed
    once the new families run out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_family_"))
        self.data = self.tmp / "data"
        self.repo = make_repo(self.tmp, [])

    def _seed(self, specs):
        """specs: (id, added, label, family). Fresh, all accepted."""
        for eid, added, label, family in specs:
            e = valid_entry(eid)
            e["anomaly"] = label
            if family:
                e["family"] = family
            make_img(self.tmp / f"ed_{eid}.jpg")
            make_img(self.tmp / f"or_{eid}.jpg")
            q.state_add(self.data, e, self.tmp / f"ed_{eid}.jpg",
                        self.tmp / f"or_{eid}.jpg", added)
        write_feedback(self.data, accepted=[s[0] for s in specs])

    def test_family_duplicate_loses_its_slot_to_a_recycled_scene(self):
        # Two bottles (same family, different labels) among four other fresh
        # scenes: the label pass alone would ship all five fresh scenes, but
        # the family pass skips the second bottle and pulls in the recycled
        # suitcase instead. The bottle stays fresh for another day.
        self._seed([
            ("f1", "2026-09-01", "Plastic bottle (clear PET)", "drinks"),
            ("f2", "2026-09-02", "Plastic water bottle (PET)", "drinks"),
            ("f3", "2026-09-03", "Plastic bag (white, with handles)",
             "plastic"),
            ("f4", "2026-09-04", "E-scooter", "vehicle"),
            ("f5", "2026-09-05", "Over-ear headphones", "electronics"),
            ("r1", "2026-08-01", "Wheeled suitcase", "luggage"),
        ])
        set_shown(self.data, {"r1": "2026-08-10"})
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(res["scenes"], ["f1", "f3", "f4", "f5", "r1"])
        self.assertEqual(res["recycled"], 1)
        self.assertEqual(res["distinct_labels"], 5)
        self.assertEqual(res["distinct_families"], 5)
        self.assertIsNone(q.load_state(self.data)["scenes"]["f2"]["shown"])

    def test_family_second_pass_prefers_a_new_label(self):
        # Pass two takes the family sibling (new label) before the exact
        # repeat: with two slots, "Can" + "Pepsi can" beats "Can" + "Can".
        cands = [
            {"id": "a1", "anomaly": "Can (matte aluminium)", "family": "drinks"},
            {"id": "a2", "anomaly": "Pepsi can", "family": "drinks"},
            {"id": "a3", "anomaly": "Can (matte aluminium)", "family": "drinks"},
        ]
        day = q.choose_day(cands, 2)
        self.assertEqual([s["id"] for s in day], ["a1", "a2"])

    def test_same_family_still_fills_when_no_new_family_is_left(self):
        # One family, five distinct labels: the day still ships all five, in
        # freshness order; family variety is a preference, not a block.
        self._seed([
            (f"c{i}", f"2026-09-0{i}", f"Drink {i}", "drinks")
            for i in range(1, 6)
        ])
        res = q.ship(self.data, self.repo, "2026-09-10")
        self.assertEqual(res["scenes"],
                         ["c1", "c2", "c3", "c4", "c5"])
        self.assertEqual(res["distinct_labels"], 5)
        self.assertEqual(res["distinct_families"], 1)

    def test_missing_family_never_blocks(self):
        cands = [{"id": "x", "anomaly": "A"}, {"id": "y", "anomaly": "A"}]
        self.assertEqual([s["id"] for s in q.choose_day(cands)],
                         ["x", "y"])


class BackfillFamilyTest(unittest.TestCase):
    """The one-time #1232 migration fills family on entries that predate the
    field, resolving current catalog labels, retired aliases and the
    historical time-traveler labels, and reports what it cannot resolve."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_backfill_"))
        self.data = self.tmp / "data"

    def _add(self, eid, label):
        e = valid_entry(eid)
        e["anomaly"] = label
        make_img(self.tmp / f"ed_{eid}.jpg")
        make_img(self.tmp / f"or_{eid}.jpg")
        q.state_add(self.data, e, self.tmp / f"ed_{eid}.jpg",
                    self.tmp / f"or_{eid}.jpg", "2026-09-01")

    def test_fills_catalog_alias_and_prefix_labels(self):
        self._add("a1", "Plastic bottle (clear PET)")   # current catalog
        self._add("a2", "Plastic water bottle (PET)")   # retired alias
        self._add("a3", "Drink can")                    # retired alias
        self._add("a4", "Time traveler: young man in a suit with sneakers")
        self._add("a5", "Robot time traveler")          # catalog, robot
        self._add("a6", "Mystery widget")               # unresolvable
        res = q.backfill_family(self.data)
        self.assertEqual(res["filled"], 5)
        self.assertEqual(res["unknown"], ["Mystery widget"])
        state = q.load_state(self.data)
        fams = {eid: s.get("family")
                for eid, s in state["scenes"].items()}
        self.assertEqual(fams, {
            "a1": "drinks", "a2": "drinks", "a3": "drinks",
            "a4": "person", "a5": "robot", "a6": None,
        })
        # Idempotent: resolved entries are untouched; the unresolvable label
        # is reported again (it is still unresolved data, not a new failure).
        again = q.backfill_family(self.data)
        self.assertEqual(again, {"filled": 0, "unknown": ["Mystery widget"]})

    def test_backfilled_family_drives_the_ship_pass(self):
        # Without the backfill both bottles share no family key and would
        # both ship; after it, the second bottle loses its slot.
        self._add("b1", "Plastic bottle (clear PET)")
        self._add("b2", "Plastic water bottle (PET)")
        self._add("c1", "Plastic bag")
        self._add("d1", "E-scooter")
        self._add("e1", "Over-ear headphones")
        self._add("r1", "Wheeled suitcase")
        set_shown(self.data, {"r1": "2026-08-10"})
        write_feedback(self.data, accepted=["b1", "b2", "c1", "d1", "e1", "r1"])
        q.backfill_family(self.data)
        repo = make_repo(self.tmp, [])
        res = q.ship(self.data, repo, "2026-09-10")
        self.assertEqual(res["scenes"], ["b1", "c1", "d1", "e1", "r1"])


class UnshownOrderTest(unittest.TestCase):
    def test_oldest_first(self):
        state = {"scenes": {
            "a": {"id": "a", "added": "2026-09-09", "shown": None},
            "b": {"id": "b", "added": "2026-09-08", "shown": None},
            "c": {"id": "c", "added": "2026-09-08", "shown": "2026-09-07"},
        }}
        order = q.unshown_oldest_first(state, accepted={"a", "b"})
        self.assertEqual([s["id"] for s in order], ["b", "a"])

    def test_rejected_ids_are_skipped(self):
        # ticket #1113: human-rejected scenes must never be candidates, even
        # if they were previously accepted (rejected wins, #1163)
        state = {"scenes": {
            "a": {"id": "a", "added": "2026-09-08", "shown": None},
            "b": {"id": "b", "added": "2026-09-08", "shown": None},
            "c": {"id": "c", "added": "2026-09-07", "shown": None},
        }}
        order = q.unshown_oldest_first(state, accepted={"a", "b", "c"},
                                       rejected={"a", "c"})
        self.assertEqual([s["id"] for s in order], ["b"])

    def test_unmoderated_are_not_candidates(self):
        # ticket #1163: only accepted scenes are ship-eligible; an unmoderated
        # scene (in neither accepted nor rejected) is playtest-only and must
        # not be chosen by the pipeline
        state = {"scenes": {
            "a": {"id": "a", "added": "2026-09-08", "shown": None},
            "b": {"id": "b", "added": "2026-09-08", "shown": None},
        }}
        order = q.unshown_oldest_first(state, accepted={"b"}, rejected=set())
        self.assertEqual([s["id"] for s in order], ["b"])

    def test_shown_oldest_first_orders_by_shown_date(self):
        # ticket #1206: back-catalogue candidates are ordered least recently
        # shown first; rejected and unmoderated scenes stay out
        state = {"scenes": {
            "a": {"id": "a", "added": "2026-08-01", "shown": "2026-08-20"},
            "b": {"id": "b", "added": "2026-08-01", "shown": "2026-08-10"},
            "c": {"id": "c", "added": "2026-08-01", "shown": "2026-08-15"},
            "d": {"id": "d", "added": "2026-08-01", "shown": "2026-08-05"},
            "e": {"id": "e", "added": "2026-08-01", "shown": None},
        }}
        order = q.shown_oldest_first(state, accepted={"a", "b", "c", "d"},
                                     rejected={"d"})
        self.assertEqual([s["id"] for s in order], ["b", "c", "a"])


class ExclusionTest(unittest.TestCase):
    """feedback.json handling (ticket #1113): scripts read rejections the
    dashboard writes and never ship / re-add / migrate rejected scenes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_excl_"))
        self.data = self.tmp / "data"

    def _add(self, eid, date="2026-09-08"):
        e = valid_entry(eid)
        make_img(self.tmp / f"ed_{eid}.jpg")
        make_img(self.tmp / f"or_{eid}.jpg")
        q.state_add(self.data, e, self.tmp / f"ed_{eid}.jpg",
                    self.tmp / f"or_{eid}.jpg", date)
        return e

    def test_rejected_ids_loads_feedback_keys(self):
        self.assertEqual(q.rejected_ids(self.data), set())
        write_feedback(self.data, rejected=["a1", "b2"])
        self.assertEqual(q.rejected_ids(self.data), {"a1", "b2"})

    def test_rejected_ids_reads_legacy_excluded_key(self):
        # ticket #1207: feedback.json written before the exclude->reject
        # rename carries "excluded"; readers merge it until the API rewrites
        # the file with "rejected".
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "feedback.json").write_text(json.dumps({
            "version": 1,
            "accepted": {"a1": "2026-09-08T08:00:00+02:00"},
            "excluded": {"b2": "2026-09-08T08:00:00+02:00"},
            "comments": {},
        }), encoding="utf-8")
        self.assertEqual(q.rejected_ids(self.data), {"b2"})
        self.assertEqual(q.accepted_ids(self.data), {"a1"})

    def test_accepted_ids_loads_feedback_keys(self):
        self.assertEqual(q.accepted_ids(self.data), set())
        write_feedback(self.data, accepted=["a1", "b2"])
        self.assertEqual(q.accepted_ids(self.data), {"a1", "b2"})

    def test_add_refuses_rejected_id(self):
        self._add("keep")
        write_feedback(self.data, rejected=["keep"])
        e = valid_entry("keep")
        make_img(self.tmp / "ed2.jpg")
        make_img(self.tmp / "or2.jpg")
        with self.assertRaises(ValueError):
            q.state_add(self.data, e, self.tmp / "ed2.jpg",
                        self.tmp / "or2.jpg", "2026-09-08")

    def test_ship_skips_rejected_unshown_scene(self):
        # 6 unshown accepted, one of them also rejected: ship must pick only
        # the accepted-unrejected ones
        for i in range(6):
            self._add(f"n{i}")
        write_feedback(self.data, accepted=[f"n{i}" for i in range(6)],
                       rejected=["n2"])
        repo = make_repo(self.tmp, [])  # empty shell repo
        res = q.ship(self.data, repo, "2026-09-09")
        self.assertNotIn("n2", res["scenes"])
        # the rejected scene stays unshown after shipping
        state = q.load_state(self.data)
        self.assertIsNone(state["scenes"]["n2"]["shown"])
        self.assertEqual(
            len([s for s in state["scenes"].values() if s.get("shown")]), 5)

    def test_ship_refuses_rejected_explicit_id(self):
        self._add("n1")
        write_feedback(self.data, rejected=["n1"])
        repo = make_repo(self.tmp, [])
        with self.assertRaises(ValueError):
            q.ship(self.data, repo, "2026-09-09", ids=["n1"])

    def test_ship_rejects_unmoderated_explicit_id(self):
        # ticket #1163: only accepted scenes are ship-eligible, even via ids
        self._add("n1")
        repo = make_repo(self.tmp, [])
        with self.assertRaises(ValueError):
            q.ship(self.data, repo, "2026-09-09", ids=["n1"])

    def test_ship_skips_unmoderated_auto(self):
        # ticket #1163: with no accepted scenes, ship must not pick the
        # unmoderated pool on its own
        for i in range(6):
            self._add(f"n{i}")
        repo = make_repo(self.tmp, [])
        with self.assertRaises(ValueError):
            q.ship(self.data, repo, "2026-09-09")

    def test_status_counts_and_flags_rejected(self):
        import contextlib
        import io
        for i in range(4):
            self._add(f"n{i}", date="2026-09-08")
        # ship 2 (must accept them first, #1163) so the pool has shown +
        # unshown, then reject one shown
        repo = make_repo(self.tmp, [])
        write_feedback(self.data, accepted=["n0", "n1", "n2", "n3"])
        q.ship(self.data, repo, "2026-09-09", ids=["n0", "n1"])
        write_feedback(self.data, accepted=["n2", "n3"], rejected=["n0", "n3"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = q.cmd_status(self.data)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("total: 4", out)
        self.assertIn("shown: 1", out)     # n1 (n0 rejected -> not shown)
        self.assertIn("unshown: 1", out)   # n2 (n3 rejected -> not unshown)
        self.assertIn("rejected: 2", out)
        self.assertIn("accepted: 2", out)  # n0+n3 accepted but rejected; count is raw accept keys
        self.assertIn("rejected ids: n0, n3", out)

    def test_init_skips_rejected_ids(self):
        write_feedback(self.data, rejected=["m1"])
        repo = make_repo(self.tmp, [valid_entry("m1"), valid_entry("m2")])
        n = q.state_init(self.data, repo, "2026-09-07")
        self.assertEqual(n, 1)
        self.assertNotIn("m1", q.load_state(self.data)["scenes"])


class RecentAnomaliesTest(unittest.TestCase):
    """Anomaly-variety view (ticket #1115): status prints the newest-added
    anomalies so the daily cron can stop drawing from a tiny implicit set
    (the first 19 queue scenes were 4 objects) and can mix object + person
    anomalies."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agq_rec_"))
        self.data = self.tmp / "data"

    def _add(self, eid, anomaly, date="2026-09-08"):
        e = valid_entry(eid)
        e["anomaly"] = anomaly
        make_img(self.tmp / f"ed_{eid}.jpg")
        make_img(self.tmp / f"or_{eid}.jpg")
        q.state_add(self.data, e, self.tmp / f"ed_{eid}.jpg",
                    self.tmp / f"or_{eid}.jpg", date)

    def test_recent_scenes_newest_first_capped(self):
        state = {"scenes": {}}
        for i in range(12):
            state["scenes"][f"s{i:02d}"] = {
                "id": f"s{i:02d}", "added": "2026-09-08", "anomaly": f"a{i}",
            }
        rec = q.recent_scenes(state, n=8)
        self.assertEqual(len(rec), 8)
        # newest = highest id (same added date), returned first
        self.assertEqual(rec[0]["id"], "s11")

    def test_status_lists_recent_anomalies_newest_first(self):
        import contextlib
        import io
        self._add("a1", "Plastic bottle", date="2026-09-07")
        self._add("a2", "Drink can", date="2026-09-08")
        self._add("a3", "Time traveler: man with modern sneakers",
                  date="2026-09-08")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = q.cmd_status(self.data)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("recent anomalies (newest first):", out)
        # newest-added first (a3 > a2 on the same date, a1 on the day before)
        self.assertLess(out.index("Time traveler"),
                        out.index("Drink can"))
        self.assertLess(out.index("Drink can"),
                        out.index("Plastic bottle"))
        self.assertIn("a3: Time traveler: man with modern sneakers", out)


if __name__ == "__main__":
    unittest.main()
