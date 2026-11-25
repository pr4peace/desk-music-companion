"""Transliteration: the spelling rules on top of ICU's Any-Latin transform.

ICU itself is stood in for with the output it gives on a Mac, so these run anywhere.
RealTransform checks the real thing on a Mac, or wherever PyICU is installed.
"""
import sys
import unittest
from unittest import mock

import server

# What ICU's Any-Latin transform gives for each word, as on macOS.
ICU = {
    "மகளே": "makaḷē",
    "தங்கம்": "taṅkam",
    "அன்பு": "aṉpu",
    "காற்று": "kāṟṟu",
    "चल": "cala",
    "मैं": "maiṁ",
    "शाम": "śāma",
    "कहाँ": "kahām̐",
}


class Romanize(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(server.system, "to_latin", side_effect=lambda word: ICU.get(word, word))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_latin_needs_nothing(self):
        self.assertIsNone(server.romanize("Hello, world"))
        self.assertFalse(server.not_latin("Café déjà vu"))
        self.assertTrue(server.not_latin("चल"))

    def test_tamil_is_spelled_as_sung(self):
        self.assertEqual(server.romanize("மகளே"), "magale")    # medial k sounds as g, long ē as e
        self.assertEqual(server.romanize("தங்கம்"), "thangam")  # first t is th, ṅk is ng
        self.assertEqual(server.romanize("அன்பு"), "anbu")      # medial p after ṉ sounds as b
        self.assertEqual(server.romanize("காற்று"), "kaatru")   # ṟṟ is tr, ā is aa

    def test_hindi_drops_the_final_a_and_spells_sounds(self):
        self.assertEqual(server.romanize("चल"), "chal")
        self.assertEqual(server.romanize("मैं"), "main")
        self.assertEqual(server.romanize("शाम"), "sham")
        self.assertEqual(server.romanize("कहाँ"), "kahan")

    def test_english_words_inside_a_line_stay(self):
        self.assertEqual(server.romanize("மகளே baby"), "magale baby")

    def test_japanese_with_kanji_is_left_alone(self):
        self.assertIsNone(server.romanize("君の名は"))


@unittest.skipUnless(
    sys.platform == "darwin" or getattr(server.system, "ANY_LATIN", None),
    "needs macOS or PyICU for the real ICU transform",
)
class RealTransform(unittest.TestCase):
    def test_tamil_and_hindi(self):
        self.assertEqual(server.romanize("மகளே"), "magale")
        self.assertEqual(server.romanize("चल"), "chal")


if __name__ == "__main__":
    unittest.main()
