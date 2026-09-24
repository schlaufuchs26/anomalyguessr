"""Tests for pipeline/ag_describe.py (ticket #1883).

No network and no API spend: the model call is monkeypatched and the
catalogue fetch is injected, so the guard, the record-text assembly and the
pass over the queue are covered deterministically.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import ag_describe as d
import ag_llm
import ag_queue

GALLICA_SOURCE = {
    "repository": "Bibliothèque nationale de France (Gallica)",
    "fileUrl": "https://gallica.bnf.fr/ark:/12148/btv1b6917658k",
    "originalTitle": "Christian, boxeur : [photographie de presse] / [Agence Rol]",
    "date": "1911",
    "place": "",
    "license": "Public domain",
    "description": "Référence bibliographique : Rol, 16775 Appartient à "
                   "l’ensemble documentaire : Pho20Rol",
}

COMMONS_SOURCE = {
    "repository": "Wikimedia Commons",
    "fileUrl": "https://commons.wikimedia.org/wiki/File:Market.jpg",
    "originalTitle": "Busy market street, 1905",
    "date": "1905",
    "place": "Vienna",
    "license": "CC BY-SA 4.0",
    "description": "A busy market street with stalls and shoppers.",
}

GALLICA_OAI = """<?xml version="1.0"?><record><metadata>
<dc:title>Christian, boxeur : [photographie de presse] / [Agence Rol]</dc:title>
<dc:creator>Agence Rol</dc:creator>
<dc:date>1911</dc:date>
<dc:subject>Boxe</dc:subject>
<dc:description>Référence bibliographique : Rol, 16775</dc:description>
</metadata></record>"""


def usage(cost=0.0002, pt=200, ct=40):
    return {"prompt_tokens": pt, "completion_tokens": ct, "cost": cost}


def call(answer, cost=0.0002):
    return {"model": "stub", "prompt": "P", "answer": answer, "reasoning": "",
            "image": "", "at": "2026-09-24T22:00:00+02:00",
            "usage": usage(cost), "duration_s": 0.5}


def entry(description, source=None):
    return {"id": "scene-1", "title": "Christian, boxeur", "place": "",
            "year": "1911", "description": description,
            "source": dict(source or GALLICA_SOURCE)}


class CaptionTests(unittest.TestCase):
    def test_the_assembled_caption_is_recognised(self):
        self.assertTrue(d.is_caption("Christian, boxeur · Gallica."))
        self.assertFalse(d.is_caption("A village market in 1905."))

    def test_language_markers(self):
        self.assertEqual(d.language_guess(
            "Christian, boxeur · Bibliothèque nationale de France (Gallica)."),
            "fr")
        self.assertEqual(d.language_guess(
            "Market bustle on Pike Place in Seattle, c. 1907: farm wagons."),
            "en")

    def test_a_quoted_foreign_title_does_not_decide_the_label(self):
        self.assertEqual(d.language_guess(
            'A press photograph titled "Tunis, le port", le port, taken by '
            'Agence Rol in 1912, showing the harbour.'), "en")

    def test_needs_description_covers_empty_caption_and_foreign(self):
        self.assertTrue(d.needs_description(""))
        self.assertTrue(d.needs_description("Christian · Gallica."))
        self.assertFalse(d.needs_description(
            "A market street with stalls and shoppers in 1905."))


class RecordTextTests(unittest.TestCase):
    def test_boilerplate_is_not_a_usable_description(self):
        self.assertFalse(d.usable_description(GALLICA_SOURCE["description"]))
        self.assertFalse(d.usable_description(
            "{'firstIndexTime': '2025-08-20T01:15:52.248Z'}"))
        self.assertTrue(d.usable_description(
            "A busy market street with stalls and shoppers."))

    def test_source_block_text_leaves_the_url_out(self):
        text = d.source_block_text(GALLICA_SOURCE)
        self.assertIn("Christian, boxeur", text)
        self.assertIn("1911", text)
        self.assertNotIn("gallica.bnf.fr", text)

    def test_a_junk_description_triggers_the_fetch(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return GALLICA_OAI

        text, fetched = d.record_text(GALLICA_SOURCE, fetch)
        self.assertTrue(fetched)
        self.assertEqual(len(calls), 1)
        self.assertIn("Boxe", text)
        self.assertIn("Christian, boxeur", text)

    def test_a_real_description_is_not_fetched_again(self):
        text, fetched = d.record_text(
            COMMONS_SOURCE, lambda url: self.fail("must not fetch"))
        self.assertFalse(fetched)
        self.assertIn("stalls and shoppers", text)

    def test_a_fetch_failure_leaves_the_source_block_alone(self):
        def broken(url):
            raise d.DescriptionError("offline")

        text, fetched = d.record_text(GALLICA_SOURCE, broken)
        self.assertFalse(fetched)
        self.assertIn("Christian, boxeur", text)

    def test_no_fetcher_means_no_fetch(self):
        _, fetched = d.record_text(GALLICA_SOURCE, None)
        self.assertFalse(fetched)

    def test_repository_dispatch(self):
        gallica = d.fetch_record_text(
            GALLICA_SOURCE, lambda u: GALLICA_OAI)
        self.assertIn("Boxe", gallica)
        common = d.fetch_record_text(COMMONS_SOURCE, lambda u: json.dumps(
            {"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {
                "ImageDescription": {"value": "<b>A market</b> in 1905."}}}]}}}}))
        self.assertIn("A market in 1905.", common)
        unknown = d.fetch_record_text(
            {"repository": "Elsewhere", "fileUrl": "https://x.test/1"},
            lambda u: self.fail("must not fetch"))
        self.assertEqual(unknown, "")


class GuardTests(unittest.TestCase):
    VOCAB = ("Bibliothèque nationale de France (Gallica) Christian, boxeur "
             "photographie de presse Agence Rol 1911")

    def test_a_number_outside_the_record_is_refused(self):
        facts = d.unsupported_facts("Photographed in 1914 by Agence Rol.",
                                    self.VOCAB)
        self.assertIn("1914", facts)

    def test_a_name_outside_the_record_is_refused(self):
        facts = d.unsupported_facts(
            "A boxer photographed by Agence Rol in Paris.", self.VOCAB)
        self.assertEqual(facts, ["Paris"])

    def test_sourced_text_passes(self):
        self.assertEqual(d.unsupported_facts(
            "Christian, a boxer, photographed by Agence Rol in 1911.",
            self.VOCAB), [])

    def test_sentence_starts_and_common_words_are_not_names(self):
        self.assertEqual(d.unsupported_facts(
            "The photograph shows a boxer. A print from the era.",
            self.VOCAB), [])

    def test_institutional_words_are_not_names(self):
        # "the New York Public Library" for a record that says "NYPL" adds
        # no place or date; only the sourced words must clear the guard.
        vocab = self.VOCAB + " New York 1900"
        self.assertEqual(d.unsupported_facts(
            "Held by the New York Public Library.", vocab), [])

    def test_clean_description_strips_fences_and_quotes(self):
        self.assertEqual(d.clean_description('```"A market in 1905."```'),
                         "A market in 1905.")

    def test_clean_description_caps_at_a_sentence(self):
        long = ("A " + "word " * 200).strip()
        out = d.clean_description(long, limit=60)
        self.assertLessEqual(len(out), 61)
        self.assertTrue(out.endswith("."))


class DescribeEntryTests(unittest.TestCase):
    def setUp(self):
        self._old = d.describe_text

    def tearDown(self):
        d.describe_text = self._old

    def test_a_clean_answer_lands_with_provenance(self):
        d.describe_text = lambda *a, **kw: call(
            "Christian, a boxer, photographed by Agence Rol in 1911.")
        out, info = d.describe_entry(entry("Christian, boxeur · Gallica."),
                                     "key")
        self.assertEqual(info["status"], "described")
        self.assertEqual(out["description"],
                         "Christian, a boxer, photographed by Agence Rol in 1911.")
        self.assertEqual(out["description_source"], "catalog")

    def test_an_outside_fact_is_never_written(self):
        d.describe_text = lambda *a, **kw: call(
            "Christian, a boxer, photographed in Paris in 1911.")
        out, info = d.describe_entry(entry("Christian, boxeur · Gallica."),
                                     "key")
        self.assertEqual(info["status"], "guarded")
        self.assertEqual(info["facts"], ["Paris"])
        self.assertEqual(out["description"], "Christian, boxeur · Gallica.")
        self.assertNotIn("description_source", out)
        # the guard hit triggered the re-ask, which leaked the same fact
        self.assertEqual(len(info["calls"]), 2)

    def test_a_guard_hit_is_repaired_by_the_reask(self):
        answers = ["A boxer photographed in Paris in 1911.",
                   "Christian, a boxer, photographed by Agence Rol in 1911."]
        seen = []

        def fake(record, api_key, model, base_url=None, max_tokens=0,
                 timeout=0, avoid=()):
            seen.append(tuple(avoid))
            return call(answers[min(len(seen) - 1, 1)])

        d.describe_text = fake
        out, info = d.describe_entry(entry("Christian, boxeur · Gallica."),
                                     "key")
        self.assertEqual(info["status"], "described")
        self.assertEqual(seen, [(), ("Paris",)])
        self.assertIn("Christian, a boxer", out["description"])

    def test_an_english_description_is_left_alone(self):
        src = entry("A market street with stalls and shoppers in 1905.",
                    COMMONS_SOURCE)
        d.describe_text = lambda *a, **kw: self.fail("must not call")
        out, info = d.describe_entry(src, "key")
        self.assertEqual(info["status"], "kept")
        self.assertEqual(out["description"], src["description"])

    def test_an_empty_answer_changes_nothing(self):
        d.describe_text = lambda *a, **kw: call("   ")
        out, info = d.describe_entry(entry("Christian · Gallica."), "key")
        self.assertEqual(info["status"], "empty")
        self.assertEqual(out["description"], "Christian · Gallica.")

    def test_a_broken_call_is_an_error_not_a_crash(self):
        def boom(*a, **kw):
            raise TimeoutError("the read operation timed out")

        d.describe_text = boom
        out, info = d.describe_entry(entry("Christian · Gallica."), "key")
        self.assertEqual(info["status"], "error")
        self.assertIn("TimeoutError", info["error"])
        self.assertEqual(out["description"], "Christian · Gallica.")


class DescribeStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.data_dir = self._tmp / "anomalyguessr"
        self.data_dir.mkdir(parents=True)
        self._old = d.describe_text

    def tearDown(self):
        d.describe_text = self._old
        shutil.rmtree(self._tmp, ignore_errors=True)

    def write_state(self, scenes):
        ag_queue.save_state(self.data_dir, {"version": 1, "scenes": scenes})

    def test_measure_counts_captions_and_english(self):
        report = d.measure_state({"scenes": {
            "a": {"description": "Christian · Gallica."},
            "b": {"description": "A market in 1905 with stalls."},
            "c": {"description": ""},
        }})
        self.assertEqual(report["scenes"], 3)
        self.assertEqual(report["caption"], 1)
        self.assertEqual(report["empty"], 1)
        self.assertEqual(report["english"], 1)
        self.assertEqual(report["without_description"], 2)

    def test_apply_writes_and_reports_the_cost(self):
        self.write_state({
            "gallica-a": entry("Christian · Gallica."),
            "commons-b": entry("A market street with stalls and shoppers.",
                               COMMONS_SOURCE),
        })
        d.describe_text = lambda *a, **kw: call(
            "Christian, a boxer, photographed by Agence Rol in 1911.")
        report = d.describe_state(self.data_dir, "key",
                                  fetch=lambda u: GALLICA_OAI)
        self.assertEqual(report["targets"], 1)
        self.assertEqual(report["described"], 1)
        self.assertEqual(report["guarded"], [])
        self.assertEqual(report["usage"]["cost"], 0.0002)
        self.assertEqual(report["cost_per_scene"], 0.0002)
        state = json.loads((self.data_dir / "state.json").read_text())
        self.assertEqual(
            state["scenes"]["gallica-a"]["description"],
            "Christian, a boxer, photographed by Agence Rol in 1911.")
        self.assertEqual(state["scenes"]["commons-b"]["description"],
                         "A market street with stalls and shoppers.")

    def test_a_guard_hit_is_reported_and_not_written(self):
        self.write_state({"gallica-a": entry("Christian · Gallica.")})
        d.describe_text = lambda *a, **kw: call(
            "Christian, a boxer, photographed in Paris in 1911.")
        report = d.describe_state(self.data_dir, "key",
                                  fetch=lambda u: GALLICA_OAI)
        self.assertEqual(report["described"], 0)
        self.assertEqual(report["guarded"][0]["facts"], ["Paris"])
        state = json.loads((self.data_dir / "state.json").read_text())
        self.assertEqual(state["scenes"]["gallica-a"]["description"],
                         "Christian · Gallica.")

    def test_a_failing_scene_does_not_stop_the_pass(self):
        self.write_state({"gallica-a": entry("Christian · Gallica.")})

        def boom(*a, **kw):
            raise TimeoutError("the read operation timed out")

        d.describe_text = boom
        report = d.describe_state(self.data_dir, "key",
                                  fetch=lambda u: GALLICA_OAI)
        self.assertEqual(report["described"], 0)
        self.assertEqual(report["skipped"][0]["status"], "error")

    def test_dry_run_calls_nothing(self):
        self.write_state({"gallica-a": entry("Christian · Gallica.")})
        d.describe_text = lambda *a, **kw: self.fail("must not call")
        report = d.describe_state(self.data_dir, "key", dry_run=True)
        self.assertEqual(report["targets"], 1)
        self.assertEqual(report["described"], 0)


class LlmClientTests(unittest.TestCase):
    def test_describe_text_shapes_a_trace_call(self):
        old = ag_llm.chat
        ag_llm.chat = lambda *a, **kw: {
            "choices": [{"message": {"content": " A market in 1905. "}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                      "cost": 0.001}}
        try:
            got = d.describe_text("record text", "key", "stub/model")
        finally:
            ag_llm.chat = old
        self.assertEqual(got["answer"], " A market in 1905. ")
        self.assertEqual(got["usage"]["cost"], 0.001)
        self.assertIn("record text", got["prompt"])


if __name__ == "__main__":
    unittest.main()
