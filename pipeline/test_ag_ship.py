"""Tests for pipeline/ag_ship.py (ticket #1508).

The daily ship wrapper and the live-manifest watchdog: manifest parsing,
staleness arithmetic, the HTTP fetch (against a local server, no external
network), and both commands end to end with a temp data dir + fake game repo.
"""

import contextlib
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ag_ship as s


def write_manifest(repo: Path, date: str) -> None:
    scenes = repo / "scenes"
    scenes.mkdir(parents=True, exist_ok=True)
    (scenes / "manifest.json").write_text(
        json.dumps({"version": 2, "date": date, "scenes": []}),
        encoding="utf-8")


def scene(eid: str, added: str, anomaly: str = "Plastic bottle",
          family: str = "drinks") -> dict:
    return {
        "id": eid,
        "title": f"Scene {eid}",
        "place": "Testville",
        "year": "1900",
        "credit": "Public Domain",
        "anomaly": anomaly,
        "family": family,
        "added": added,
        "shown": None,
    }


def make_data_dir(root: Path, scenes: list, accepted: list) -> Path:
    data = root / "data"
    (data / "library").mkdir(parents=True)
    for sc in scenes:
        eid = sc["id"]
        d = data / "library" / eid
        d.mkdir()
        (d / f"{eid}.jpg").write_bytes(b"edited")
        (d / f"{eid}-original.jpg").write_bytes(b"original")
    (data / "state.json").write_text(json.dumps({
        "version": 1, "last_shipped": None, "scenes": {sc["id"]: sc for sc in scenes},
    }), encoding="utf-8")
    (data / "feedback.json").write_text(json.dumps({
        "version": 1,
        "accepted": {eid: "2026-09-15T00:00:00+02:00" for eid in accepted},
        "rejected": {},
    }), encoding="utf-8")
    return data


class ManifestParsingTest(unittest.TestCase):
    def test_date_round_trips(self):
        self.assertEqual(
            s.parse_manifest_date('{"version": 2, "date": "2026-09-15"}'),
            "2026-09-15")

    def test_missing_date_is_an_error(self):
        with self.assertRaises(ValueError):
            s.parse_manifest_date('{"version": 2}')

    def test_malformed_date_is_an_error(self):
        with self.assertRaises(ValueError):
            s.parse_manifest_date('{"date": "yesterday"}')

    def test_non_json_is_an_error(self):
        with self.assertRaises(ValueError):
            s.parse_manifest_date("<html>not found</html>")

    def test_manifest_date_reads_the_repo_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_manifest(repo, "2026-09-15")
            self.assertEqual(s.manifest_date(repo), "2026-09-15")

    def test_manifest_date_missing_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                s.manifest_date(Path(tmp))


class StalenessTest(unittest.TestCase):
    def test_today_is_not_stale(self):
        self.assertFalse(s.is_stale("2026-09-15", "2026-09-15", 0))

    def test_yesterday_is_stale_at_zero_tolerance(self):
        self.assertTrue(s.is_stale("2026-09-14", "2026-09-15", 0))

    def test_yesterday_is_tolerated_at_one_day(self):
        self.assertFalse(s.is_stale("2026-09-14", "2026-09-15", 1))

    def test_two_days_old_is_stale_at_one_day(self):
        self.assertTrue(s.is_stale("2026-09-13", "2026-09-15", 1))

    def test_future_date_is_not_stale(self):
        # Clock skew on the CDN must not read as a frozen site.
        self.assertFalse(s.is_stale("2026-09-16", "2026-09-15", 0))


@contextlib.contextmanager
def serve(body: bytes, status: int = 200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib hook name
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the test output clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/scenes/manifest.json"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class FetchTest(unittest.TestCase):
    def test_fetch_returns_the_body(self):
        with serve(b'{"date": "2026-09-15"}') as url:
            self.assertIn("2026-09-15", s.fetch_manifest(url, retries=1))

    def test_fetch_raises_on_http_error(self):
        with serve(b"nope", status=500) as url:
            with self.assertRaises(RuntimeError):
                s.fetch_manifest(url, retries=1, retry_delay=0)


class ShipCommandTest(unittest.TestCase):
    def test_ship_writes_todays_manifest_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            repo.mkdir()
            scenes = [scene("b1", "2026-09-10"), scene("a1", "2026-09-11")]
            data = make_data_dir(root, scenes, accepted=["a1", "b1"])
            rc = s.main(["ship", "--data", str(data), "--repo", str(repo),
                         "--date", "2026-09-15"])
            self.assertEqual(rc, 0)
            self.assertEqual(s.manifest_date(repo), "2026-09-15")
            state = json.loads((data / "state.json").read_text())
            self.assertEqual(state["last_shipped"], "2026-09-15")
            self.assertEqual(sorted(state["last_shipped_ids"]), ["a1", "b1"])

    def test_ship_without_accepted_scenes_alerts_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            repo.mkdir()
            data = make_data_dir(root, [scene("a1", "2026-09-11")], accepted=[])
            with mock.patch.object(s, "notify_discord",
                                   return_value=True) as send:
                rc = s.main(["ship", "--data", str(data), "--repo", str(repo),
                             "--date", "2026-09-15", "--dm-channel", "42"])
            self.assertEqual(rc, 1)
            self.assertEqual(send.call_count, 1)
            self.assertIn("failed", send.call_args.args[1])

    def test_ship_is_idempotent_for_one_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "game"
            repo.mkdir()
            data = make_data_dir(root, [scene("a1", "2026-09-11")], ["a1"])
            argv = ["ship", "--data", str(data), "--repo", str(repo),
                    "--date", "2026-09-15"]
            self.assertEqual(s.main(argv), 0)
            self.assertEqual(s.main(argv), 0)  # second run: already shipped


class CheckCommandTest(unittest.TestCase):
    def test_current_manifest_passes(self):
        with serve(b'{"version": 2, "date": "2026-09-15"}') as url:
            rc = s.main(["check", "--manifest-url", url, "--date", "2026-09-15"])
        self.assertEqual(rc, 0)

    def test_stale_manifest_alerts(self):
        with serve(b'{"version": 2, "date": "2026-09-11"}') as url:
            with mock.patch.object(s, "notify_discord",
                                   return_value=True) as send:
                rc = s.main(["check", "--manifest-url", url,
                             "--date", "2026-09-15", "--dm-channel", "42"])
        self.assertEqual(rc, 1)
        self.assertEqual(send.call_count, 1)
        self.assertIn("2026-09-11", send.call_args.args[1])

    def test_unreachable_manifest_alerts(self):
        # A closed port: the fetch fails, which is itself worth an alert.
        with mock.patch.object(s, "notify_discord", return_value=True) as send:
            rc = s.main(["check", "--manifest-url", "http://127.0.0.1:1/m.json",
                         "--date", "2026-09-15", "--dm-channel", "42"])
        self.assertEqual(rc, 1)
        self.assertEqual(send.call_count, 1)

    def test_max_age_days_tolerates_yesterday(self):
        with serve(b'{"version": 2, "date": "2026-09-14"}') as url:
            rc = s.main(["check", "--manifest-url", url, "--date", "2026-09-15",
                         "--max-age-days", "1"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
