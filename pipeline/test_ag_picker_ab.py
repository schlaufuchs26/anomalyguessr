"""Tests for pipeline/ag_picker_ab.py (ticket #1217).

Run with: python3 -m unittest discover -s pipeline -t pipeline -p 'test_ag_*.py'

No network and no API spend: the picker is injected, and the only image
bytes the harness reads are dummy files. The live OpenRouter path is what a
manual `ag_picker_ab.py --env …` run exercises.
"""

import contextlib
import io
import json
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

import ag_picker_ab as ab
import ag_sources


def make_img(path: Path, w=1200, h=800, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
        check=True, capture_output=True,
    )


def source(sid="commons-market-1900-abc123",
           title="Busy market street in Springfield, 1900",
           date="1900", width=1200, height=800):
    return {
        "id": sid,
        "repository": "Wikimedia Commons",
        "fileUrl": f"https://commons.wikimedia.org/wiki/File:{sid}.jpg",
        "originalTitle": title,
        "date": date,
        "place": "Springfield",
        "license": "Public domain",
        "licenseUrl": "",
        "description": "A busy market with shoppers.",
        "image": f"images/{sid}.jpg",
        "width": width, "height": height,
        "used": False,
        "added": "2026-09-11",
        "raw": {"categories": "Markets|Streets", "artist": "A. Photographer"},
    }


def first_label(prompt: str) -> str:
    """The first candidate label in a picker prompt (fake answers)."""
    m = re.search(r'1\. "(.+?)" - ', prompt)
    assert m, prompt
    return m.group(1)


def read_chunk(png: bytes, tag: bytes) -> bytes:
    """The payload of the first PNG chunk with this tag."""
    off = 8
    while off < len(png):
        length = struct.unpack(">I", png[off:off + 4])[0]
        if png[off + 4:off + 8] == tag:
            return png[off + 8:off + 8 + length]
        off += 12 + length
    raise AssertionError(f"no {tag!r} chunk in {png[:20]!r}")


def body_for(label: str, cost=0.0001, prompt_tokens=100):
    return {
        "choices": [{"message": {"content": json.dumps(
            {"label": label, "reason": "fits"})}}],
        "usage": {"cost": cost, "prompt_tokens": prompt_tokens,
                  "completion_tokens": 15},
    }


class TempDataMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        (self.data_dir / "sources" / "images").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write_source(self, src, w=1200, h=800):
        make_img(self.data_dir / "sources" / src["image"], w, h)
        index = ag_sources.load_index(self.data_dir)
        index["sources"][src["id"]] = src
        ag_sources.save_index(self.data_dir, index)
        return src


class BlankPngTests(unittest.TestCase):
    def test_signature_and_dimensions(self):
        png = ab.blank_png(40, 25)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        # IHDR payload starts at byte 16: width, height, bit depth, colour.
        w, h, depth, colour = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((w, h, depth, colour), (40, 25, 8, 2))

    def test_pixels_are_uniform_gray(self):
        png = ab.blank_png(4, 3, gray=128)
        raw = zlib.decompress(read_chunk(png, b"IDAT"))
        # one filter byte + 3 bytes per pixel per row
        self.assertEqual(len(raw), 3 * (1 + 3 * 4))
        self.assertEqual(raw, (b"\x00" + b"\x80\x80\x80" * 4) * 3)

    def test_zero_size_is_clamped(self):
        png = ab.blank_png(0, 0)
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (1, 1))

    def test_data_url_prefix(self):
        url = ab.blank_data_url(10, 10)
        self.assertTrue(url.startswith("data:image/png;base64,"))


class ImageUrlTests(TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_source(source())

    def test_image_variant_sends_the_source_photo(self):
        url = ab.image_url_for("image", self.src, self.data_dir)
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))

    def test_blank_variant_sends_a_png_of_the_source_size(self):
        url = ab.image_url_for("blank", self.src, self.data_dir)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(len(url), len(ab.blank_data_url(1200, 800)))

    def test_text_variant_sends_no_image(self):
        self.assertIsNone(ab.image_url_for("text", self.src, self.data_dir))

    def test_missing_source_image_is_not_a_data_url(self):
        src = source(sid="commons-gone")
        self.assertIsNone(ab.image_url_for("image", src, self.data_dir))


class MajorityTests(unittest.TestCase):
    def test_majority_and_tie_break_to_first(self):
        self.assertEqual(ab.majority(["a", "b", "a"]), "a")
        self.assertEqual(ab.majority(["b", "a", "b", "a"]), "b")
        self.assertIsNone(ab.majority([]))
        self.assertEqual(ab.majority([None, None, "a"]), "a")

    def test_stable_requires_every_repeat_to_agree(self):
        self.assertTrue(ab.stable(["a", "a", "a"]))
        self.assertFalse(ab.stable(["a", "a", "b"]))
        self.assertFalse(ab.stable(["a", None, None]))


class AskSourceTests(TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_source(source())
        self.cands = ab.candidates_for(self.src, ab.DEFAULT_SEED)
        self.A = self.cands[0]["label"]
        self.B = self.cands[1]["label"]

    def test_every_variant_sees_the_same_prompt_and_candidates(self):
        seen = {}

        def call(prompt, image_url):
            seen.setdefault(prompt, set()).add(bool(image_url))
            return body_for(first_label(prompt))

        row = ab.ask_source(self.src, self.cands, self.data_dir, call,
                            repeats=2)
        self.assertEqual(len(seen), 1, "one prompt per source, shared")
        self.assertEqual(len(row["candidates"]), len(self.cands))

    def test_repeats_are_recorded_and_majority_wins(self):
        answers = iter([self.A, self.A, self.B, self.B, self.B, self.B])

        def call(prompt, image_url):
            return body_for(next(answers))

        row = ab.ask_source(self.src, self.cands, self.data_dir, call,
                            repeats=2)
        self.assertEqual(row["picks"]["image"], [self.A, self.A])
        self.assertEqual(row["picks"]["text"], [self.B, self.B])
        self.assertTrue(row["stable"]["image"])
        self.assertEqual(row["majority"]["text"], self.B)
        self.assertEqual(row["usage"]["image"]["prompt_tokens"], 200)
        self.assertAlmostEqual(row["usage"]["text"]["cost"], 0.0002)
        self.assertEqual(len(row["latency_s"]["image"]), 2)

    def test_unstable_repeats_are_visible(self):
        answers = iter([self.A, self.B])

        def call(prompt, image_url):
            return body_for(next(answers))

        row = ab.ask_source(self.src, self.cands, self.data_dir, call,
                            repeats=2)
        self.assertFalse(row["stable"]["image"])
        self.assertEqual(row["errors"]["image"], [])

    def test_raising_call_is_an_error_not_a_crash(self):
        def call(prompt, image_url):
            raise RuntimeError("boom")

        row = ab.ask_source(self.src, self.cands, self.data_dir, call,
                            repeats=2)
        self.assertEqual(row["picks"]["image"], [])
        self.assertIsNone(row["majority"]["image"])
        self.assertEqual(len(row["errors"]["image"]), 2)
        self.assertIn("boom", row["errors"]["image"][0])

    def test_off_list_answer_is_a_fallback(self):
        def call(prompt, image_url):
            return body_for("not in the catalog")

        row = ab.ask_source(self.src, self.cands, self.data_dir, call,
                            repeats=1)
        self.assertIsNone(row["majority"]["text"])
        self.assertIn("off-list", row["errors"]["text"][0])

    def test_empty_answer_is_reported(self):
        def call(prompt, image_url):
            return {"choices": [{"message": {"content": ""}}],
                    "usage": {}}

        row = ab.ask_source(self.src, self.cands, self.data_dir, call,
                            repeats=1)
        self.assertIn("empty model answer", row["errors"]["text"][0])

    def test_missing_image_fails_only_that_variant(self):
        src = source(sid="commons-gone")
        row = ab.ask_source(src, self.cands, self.data_dir,
                            lambda p, u: body_for(first_label(p)),
                            repeats=1)
        self.assertEqual(row["errors"]["image"], ["missing image"])
        self.assertIsNone(row["majority"]["image"])
        self.assertIsNotNone(row["majority"]["blank"])

    def test_candidates_come_from_the_hard_rules(self):
        settings = ab.g.infer_settings(self.src)
        crowd = ab.g.has_crowd(self.src)
        self.assertIn("market", settings)
        self.assertTrue(self.cands)
        for e in self.cands:
            self.assertTrue(ab.g.anomaly_fits(e, settings, 1900, crowd))


def row(source_id, image_labels, blank_labels, text_labels, rule="A",
        prompt_tokens=100, cost=0.0002):
    picks = {"image": image_labels, "blank": blank_labels,
             "text": text_labels}
    return {
        "source": source_id, "rule_pick": rule, "candidates": ["A", "B"],
        "picks": picks,
        "majority": {v: ab.majority(p) for v, p in picks.items()},
        "stable": {v: ab.stable(p) for v, p in picks.items()},
        "errors": {v: [] for v in ab.VARIANTS},
        "usage": {v: {"prompt_tokens": prompt_tokens, "completion_tokens": 10,
                      "cost": cost} for v in ab.VARIANTS},
        "latency_s": {v: [1.0, 2.0] for v in ab.VARIANTS},
    }


class SummarizeTests(unittest.TestCase):
    def test_rates_counts_and_totals(self):
        rows = [
            row("s1", ["A"], ["A"], ["A"]),               # all agree
            row("s2", ["A"], ["B"], ["A"]),               # blank changed
            row("s3", ["A"], ["A"], ["B"], cost=0.0004),  # text changed
        ]
        s = ab.summarize(rows)
        self.assertEqual(s["sources"], 3)
        self.assertEqual(s["changed_vs_baseline"], {
            "image": 0.0, "blank": 0.333, "text": 0.333})
        self.assertEqual(s["pick_stable_rate"]["image"], 1.0)
        self.assertEqual(s["prompt_tokens"]["image"], 300)
        self.assertEqual(s["prompt_tokens"]["text"], 300)
        self.assertEqual(s["cost_usd"]["text"], 0.0008)
        self.assertEqual(s["latency_s_median"]["image"], 1.5)
        self.assertEqual(s["latency_s_total"]["image"], 9.0)

    def test_failed_variant_counts_as_changed_and_fallback(self):
        rows = [row("s1", ["A"], ["A"], [])]
        s = ab.summarize(rows)
        self.assertEqual(s["changed_vs_baseline"]["text"], 1.0)
        self.assertEqual(s["fallback_sources"]["text"], 1)
        self.assertEqual(s["pick_stable_rate"]["text"], 0.0)

    def test_differs_from_rule_counts_only_successful_picks(self):
        rows = [row("s1", ["A"], ["B"], []), row("s2", ["B"], ["A"], [])]
        s = ab.summarize(rows)
        self.assertEqual(s["differs_from_rule"]["image"], 0.5)
        self.assertEqual(s["differs_from_rule"]["blank"], 0.5)
        self.assertEqual(s["differs_from_rule"]["text"], 0.0)

    def test_empty_rows_do_not_divide_by_zero(self):
        s = ab.summarize([])
        self.assertEqual(s["sources"], 0)
        self.assertEqual(s["changed_vs_baseline"]["text"], 0.0)


class RunTests(TempDataMixin, unittest.TestCase):
    def fake_call(self, prompt, image_url):
        return body_for(first_label(prompt))

    def test_report_shape_and_deterministic_sampling(self):
        for i in range(3):
            self.write_source(source(sid=f"commons-src-{i}",
                                     title="Busy market street, 1900"))
        args = ab.parse_args(["--data", str(self.data_dir), "--count", "2",
                              "--repeats", "1"])
        r1 = ab.run(args, self.fake_call)
        r2 = ab.run(args, self.fake_call)
        self.assertEqual(r1["sampled"], 2)
        self.assertEqual(r1["available"], 3)
        self.assertEqual([x["source"] for x in r1["rows"]],
                         [x["source"] for x in r2["rows"]])
        self.assertEqual(r1["summary"]["sources"], 2)
        self.assertEqual(r1["summary"]["changed_vs_baseline"]["text"], 0.0)
        self.assertEqual(r1["summary"]["pick_stable_rate"]["image"], 1.0)

    def test_sources_without_an_image_are_not_sampled(self):
        self.write_source(source(sid="commons-has-image"))
        index = ag_sources.load_index(self.data_dir)
        index["sources"]["commons-no-image"] = source(sid="commons-no-image")
        ag_sources.save_index(self.data_dir, index)
        args = ab.parse_args(["--data", str(self.data_dir)])
        report = ab.run(args, self.fake_call)
        self.assertEqual([r["source"] for r in report["rows"]],
                         ["commons-has-image"])

    def test_progress_flag_prints_one_line_per_source(self):
        self.write_source(source(sid="commons-progress"))
        args = ab.parse_args(["--data", str(self.data_dir), "--progress"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ab.run(args, self.fake_call)
        self.assertIn("[1/1] commons-progress", buf.getvalue())


class JobsArgTests(unittest.TestCase):
    def test_default_is_a_pool(self):
        self.assertEqual(ab.parse_args([]).jobs, ab.DEFAULT_JOBS)
        self.assertGreater(ab.DEFAULT_JOBS, 1)

    def test_zero_or_negative_jobs_is_rejected(self):
        for value in ("0", "-2"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        ab.parse_args(["--jobs", value])


class ParallelRunTests(TempDataMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for i in range(4):
            self.write_source(source(sid=f"commons-par-{i}",
                                     title="Busy market street, 1900"))

    def fake_call(self, prompt, image_url):
        return body_for(first_label(prompt))

    def tracking_call(self, inflight, lock, delay=0.05):
        """A fake call that records how many calls run at the same time."""
        def call(prompt, image_url):
            with lock:
                inflight["now"] += 1
                inflight["max"] = max(inflight["max"], inflight["now"])
            time.sleep(delay)
            with lock:
                inflight["now"] -= 1
            return body_for(first_label(prompt))
        return call

    def run_with_jobs(self, jobs, call, count=4):
        args = ab.parse_args(["--data", str(self.data_dir), "--count",
                              str(count), "--repeats", "1", "--jobs",
                              str(jobs)])
        return ab.run(args, call)

    def test_jobs_overlap_the_sources(self):
        inflight = {"now": 0, "max": 0}
        report = self.run_with_jobs(
            4, self.tracking_call(inflight, threading.Lock()))
        self.assertEqual(report["summary"]["sources"], 4)
        self.assertEqual(report["jobs"], 4)
        self.assertGreater(inflight["max"], 1,
                           "the pool serialized despite --jobs 4")

    def test_jobs_one_runs_serially(self):
        inflight = {"now": 0, "max": 0}
        report = self.run_with_jobs(
            1, self.tracking_call(inflight, threading.Lock()))
        self.assertEqual(report["summary"]["sources"], 4)
        self.assertEqual(inflight["max"], 1)

    def test_report_matches_the_serial_run(self):
        def without_latency(report):
            """Latency is wall time and differs by construction; ``jobs`` only
            labels how the run was scheduled, not what it measured."""
            report = json.loads(json.dumps(report))
            report.pop("jobs", None)
            for row in report["rows"]:
                row.pop("latency_s", None)
            for key in ("latency_s_median", "latency_s_total"):
                report["summary"].pop(key, None)
            return report

        serial = self.run_with_jobs(1, self.fake_call)
        parallel = self.run_with_jobs(4, self.fake_call)
        self.assertEqual(without_latency(serial), without_latency(parallel))
        self.assertEqual([r["source"] for r in parallel["rows"]],
                         [r["source"] for r in serial["rows"]],
                         "the pool must keep the sampled order")
        self.assertEqual(len(parallel["rows"]), 4)

    def test_progress_still_prints_one_line_per_source(self):
        args = ab.parse_args(["--data", str(self.data_dir), "--count", "4",
                              "--repeats", "1", "--jobs", "3", "--progress"])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ab.run(args, self.fake_call)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(l.startswith("[") for l in lines), lines)


class MainTests(TempDataMixin, unittest.TestCase):
    def test_without_api_key_it_fails_before_any_call(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            code = ab.main(["--data", str(self.data_dir)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
