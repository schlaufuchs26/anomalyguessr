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
    # png:include-chunk=none keeps ImageMagick's date:create/date:modify text
    # chunks out of PNG output, so two renders of the same image are
    # byte-identical; the byte comparison in the best-of-k test flaked
    # whenever the two renders straddled a second boundary.
    subprocess.run(
        ["convert", "-size", f"{w}x{h}", f"xc:{color}",
         "-define", "png:include-chunk=none", str(path)],
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


def call(model="stub/model", prompt="P", answer="A", cost=0.001,
         reasoning="R"):
    return {"model": model, "prompt": prompt, "answer": answer,
            "reasoning": reasoning, "image": "source.jpg",
            "at": "2026-09-12T12:00:00+02:00",
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


def stub_check(failed=None, reason="fine", fix_prompt=""):
    failed = list(failed or [])
    return lambda *a, **kw: {
        "ok": not failed, "score": g.REQUIREMENTS_TOTAL - len(failed),
        "failed": failed, "reason": reason, "fix_prompt": fix_prompt,
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
        self._old_candidate_gate = g.candidate_gate

    def tearDown(self):
        g.candidate_gate = self._old_candidate_gate
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
                       "unchanged", "Identifiable"):
            self.assertIn(needle, text)
        self.assertIn("fix_prompt", text)
        # The checker scores, it does not vote (#1381).
        self.assertIn('"failed"', text)
        self.assertNotIn('"ok"', text)


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

    def test_scene_place_is_empty_when_the_keys_carry_none(self):
        self.assertEqual(g.scene_place({"place": "Berlin"}), "Berlin")
        self.assertEqual(g.scene_place({}), "")
        self.assertEqual(g.scene_place({"place": "   "}), "")

    def test_build_entry_passes_the_queue_validator(self):
        entry = g.build_entry(source(place="Berlin"), proposal(),
                              self.answer(), "2026-09-12")
        self.assertEqual(ag_queue.validate_entry(entry), [])
        self.assertEqual(entry["year"], "1905")
        self.assertEqual(entry["place"], "Berlin")
        self.assertEqual(len(entry["hints"]), 3)
        self.assertEqual(entry["family"], "drinks")

    def test_build_entry_leaves_an_unknown_place_empty(self):
        entry = g.build_entry(source(), proposal(), self.answer(),
                              "2026-09-12")
        self.assertEqual(entry["place"], "")
        self.assertEqual(ag_queue.validate_entry(entry), [])
        self.assertEqual(entry["source"]["place"], "")

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

    def test_clean_title_strips_the_reported_metadata(self):
        # The live scene #1402: artist year, upload stamp, a quote that only
        # balanced through the old strip(' "') hack.
        s = source(title='Access to Justice" by John Atkin (2017), McMurtry '
                         'Gardens of Justice, Toronto, Ontario, 2025-08-25 02')
        self.assertEqual(
            g.clean_title(s),
            "Access to Justice by John Atkin, McMurtry Gardens of Justice, "
            "Toronto, Ontario")

    def test_clean_title_strips_wikidata_bookkeeping(self):
        s = source(title='Cabbage market label QS:Len,"Cabbage market"')
        self.assertEqual(g.clean_title(s), "Cabbage market")

    def test_clean_title_leaves_no_year_or_half_quote(self):
        s = source(title='"Bekleben verboten"-Schild auf Kasten Hof '
                         '20210223 DSC8005')
        self.assertEqual(g.clean_title(s),
                         '"Bekleben verboten"-Schild auf Kasten Hof DSC8005')
        s = source(title="Market Place, Reading, 17 June 1907")
        self.assertEqual(g.clean_title(s), "Market Place, Reading, 17 June")

    def test_clean_title_strips_flickr_suffix_and_decade(self):
        s = source(title="Stand still, it's candid camera - Flickr - "
                         "National Library of Ireland on The Commons")
        self.assertEqual(g.clean_title(s), "Stand still, it's candid camera")
        self.assertEqual(
            g.clean_title(source(title="Oulu Market Place 1900s")),
            "Oulu Market Place")

    def test_clean_title_falls_back_to_photograph(self):
        self.assertEqual(g.clean_title({"originalTitle": "1905"}), "Photograph")

    def test_scene_title_prefers_the_proposal(self):
        entry = g.build_entry(source(place="Berlin"),
                              proposal(title="Sculpture garden in Toronto"),
                              self.answer(), "2026-09-12")
        self.assertEqual(entry["title"], "Sculpture garden in Toronto")

    def test_scene_title_falls_back_to_the_cleaned_source_name(self):
        entry = g.build_entry(source(title="Busy market street, 1905"),
                              proposal(), self.answer(), "2026-09-12")
        self.assertEqual(entry["title"], "Busy market street")

    def test_description_omits_an_unknown_place(self):
        entry = g.build_entry(source(), proposal(), self.answer(),
                              "2026-09-12")
        self.assertEqual(entry["description"],
                         "Busy market street · Wikimedia Commons.")

    def test_description_has_one_era_and_no_run_on(self):
        # The era lives in its own field; the caption must not repeat it, and
        # the parts are separated instead of glued with commas.
        entry = g.build_entry(source(place="Berlin"), proposal(),
                              self.answer(), "2026-09-12")
        self.assertEqual(entry["year"], "1905")
        self.assertEqual(entry["description"],
                         "Busy market street · Berlin · Wikimedia Commons.")
        self.assertNotIn("1905", entry["description"])

    def test_description_drops_a_place_already_in_the_title(self):
        entry = g.build_entry(source(title="Market day in Berlin"),
                              proposal(title="Market day in Berlin"),
                              self.answer(), "2026-09-12")
        self.assertEqual(entry["description"],
                         "Market day in Berlin · Wikimedia Commons.")

    def test_reported_scene_renders_clean(self):
        # The whole reported case end to end: no timestamp, no coordinate
        # place, no second year, balanced quotes.
        src = source(
            title='Access to Justice" by John Atkin (2017), McMurtry Gardens '
                  'of Justice, Toronto, Ontario, 2025-08-25 02',
            place="43.6527, -79.3851")
        entry = g.build_entry(src, proposal(), self.answer(), "2026-09-12")
        entry["year"] = "2010"  # the proposal's judgement for that scene
        self.assertEqual(entry["place"], "")
        self.assertEqual(ag_queue.caption_problems(entry), [])
        self.assertEqual(ag_queue.validate_entry(entry), [])


class RetextTest(TempDataMixin, unittest.TestCase):
    """The #1402 repair path over the queue (ag_generate.py --retext)."""

    def answer(self):
        return {"x": 0.5, "y": 0.6, "r": 0.08}

    def damaged(self):
        """A scene as the pre-#1402 builder wrote it (the live state shape)."""
        scene = g.build_entry(source(), proposal(), self.answer(),
                              "2026-09-12")
        raw_title = ('Access to Justice" by John Atkin (2017), McMurtry '
                     'Gardens of Justice, Toronto, Ontario, 2025-08-25 02')
        scene["title"] = raw_title
        scene["place"] = "43.6527, -79.3851"
        scene["description"] = (raw_title + ", 43.6527, -79.3851, circa "
                                "2010, from the Wikimedia Commons catalogue.")
        scene["year"] = "2010"
        return scene

    def save(self, scenes):
        ag_queue.save_state(self.data_dir,
                            {"version": 1, "last_shipped": None,
                             "scenes": scenes})

    def test_retext_entry_fixes_a_damaged_caption(self):
        scene = self.damaged()
        fixed, changed = g.retext_entry(scene)
        self.assertEqual(sorted(changed),
                         ["description", "place", "title"])
        self.assertEqual(fixed["place"], "")
        self.assertEqual(fixed["year"], "2010")
        self.assertEqual(fixed["description"],
                         "Access to Justice by John Atkin, McMurtry Gardens "
                         "of Justice, Toronto, Ontario · Wikimedia Commons.")
        self.assertEqual(ag_queue.caption_problems(fixed), [])

    def test_retext_state_rewrites_and_counts(self):
        clean = g.build_entry(source(), proposal(), self.answer(),
                              "2026-09-12")
        self.save({"damaged": self.damaged(), "clean": clean})
        report = g.retext_state(self.data_dir)
        self.assertEqual(report["scenes"], 2)
        self.assertEqual(report["changed"], 1)
        self.assertEqual(report["unfixable"], [])
        state = ag_queue.load_state(self.data_dir)
        self.assertEqual(state["scenes"]["damaged"]["place"], "")
        self.assertEqual(state["scenes"]["clean"], clean)

    def test_retext_state_dry_run_writes_nothing(self):
        self.save({"damaged": self.damaged()})
        report = g.retext_state(self.data_dir, dry_run=True)
        self.assertEqual(report["changed"], 1)
        self.assertTrue(report["dry_run"])
        state = ag_queue.load_state(self.data_dir)
        self.assertIn("43.6527", state["scenes"]["damaged"]["place"])

    def test_retext_keeps_a_model_title(self):
        scene = g.build_entry(source(), proposal(title="Market day"),
                              self.answer(), "2026-09-12")
        self.save({"s": scene})
        report = g.retext_state(self.data_dir)
        self.assertEqual(report["changed"], 0)


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

    def test_record_call_flushes_a_pending_trace(self):
        trace = {"source": "src-1", "calls": []}
        g.record_call(self.data_dir, trace,
                      {"stage": "proposal", "answer": "A"})
        pending = g.pending_trace_path(self.data_dir, "src-1")
        self.assertTrue(pending.exists())
        self.assertEqual(json.loads(pending.read_text())["calls"][0]["stage"],
                         "proposal")

    def test_write_trace_clears_the_pending_file(self):
        trace = {"source": "src-1", "calls": [{"stage": "edit"}]}
        g.record_call(self.data_dir, trace, {"stage": "proposal"})
        g.write_trace(self.data_dir, "scene-1", trace)
        self.assertFalse(g.pending_trace_path(self.data_dir,
                                              "src-1").exists())
        self.assertTrue(g.trace_path(self.data_dir, "scene-1").exists())

    def test_write_trace_survives_a_broken_traces_dir(self):
        # The recorder must never take a scene down with it: a traces path
        # that cannot be created (here a file where the dir belongs) is
        # swallowed, the queue entry stays.
        (self.data_dir / "traces").write_text("not a dir")
        g.write_trace(self.data_dir, "scene-1", {"calls": []})  # no raise
        self.assertFalse(g.trace_path(self.data_dir, "scene-1").exists())

    def test_scrub_removes_keys_and_host_paths(self):
        cleaned = g._scrub({"prompt": "key sk-or-abcdefghijklmnop here",
                            "path": str(Path.home() / "fuchs" / "x"),
                            "list": ["pk-live-1234567890"]})
        blob = json.dumps(cleaned)
        self.assertNotIn("sk-or-", blob)
        self.assertNotIn("pk-live-", blob)
        self.assertNotIn(str(Path.home()), blob)
        self.assertIn("<redacted>", blob)

    def test_recent_labels_reads_the_state(self):
        state = {"version": 1, "scenes": {
            "a": {"id": "a", "anomaly": "Bottle", "added": "2026-09-12"}}}
        ag_queue.save_state(self.data_dir, state)
        self.assertEqual(g.recent_labels(self.data_dir,
                                         today=__import__("datetime").date(
                                             2026, 9, 12)), ["Bottle"])


# ── Run loop ───────────────────────────────────────────────────────────────

class CheckScoringTest(unittest.TestCase):
    """The comparable scale behind best-of-k (ticket #1381)."""

    def test_score_counts_the_requirements_met(self):
        self.assertEqual(g.score_from_failed([]), g.REQUIREMENTS_TOTAL)
        self.assertEqual(g.score_from_failed([1, 2]), g.REQUIREMENTS_TOTAL - 2)

    def test_failed_numbers_are_cleaned(self):
        self.assertEqual(g.failed_requirements([3, 1, 3]), [1, 3])
        self.assertEqual(g.failed_requirements(["2"]), [2])
        # out-of-range numbers and non-numbers cannot inflate the count
        self.assertEqual(g.failed_requirements([0, 9, "x", None]), [])
        self.assertEqual(g.failed_requirements("nope"), [])

    def test_check_scene_scores_from_the_failed_list(self):
        old = g._run_call
        g._run_call = lambda *a, **kw: {
            "parsed": {"failed": [2, 4], "reason": "tone off",
                       "fix_prompt": "match the tone"},
            "answer": "A", "model": "m", "prompt": "p", "usage": {}}
        try:
            check = g.check_scene(Path("x.png"), proposal(), "k", "m", "u", 1,
                                  1, 0.0)
        finally:
            g._run_call = old
        self.assertEqual(check["failed"], [2, 4])
        self.assertEqual(check["score"], g.REQUIREMENTS_TOTAL - 2)
        self.assertFalse(check["ok"])
        self.assertEqual(check["fix_prompt"], "match the tone")

    def test_check_scene_is_unscored_without_a_failed_list(self):
        old = g._run_call
        g._run_call = lambda *a, **kw: {"parsed": {"ok": True}, "answer": "A",
                                        "model": "m", "prompt": "p",
                                        "usage": {}}
        try:
            check = g.check_scene(Path("x.png"), proposal(), "k", "m", "u", 1,
                                  1, 0.0)
        finally:
            g._run_call = old
        self.assertIsNone(check["score"])
        self.assertIsNone(check["ok"])


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

    def shipped_bytes(self, scene) -> bytes:
        eid = scene["report"]["scene"]
        return (self.data_dir / "library" / eid / f"{eid}.jpg").read_bytes()

    def trace_of(self, scene) -> dict:
        return json.loads(g.trace_path(self.data_dir,
                                       scene["report"]["scene"]).read_text())

    def test_happy_path_lands_a_scene(self):
        scene, failed, totals = self.run_one(candidates=1)
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
        scene, _, _ = self.run_one(candidates=1)
        data = self.trace_of(scene)
        # #1381 moved the coordinate call after the correction decision, so
        # the click target is computed on the image that actually ships.
        self.assertEqual([c["stage"] for c in data["calls"]],
                         ["proposal", "edit", "check", "coordinates"])
        proposal_call = data["calls"][0]
        self.assertEqual(proposal_call["prompt"], "proposal-prompt")
        self.assertEqual(proposal_call["answer"], "A")
        # #1373: reasoning, timestamp and the image used ride along
        self.assertEqual(proposal_call["reasoning"], "R")
        self.assertEqual(proposal_call["at"], "2026-09-12T12:00:00+02:00")
        self.assertEqual(proposal_call["usage"]["cost"], 0.001)
        edit_call = data["calls"][1]
        self.assertEqual(edit_call["stage"], "edit")
        # the edit names its source photo (no host path)
        self.assertEqual(edit_call["image"],
                         f"{SOURCE_ID}.jpg")
        # the finished trace replaces the in-progress sidecar
        self.assertFalse(
            g.pending_trace_path(self.data_dir,
                                 data["source"]).exists())

    def test_best_of_k_ships_the_highest_scoring_candidate(self):
        def edit(*a, **kw):
            color = ["red", "blue", "green"][len(drawn)]
            drawn.append(color)
            return img_bytes(color=color)

        drawn = []

        def check(image, *a, **kw):
            if Path(image).name.endswith("-c2.png"):
                return {"ok": True, "score": g.REQUIREMENTS_TOTAL,
                        "failed": [], "reason": "clean", "fix_prompt": "",
                        "call": call()}
            return {"ok": False, "score": 3, "failed": [1, 2, 3, 4],
                    "reason": "tone and scale", "fix_prompt": "fix it",
                    "call": call()}

        scene, failed, totals = self.run_one(edit=edit, check=check,
                                             candidates=3)
        self.assertIsNone(failed)
        self.assertEqual(totals["image_calls"], 3)
        report = scene["report"]
        self.assertEqual(report["winner"], 2)
        self.assertEqual(report["candidate_count"], 3)
        self.assertEqual(report["checker"]["score"], g.REQUIREMENTS_TOTAL)
        self.assertFalse(report["checker"]["corrected"])
        self.assertEqual(self.shipped_bytes(scene), img_bytes(color="blue"))
        # every candidate is in the trace, with prompt, seed and score, so
        # an audit can see why candidate 2 beat 1 and 3
        trace = self.trace_of(scene)
        self.assertEqual([c["candidate"] for c in trace["candidates"]],
                         [1, 2, 3])
        self.assertEqual([c["score"] for c in trace["candidates"]], [3, 7, 3])
        self.assertEqual(trace["candidates"][0]["reason"], "tone and scale")
        self.assertIn("Plastic bottle", trace["candidates"][0]["prompt"])
        self.assertTrue(trace["candidates"][1]["seed"] is not None)
        self.assertEqual(trace["candidate_policy"]["candidates"], 3)

    def test_one_correction_runs_and_cannot_veto(self):
        edits = {"n": 0}

        def edit(*a, **kw):
            edits["n"] += 1
            return img_bytes(color="blue" if edits["n"] > 1 else "red")

        checks = {"n": 0}

        def check(*a, **kw):
            checks["n"] += 1
            return {"ok": False, "score": 4, "failed": [4, 5],
                    "reason": "tone off", "fix_prompt": "match the tone",
                    "call": call()}

        scene, failed, totals = self.run_one(edit=edit, check=check,
                                             candidates=1)
        self.assertIsNone(failed)
        self.assertEqual(edits["n"], 2)   # one candidate + one correction
        self.assertEqual(checks["n"], 2)  # candidate check + post-fix check
        self.assertEqual(totals["image_calls"], 2)
        report = scene["report"]
        self.assertTrue(report["checker"]["corrected"])
        # the post-correction verdict is recorded but did not veto
        self.assertEqual(report["checker"]["scoreAfterCorrection"], 4)
        trace = self.trace_of(scene)
        self.assertTrue(trace["correction"]["applied"])
        self.assertEqual([c["stage"] for c in trace["calls"]].count("fix-edit"),
                         1)
        self.assertEqual([c["stage"] for c in trace["calls"]].count("recheck"),
                         1)

    def test_no_fix_prompt_ships_the_winner_unchanged(self):
        scene, failed, totals = self.run_one(
            check=stub_check(failed=[1], fix_prompt=""), candidates=1)
        self.assertIsNone(failed)
        self.assertEqual(totals["image_calls"], 1)
        self.assertFalse(scene["report"]["checker"]["corrected"])
        self.assertEqual(scene["report"]["checker"]["failed"], [1])

    def test_correction_that_fails_the_gates_keeps_the_winner(self):
        calls = {"n": 0}

        def gate(edited, original, dedup):
            calls["n"] += 1
            if calls["n"] == 2:  # the correction draw re-renders the frame
                return "whole-frame repaint", None
            return None, {"cx": 0.5, "cy": 0.6}

        g.candidate_gate = gate
        scene, failed, totals = self.run_one(
            check=stub_check(failed=[4], fix_prompt="fix the tone"),
            candidates=1)
        self.assertIsNone(failed)
        # the pre-correction winner ships; the broken correction is recorded
        self.assertFalse(scene["report"]["checker"]["corrected"])
        self.assertEqual(totals["image_calls"], 2)
        self.assertEqual(self.trace_of(scene)["correction"]["rejected"],
                         "whole-frame repaint")

    def test_a_broken_draw_is_replaced_by_another_one(self):
        calls = {"n": 0}

        def gate(edited, original, dedup):
            calls["n"] += 1
            if calls["n"] == 1:
                return "identical output", None
            return None, {"cx": 0.5, "cy": 0.6}

        g.candidate_gate = gate
        scene, failed, totals = self.run_one(candidates=2)
        self.assertIsNotNone(scene)
        self.assertEqual(calls["n"], 3)          # 1 broken + 2 shipped draws
        self.assertEqual(totals["image_calls"], 3)
        self.assertEqual(scene["report"]["mechanical_failures"],
                         ["identical output"])
        self.assertEqual(scene["report"]["candidate_count"], 2)

    def test_non_landscape_output_is_replaced(self):
        g.deterministic_gate = lambda *a, **kw: (None, {"cx": 0.5,
                                                        "cy": 0.6})
        shapes = [(1024, 1024), (1200, 800)]

        def edit(*a, **kw):
            w, h = shapes.pop(0) if shapes else (1200, 800)
            return img_bytes(w=w, h=h)

        scene, failed, totals = self.run_one(edit=edit, candidates=1)
        self.assertIsNotNone(scene)
        self.assertEqual(totals["image_calls"], 2)
        self.assertEqual(scene["report"]["mechanical_failures"],
                         ["non-landscape output (1024x1024)"])
        self.assertEqual(scene["report"]["candidate_count"], 1)

    def test_no_candidate_passing_the_gates_is_reported(self):
        g.candidate_gate = lambda *a, **kw: ("identical output", None)
        scene, failed, totals = self.run_one(candidates=1)
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "image")
        # 3 draws in the first attempt, the last budgeted draw in the second
        self.assertEqual(totals["image_calls"], 1 + g.MECHANICAL_RETRIES + 1)

    def test_budget_degrades_to_fewer_candidates_but_still_ships(self):
        scene, failed, totals = self.run_one(candidates=3, max_generations=2)
        self.assertIsNone(failed)
        self.assertEqual(totals["image_calls"], 2)
        self.assertEqual(scene["report"]["candidate_count"], 2)

    def test_no_check_ships_the_first_gate_passing_candidate(self):
        def boom(*a, **kw):
            raise AssertionError("--no-check must not call the checker")

        scene, failed, totals = self.run_one(check=boom, candidates=2,
                                             no_check=True)
        self.assertIsNone(failed)
        self.assertEqual(totals["image_calls"], 2)
        self.assertTrue(scene["report"]["checker"]["skipped"])
        self.assertIsNone(scene["report"]["checker"]["score"])

    def test_invalid_proposal_fails_without_an_image_call(self):
        scene, failed, totals = self.run_one(propose=stub_propose(
            result={"anomaly": ""}, errors=["anomaly must be non-empty"]))
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "proposal")
        self.assertEqual(totals.get("image_calls", 0), 0)
        self.assertEqual(len(ag_queue.load_state(self.data_dir)["scenes"]), 0)

    def test_failed_scene_keeps_a_pending_trace(self):
        # A scene that never lands still leaves what was tried: the trace
        # is flushed as the pipeline runs (#1373).
        self.run_one(propose=stub_propose(
            result={"anomaly": ""}, errors=["anomaly must be non-empty"]))
        pending = g.pending_trace_path(self.data_dir, SOURCE_ID)
        self.assertTrue(pending.exists())
        data = json.loads(pending.read_text())
        self.assertEqual(data["calls"][0]["stage"], "proposal")
        self.assertIn("anomaly must be non-empty", data["error"])
        # No final trace exists for a scene that was never saved.
        self.assertEqual(list((self.data_dir / "traces").glob("*.json")), [])

    def test_second_proposal_after_a_gate_only_failure(self):
        # Even with every candidate broken, a second proposal (fresh anomaly)
        # gets a chance before the scene is reported failed.
        calls = {"n": 0}

        def gate(edited, original, dedup):
            calls["n"] += 1
            if calls["n"] <= 1 + g.MECHANICAL_RETRIES:
                return "no localized edit", None
            return None, {"cx": 0.5, "cy": 0.6}

        g.candidate_gate = gate
        scene, failed, totals = self.run_one(candidates=1)
        self.assertIsNotNone(scene)
        self.assertEqual(scene["report"]["attempt"], 2)

    def test_missing_coordinates_falls_back_to_the_hotspot(self):
        scene, failed, _ = self.run_one(locate=stub_locate(coords=None),
                                        candidates=1)
        self.assertIsNotNone(scene)
        self.assertEqual(scene["report"]["answer"]["fallback"], "hotspot")
        self.assertEqual(scene["report"]["answer"]["x"], 0.5)

    def test_coordinate_conflict_is_reported(self):
        scene, _, _ = self.run_one(locate=stub_locate(
            {"x": 0.1, "y": 0.1, "r": 0.02, "figure": False}), candidates=1)
        self.assertIsNotNone(scene["report"]["coord_conflict"])

    def test_budget_exhausted_before_the_first_edit(self):
        self.write_source()
        g.propose_anomaly = stub_propose()
        g.image_edit = lambda *a, **kw: img_bytes()
        args = self.make_args(count=1)
        args.max_generations = 0
        totals = ag_llm.zero_usage()
        src = ag_sources.list_sources(self.data_dir)[0]
        scene, failed = g._generate_one(src, args, self.data_dir,
                                        "2026-09-12", self._tmp / "out", [],
                                        totals, lambda *a, **kw: None)
        self.assertIsNone(scene)
        self.assertEqual(failed["stage"], "budget")
        self.assertIn("generation budget", failed["reason"])

    def test_image_usage_is_folded_into_the_totals_and_the_trace(self):
        def edit(*a, **kw):
            kw["usage_out"]["cost"] = 0.05
            kw["usage_out"]["prompt_tokens"] = 900
            return img_bytes()

        scene, _, totals = self.run_one(edit=edit, candidates=2)
        self.assertEqual(totals["image_cost"], 0.1)
        self.assertEqual(totals["image_calls"], 2)
        trace = self.trace_of(scene)
        # per-call image cost lands in the trace (#1381)
        edit_calls = [c for c in trace["calls"] if c["stage"] == "edit"]
        self.assertEqual(edit_calls[0]["usage"]["cost"], 0.05)

    def test_aspect_ratio_never_asks_for_a_square(self):
        self.assertNotIn(1.0, [ratio for _, ratio in g.ASPECTS])
        self.assertEqual(g.aspect_ratio_for(1000, 997), "5:4")
        self.assertEqual(g.aspect_ratio_for(1200, 800), "3:2")
        self.assertEqual(g.aspect_ratio_for(1920, 800), "21:9")

    def test_budget_default_covers_every_scene_at_full_k(self):
        args = self.make_args(count=2, candidates=3)
        self.assertEqual(args.max_generations,
                         2 * (3 + g.MECHANICAL_RETRIES + 1))


class RunTest(TempDataMixin, unittest.TestCase):
    def test_run_adds_a_scene_and_reports(self):
        self.write_source()
        g.propose_anomaly = stub_propose()
        g.locate_anomaly = stub_locate()
        g.check_scene = stub_check()
        g.image_edit = lambda *a, **kw: img_bytes()
        report = g.run(self.make_args(count=1, candidates=1))
        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(report["image_calls"], 1)
        self.assertEqual(report["candidates"], 1)
        self.assertIn("cost_per_scene", report)
        self.assertIn("moderation", report)
        self.assertIn("winner_scores", report)
        status = json.loads(g.status_path(self.data_dir).read_text())
        self.assertEqual(status["state"], "done")
        # ticket #1381: the status the dev button polls is honest now
        self.assertEqual(status["phase"], "done")
        self.assertEqual(status["candidates"], 1)
        self.assertEqual(status["imageCalls"], 1)
        self.assertEqual(status["planned"], 1)
        self.assertEqual(status["scenesTotal"], 1)
        self.assertGreater(status["cost"], 0)

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
        ag_llm.add_usage(total, {"completion_tokens": 2,
                                 "reasoning_tokens": 9, "cost": 0.1})
        self.assertEqual(total, {"prompt_tokens": 3, "completion_tokens": 6,
                                 "reasoning_tokens": 9, "cost": 0.6})

    def test_usage_of_reads_reasoning_tokens_and_cost(self):
        usage = ag_llm.usage_of({"usage": {
            "prompt_tokens": 10, "completion_tokens": 40, "cost": 0.02,
            "completion_tokens_details": {"reasoning_tokens": 31}}})
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 40,
                                 "reasoning_tokens": 31, "cost": 0.02})

    def test_reasoning_of_takes_both_field_names(self):
        body = {"choices": [{"message": {"reasoning_content": "why"}}]}
        self.assertEqual(ag_llm.reasoning_of(body), "why")
        alt = {"choices": [{"message": {"reasoning": "how"}}]}
        self.assertEqual(ag_llm.reasoning_of(alt), "how")
        self.assertEqual(ag_llm.reasoning_of({}), "")

    def test_clean_text_collapses_and_caps(self):
        self.assertEqual(ag_llm.clean_text(" a\n b ", 10), "a b")
        self.assertEqual(len(ag_llm.clean_text("x" * 500, 10)), 10)


if __name__ == "__main__":
    unittest.main()
