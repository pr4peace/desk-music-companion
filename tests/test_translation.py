"""Translation: which lines are sent, how replies are read, and which translator is tried when."""
import unittest
import urllib.error
from unittest import mock

import server


class LyricTexts(unittest.TestCase):
    def test_synced_lines_are_distinct_and_in_order(self):
        entry = {"lines": [{"t": 1, "text": "One"}, {"t": 2, "text": " "}, {"t": 3, "text": "Two"}, {"t": 4, "text": "One"}]}
        self.assertEqual(server.lyric_texts(entry), ["One", "Two"])

    def test_plain_text_is_split_into_lines(self):
        self.assertEqual(server.lyric_texts({"text": "One\n\n Two \nOne"}), ["One", "Two"])
        self.assertEqual(server.lyric_texts({}), [])


class TranslateLines(unittest.TestCase):
    def test_maps_each_line_and_skips_unchanged_ones(self):
        reply = (["Hello", "Name", ""], "ta")
        with mock.patch.object(server, "translate_rows", return_value=reply):
            self.assertEqual(server.translate_lines(["Vanakkam", "Name", "Ennamo"]), {"Vanakkam": "Hello"})

    def test_english_song_needs_no_translation(self):
        with mock.patch.object(server, "translate_rows", return_value=(["Hi"], "en")):
            self.assertEqual(server.translate_lines(["Hi"]), {})

    def test_long_songs_go_in_chunks(self):
        lines = [f"{n:03d} " + "x" * 400 for n in range(10)]
        sent = []

        def fake(chunk):
            sent.append(chunk)
            return [line.upper() for line in chunk], "ta"
        with mock.patch.object(server, "translate_rows", side_effect=fake):
            result = server.translate_lines(lines)
        self.assertGreater(len(sent), 1)
        self.assertEqual([line for chunk in sent for line in chunk], lines)
        self.assertTrue(all(sum(len(line) + 1 for line in chunk) <= server.TRANSLATE_CHUNK + 1 for chunk in sent))
        self.assertEqual(len(result), 10)

    def test_romanized_indian_lyrics_get_a_second_pass(self):
        with mock.patch.object(server, "translate_rows", return_value=(["tum hi ho"], "hi")), \
             mock.patch.object(server, "improve_romanized", return_value=["You are the one"]) as improve:
            self.assertEqual(server.translate_lines(["tum hi ho"]), {"tum hi ho": "You are the one"})
        improve.assert_called_once()

    def test_translate_rows_goes_line_by_line_when_google_merges_them(self):
        calls = []

        def fake(rows):
            calls.append(rows)
            return (["merged"], "ta") if len(rows) > 1 else ([rows[0].upper()], "ta")
        with mock.patch.object(server, "google_translate", side_effect=fake):
            english, language = server.translate_rows(["a", "b"])
        self.assertEqual((english, language), (["A", "B"], "ta"))
        self.assertEqual(calls, [["a", "b"], ["a"], ["b"]])


class ImproveRomanized(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(server, "ENGLISH_WORDS", {"love", "you", "the", "night", "is", "young"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_as_english(self):
        self.assertTrue(server.reads_as_english("Love you, the night is young"))
        self.assertFalse(server.reads_as_english("tum hi ho ab tum hi ho"))

    def test_respells_only_non_english_lines_and_keeps_order(self):
        chunk = ["love you", "tum hi ho", "jeena bhi kya"]
        with mock.patch.object(server, "spell_natively", side_effect=lambda line, lang: f"<{line}>") as spell, \
             mock.patch.object(server, "translate_rows", return_value=(["You are it", "What is life"], "hi")):
            merged = server.improve_romanized(chunk, ["love you", "tum hi ho", "jeena bhi kya"], "hi")
        self.assertEqual(merged, ["love you", "You are it", "What is life"])
        self.assertEqual([c.args[0] for c in spell.call_args_list], ["tum hi ho", "jeena bhi kya"])

    def test_network_trouble_keeps_the_first_translation(self):
        with mock.patch.object(server, "spell_natively", side_effect=OSError("offline")):
            self.assertEqual(server.improve_romanized(["tum hi ho"], ["You only"], "hi"), ["You only"])


class ModelReplies(unittest.TestCase):
    def test_numbered_lines_reads_json_inside_chatter(self):
        reply = ('Sure! {"lines": [{"n": 1, "en": "Hi"}, {"n": 9, "en": "too far"}, {"n": "2", "en": " there "}]}'
                 ' and then {"n": 3, "en": "late"} {broken')
        self.assertEqual(server.numbered_lines(reply, 3), {1: "Hi", 2: "there", 3: "late"})

    def test_numbered_lines_other_field_and_bad_items(self):
        reply = '{"lines": [{"n": 1, "note": "means home"}, {"n": "x", "note": "?"}, "text", {"n": 2}]}'
        self.assertEqual(server.numbered_lines(reply, 2, field="note"), {1: "means home"})
        self.assertEqual(server.numbered_lines("no json at all", 2), {})


class OnMacModels(unittest.TestCase):
    def test_main_script(self):
        self.assertEqual(server.main_script("வணக்கம் hi"), "TAMIL")
        self.assertEqual(server.main_script("नमस्ते"), "DEVANAGARI")
        self.assertIsNone(server.main_script("123 !"))

    def test_mostly_latin_songs_are_left_to_google(self):
        with mock.patch.object(server.subprocess, "run") as run:
            self.assertIsNone(server.translate_with_nllb(["hello there", "how are you", "வணக்கம்"], script="x"))
        run.assert_not_called()

    def test_no_model_installed(self):
        with mock.patch.object(server, "mac_translator", return_value=None):
            self.assertIsNone(server.translate_with_nllb(["வணக்கம்", "நன்றி"]))

    def test_lines_are_grouped_by_language_for_the_helper(self):
        done = mock.Mock(returncode=0, stdout='{"lines": {"வணக்கம்": "Hello", "नमस्ते": "नमस्ते"}}')
        with mock.patch.object(server.subprocess, "run", return_value=done) as run:
            result = server.translate_with_nllb(["வணக்கம்", "நன்றி", "नमस्ते"], script="helper.py")
        self.assertEqual(result, {"வணக்கம்": "Hello"})   # a line sent back unchanged isn't a translation
        sent = run.call_args.kwargs["input"]
        self.assertIn('"lang": "tam_Taml", "lines": ["வணக்கம்", "நன்றி"]', sent)
        self.assertIn('"lang": "hin_Deva", "lines": ["नमस्ते"]', sent)

    def test_helper_failure_raises(self):
        done = mock.Mock(returncode=1, stderr="Traceback\nOutOfMemory")
        with mock.patch.object(server.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(RuntimeError, "OutOfMemory"):
                server.translate_with_nllb(["வணக்கம்"], script="helper.py")

    def test_mac_translator_choice(self):
        def installed(python=True, indic=False, nllb=False):
            def exists(path):
                name = str(path)
                if name.endswith("python"):
                    return python
                if "indictrans2" in name:
                    return indic
                if "nllb" in name:
                    return nllb
                return False
            return mock.patch.object(server.Path, "exists", exists)

        with installed(python=False, indic=True, nllb=True):
            self.assertIsNone(server.mac_translator("tam_Taml"))
        with installed(indic=True, nllb=True):
            self.assertEqual(server.mac_translator("tam_Taml"), server.INDIC_SCRIPT)
            self.assertEqual(server.mac_translator("hin_Deva"), server.NLLB_SCRIPT)
        with installed(nllb=True):
            self.assertEqual(server.mac_translator("tam_Taml"), server.NLLB_SCRIPT)
        with installed(indic=True):
            self.assertEqual(server.mac_translator("hin_Deva"), server.INDIC_SCRIPT)
        with installed():
            self.assertIsNone(server.mac_translator("tam_Taml"))


class EnglishFor(unittest.TestCase):
    """The order translators are tried in: on the Mac, then Google, then the local model."""

    def setUp(self):
        for patcher in (mock.patch.object(server, "google_resting_until", 0.0), mock.patch("builtins.print")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_on_mac_model_answers_first(self):
        with mock.patch.object(server, "translate_with_nllb", return_value={"a": "A"}), \
             mock.patch.object(server, "translate_lines") as google:
            self.assertEqual(server.english_for(["a"]), {"a": "A"})
        google.assert_not_called()

    def test_google_when_no_model_fits(self):
        with mock.patch.object(server, "translate_with_nllb", return_value=None), \
             mock.patch.object(server, "translate_lines", return_value={"a": "B"}):
            self.assertEqual(server.english_for(["a"]), {"a": "B"})

    def test_model_crash_falls_through_to_google(self):
        with mock.patch.object(server, "translate_with_nllb", side_effect=RuntimeError("boom")), \
             mock.patch.object(server, "translate_lines", return_value={"a": "B"}):
            self.assertEqual(server.english_for(["a"]), {"a": "B"})

    def test_google_rate_limit_rests_google_and_uses_local_model(self):
        refused = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        with mock.patch.object(server, "translate_with_nllb", return_value=None), \
             mock.patch.object(server, "translate_lines", side_effect=refused), \
             mock.patch.object(server, "translate_locally", return_value={"a": "C"}):
            self.assertEqual(server.english_for(["a"]), {"a": "C"})
        self.assertGreater(server.google_resting_until, server.time.time())

        with mock.patch.object(server, "translate_with_nllb", return_value=None), \
             mock.patch.object(server, "translate_lines") as google, \
             mock.patch.object(server, "translate_locally", return_value={"a": "C"}):
            server.english_for(["a"])
        google.assert_not_called()   # still resting

    def test_none_when_everything_fails(self):
        with mock.patch.object(server, "translate_with_nllb", return_value=None), \
             mock.patch.object(server, "translate_lines", side_effect=OSError("offline")), \
             mock.patch.object(server, "translate_locally", side_effect=RuntimeError("no local model installed")):
            self.assertIsNone(server.english_for(["a"]))


if __name__ == "__main__":
    unittest.main()
