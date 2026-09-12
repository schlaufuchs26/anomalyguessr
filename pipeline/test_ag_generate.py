"""Tests for pipeline/ag_generate.py (ticket #1169).

Run with: python3 scripts/test_ag_generate.py

No network and no API spend: the image call and the verification are
monkeypatched, sources are synthetic files created with ImageMagick, and the
queue is a temp dir. The live API path is deliberately never exercised here
(that is what the cron run is for).
"""

import datetime
import io
import json
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_catalog
import ag_generate as g
import ag_queue
import ag_sources
import ag_verify


def make_img(path: Path, w=1200, h=800, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
        check=True, capture_output=True,
    )


def source(sid="commons-market-1900-abc123",
           title="Busy market street in Springfield, 1900",
           date="1900", width=1200, height=800, image=None,
           description="", categories=""):
    return {
        "id": sid,
        "repository": "Wikimedia Commons",
        "fileUrl": f"https://commons.wikimedia.org/wiki/File:{sid}.jpg",
        "originalTitle": title,
        "date": date,
        "place": "",
        "license": "Public domain",
        "licenseUrl": "",
        "description": description,
        "image": image or f"images/{sid}.jpg",
        "width": width, "height": height,
        "used": False,
        "added": "2026-09-11",
        "raw": {"categories": categories, "artist": "A. Photographer"},
    }


class TempDataMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        (self.data_dir / "sources" / "images").mkdir(parents=True)
        self._old_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        # The run loop preflights the VISION_MODEL with one real call before
        # the first image call (ticket #1307). No test here may hit the
        # network, so stub it; tests that check the wiring reassign it.
        self._old_preflight = ag_verify.preflight_vision
        ag_verify.preflight_vision = lambda *a, **kw: {
            "ok": True, "model": "stub/model", "answer": "stub"}

    def tearDown(self):
        ag_verify.preflight_vision = self._old_preflight
        if self._old_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = self._old_key
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write_source(self, src, w=1200, h=800):
        make_img(self.data_dir / "sources" / src["image"], w, h)
        index = ag_sources.load_index(self.data_dir)
        index["sources"][src["id"]] = src
        ag_sources.save_index(self.data_dir, index)
        return src


class ParseYearTests(unittest.TestCase):
    def test_title_wins_over_upload_date(self):
        s = source(title="Street scene in Agana (1899-1900)",
                   date="2005-09-30 08:11:46")
        self.assertEqual(g.parse_year(s), 1899)

    def test_upload_timestamp_without_title_year_is_not_a_photo_year(self):
        s = source(title="Unnamed street view", date="2008-11-06 23:29")
        self.assertIsNone(g.parse_year(s))

    def test_description_fallback_and_circa(self):
        s = source(title="Street scene", date="")
        s["description"] = "Photograph taken around 1913 in Hamburg."
        self.assertEqual(g.parse_year(s), 1913)

    def test_range_takes_earliest_year(self):
        self.assertEqual(g.parse_year(source(title="Market, 1905-1910")), 1905)


class SettingTests(unittest.TestCase):
    def test_market_and_crowd(self):
        s = source(title="Farmers market with shoppers, 1910",
                   categories="Markets|Streets")
        self.assertIn("market", g.infer_settings(s))
        self.assertTrue(g.has_crowd(s))

    def test_harbor(self):
        s = source(title="Steam ship at the harbor quay, 1905")
        self.assertIn("harbor", g.infer_settings(s))
        self.assertFalse(g.has_crowd(s))

    def test_empty_scene_has_no_crowd(self):
        s = source(title="Empty valley at dawn, 1905")
        self.assertFalse(g.has_crowd(s))
        self.assertEqual(g.infer_settings(s), ())


class AnomalyFitTests(unittest.TestCase):
    def entry(self, label):
        return next(e for e in ag_catalog.CATALOG if e["label"] == label)

    def test_pet_bottle_not_anachronistic_in_1980_photo(self):
        e = self.entry("Plastic bottle (clear PET)")
        self.assertFalse(g.anomaly_fits(e, ("market",), 1980, False))
        self.assertTrue(g.anomaly_fits(e, ("market",), 1950, False))

    def test_era_rule_needs_a_photo_year(self):
        e = self.entry("Plastic bottle (clear PET)")
        self.assertFalse(g.anomaly_fits(e, ("market",), None, False))

    def test_future_elements_need_no_era(self):
        e = self.entry("Robot time traveler")
        self.assertTrue(g.anomaly_fits(e, ("street",), None, True))
        self.assertFalse(g.anomaly_fits(e, ("street",), None, False))

    def test_person_requires_a_crowd(self):
        e = self.entry("Time traveler: tourist")
        self.assertFalse(g.anomaly_fits(e, ("street",), 1900, False))
        self.assertTrue(g.anomaly_fits(e, ("street",), 1900, True))

    def test_setting_mismatch(self):
        e = self.entry("Shipping container")
        self.assertFalse(g.anomaly_fits(e, ("street",), 1900, False))
        self.assertTrue(g.anomaly_fits(e, ("harbor",), 1900, False))


class PlanTests(unittest.TestCase):
    def sources(self, n=10):
        out = []
        for i in range(n):
            out.append(source(sid=f"commons-market-{i}-190{i % 10}",
                              title=f"Market street scene {i}, 1900"))
        return out

    def test_plan_is_deterministic_for_a_seed(self):
        rng1, rng2 = random.Random(7), random.Random(7)
        p1, _, _ = g.plan_day(self.sources(), rng1)
        p2, _, _ = g.plan_day(self.sources(), rng2)
        self.assertEqual([e["label"] for _, e in p1],
                         [e["label"] for _, e in p2])

    def test_variety_caps(self):
        plan, _, _ = g.plan_day(self.sources(12), random.Random(3))
        labels = [e["label"] for _, e in plan]
        families = [e["family"] for _, e in plan]
        self.assertGreaterEqual(len(set(labels)), 6)
        for label in set(labels):
            self.assertLessEqual(labels.count(label), g.MAX_PER_LABEL)
        for family in set(families):
            self.assertLessEqual(families.count(family), g.MAX_PER_FAMILY)

    def test_unknown_year_source_is_skipped_or_gets_a_future_element(self):
        quiet = source(sid="commons-quiet-1", title="Empty road", date="")
        plan, skipped, _ = g.plan_day([quiet], random.Random(1))
        self.assertEqual(len(plan) + len(skipped), 1)
        if plan:
            self.assertEqual(plan[0][1]["type"], "future")
            self.assertIsNone(plan[0][1]["min_year"])

    def test_recent_photo_without_crowd_only_gets_a_future_element(self):
        # 1995: no era-anchored object is anachronistic any more, and there
        # is no crowd for a person -> only the fictional-future gadget fits.
        s = source(sid="commons-late-1", title="Empty station platform",
                   date="1995")
        plan, skipped, _ = g.plan_day([s], random.Random(1))
        self.assertEqual(skipped, [])
        self.assertEqual(plan[0][1]["type"], "future")


class PickerTests(unittest.TestCase):
    """Anomaly picker (ticket #1211): the hard rules pre-filter, the model
    picks from that list, and every picker failure degrades to the rule pick.
    """

    def entry(self, label):
        return next(e for e in ag_catalog.CATALOG if e["label"] == label)

    def market(self, sid="commons-m-1", title="Busy market street, 1900"):
        return source(sid=sid, title=title)

    def test_parse_pick_valid_json(self):
        cands = [self.entry("Plastic bottle (clear PET)"),
                 self.entry("Traffic cone (orange)")]
        got = g.parse_pick(
            '{"label": "Traffic cone (orange)", "reason": "street corner"}',
            cands)
        self.assertEqual(got["label"], "Traffic cone (orange)")
        self.assertEqual(got["reason"], "street corner")

    def test_parse_pick_off_list_or_empty_is_none(self):
        cands = [self.entry("Plastic bottle (clear PET)")]
        self.assertIsNone(g.parse_pick(
            '{"label": "Space shuttle", "reason": "x"}', cands))
        self.assertIsNone(g.parse_pick("", cands))
        self.assertIsNone(g.parse_pick("no json here", cands))
        self.assertIsNone(g.parse_pick(None, cands))

    def test_parse_pick_tolerates_the_size_suffix_and_quotes(self):
        # Measured 2026-09-11: the model echoes the candidate line, size
        # included; that must still resolve to the exact catalog label.
        cands = [self.entry("Modern daypack"),
                 self.entry("Plastic bag (white, with handles)")]
        got = g.parse_pick(
            '{"label": "Modern daypack (a modern nylon daypack, 45-55 cm)", '
            '"reason": "x"}', cands)
        self.assertEqual(got["label"], "Modern daypack")
        got = g.parse_pick('{"label": "\\"Plastic bag (white, with handles)\\"",'
                           ' "reason": "x"}', cands)
        self.assertEqual(got["label"], "Plastic bag (white, with handles)")

    def test_picker_prompt_lists_candidates_and_demands_exact_label(self):
        cands = [self.entry("Plastic bottle (clear PET)")]
        p = g.picker_prompt(self.market(), cands)
        self.assertIn("Plastic bottle (clear PET)", p)
        self.assertIn("exact candidate label", p)
        self.assertIn("year: 1900", p)

    def test_pick_via_vision_disables_reasoning_and_sends_the_image(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            img = tmp / "src.jpg"
            make_img(img, 200, 100)
            captured = {}
            body = {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"cost": 0.0001}}

            def fake_urlopen(req, timeout=None):
                captured["payload"] = json.loads(req.data)
                return io.BytesIO(json.dumps(body).encode())

            orig = g.urllib.request.urlopen
            g.urllib.request.urlopen = fake_urlopen
            try:
                out = g.pick_via_vision(img, "pick this", "key",
                                        max_tokens=123)
            finally:
                g.urllib.request.urlopen = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        p = captured["payload"]
        # Reasoning off + tight max_tokens is the ticket-#1211 gotcha: with
        # thinking on V4.1-Flash returns content: null.
        self.assertEqual(p["reasoning"], {"enabled": False})
        self.assertEqual(p["max_tokens"], 123)
        self.assertEqual(p["model"], g.DEFAULT_PICKER_MODEL)
        # Ticket #1313: temperature 0 makes the pick reproduce.
        self.assertEqual(p["temperature"], g.DEFAULT_PICKER_TEMPERATURE)
        parts = p["messages"][0]["content"]
        self.assertTrue(any(x["type"] == "image_url" for x in parts))
        self.assertEqual(out, body)

    def test_picker_request_omits_temperature_when_none(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data)
            return io.BytesIO(json.dumps({"choices": [{}]}).encode())

        orig = g.urllib.request.urlopen
        g.urllib.request.urlopen = fake_urlopen
        try:
            g.picker_request("p", "key", temperature=None)
        finally:
            g.urllib.request.urlopen = orig
        self.assertNotIn("temperature", captured["payload"])

        g.urllib.request.urlopen = fake_urlopen
        try:
            g.picker_request("p", "key", temperature=0.4)
        finally:
            g.urllib.request.urlopen = orig
        self.assertEqual(captured["payload"]["temperature"], 0.4)

    def test_picker_request_omits_seed_and_provider_when_none(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data)
            return io.BytesIO(json.dumps({"choices": [{}]}).encode())

        orig = g.urllib.request.urlopen
        g.urllib.request.urlopen = fake_urlopen
        try:
            g.picker_request("p", "key")
        finally:
            g.urllib.request.urlopen = orig
        self.assertNotIn("seed", captured["payload"])
        self.assertNotIn("provider", captured["payload"])

    def test_picker_request_sends_seed_and_provider_when_given(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data)
            return io.BytesIO(json.dumps({"choices": [{}]}).encode())

        orig = g.urllib.request.urlopen
        g.urllib.request.urlopen = fake_urlopen
        try:
            g.picker_request("p", "key", seed=42,
                             provider={"order": ["novita"]})
        finally:
            g.urllib.request.urlopen = orig
        # Ticket #1334: both knobs exist for reproducibility experiments.
        self.assertEqual(captured["payload"]["seed"], 42)
        self.assertEqual(captured["payload"]["provider"],
                         {"order": ["novita"]})

    def test_majority_label_ties_resolve_to_the_first_pick(self):
        self.assertEqual(g.majority_label(["a", "b", "a"]), "a")
        self.assertEqual(g.majority_label(["b", "a", "b", "a"]), "b")
        self.assertIsNone(g.majority_label([]))
        self.assertEqual(g.majority_label([None, None, "a"]), "a")

    def test_make_vision_picker_majority_over_draws(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            img = tmp / "sources" / "images" / "s.jpg"
            make_img(img, 100, 60)
            cands = [self.entry("Modern daypack"),
                     self.entry("Plastic bottle (clear PET)")]
            answers = iter(['{"label": "Modern daypack"}',
                            '{"label": "Plastic bottle (clear PET)"}',
                            '{"label": "Modern daypack"}'])
            bodies = []

            def fake_call(image, prompt, api_key, base_url, model,
                          max_tokens, timeout, temperature=None):
                body = {"choices": [{"message": {"content": next(answers)}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                  "cost": 0.001}}
                bodies.append(temperature)
                return body

            orig = g.pick_via_vision
            g.pick_via_vision = fake_call
            try:
                choose = g.make_vision_picker(tmp, "key", g.DEFAULT_BASE_URL,
                                              draws=3)
                res = choose({"id": "x", "image": "images/s.jpg"}, cands)
            finally:
                g.pick_via_vision = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(res["label"], "Modern daypack")  # 2 of 3 draws
        self.assertEqual(len(bodies), 3)
        self.assertEqual(bodies[0], g.DEFAULT_PICKER_TEMPERATURE)
        self.assertEqual(res["usage"]["cost"], 0.003)
        self.assertEqual(res["usage"]["prompt_tokens"], 30)

    def test_make_vision_picker_all_draws_fail_is_a_failed_pick(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            img = tmp / "sources" / "images" / "s.jpg"
            make_img(img, 100, 60)

            def boom(*a, **kw):
                raise RuntimeError("API down")

            orig = g.pick_via_vision
            g.pick_via_vision = boom
            try:
                choose = g.make_vision_picker(tmp, "key", g.DEFAULT_BASE_URL,
                                              draws=3)
                res = choose({"id": "x", "image": "images/s.jpg"},
                             [self.entry("Modern daypack")])
            finally:
                g.pick_via_vision = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNone(res["label"])
        self.assertIn("API down", res["error"])

    def test_picker_temperature_provider_default_flag(self):
        args = g.parse_args([])
        self.assertEqual(g.picker_temperature(args),
                         g.DEFAULT_PICKER_TEMPERATURE)
        args = g.parse_args(["--picker-provider-default"])
        self.assertIsNone(g.picker_temperature(args))
        args = g.parse_args(["--picker-temperature", "0.7"])
        self.assertEqual(g.picker_temperature(args), 0.7)

    def test_picker_stats_record_temperature_and_draws(self):
        stats = g.picker_stats("llm", False, [], temperature=0.0, draws=3)
        self.assertEqual(stats["temperature"], 0.0)
        self.assertEqual(stats["draws"], 3)
        self.assertNotIn("temperature", g.picker_stats("rules", False, []))

    def test_llm_pick_is_used(self):
        seen = []

        def choose(src, cands):
            seen.append([e["label"] for e in cands])
            return {"label": cands[-1]["label"], "reason": "picked",
                    "usage": {"cost": 0.0002, "prompt_tokens": 100,
                              "completion_tokens": 20}}

        plan, _, picks = g.plan_day([self.market()], random.Random(1),
                                    choose=choose)
        self.assertEqual(plan[0][1]["label"], seen[0][-1])
        self.assertEqual(picks[0]["mode"], "llm")
        self.assertEqual(picks[0]["reason"], "picked")
        self.assertEqual(picks[0]["candidates"], seen[0])
        stats = g.picker_stats("llm", False, picks)
        self.assertEqual(stats["llm"], 1)
        self.assertAlmostEqual(stats["cost"], 0.0002)

    def test_off_list_label_falls_back_to_the_rule_pick(self):
        def choose(src, cands):
            return {"label": "Space shuttle", "reason": "nope"}

        plan, _, picks = g.plan_day([self.market()], random.Random(1),
                                    choose=choose)
        rule_plan, _, _ = g.plan_day([self.market()], random.Random(1))
        self.assertEqual(plan[0][1]["label"], rule_plan[0][1]["label"])
        self.assertEqual(picks[0]["mode"], "fallback")
        self.assertEqual(picks[0]["model_pick"], "Space shuttle")
        self.assertTrue(picks[0]["error"])

    def test_empty_pick_falls_back(self):
        def choose(src, cands):
            return {"label": None, "error": "empty model answer"}

        plan, _, picks = g.plan_day([self.market()], random.Random(1),
                                    choose=choose)
        self.assertEqual(picks[0]["mode"], "fallback")
        self.assertEqual(picks[0]["picked"], plan[0][1]["label"])

    def test_raising_picker_falls_back(self):
        def choose(src, cands):
            raise RuntimeError("API down")

        plan, _, picks = g.plan_day([self.market()], random.Random(1),
                                    choose=choose)
        self.assertTrue(plan)
        self.assertEqual(picks[0]["mode"], "fallback")
        self.assertIn("API down", picks[0]["error"])

    def test_candidates_respect_the_hard_rules(self):
        seen = {}

        def choose(src, cands):
            seen["labels"] = [e["label"] for e in cands]
            return None

        s = source(sid="commons-harbor-1",
                   title="Steam ship at the harbor quay, 1905")
        g.plan_day([s], random.Random(1), choose=choose)
        self.assertTrue(seen["labels"])
        for label in seen["labels"]:
            e = self.entry(label)
            # setting fit, density (no person without a crowd) and era rule
            self.assertTrue(set(e["settings"]) & {"harbor"})
            self.assertNotEqual(e["type"], "person")
            if e["min_year"] is not None:
                self.assertGreater(e["min_year"], 1905)

    def test_no_label_twice_in_one_set(self):
        plan, skipped, _ = g.plan_day(self.sources(12), random.Random(3))
        labels = [e["label"] for _, e in plan]
        self.assertEqual(skipped, [])
        self.assertEqual(len(labels), len(set(labels)))
        keys = [g.build_prompt_key(e) for _, e in plan]
        self.assertEqual(len(keys), len(set(keys)))

    def test_make_vision_picker_missing_image_is_a_failed_pick(self):
        data_dir = Path(tempfile.mkdtemp())
        try:
            choose = g.make_vision_picker(data_dir, "key", g.DEFAULT_BASE_URL)
            res = choose({"id": "x", "image": "images/nope.jpg"},
                         [self.entry("Plastic bottle (clear PET)")])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)
        self.assertIsNone(res["label"])
        self.assertIn("missing image", res["error"])

    def sources(self, n=10):
        return [source(sid=f"commons-market-{i}-190{i % 10}",
                       title=f"Market street scene {i}, 1900")
                for i in range(n)]


class PromptTests(unittest.TestCase):
    def entry(self, label):
        return next(e for e in ag_catalog.CATALOG if e["label"] == label)

    def test_object_prompt_has_scale_number_and_recipe(self):
        p = g.build_prompt(self.entry("Plastic bottle (clear PET)"))
        self.assertIn("2 percent", p)
        self.assertIn("never more than 3 percent", p)
        self.assertIn("bottom edge", p)
        self.assertIn("Keep every other part of the photograph EXACTLY", p)
        self.assertNotIn("difficulty", p.lower())

    def test_every_prompt_anchors_the_size_at_the_same_distance(self):
        # #1328: Evan's "scaling is off" feedback is about perspective, so the
        # budget sentence names a same-distance reference in every prompt type
        # (the absolute pixel number alone cannot place the object in depth).
        for label in ("Plastic bottle (clear PET)", "Traffic cone (orange)",
                      "Time traveler: tourist", "Robot time traveler"):
            p = g.build_prompt(self.entry(label))
            self.assertIn("same distance", p, label)
            self.assertIn("perspective matches the scene", p, label)

    def test_large_object_uses_its_own_scale_budget(self):
        p = g.build_prompt(self.entry("Traffic cone (orange)"))
        self.assertIn("never more than 4 percent", p)
        self.assertNotIn("20-30 pixels", p)

    def test_person_prompt_carries_tells_and_height_budget(self):
        p = g.build_prompt(self.entry("Time traveler: tourist"))
        self.assertIn("baseball cap", p)
        self.assertIn("8-15 percent of the image height", p)
        self.assertIn("far background", p)

    def test_future_prompt_forbids_fantasy(self):
        p = g.build_prompt(self.entry("Robot time traveler"))
        self.assertIn("fictional FUTURE", p)
        self.assertIn("flying saucer", p)
        self.assertIn("8-15 percent of the image height", p)


class EntryTests(unittest.TestCase):
    def entry(self, label):
        return next(e for e in ag_catalog.CATALOG if e["label"] == label)

    def test_is_figure_covers_persons_and_humanoid_robots(self):
        # #1308: the verify call marks whole-body anomalies so the answer
        # covers the full figure; objects must stay unflagged.
        self.assertTrue(g.is_figure(self.entry("Time traveler: tourist")))
        self.assertTrue(g.is_figure(self.entry("Robot time traveler")))
        self.assertFalse(g.is_figure(self.entry("Plastic bottle (clear PET)")))
        self.assertFalse(g.is_figure(self.entry("Futuristic gadget")))

    def test_built_entry_passes_queue_validation(self):
        s = source()
        answer = {"x": 0.4, "y": 0.8, "r": 0.05}
        e = g.build_entry(s, self.entry("Plastic bottle (clear PET)"), answer,
                          1900, "Springfield", "2026-09-11")
        self.assertEqual(ag_queue.validate_entry(e), [])
        self.assertEqual(e["anomaly"], "Plastic bottle (clear PET)")
        self.assertEqual(e["answer"], answer)
        self.assertEqual(len(e["hints"]), 3)
        self.assertTrue(e["references"])
        self.assertIn("1970s", e["explanation"])
        self.assertEqual(e["source"]["repository"], "Wikimedia Commons")
        self.assertEqual(e["sourceUrl"], s["fileUrl"])

    def test_place_falls_back_when_unknown(self):
        s = source(title="Street scene", description="")
        e = g.build_entry(s, self.entry("Caution tape"), {"x": 0.1, "y": 0.9,
                                                          "r": 0.05}, None,
                          "", "2026-09-11")
        self.assertEqual(e["place"], "Unidentified location")
        self.assertEqual(e["year"], "1900")

    def test_hints_name_the_region_from_the_answer(self):
        s = source()
        e = g.build_entry(s, self.entry("Plastic bottle (clear PET)"),
                          {"x": 0.9, "y": 0.9, "r": 0.05}, 1900, "X", "d")
        self.assertIn("lower right", e["hints"][-1])

    def test_scene_id_is_a_slug(self):
        sid = g.scene_id("commons-foo-1900-abc", "Time traveler: tourist")
        self.assertRegex(sid, r"^[a-z0-9][a-z0-9-]*$")

    def test_clean_title_drops_archive_bookkeeping(self):
        s = source(title="Street scene in Garfield, Washington, circa 1900 - "
                         "DPLA - 17896dc3c3e42d3daef44a305a3d964f")
        self.assertEqual(g.clean_title(s),
                         "Street scene in Garfield, Washington, circa 1900")

    def test_description_does_not_repeat_place_or_year(self):
        s = source(title="Street scene in Agana (1899-1900)",
                   date="2005-09-30")
        desc = g.build_description(s, 1899, "Agana")
        self.assertNotIn("Agana,", desc.replace("Agana (1899-1900),", ""))
        self.assertNotIn("circa 1899", desc)
        self.assertIn("Wikimedia Commons catalogue", desc)

    def test_hint_article_before_vowel(self):
        s = source()
        e = g.build_entry(s, self.entry("E-scooter"),
                          {"x": 0.9, "y": 0.9, "r": 0.05}, 1900, "X", "d")
        self.assertIn("An E-scooter", e["hints"][-1])

    def test_guess_place_from_title(self):
        self.assertEqual(g.guess_place(source(title="Street scene in Agana (1899-1900)")),
                         "Agana")
        self.assertEqual(g.guess_place(source(title="Unnamed view")), "")


class AspectTests(unittest.TestCase):
    def test_nearest_landscape_ratio(self):
        self.assertEqual(g.aspect_ratio_for(1600, 900), "16:9")
        self.assertEqual(g.aspect_ratio_for(1200, 800), "3:2")
        self.assertEqual(g.aspect_ratio_for(0, 0), "3:2")


class RunTests(TempDataMixin, unittest.TestCase):
    def _verified(self, answer=None):
        return {"ok": True, "answer": answer or {"x": 0.5, "y": 0.8,
                                                 "r": 0.05},
                "localization": "vision"}

    def test_run_adds_a_scene_and_marks_the_source_used(self):
        src = self.write_source(source())
        made = []

        def fake_edit(source_image, prompt, *a, **kw):
            made.append(prompt)
            out = self._tmp / f"out-{len(made)}.png"
            make_img(out)
            return out.read_bytes()

        orig_edit, orig_verify = g.image_edit, ag_verify.verify
        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "rules", "--env", ""]))
        finally:
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(report["failed"], [])
        state = ag_queue.load_state(self.data_dir)
        self.assertEqual(len(state["scenes"]), 1)
        scene = next(iter(state["scenes"].values()))
        self.assertEqual(ag_queue.validate_entry(scene), [])
        # used-tracking: consumed only after the successful queue add
        self.assertTrue(ag_sources.load_index(self.data_dir)
                        ["sources"][src["id"]]["used"])

    def test_rejected_scene_keeps_the_source_unused_and_retries_other_anomaly(self):
        src = self.write_source(source())
        prompts = []

        def fake_edit(source_image, prompt, *a, **kw):
            prompts.append(prompt)
            out = self._tmp / f"out-{len(prompts)}.png"
            make_img(out)
            return out.read_bytes()

        orig_edit, orig_verify = g.image_edit, ag_verify.verify
        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: {"ok": False,
                                             "reason": "no change hotspot"}
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--max-attempts", "2", "--picker", "rules", "--env", ""]))
        finally:
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertEqual(report["added"], [])
        self.assertEqual(len(report["failed"]), 1)
        self.assertEqual(report["image_calls"], 2)
        self.assertNotEqual(prompts[0], prompts[1])  # materially different
        self.assertFalse(ag_sources.load_index(self.data_dir)
                         ["sources"][src["id"]]["used"])

    def test_run_uses_the_llm_picker_when_selected(self):
        self.write_source(source())
        chosen = {}

        def fake_make(data_dir, api_key, base_url, model, max_tokens, timeout,
                      *a, **kw):
            def choose(src, cands):
                chosen["label"] = cands[-1]["label"]
                return {"label": cands[-1]["label"], "reason": "test pick",
                        "usage": {"cost": 0.0, "prompt_tokens": 5,
                                  "completion_tokens": 3}}
            return choose

        def fake_edit(source_image, prompt, *a, **kw):
            out = self._tmp / "out-pick.png"
            make_img(out)
            return out.read_bytes()

        orig_make = g.make_vision_picker
        orig_edit, orig_verify = g.image_edit, ag_verify.verify
        g.make_vision_picker = fake_make
        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "llm", "--env", ""]))
        finally:
            g.make_vision_picker = orig_make
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(report["added"][0]["anomaly"], chosen["label"])
        self.assertEqual(report["picker_stats"]["mode"], "llm")
        self.assertEqual(report["picker_stats"]["llm"], 1)

    def test_run_tells_verify_that_a_person_scene_is_a_figure(self):
        # #1308: the run loop must mark a time-traveler pick as a person so
        # the answer covers the whole figure; objects (and the rule pick)
        # stay unflagged.
        self.write_source(source())
        got = {}

        def fake_make(data_dir, api_key, base_url, model, max_tokens, timeout,
                      *a, **kw):
            def choose(src, cands):
                labels = [c["label"] for c in cands]
                assert "Time traveler: tourist" in labels, labels
                return {"label": "Time traveler: tourist", "reason": "test",
                        "usage": {"cost": 0.0, "prompt_tokens": 5,
                                  "completion_tokens": 3}}
            return choose

        def fake_edit(source_image, prompt, *a, **kw):
            out = self._tmp / "out-person.png"
            make_img(out)
            return out.read_bytes()

        def fake_verify(*a, **kw):
            got["person"] = kw.get("person")
            return self._verified()

        orig_make = g.make_vision_picker
        orig_edit, orig_verify = g.image_edit, ag_verify.verify
        g.make_vision_picker = fake_make
        g.image_edit = fake_edit
        ag_verify.verify = fake_verify
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "llm", "--env", ""]))
        finally:
            g.make_vision_picker = orig_make
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertEqual(len(report["added"]), 1, report)
        self.assertEqual(report["added"][0]["anomaly"],
                         "Time traveler: tourist")
        self.assertTrue(got["person"])

    def test_empty_dataset_reports_the_error_without_calling_anything(self):
        report = g.run(g.parse_args(["--data", str(self.data_dir)]))
        self.assertIn("no unused sources", report["error"])

    def test_top_up_runs_before_source_selection(self):
        self.write_source(source())
        calls = []
        orig_topup = ag_sources.top_up
        ag_sources.top_up = lambda data_dir, **kw: calls.append(kw) or {"added": 0}
        orig_edit, orig_verify = g.image_edit, ag_verify.verify

        def fake_edit(source_image, prompt, *a, **kw):
            out = self._tmp / "out-topup.png"
            make_img(out)
            return out.read_bytes()

        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--env", "", "--top-up", "--topup-target", "7",
                 "--picker", "rules", "--topup-max-calls", "2"]))
        finally:
            ag_sources.top_up = orig_topup
            g.image_edit, ag_verify.verify = orig_edit, orig_verify
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target"], 7)
        self.assertEqual(calls[0]["max_calls"], 2)
        self.assertIn("topup", report)
        self.assertEqual(len(report["added"]), 1)

    def test_dry_run_does_not_top_up(self):
        self.write_source(source())
        calls = []
        orig_topup = ag_sources.top_up
        ag_sources.top_up = lambda *a, **kw: calls.append(1) or {}
        try:
            g.run(g.parse_args(["--data", str(self.data_dir), "--dry-run",
                                "--top-up"]))
        finally:
            ag_sources.top_up = orig_topup
        self.assertEqual(calls, [])

    def test_top_up_failure_does_not_block_generation(self):
        self.write_source(source())
        orig_topup = ag_sources.top_up

        def boom(*a, **kw):
            raise RuntimeError("network down")

        ag_sources.top_up = boom
        orig_edit, orig_verify = g.image_edit, ag_verify.verify

        def fake_edit(source_image, prompt, *a, **kw):
            out = self._tmp / "out-topup-fail.png"
            make_img(out)
            return out.read_bytes()

        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--env", "", "--picker", "rules", "--top-up"]))
        finally:
            ag_sources.top_up = orig_topup
            g.image_edit, ag_verify.verify = orig_edit, orig_verify
        self.assertIn("network down", report["topup"]["error"])
        self.assertEqual(len(report["added"]), 1)

    def test_low_pool_sets_a_warning(self):
        self.write_source(source())
        report = g.run(g.parse_args(["--data", str(self.data_dir), "--dry-run",
                                     "--count", "5"]))
        self.assertIn("source pool low", report["warning"])
        self.assertIn("source pool low", g.failure_message(report))

    def test_sufficient_pool_has_no_warning(self):
        self.write_source(source())
        report = g.run(g.parse_args(["--data", str(self.data_dir), "--dry-run",
                                     "--count", "1"]))
        self.assertNotIn("warning", report)

    def test_vision_preflight_records_the_configured_model(self):
        # #1307: the run makes one real vision call before the image calls,
        # against the --vision-model the verify pass will use.
        self.write_source(source())
        seen = {}

        def fake_preflight(api_key, model=None, **kw):
            seen["key"], seen["model"] = api_key, model
            return {"ok": True, "model": "stub/model", "answer": "stub"}

        def fake_edit(source_image, prompt, *a, **kw):
            out = self._tmp / "out-preflight.png"
            make_img(out)
            return out.read_bytes()

        orig_pre, orig_edit, orig_verify = (ag_verify.preflight_vision,
                                            g.image_edit, ag_verify.verify)
        ag_verify.preflight_vision = fake_preflight
        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "rules", "--env", "",
                 "--vision-model", "vendor/vision"]))
        finally:
            ag_verify.preflight_vision = orig_pre
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(seen["model"], "vendor/vision")
        self.assertEqual(seen["key"], "test-key")
        self.assertEqual(report["vision_preflight"]["model"], "stub/model")

    def test_failing_vision_preflight_aborts_before_any_image_call(self):
        # The #1285 trap: a model that returns no content must fail the run
        # loudly instead of burning 10 image generations on scenes that all
        # fail verification.
        src = self.write_source(source())
        image_calls = []

        def boom(api_key, model=None, **kw):
            raise RuntimeError("vision model bogus/model returned an empty "
                               "answer")

        def fake_edit(*a, **kw):
            image_calls.append(1)
            return b""

        orig_pre, orig_edit = ag_verify.preflight_vision, g.image_edit
        ag_verify.preflight_vision = boom
        g.image_edit = fake_edit
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "rules", "--env", ""]))
        finally:
            ag_verify.preflight_vision = orig_pre
            g.image_edit = orig_edit

        self.assertIn("vision preflight failed", report["error"])
        self.assertIn("bogus/model", report["error"])
        self.assertEqual(image_calls, [])
        self.assertEqual(report["added"], [])
        self.assertNotIn("vision_preflight", report)
        # the source stays unused, so tomorrow's run retries it
        self.assertFalse(ag_sources.load_index(self.data_dir)
                         ["sources"][src["id"]]["used"])

    def test_no_vision_and_dry_run_skip_the_preflight(self):
        self.write_source(source())

        def boom(*a, **kw):
            raise AssertionError("preflight must not run")

        def fake_edit(source_image, prompt, *a, **kw):
            out = self._tmp / "out-skip.png"
            make_img(out)
            return out.read_bytes()

        orig_pre = ag_verify.preflight_vision
        orig_edit, orig_verify = g.image_edit, ag_verify.verify
        ag_verify.preflight_vision = boom
        g.image_edit = fake_edit
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            dry = g.run(g.parse_args(["--data", str(self.data_dir),
                                      "--dry-run", "--count", "1"]))
            no_vision = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "rules", "--env", "", "--no-vision"]))
        finally:
            ag_verify.preflight_vision = orig_pre
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertTrue(dry["dry_run"])
        self.assertNotIn("vision_preflight", dry)
        self.assertNotIn("vision_preflight", no_vision)


class RunLockAndStatusTests(TempDataMixin, unittest.TestCase):
    """Run lock + progress status (ticket #1210).

    The daily cron and the dashboard's manual "generate more" share the
    generator, so overlapping runs must be refused and the dashboard must
    be able to watch a run's progress. Both are exercised here without a
    real image call.
    """

    def _verified(self, answer=None):
        return {"ok": True, "answer": answer or {"x": 0.5, "y": 0.8,
                                                 "r": 0.05},
                "localization": "vision"}

    def _stub_edit(self, seen=None):
        def fake_edit(source_image, prompt, *a, **kw):
            if seen is not None:
                status = json.loads(
                    g.status_path(self.data_dir).read_text())
                seen.append(status)
            out = self._tmp / f"out-{len(seen or [])}-{id(prompt)}.png"
            make_img(out)
            return out.read_bytes()
        return fake_edit

    def test_status_file_tracks_progress_and_reaches_done(self):
        self.write_source(source())
        seen = []
        orig_edit, orig_verify = g.image_edit, ag_verify.verify
        g.image_edit = self._stub_edit(seen)
        ag_verify.verify = lambda *a, **kw: self._verified()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--seed", "1",
                 "--picker", "rules", "--env", ""]))
        finally:
            g.image_edit, ag_verify.verify = orig_edit, orig_verify

        self.assertEqual(len(report["added"]), 1)
        # While the image call was running the status said running, with the
        # plan already known (the UI's "generating n / planned").
        self.assertTrue(seen, "status not readable during the run")
        self.assertEqual(seen[0]["state"], "running")
        self.assertEqual(seen[0]["planned"], 1)
        self.assertIn("startedAt", seen[0])

        status = json.loads(g.status_path(self.data_dir).read_text())
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["added"], 1)
        self.assertEqual(status["planned"], 1)
        self.assertIn("finishedAt", status)
        self.assertIn("pid", status)

    def test_second_run_is_refused_while_the_lock_is_held(self):
        self.write_source(source())
        lock = g.RunLock(self.data_dir)
        lock.acquire()
        try:
            report = g.run(g.parse_args(
                ["--data", str(self.data_dir), "--count", "1", "--env", ""]))
        finally:
            lock.release()

        self.assertTrue(report["locked"])
        self.assertIn("another generation run is in progress", report["error"])
        # the refused run must not touch the queue
        self.assertEqual(ag_queue.load_state(self.data_dir)["scenes"], {})

    def test_lock_is_reusable_after_release(self):
        self.write_source(source())
        first = g.RunLock(self.data_dir)
        first.acquire()
        first.release()
        second = g.RunLock(self.data_dir)
        second.acquire()
        second.release()

    def test_dry_run_takes_no_lock_and_writes_no_status(self):
        self.write_source(source())
        lock = g.RunLock(self.data_dir)
        lock.acquire()
        try:
            report = g.run(g.parse_args(["--data", str(self.data_dir),
                                         "--dry-run", "--count", "1"]))
        finally:
            lock.release()
        self.assertTrue(report["dry_run"])
        self.assertNotIn("error", report)
        self.assertFalse(g.status_path(self.data_dir).exists())

    def test_failed_run_reports_error_in_status(self):
        # empty source dataset: a hard error, and the UI must see it
        report = g.run(g.parse_args(["--data", str(self.data_dir)]))
        self.assertIn("no unused sources", report["error"])
        status = json.loads(g.status_path(self.data_dir).read_text())
        self.assertEqual(status["state"], "error")
        self.assertIn("no unused sources", status["error"])
        self.assertIn("finishedAt", status)


class NoveltyTests(unittest.TestCase):
    """Cross-day novelty (ticket #1328): recent use steers the candidate list
    and the source pick, so the same bottles/luggage do not win every day.
    """

    def entry(self, label):
        return next(e for e in ag_catalog.CATALOG if e["label"] == label)

    def state(self, scenes):
        return {"version": 1, "scenes": {
            str(i): {"anomaly": label, "added": day}
            for i, (day, label) in enumerate(scenes)}}

    def test_usage_counts_only_the_window_and_resolves_retired_labels(self):
        today = datetime.date(2026, 9, 12)
        state = self.state([
            ("2026-09-12", "Plastic bottle (clear PET)"),   # in window
            ("2026-09-08", "Plastic bottle (clear PET)"),   # 4 days back
            ("2026-08-01", "Plastic bottle (clear PET)"),   # outside
            ("2026-09-11", "Plastic bottle"),               # retired label
        ])
        usage = g.label_usage(state, today)
        # three in-window uses count, the old one does not; the retired label
        # lands in the same family
        self.assertEqual(usage["labels"]["Plastic bottle (clear PET)"], 2)
        self.assertEqual(usage["families"]["drinks"], 3)

    def test_is_fresh_thresholds(self):
        bottle = self.entry("Plastic bottle (clear PET)")
        fresh_usage = {"families": {"drinks": g.FAMILY_REUSE_MAX},
                       "labels": {}}
        used_usage = {"families": {"drinks": g.FAMILY_REUSE_MAX + 1},
                      "labels": {}}
        self.assertTrue(g.is_fresh(bottle, fresh_usage))
        self.assertFalse(g.is_fresh(bottle, used_usage))
        # a label already used twice is used up even in a fresh family
        lab = {"families": {}, "labels": {bottle["label"]:
                                          g.LABEL_REUSE_MAX + 1}}
        self.assertFalse(g.is_fresh(bottle, lab))
        self.assertTrue(g.is_fresh(bottle, None))  # no memory -> all fresh

    def test_pick_entries_drops_used_labels_when_fresh_ones_remain(self):
        pool = list(ag_catalog.CATALOG)
        usage = {"families": {"drinks": 99}, "labels": {}}
        # A market with a crowd fits plenty of fresh non-drinks entries, so
        # the over-used drinks family disappears from the candidate list.
        cands = g.pick_entries(pool, {}, {}, ("market",), 1900, True, set(),
                               usage=usage)
        self.assertTrue(cands)
        self.assertNotIn("drinks", {e["family"] for e in cands})

    def test_pick_entries_keeps_used_labels_when_the_fresh_pool_is_small(self):
        pool = [self.entry("Plastic bottle (clear PET)"),
                self.entry("Paper coffee cup with lid")]
        usage = {"families": {"drinks": 99}, "labels": {}}
        cands = g.pick_entries(pool, {}, {}, ("market",), 1900, False, set(),
                               usage=usage)
        # fewer than FRESH_CHOICE_MIN fresh candidates: the hard rules still
        # fill the scene instead of leaving the source empty
        self.assertEqual(len(cands), 2)

    def test_plan_day_prefers_a_fresh_label(self):
        src = source(sid="commons-market-1", title="Busy market street, 1900")
        usage = {"families": {"drinks": 99, "luggage": 99, "person": 99},
                 "labels": {}}
        plan, _, picks = g.plan_day([src], random.Random(5), usage=usage)
        self.assertEqual(len(plan), 1)
        self.assertNotIn(plan[0][1]["family"], ("drinks", "luggage", "person"))

    def test_fresh_candidate_count_ranks_novel_sources_higher(self):
        market = source(sid="commons-market-2",
                        title="Busy market street, 1900")
        # a crowded street fits persons + objects; with every object family
        # used up it can only offer recently used entries, so it scores lower
        usage = {"families": {"drinks": 99, "luggage": 99, "container": 99,
                              "plastic": 99, "packaging": 99, "print": 99,
                              "street-furniture": 99, "vehicle": 99,
                              "cordage": 99, "robot": 99},
                 "labels": {}}
        self.assertLess(g.fresh_candidate_count(market, usage),
                        g.fresh_candidate_count(market, {"families": {},
                                                         "labels": {}}))

    def test_report_carries_the_novelty_snapshot(self):
        # a run with a seeded queue state reports which families were used;
        # the dry run needs no API key and no queue write
        src = source(sid="commons-market-3",
                     title="Busy market street, 1900")
        data_dir = Path(tempfile.mkdtemp())
        try:
            make_img(data_dir / "sources" / src["image"])
            index = ag_sources.load_index(data_dir)
            index["sources"][src["id"]] = src
            ag_sources.save_index(data_dir, index)
            ag_queue.save_state(data_dir, {"version": 1, "scenes": {
                "old-1": {"anomaly": "Plastic bottle (clear PET)",
                          "added": datetime.date.today().isoformat()}}})
            report = g.run(g.parse_args(
                ["--data", str(data_dir), "--dry-run", "--count", "1",
                 "--seed", "1"]))
            self.assertEqual(report["novelty"]["recent_families"]["drinks"], 1)
            self.assertEqual(report["novelty"]["window_days"],
                             g.NOVELTY_WINDOW_DAYS)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


class ScaleReportTests(unittest.TestCase):
    """Measured rendered size is reported, never gated (ticket #1328)."""

    def entry(self, label):
        return next(e for e in ag_catalog.CATALOG if e["label"] == label)

    def verdict(self, box=None, hint=None, present=True):
        return {"ok": True, "answer": {"x": 0.5, "y": 0.5, "r": 0.05},
                "vision": {"present": present, "note": "n", "box": box,
                           "size_hint": hint}}

    def test_size_hint_fraction_takes_the_largest_percent_number(self):
        self.assertEqual(g.size_hint_fraction("about 2 percent of the image "
                                              "height"), 0.02)
        self.assertEqual(g.size_hint_fraction("roughly 5-12 percent"), 0.12)
        self.assertEqual(g.size_hint_fraction("12%"), 0.12)
        self.assertIsNone(g.size_hint_fraction("tiny"))

    def test_measured_scale_takes_box_and_hint_maximum(self):
        self.assertAlmostEqual(
            g.measured_scale(self.verdict(box=[0.1, 0.1, 0.16, 0.2])), 0.1)
        self.assertAlmostEqual(
            g.measured_scale(self.verdict(box=[0.1, 0.1, 0.16, 0.2],
                                          hint="about 20 percent")), 0.2)
        self.assertIsNone(g.measured_scale({"vision": None}))

    def test_scale_report_flags_a_scene_over_budget(self):
        bottle = self.entry("Plastic bottle (clear PET)")
        budget = ag_catalog.scale_max(bottle)
        over = g.scale_report(bottle, self.verdict(
            box=[0.0, 0.0, 0.10, 0.10], hint=None))
        self.assertEqual(over["scale_budget"], budget)
        self.assertTrue(over["scale_over_budget"])
        ok = g.scale_report(bottle, self.verdict(
            box=[0.0, 0.0, 0.01, 0.02], hint="about 2 percent"))
        self.assertFalse(ok["scale_over_budget"])
        none = g.scale_report(bottle, {"ok": True, "vision": None})
        self.assertIsNone(none["scale_measured"])
        self.assertIsNone(none["scale_over_budget"])

    def test_added_scene_reports_its_measured_scale(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src = source(sid="commons-market-scale-1")
            make_img(tmp / "sources" / src["image"])
            index = ag_sources.load_index(tmp)
            index["sources"][src["id"]] = src
            ag_sources.save_index(tmp, index)

            def fake_edit(source_image, prompt, *a, **kw):
                out = tmp / "out.png"
                make_img(out)
                return out.read_bytes()

            verdict = {"ok": True, "answer": {"x": 0.5, "y": 0.8, "r": 0.05},
                       "localization": "vision",
                       "vision": {"present": True, "note": "n",
                                  "box": [0.1, 0.7, 0.12, 0.75],
                                  "size_hint": "about 5 percent of the "
                                               "image height"}}
            orig_edit, orig_verify = g.image_edit, ag_verify.verify
            g.image_edit = fake_edit
            ag_verify.verify = lambda *a, **kw: verdict
            try:
                report = g.run(g.parse_args(
                    ["--data", str(tmp), "--count", "1", "--seed", "1",
                     "--picker", "rules", "--env", ""]))
            finally:
                g.image_edit, ag_verify.verify = orig_edit, orig_verify
            self.assertEqual(len(report["added"]), 1)
            added = report["added"][0]
            self.assertAlmostEqual(added["scale_measured"], 0.05)
            self.assertIn("scale_over_budget", added)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
