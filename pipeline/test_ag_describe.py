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


GREEK_SOURCE = {
    "repository": "Europeana",
    "fileUrl": "https://example.org/item.jpg",
    "originalTitle": "Ναός Αγίου Γεωργίου",
    "date": "1900",
    "place": "Χαϊδάρι",
    "license": "CC BY-SA",
    "description": "Η ίδρυση του ναΐσκου του Αγίου Γεωργίου ανάγεται στα "
                   "μεταβυζαντινά χρόνια.",
}


def greek_entry(description="Ναός Αγίου Γεωργίου · Χαϊδάρι · Europeana."):
    return {"id": "europeana-1", "title": "Ναός Αγίου Γεωργίου",
            "place": "Χαϊδάρι", "year": "1900", "description": description,
            "source": dict(GREEK_SOURCE)}


# The reported scene (ticket #2326): a Europeana record whose only text is a
# Dutch title, so the pool stores no English title/description and the
# English-description step has to translate. The title carries no marker
# ``language_guess`` knows, which is why the cheap test alone left it Dutch.
DUTCH_SOURCE = {
    "repository": "Europeana",
    "fileUrl": "https://www.europeana.eu/item/2058632/13cb4566",
    "originalTitle": "Zicht op vestingwerk met twee rondelen",
    "date": "1897",
    "place": "Maastricht",
    "license": "CC BY-SA",
    "description": "Zicht op vestingwerk met twee rondelen",
    "titleEn": "",
}


def dutch_entry(description="Zicht op vestingwerk met twee rondelen · "
                            "Maastricht · Europeana."):
    return {"id": "europeana-2", "title": "Zicht op vestingwerk met twee "
                                        "rondelen",
            "place": "Maastricht", "year": "1897", "description": description,
            "source": dict(DUTCH_SOURCE)}


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

    def test_a_quoted_translation_is_not_a_name(self):
        # The model quotes the translated catalogue title; that is a citation
        # of the record, not an invented name (ticket #2326, scene
        # "Zicht op vestingwerk met twee rondelen").
        self.assertEqual(d.unsupported_facts(
            'A view of a fortification, taken in Paris in 1911. The record is '
            'titled "Christian, boxeur" ("View of the boxer").',
            self.VOCAB + " Paris"), [])

    def test_a_number_inside_quotes_is_still_checked(self):
        facts = d.unsupported_facts('A print from "1914".', self.VOCAB)
        self.assertIn("1914", facts)

    def test_institutional_words_are_not_names(self):
        # "the New York Public Library" for a record that says "NYPL" adds
        # no place or date; only the sourced words must clear the guard.
        vocab = self.VOCAB + " New York 1900"
        self.assertEqual(d.unsupported_facts(
            "Held by the New York Public Library.", vocab), [])

    def test_a_name_the_record_spells_differently_is_the_same_name(self):
        # The reported Bolzani scene (ticket #2328): the French record says
        # "frontière italienne", the English description "the Italian
        # border". The shared stem is the record's own name.
        vocab = ("Bolzani à la frontière italienne 1915 Agence Meurisse "
                 "Bibliothèque nationale de France")
        self.assertEqual(d.unsupported_facts(
            "Bolzani at the Italian border.", vocab), [])
        # An accent is a spelling variant, not a different name.
        self.assertEqual(d.unsupported_facts(
            "Held by the Musée.", "Agence Meurisse Musee"), [])
        # A name with no such stem is still refused.
        self.assertEqual(d.unsupported_facts(
            "Bolzani at the Bavarian border.", vocab), ["Bavarian"])

    def test_a_glossed_quotation_is_not_a_name(self):
        # The reported scene (ticket #2328): the model quotes the French
        # title and glosses the quotation in parentheses. "Christmas"
        # translates "Noël", so it cites the record like the quotation does.
        self.assertEqual(d.unsupported_facts(
            'Titled "Scène enfantine, le lendemain de Noël" (childish scene, '
            'the day after Christmas).', self.VOCAB), [])

    def test_a_parenthesis_that_does_not_gloss_a_quote_is_still_checked(self):
        self.assertEqual(d.unsupported_facts("A boxer (Paris).", self.VOCAB),
                         ["Paris"])

    def test_a_glossed_quotation_still_checks_numbers(self):
        facts = d.unsupported_facts(
            '"Christian, boxeur" (the boxer photographed in 1939).',
            self.VOCAB)
        self.assertEqual(facts, ["1939"])

    def test_a_non_latin_record_leaves_names_to_the_prompt(self):
        # No English spelling can match a Greek record, so the translated
        # names cannot be checked against it (ticket #2328).
        vocab = "Ναός Αγίου Γεωργίου Χαϊδάρι 1900"
        self.assertEqual(d.unsupported_facts(
            "The Church of Saint George in Chaidari, Greece.", vocab), [])

    def test_a_non_latin_record_still_refuses_a_number(self):
        vocab = "Ναός Αγίου Γεωργίου Χαϊδάρι 1900"
        self.assertEqual(
            d.unsupported_facts("The church, built in 1912.", vocab),
            ["1912"])

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

    def test_a_quoted_translation_lands_as_an_english_description(self):
        # The reported scene: the model translates the Dutch title and quotes
        # it; the quoted words are a citation, so the guard lets the answer
        # through instead of leaving the Dutch caption (ticket #2326).
        d.describe_text = lambda *a, **kw: call(
            "TITLE: View of the fortification with two roundels\n\n"
            "A view of a fortification with two roundels, taken in "
            "Maastricht in 1897. The photograph comes from the Europeana "
            'record "Zicht op vestingwerk met twee rondelen" ("View of the '
            'fortification with two roundels").')
        out, info = d.describe_entry(dutch_entry(), "key")
        self.assertEqual(info["status"], "described")
        self.assertTrue(out["description"].startswith("A view of a fortification"))
        self.assertEqual(out["title"], "View of the fortification with two roundels")
        self.assertEqual(out["source"]["titleEn"],
                         "View of the fortification with two roundels")

    def test_a_translated_name_lands_on_a_non_latin_record(self):
        # The reported Greek scene (ticket #2328): the record names its
        # church in Greek, the model renders it in English. The guard must
        # not read the translation as an invented name and keep the caption.
        d.describe_text = lambda *a, **kw: call(
            "TITLE: Church of Saint George\n\n"
            "A photograph from 1900 shows the Church of Saint George in "
            "Chaidari, Greece.")
        out, info = d.describe_entry(greek_entry(), "key")
        self.assertEqual(info["status"], "described")
        self.assertEqual(out["title"], "Church of Saint George")
        self.assertIn("Chaidari", out["description"])

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
                 timeout=0, avoid=(), title=False):
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


class TitleTests(unittest.TestCase):
    """English titles for foreign catalogue names (ticket #2300)."""

    def setUp(self):
        self._old = d.describe_text

    def tearDown(self):
        d.describe_text = self._old

    def test_needs_english_title(self):
        # A record that already names its English title is left alone.
        self.assertFalse(d.needs_english_title({"titleEn": "Church"}))
        # A Greek or Dutch catalogue name is translated.
        self.assertTrue(d.needs_english_title(
            {}, "Ναός Αγίου Γεωργίου"))
        self.assertTrue(d.needs_english_title(
            {}, "Zicht op de gevels van gebouwen"))
        # An English name and an empty name are not.
        self.assertFalse(d.needs_english_title({}, "Busy market street"))
        self.assertFalse(d.needs_english_title({}, ""))

    def test_a_foreign_description_forces_the_title(self):
        # The Dutch title carries no marker the cheap test knows, so on its
        # own it reads as English; a description being written from the same
        # record is the signal that the title is rendered too (ticket #2326).
        dutch = "Zicht op vestingwerk met twee rondelen"
        self.assertFalse(d.needs_english_title({}, dutch))
        self.assertTrue(d.needs_english_title(
            {}, dutch, translating=True))
        # titleEn still wins: the record already names its English title.
        self.assertFalse(d.needs_english_title(
            {"titleEn": "View of the fortification"}, dutch,
            translating=True))

    def test_title_numbers_outside_the_record_are_guarded(self):
        self.assertEqual(d.unsupported_title_facts(
            "Church of Saint George, 1912", "Ναός Αγίου Γεωργίου 1900"),
            ["1912"])
        self.assertEqual(d.unsupported_title_facts(
            "Church of Saint George", "Ναός Αγίου Γεωργίου"), [])
        # A translated name is not an invented fact, so proper nouns pass.
        self.assertEqual(d.unsupported_title_facts(
            "Church of Saint George", "Ναός Αγίου Γεωργίου"), [])

    def test_split_answer_parses_the_title_line(self):
        title, desc = d.split_answer(
            "TITLE: Church of Saint George\n\nA small church in Haidari.")
        self.assertEqual(title, "Church of Saint George")
        self.assertEqual(desc, "A small church in Haidari.")
        # An answer without the marker is all description.
        self.assertEqual(d.split_answer("A market in 1905."),
                         ("", "A market in 1905."))

    def test_describe_entry_writes_the_english_title(self):
        d.describe_text = lambda *a, **kw: call(
            "TITLE: Church of Saint George\n\n"
            "The church Ναός Αγίου Γεωργίου in Χαϊδάρι, founded in the "
            "post-Byzantine years.")
        out, info = d.describe_entry(greek_entry(), "key")
        self.assertEqual(info["status"], "described")
        self.assertEqual(out["title"], "Church of Saint George")
        self.assertEqual(out["title_source"], "catalog")
        self.assertEqual(out["source"]["titleEn"], "Church of Saint George")
        self.assertIn("Χαϊδάρι", out["description"])

    def test_a_title_only_scene_is_retitled(self):
        # The English description is already there; only the Greek title is
        # translated, and that still counts as a change.
        d.describe_text = lambda *a, **kw: call(
            "TITLE: Church of Saint George\n\n"
            "A small church in Haidari.")
        out, info = d.describe_entry(
            greek_entry("A small church in Haidari, founded long ago."), "key")
        self.assertEqual(info["status"], "retitled")
        self.assertEqual(out["title"], "Church of Saint George")

    def test_an_english_title_is_not_sent_to_the_model(self):
        d.describe_text = lambda *a, **kw: self.fail("must not call")
        out, info = d.describe_entry(entry(
            "A market street with stalls and shoppers in 1905."),
            "key")
        self.assertEqual(info["status"], "kept")


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
