"""Tests for pipeline/ag_backup.py (ticket #1804).

The backup plan (which files and scene images go in, and what the cache cap
drops), the archive layout, the retention rule, and the whole run with a
faked ``gh``: draft release, upload, prune and the guards. No network.
"""

import datetime
import json
import os
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ag_backup as b


def scene(eid: str, added: str = "2026-09-10", shown=None) -> dict:
    return {"id": eid, "title": f"Scene {eid}", "added": added, "shown": shown}


def write_data(root: Path, scenes: list, rejected=(), accepted=()) -> Path:
    """A queue dir with state.json, feedback.json and one library dir per scene."""
    data = root / "data" / "anomalyguessr"
    (data / "library").mkdir(parents=True)
    (data / "state.json").write_text(json.dumps({
        "version": 1, "last_shipped": None,
        "scenes": {s["id"]: s for s in scenes},
    }), encoding="utf-8")
    (data / "feedback.json").write_text(json.dumps({
        "version": 1,
        "accepted": {i: "2026-09-15T00:00:00+02:00" for i in accepted},
        "rejected": {i: "2026-09-15T00:00:00+02:00" for i in rejected},
    }), encoding="utf-8")
    for s in scenes:
        d = data / "library" / s["id"]
        d.mkdir()
        (d / f"{s['id']}.jpg").write_bytes(b"e" * 100)
        (d / f"{s['id']}-original.jpg").write_bytes(b"o" * 100)
    return data


class PlanTest(unittest.TestCase):
    def test_plan_takes_the_queue_json_and_the_pending_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = write_data(Path(tmp), [
                scene("fresh", added="2026-09-10"),
                scene("played", added="2026-09-10", shown="2026-09-20"),
                scene("bad", added="2026-09-11"),
            ], rejected=["bad"], accepted=["fresh"])
            plan = b.plan_backup(data, b.DEFAULT_CACHE_CAP)

            # The whole pipeline state goes in: state.json alone would lose
            # the moderation verdicts.
            self.assertEqual([f["name"] for f in plan["json_files"]],
                             ["feedback.json", "state.json"])
            self.assertEqual(plan["json_bytes"],
                             sum(f["bytes"] for f in plan["json_files"]))
            # Shown scenes live in git (scenes/), rejected ones never ship.
            self.assertEqual([s["id"] for s in plan["scenes"]], ["fresh"])
            self.assertEqual(plan["pending"], 1)
            self.assertEqual(plan["skipped"], [])

    def test_plan_follows_the_ship_order(self):
        # Ship picks the oldest added scene first, so the backup lists them
        # that way too: the next days' sets survive a cap that has to drop
        # something.
        with tempfile.TemporaryDirectory() as tmp:
            data = write_data(Path(tmp), [
                scene("new", added="2026-09-12"),
                scene("old", added="2026-09-09"),
                scene("mid", added="2026-09-10"),
            ])
            plan = b.plan_backup(data, b.DEFAULT_CACHE_CAP)
            self.assertEqual([s["id"] for s in plan["scenes"]],
                             ["old", "mid", "new"])

    def test_cache_cap_drops_the_tail_and_keeps_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = write_data(Path(tmp), [
                scene("old", added="2026-09-09"),
                scene("mid", added="2026-09-10"),
                scene("new", added="2026-09-11"),
            ])
            # Every library dir is 200 bytes; the cap holds exactly one.
            plan = b.plan_backup(data, 250)
            self.assertEqual([s["id"] for s in plan["scenes"]], ["old"])
            self.assertEqual([s["reason"] for s in plan["skipped"]],
                             ["over-cap", "over-cap"])
            self.assertEqual(plan["cache_bytes"], 200)
            # The JSON state is never capped: a restore still knows the queue.
            self.assertEqual(len(plan["json_files"]), 2)

    def test_plan_reports_a_scene_without_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("gone")])
            for f in (data / "library" / "gone").glob("*"):
                f.unlink()
            plan = b.plan_backup(data, b.DEFAULT_CACHE_CAP)
            self.assertEqual(plan["scenes"], [])
            self.assertEqual(plan["skipped"],
                             [{"id": "gone", "reason": "no-images", "bytes": 0}])


class ArchiveTest(unittest.TestCase):
    def test_archive_mirrors_the_repo_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh"), scene("played",
                                                           shown="2026-09-20")])
            plan = b.plan_backup(data, b.DEFAULT_CACHE_CAP)
            tarball = root / "out" / "data.tar.gz"
            size = b.build_tarball(data, plan, tarball, "2026-09-22")

            self.assertEqual(size, tarball.stat().st_size)
            with tarfile.open(tarball) as tar:
                names = tar.getnames()
                self.assertIn("data/anomalyguessr/state.json", names)
                self.assertIn("data/anomalyguessr/feedback.json", names)
                self.assertIn("data/anomalyguessr/library/fresh/fresh.jpg", names)
                self.assertIn(
                    "data/anomalyguessr/library/fresh/fresh-original.jpg", names)
                self.assertNotIn("data/anomalyguessr/library/played", names)
                info = json.load(tar.extractfile("BACKUP-INFO.json"))
            self.assertEqual(info["date"], "2026-09-22")
            self.assertEqual(info["scenes"], ["fresh"])
            self.assertEqual(info["pending_scenes"], 1)
            self.assertEqual(sorted(f["name"] for f in info["json_files"]),
                             ["feedback.json", "state.json"])


class RetentionTest(unittest.TestCase):
    # 2026-09-22 is a Tuesday: 09-20 and 09-13 are Sundays, 09-14 a Monday.
    NOW = datetime.date(2026, 9, 22)

    def test_retention_keeps_recent_sundays_and_firsts(self):
        releases = [
            ("ag-data-2026-09-22", ""),   # today
            ("ag-data-2026-09-20", ""),   # Sunday, 2 days
            ("ag-data-2026-09-15", ""),   # 7 days, kept by the daily window
            ("ag-data-2026-09-14", ""),   # Monday, 8 days: no
            ("ag-data-2026-09-13", ""),   # Sunday, 9 days: kept
            ("ag-data-2026-09-01", ""),   # 21 days, a Tuesday: no
            ("ag-data-2026-08-15", ""),   # over 30 days, mid-month: no
            ("ag-data-2026-08-01", ""),   # over 30 days, the 1st: kept
            ("schlaufuchs-2026-08-15", ""),  # not ours
        ]
        self.assertEqual(b.retention_plan(releases, self.NOW),
                         ["ag-data-2026-08-15", "ag-data-2026-09-01",
                          "ag-data-2026-09-14"])


class RunTest(unittest.TestCase):
    """The whole command with a faked gh: no network, no release."""

    def fake_gh(self, calls, releases=(), view=None):
        """Fake gh: release view answers ``view`` (None = no release)."""
        def run(*args, check=True):
            calls.append(args)
            if args[:2] == ("release", "view"):
                if view is None:
                    return subprocess.CompletedProcess(args, 1, "", "not found")
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"isDraft": view}), "")
            out = (json.dumps([{"tagName": t, "createdAt": ""}
                               for t in releases])
                   if args[:2] == ("release", "list") else "")
            return subprocess.CompletedProcess(args, 0, out, "")
        return run

    def test_main_uploads_a_draft_release_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh")])
            calls = []
            with mock.patch.object(b, "gh", self.fake_gh(
                    calls, releases=["ag-data-2026-08-15"])), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": "test"}):
                rc = b.main(["--data", str(data), "--date", "2026-09-22",
                             "--work-dir", str(root / "work"),
                             "--slug", "acme/game"])
            self.assertEqual(rc, 0)
            create = [c for c in calls if c[:2] == ("release", "create")][0]
            self.assertIn("--draft", create)
            self.assertIn("ag-data-2026-09-22", create)
            self.assertIn("acme/game", create)
            upload = [c for c in calls if c[:2] == ("release", "upload")][0]
            self.assertIn("ag-data-2026-09-22", upload)
            self.assertIn("anomalyguessr-data-2026-09-22.tar.gz", upload[3])
            deletes = [c for c in calls if c[:2] == ("release", "delete")]
            self.assertEqual([c[2] for c in deletes], ["ag-data-2026-08-15"])
            # The staged tarball does not accumulate.
            self.assertEqual(list((root / "work").glob("ag-backup.*")), [])

    def test_rerun_reuses_an_existing_draft(self):
        # A second run on the same day must not leave a second draft behind:
        # the existing one gets the fresh asset.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh")])
            calls = []
            with mock.patch.object(b, "gh", self.fake_gh(calls, view=True)), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": "test"}):
                rc = b.main(["--data", str(data), "--date", "2026-09-22",
                             "--work-dir", str(root / "work"),
                             "--slug", "acme/game"])
            self.assertEqual(rc, 0)
            self.assertEqual([c for c in calls if c[:2] == ("release", "create")],
                             [])
            self.assertEqual(len([c for c in calls
                                  if c[:2] == ("release", "upload")]), 1)

    def test_main_refuses_a_published_release(self):
        # Publishing the draft makes the queue world-readable; the run must
        # stop instead of refreshing that asset.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh")])
            calls = []
            with mock.patch.object(b, "gh", self.fake_gh(calls, view=False)), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": "test"}):
                rc = b.main(["--data", str(data), "--date", "2026-09-22",
                             "--work-dir", str(root / "work"),
                             "--slug", "acme/game"])
            self.assertEqual(rc, 1)
            self.assertEqual([c for c in calls if c[:2] == ("release", "upload")],
                             [])

    def test_local_builds_the_tarball_without_gh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh")])

            def explode(*args, **kwargs):
                raise AssertionError("--local must not call gh")

            with mock.patch.object(b, "gh", explode):
                rc = b.main(["--data", str(data), "--date", "2026-09-22",
                             "--work-dir", str(root / "work"), "--local"])
            self.assertEqual(rc, 0)
            tarballs = list((root / "work").glob("*/anomalyguessr-data-*.tar.gz"))
            self.assertEqual(len(tarballs), 1)
            with tarfile.open(tarballs[0]) as tar:
                self.assertIn("data/anomalyguessr/state.json", tar.getnames())

    def test_main_refuses_an_empty_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data" / "anomalyguessr"
            data.mkdir(parents=True)
            calls = []
            with mock.patch.object(b, "gh", self.fake_gh(calls)), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": "test"}):
                rc = b.main(["--data", str(data), "--work-dir", str(root / "work")])
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])

    def test_main_wants_a_token_unless_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh")])
            calls = []
            with mock.patch.object(b, "gh", self.fake_gh(calls)), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": ""}):
                rc = b.main(["--data", str(data), "--work-dir", str(root / "work")])
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])

    def test_asset_guard_refuses_an_oversized_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = write_data(root, [scene("fresh")])
            calls = []
            with mock.patch.object(b, "gh", self.fake_gh(calls)), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": "test"}):
                rc = b.main(["--data", str(data), "--date", "2026-09-22",
                             "--work-dir", str(root / "work"),
                             "--max-asset-bytes", "10"])
            self.assertEqual(rc, 2)
            self.assertEqual(calls, [])


class SweepTest(unittest.TestCase):
    def test_sweep_removes_only_stale_staging_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            old = work / "ag-backup.old"
            fresh = work / "ag-backup.fresh"
            for d in (old, fresh):
                d.mkdir()
                (d / "x.tar.gz").write_bytes(b"x")
            stale = time.time() - 2 * 24 * 3600
            os.utime(old, (stale, stale))

            removed = b.sweep_stale(work)

            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
