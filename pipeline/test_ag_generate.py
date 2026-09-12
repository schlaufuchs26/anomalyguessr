"""Tests for pipeline/ag_generate.py after the #1372 rebuild.

Run with: python3 -m unittest discover -s pipeline -t pipeline

No network and no API spend: the three model calls, the image edit, the
preflight and the pixel gates are monkeypatched; images are real files
created with ImageMagick so the queue's landscape check runs for real.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import ag_catalog
import ag_generate as g
import ag_llm
import ag_queue
import ag_sources
import ag_verify

SOURCE_ID = "commons-market-street-2013-10-24-abc123"


def make_img(path: Path, w=1200, h=800, color="gray"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}", str(path)],
        check=True, capture_output=True,
    )


def img_bytes(w=1200, h=800, color="red") -> bytes:
    p = Path(tempfile.mkdtemp()) / "x.png"
    make_img(p, w, h, color)
    data = p.read_bytes()
    shutil.rmtree(p.parent, ignore_errors=True)
    return data


def source(sid=SOURCE_ID, title="Busy market street, 1905", place="",
           date="2013-10-24", image=None, width=1200, height=800):
    return {
        "id": sid,
        "repository": "Wikimedia Commons",
        "fileUrl": f"https://commons.wikimedia.org/wiki/File:{sid}.jpg",
        "originalTitle": title,
        "date": date,
        "place": place,
        "license": "CC BY-SA 4.0",
        "licenseUrl": "",
        "description": "A busy market street.",
        "image": image or f"images/{sid}.jpg",
        "width": width, "height": height,
        "mime": "image/jpeg", "quality": "quality",
        "used": False,
        "added": "2026-09-12",
        "raw": {"categories": "Markets|Streets", "artist": "A. Photographer",
                "dateTimeOriginal": "2013-10-24 15:02:48"},
    }


def proposal(**kw):
    p = {
        "anomaly": "Plastic bottle (clear PET)", "kind": "later-era",
        "apparent_era": "1905", "figure": False,
        "placement": "on the ground at the bottom edge, half hidden behind "
                     "a crate",
        "explanation": "PET bottles only came into common use in the 1970s.",
        "references": [{"label": "Polyethylene terephthalate",
                        "url": "https://en.wikipedia.org/wiki/PET"}],
    }
    p.update(kw)
    return p


def usage(cost=0.001, pt=10, ct=5):
    return {"prompt_tokens": pt, "completion_tokens": ct, "cost": cost}


def call(model="stub/model", prompt="P", answer="A", cost=0.001):
    return {"model": model, "prompt": prompt, "answer": answer,
            "usage": usage(cost), "duration_s": 0.5}


def stub_propose(result=None, errors=None):
    payload = result if result is not None else proposal()
    return lambda *a, **kw: {"proposal": payload, "errors": errors or [],
                             "call": call(prompt="proposal-prompt")}


def stub_locate(coords="default"):
    if coords == "default":
        coords = {"x": 0.5, "y": 0.6, "r": 0.08, "figure": False}
    return lambda *a, **kw: {"coords": coords,
                             "call": call(prompt="coord-prompt")}


def stub_check(ok=True, problems=None, fix_prompt=""):
    return lambda *a, **kw: {"ok": ok, "problems": problems or [],
                             "fix_prompt": fix_prompt,
                             "call": call(prompt="check-prompt")}


class TempDataMixin:
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        (self.data_dir / "sources" / "images").mkdir(parents=True)
        self._old_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        self._old_preflight = ag_verify.preflight_vision
        ag_verify.preflight_vision = lambda *a, **kw: {"ok": True,
                                                       "model": "stub"}
        self._old_gate = g.deterministic_gate
        g.deterministic_gate = lambda edited, original, dedup: (
            None, {"cx": 0.5, "cy": 0.6, "x1": 0.45, "y1": 0.55,
                   "x2": 0.55, "y2": 0.65})

    def tearDown(self):
        g.deterministic_gate = self._old_gate
        ag_verify.preflight_vision = self._old_preflight
        if self._old_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = self._old_key
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write_source(self, src=None, w=1200, h=800):
        src = src or source()
        make_img(self.data_dir / "sources" / src["image"], w, h)
        index = ag_sources.load_index(self.data_dir)
        index["sources"][src["id"]] = src
        ag_sources.save_index(self.data_dir, index)
        return src

    def make_args(self, **kw):
        count = kw.pop("count", 1)
        argv = ["--count", str(count), "--data", str(self.data_dir)]
        for key, value in kw.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    argv.append(flag)
            else:
                argv += [flag, str(value)]
        return g.parse_args(argv)


# ── Prompts ────────────────────────────────────────────────────────────────

class PromptTests(unittest.TestCase):
    def test_proposal_prompt_states_goals(self):
        text = g.proposal_prompt(source(), ["Old label"])
        self.assertIn("LATER era", text)
        self.assertIn("futuristic", text)
        self.assertIn("no flying saucers", text)
        self.assertIn("Old label", text)
        for line in ag_catalog.inspiration_lines():
            self.assertIn(line, text)
        self.assertIn('"placement"', text)

    def test_scale_rule_has_one_numeric_cap(self):
        obj = g.scale_rule({"figure": False})
        self.assertIn("never more than 3 percent", obj)
        self.assertNotIn("8-15", obj)
        fig = g.scale_rule({"figure": True})
        self.assertIn("8-15 percent", fig)
        self.assertNotIn("3 percent", fig)

    def test_edit_prompt_carries_hard_constraints(self):
        text = g.edit_prompt(proposal())
        self.assertIn("Plastic bottle (clear PET)", text)
        self.assertIn("CRITICAL SCALE", text)
        self.assertIn("Keep every other part", text)
        self.assertIn("no glow", text.lower())

    def test_coord_prompt_asks_for_whole_figure(self):
        text = g.coord_prompt(proposal(), "edit instruction")
        self.assertIn("head to feet", text)
        self.assertIn("edit instruction", text)

    def test_check_prompt_lists_the_moved_rules(self):
        text = g.check_prompt(proposal())
        for needle in ("Time travel", "Subtle", "Scale", "Tone", "Grain",
                       "unchanged", "identifiable"):
            self.assertIn(needle, text)
        self.assertIn("fix_prompt", text)


# ── Proposal validation ────────────────────────────────────────────────────

class ProposalTest(unittest.TestCase):
    def test_valid_proposal_has_no_errors(self):
        self.assertEqual(g.proposal_errors(proposal()), [])

    def test_missing_and_bad_fields(self):
        self.assertIn("anomaly", " ".join(g.proposal_errors({})))
        self.assertIn("kind", " ".join(
            g.proposal_errors({**proposal(), "kind": "fantasy"})))
        self.assertIn("placement", " ".join(
            g.proposal_errors({**proposal(), "placement": ""})))
        self.assertIn("80", " ".join(
            g.proposal_errors({**proposal(), "anomaly": "x" * 81})))

    def test_normalize_caps_and_flags(self):
        p = g.normalize_proposal({"anomaly": "  Bottle \n", "figure": 1,
                                  "placement": "a" * 900})
        self.assertEqual(p["anomaly"], "Bottle")
        self.assertTrue(p["figure"])
        self.assertLessEqual(len(p["placement"]), 400)

    def test_valid_reference(self):
        self.assertTrue(g.valid_reference({"label": "x",
                                           "url": "https://e.org"}))
        self.assertFalse(g.valid_reference({"label": "x", "url": "ftp://e"}))
        self.assertFalse(g.valid_reference({"label": "", "url": "https://e"}))

    def test_resolve_references_prefers_model(self):
        refs, origin = g.resolve_references(proposal(), source())
        self.assertEqual(origin, "model")
        self.assertEqual(refs[0]["url"], "https://en.wikipedia.org/wiki/PET")

    def test_resolve_references_falls_back_to_catalog(self):
        p = proposal(references=[],
                     anomaly="Time traveler: young man in a suit")
        refs, origin = g.resolve_references(p, source())
        self.assertEqual(origin, "catalog-family")
        self.assertTrue(refs)

    def test_resolve_references_last_resort_source(self):
        p = proposal(references=[{"label": "x", "url": "nope"}],
                     anomaly="Totally novel thing")
        refs, origin = g.resolve_references(p, source())
        self.assertEqual(origin, "source")
        self.assertIn("commons.wikimedia.org", refs[0]["url"])


# ── Coordinates ────────────────────────────────────────────────────────────

class CoordinateTest(unittest.TestCase):
    def test_valid_coords(self):
        self.assertIsNotNone(g.valid_coords({"x": 0.5, "y": 0.5, "r": 0.1}))
        self.assertIsNone(g.valid_coords({"x": 1.5, "y": 0.5, "r": 0.1}))
        self.assertIsNone(g.valid_coords({"x": 0.5, "y": 0.5, "r": 0}))
        self.assertIsNone(g.valid_coords({}))
        self.assertIsNone(g.valid_coords(None))

    def test_finalize_floors_a_figure_radius(self):
        answer, conflict = g.finalize_answer(
            {"x": 0.5, "y": 0.5, "r": 0.03, "figure": True}, None)
        self.assertEqual(answer["r"], ag_verify.PERSON_MIN_RADIUS)
        self.assertIsNone(conflict)

    def test_finalize_caps_at_the_queue_maximum(self):
        answer, _ = g.finalize_answer(
            {"x": 0.5, "y": 0.5, "r": 0.5, "figure": False}, None)
        self.assertEqual(answer["r"], ag_verify.MAX_ANSWER_RADIUS)

    def test_finalize_widens_and_reports_a_conflict(self):
        hotspot = {"cx": 0.9, "cy": 0.9}
        answer, conflict = g.finalize_answer(
            {"x": 0.2, "y": 0.2, "r": 0.05, "figure": False}, hotspot)
        self.assertIsNotNone(conflict)
        self.assertGreater(answer["r"], 0.05)
        self.assertLessEqual(answer["r"], ag_verify.MAX_ANSWER_RADIUS)

    def test_no_conflict_when_hotspot_inside(self):
        answer, conflict = g.finalize_answer(
            {"x": 0.5, "y": 0.6, "r": 0.1, "figure": False},
            {"cx": 0.51, "cy": 0.61})
        self.assertIsNone(conflict)
        self.assertEqual(answer["r"], 0.1)


# ── Deterministic gate ─────────────────────────────────────────────────────

class GateTest(TempDataMixin, unittest.TestCase):
    def test_identical_output_is_refused(self):
        img = self.data_dir / "sources" / "images" / "s.jpg"
        make_img(img)
        g.deterministic_gate = self._old_gate  # the real gate
        old = ag_verify.identical_output_check
        ag_verify.identical_output_check = lambda *a, **kw: {
            "ok": False, "reason": "identical output"}
        try:
            reason, hotspot = g.deterministic_gate(img, img, None)
            self.assertEqual(reason, "identical output")
            self.assertIsNone(hotspot)
        finally:
            ag_verify.identical_output_check = old

    def test_whole_frame_repaint_is_refused(self):
        img = self.data_dir / "sources" / "images" / "s.jpg"
        make_img(img)
        g.deterministic_gate = self._old_gate
        old_dup = ag_verify.identical_output_check
        old_loc = ag_verify.locate_hotspot
        ag_verify.identical_output_check = lambda *a, **kw: None
        ag_verify.locate_hotspot = lambda *a, **kw: {
            "ok": False, "reason": "whole-frame repaint"}
        try:
            reason, hotspot = g.deterministic_gate(img, img, None)
            self.assertEqual(reason, "whole-frame repaint")
        finally:
            ag_verify.identical_output_check = old_dup
            ag_verify.locate_hotspot = old_loc


# ── Entry text ─────────────────────────────────────────────────────────────

class EntryTest(unittest.TestCase):
    def answer(self):
        return {"x": 0.5, "y": 0.6, "r": 0.08}

    def test_scene_id_shape(self):
        eid = g.scene_id(SOURCE_ID, "Plastic bottle (clear PET)")
        self.assertTrue(eid.endswith("-abc123-plastic-bottle-clear-pet"))

    def test_scene_year_extracts_a_year(self):
        self.assertEqual(g.scene_year({"apparent_era": "c. 1905"}), "1905")
        self.assertEqual(g.scene_year({"apparent_era": "modern (2020s)"}),
                         "2020")
        self.assertEqual(g.scene_year({"apparent_era": "modern"}), "modern")
        self.assertEqual(g.scene_year({"apparent_era": ""}), "unknown")

    def test_scene_place_marker(self):
        self.assertEqual(g.scene_place({"place": "Berlin"}), "Berlin")
        self.assertEqual(g.scene_place({}), "Unidentified location")

    def test_build_entry_passes_the_queue_validator(self):
        entry = g.build_entry(source(place="Berlin"), proposal(),
                              self.answer(), "2026-09-12")
        self.assertEqual(ag_queue.validate_entry(entry), [])
        self.assertEqual(entry["year"], "1905")
        self.assertEqual(entry["place"], "Berlin")
        self.assertEqual(len(entry["hints"]), 3)
        self.assertEqual(entry["family"], "drinks")

    def test_build_entry_uses_catalog_text_for_a_known_label(self):
        entry = g.build_entry(source(), proposal(), self.answer(),
                              "2026-09-12")
        curated = ag_catalog.entry_for_label("Plastic bottle (clear PET)")
        self.assertEqual(entry["explanation"], curated["explanation"])
        self.assertEqual(entry["references"], curated["references"])

    def test_build_entry_keeps_model_text_for_a_novel_label(self):
        p = proposal(anomaly="Hovering drone", kind="fictional-future",
                     explanation="No such device exists yet.",
                     references=[{"label": "Drone",
                                  "url": "https://en.wikipedia.org/wiki/Drone"}])
        entry = g.build_entry(source(), p, self.answer(), "2026-09-12")
        self.assertEqual(entry["explanation"], "No such device exists yet.")
        self.assertEqual(entry["family"], "other")

    def test_clean_title_strips_archive_suffix(self):
        s = source(title="Street scene - DPLA - 1234567890abcdef.jpg")
        self.assertEqual(g.clean_title(s), "Street scene")

    def test_description_omits_the_unknown_place(self):
        entry = g.build_entry(source(), proposal(), self.answer(),
                              "2026-09-12")
        self.assertNotIn("Unidentified", entry["description"])


# ── Trace sidecar ──────────────────────────────────────────────────────────

class TraceTest(TempDataMixin, unittest.TestCase):
    def test_write_trace_includes_calls(self):
        g.write_trace(self.data_dir, "scene-1", {"calls": [{"stage": "proposal",
                                                            "prompt": "P",
                                                            "answer": "A"}]})
        data = json.loads(g.trace_path(self.data_dir, "scene-1").read_text())
        self.assertEqual(data["scene"], "scene-1")
        self.assertEqual(data["calls"][0]["answer"], "A")
        self.assertIn("recordedAt", data)

    def test_recent_labels_reads_the_state(self):
        state = {"version": 1, "scenes": {
            "a": {"id": "a", "anomaly": "Bottle", "added": "2026-09-12"}}}
        ag_queue.save_state(self.data_dir, state)
        self.assertEqual(g.recent_labels(self.data_dir,
                                         today=__import__("datetime").date(
                                             2026, 9, 12)), ["Bottle"])


# ── Run loop ───────────────────────────────────────────────────────────────

class GenerateOneTest(TempDataMixin, unittest.TestCase):
    def run_one(self, *, propose=None, locate=None, check=None, edit=None,
                count=1, **argkw):
        self.write_source()
        g.propose_anomaly = propose or stub_propose()
        g.locate_anomaly = locate or stub_locate()
        g.check_scene = check or stub_check()
        g.image_edit = edit or (lambda *a, **kw: img_bytes())
        args = self.make_args(count=count, **argkw)
        totals = ag_llm.zero_usage()
        src = ag_sources.list_sources(self.data_dir)[0]
        scene, failed = g._generate_one(src, args, self.data_dir,
                                        "2026-09-12",
                                        self._tmp / "out", [], totals,
                                        lambda *a, **kw: None)
        return scene, failed, totals

    def test_happy_path_lands_a_scene(self):
        scene, failed, totals = self.run_one()
        self.assertIsNone(failed)
        self.assertIn("scene", scene["report"])
        self.assertEqual(totals["image_calls"], 1)
        self.assertEqual(totals["cost"], round(0.001 * 3, 12))
        state = ag_queue.load_state(self.data_dir)
        self.assertEqual(len(state["scenes"]), 1)
        entry = next(iter(state["scenes"].values()))
        self.assertEqual(ag_queue.validate_entry(entry), [])
        self.assertTrue(ag_sources.list_sources(self.data_dir)[0]["used"])

    def test_happy_path_writes_a_trace(self):
        scene, _, _ = self.run_one()
        data = json.loads(g.trace_path(self.data_dir,
                                       scene["report"]["scene"]).read_text())
        stages = [c["stage"] for c in data["calls"]]
        self.assertEqual(stages, ["proposal", "edit", "coordinates", "check"])
        proposal_call = data["calls"][0]
        self.assertEqual(proposal_call["prompt"], "proposal-prompt")
        self.assertEqual(proposal_call["answer"], "A")

    def test_invalid_proposal_fails_without_an_image_call(self):
        scene, failed, totals = self.run_one(propose=stub_propose(
            result={"anomaly": ""}, errors=["anomaly must be non-empty"]))
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "proposal")
        self.assertEqual(totals.get("image_calls", 0), 0)
        self.assertEqual(len(ag_queue.load_state(self.data_dir)["scenes"]), 0)

    def test_second_attempt_runs_after_a_gate_failure(self):
        calls = {"n": 0}

        def gate(edited, original, dedup):
            calls["n"] += 1
            if calls["n"] == 1:
                return "no localized edit", None
            return None, {"cx": 0.5, "cy": 0.6}

        g.deterministic_gate = gate
        scene, failed, totals = self.run_one()
        self.assertIsNotNone(scene)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(totals["image_calls"], 2)

    def test_checker_reports_problems_but_no_fix_drops_the_scene(self):
        scene, failed, _ = self.run_one(check=stub_check(
            ok=False, problems=["object too large"]))
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "check")
        self.assertIn("object too large", failed["reason"])

    def test_checker_fix_triggers_one_more_edit(self):
        edits = {"n": 0}

        def edit(*a, **kw):
            edits["n"] += 1
            return img_bytes(color="blue" if edits["n"] > 1 else "red")

        checks = {"n": 0}

        def check(*a, **kw):
            checks["n"] += 1
            if checks["n"] == 1:
                return {"ok": False, "problems": ["sepia cast"],
                        "fix_prompt": "remove the cast", "call": call()}
            return {"ok": True, "problems": [], "fix_prompt": "",
                    "call": call()}

        scene, failed, totals = self.run_one(edit=edit, check=check)
        self.assertIsNotNone(scene)
        self.assertEqual(edits["n"], 2)
        self.assertEqual(checks["n"], 2)   # the re-check closes the loop
        self.assertEqual(totals["image_calls"], 2)
        self.assertTrue(scene["report"]["checker"]["corrected"])
        self.assertTrue(scene["report"]["checker"]["ok"])

    def test_a_failed_correction_drops_the_scene(self):
        def check(*a, **kw):
            return {"ok": False, "problems": ["tone mismatch"],
                    "fix_prompt": "fix the tone", "call": call()}

        scene, failed, _ = self.run_one(check=check)
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "check")
        self.assertIn("after fix", failed["reason"])

    def test_missing_coordinates_falls_back_to_the_hotspot(self):
        scene, failed, _ = self.run_one(locate=stub_locate(coords=None))
        self.assertIsNotNone(scene)
        self.assertEqual(scene["report"]["answer"]["fallback"], "hotspot")
        self.assertEqual(scene["report"]["answer"]["x"], 0.5)

    def test_coordinate_conflict_is_reported(self):
        scene, _, _ = self.run_one(locate=stub_locate(
            {"x": 0.1, "y": 0.1, "r": 0.02, "figure": False}))
        self.assertIsNotNone(scene["report"]["coord_conflict"])

    def test_budget_exhausted_before_the_first_edit(self):
        self.write_source()
        g.propose_anomaly = stub_propose()
        g.image_edit = lambda *a, **kw: img_bytes()
        args = self.make_args(count=1, max_generations=0)
        args.max_generations = 0
        totals = ag_llm.zero_usage()
        src = ag_sources.list_sources(self.data_dir)[0]
        scene, failed = g._generate_one(src, args, self.data_dir,
                                        "2026-09-12", self._tmp / "out", [],
                                        totals, lambda *a, **kw: None)
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "budget")


class RunTest(TempDataMixin, unittest.TestCase):
    def test_run_adds_a_scene_and_reports(self):
        self.write_source()
        g.propose_anomaly = stub_propose()
        g.locate_anomaly = stub_locate()
        g.check_scene = stub_check()
        g.image_edit = lambda *a, **kw: img_bytes()
        report = g.run(self.make_args(count=1))
        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(report["image_calls"], 1)
        self.assertIn("cost_per_scene", report)
        self.assertIn("moderation", report)
        status = json.loads(g.status_path(self.data_dir).read_text())
        self.assertEqual(status["state"], "done")

    def test_dry_run_calls_nothing(self):
        self.write_source()
        def boom(*a, **kw):
            raise AssertionError("dry run must not call a model")
        g.propose_anomaly = boom
        g.image_edit = boom
        report = g.run(self.make_args(count=1, dry_run=True))
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["plan"][0]["source"], SOURCE_ID)
        self.assertIn("proposal_prompt", report["plan"][0])
        self.assertFalse(g.status_path(self.data_dir).exists())

    def test_empty_pool_reports_an_error(self):
        report = g.run(self.make_args(count=1))
        self.assertIn("no unused sources", report["error"])

    def test_second_run_is_refused_while_the_lock_is_held(self):
        self.write_source()
        lock = g.RunLock(self.data_dir)
        lock.acquire()
        try:
            report = g.run(self.make_args(count=1))
        finally:
            lock.release()
        self.assertTrue(report.get("locked"))
        self.assertEqual(report["added"], [])

    def test_low_pool_warns(self):
        self.write_source()
        g.propose_anomaly = stub_propose()
        g.locate_anomaly = stub_locate()
        g.check_scene = stub_check()
        g.image_edit = lambda *a, **kw: img_bytes()
        report = g.run(self.make_args(count=3))
        self.assertIn("source pool low", report["warning"])

    def test_failure_message_names_the_failures(self):
        report = {"added": [], "failed": [{"id": "x", "reason": "boom"}],
                  "image_calls": 0}
        self.assertIn("boom", g.failure_message(report))
        self.assertIn("0 scenes added", g.failure_message(report))

    def test_moderation_rate_uses_feedback(self):
        ag_queue.save_state(self.data_dir, {"version": 1, "scenes": {
            "a": {"id": "a", "added": "2026-09-12"},
            "b": {"id": "b", "added": "2026-09-11"},
            "c": {"id": "c", "added": "2026-09-10"}}})
        (self.data_dir / "feedback.json").write_text(json.dumps({
            "accepted": {"a": "2026-09-12", "b": "2026-09-12"},
            "rejected": {"c": "2026-09-12"}}))
        rate = g.moderation_rate(self.data_dir)
        self.assertEqual(rate["accepted"], 2)
        self.assertEqual(rate["rejected"], 1)
        self.assertEqual(rate["acceptance_rate"], 0.667)

    def test_select_sources_skips_used_and_missing_images(self):
        self.write_source()
        missing = source(sid="commons-missing-000000",
                         image="images/commons-missing-000000.jpg")
        index = ag_sources.load_index(self.data_dir)
        index["sources"][missing["id"]] = missing
        ag_sources.save_index(self.data_dir, index)
        picked = g.select_sources(self.data_dir, 10)
        self.assertEqual([s["id"] for s in picked], [SOURCE_ID])


# ── LLM plumbing ───────────────────────────────────────────────────────────

class LlmTest(unittest.TestCase):
    def test_parse_json_lenient_about_fences(self):
        got = ag_llm.parse_json('```json\n{"a": 1}\n```')
        self.assertEqual(got, {"a": 1})
        self.assertEqual(ag_llm.parse_json("no json here"), None)
        self.assertEqual(ag_llm.parse_json("[1,2]"), None)

    def test_content_of_joins_parts(self):
        body = {"choices": [{"message": {"content": [
            {"text": "a"}, {"text": "b"}]}}]}
        self.assertEqual(ag_llm.content_of(body), "ab")
        self.assertEqual(ag_llm.content_of({}), "")

    def test_usage_rollup(self):
        total = ag_llm.zero_usage()
        ag_llm.add_usage(total, {"prompt_tokens": 3, "completion_tokens": 4,
                                 "cost": 0.5})
        ag_llm.add_usage(total, None)
        self.assertEqual(total, {"prompt_tokens": 3, "completion_tokens": 4,
                                 "cost": 0.5})

    def test_clean_text_collapses_and_caps(self):
        self.assertEqual(ag_llm.clean_text(" a\n b ", 10), "a b")
        self.assertEqual(len(ag_llm.clean_text("x" * 500, 10)), 10)


if __name__ == "__main__":
    unittest.main()
