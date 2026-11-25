"""Finding the right lyrics: cleaning titles, matching LRCLIB records, reading LRC timing."""
import unittest
from unittest import mock

import server


def track(name, artist="", duration=0, loose=False, album=""):
    return {"name": name, "artist": artist, "album": album, "duration": duration, "loose": loose}


def record(name, artist, duration, synced=None, plain=None, **extra):
    return {"trackName": name, "artistName": artist, "duration": duration,
            "syncedLyrics": synced, "plainLyrics": plain, **extra}


class TitleCleaning(unittest.TestCase):
    def test_norm_ignores_case_spacing_and_punctuation(self):
        self.assertEqual(server.norm("Don't Stop  Me-Now!"), "dontstopmenow")
        self.assertEqual(server.norm(None), "")

    def test_title_variants_drop_remaster_and_feature_notes(self):
        self.assertEqual(server.title_variants("Yesterday (Remastered 2009)"), ["Yesterday (Remastered 2009)", "Yesterday"])
        self.assertEqual(server.title_variants("Hey Jude - Remastered 2015"), ["Hey Jude - Remastered 2015", "Hey Jude"])
        self.assertEqual(server.title_variants("Levitating (feat. DaBaby)"), ["Levitating (feat. DaBaby)", "Levitating"])
        self.assertEqual(server.title_variants("Plain Song"), ["Plain Song"])

    def test_artist_names_split_on_joiners(self):
        self.assertEqual(
            server.artist_names("A.R. Rahman, Shreya Ghoshal & Sid Sriram feat. Anirudh"),
            ["A.R. Rahman", "Shreya Ghoshal", "Sid Sriram", "Anirudh"],
        )

    def test_clean_video_title(self):
        self.assertEqual(server.clean_video_title("Kanmani Anbodu | Guna | Kamal Haasan (Official Video) 4K"), "Kanmani Anbodu")
        self.assertEqual(server.clean_video_title("Arijit Singh - Tum Hi Ho Official Lyrical Video"), "Arijit Singh - Tum Hi Ho")
        self.assertEqual(server.clean_video_title("Song Name (Tamil) Lyrics"), "Song Name")

    def test_clean_channel(self):
        self.assertEqual(server.clean_channel("Sony Music India VEVO"), "Sony Music India")
        self.assertEqual(server.clean_channel("Sid Sriram - Topic"), "Sid Sriram")


class SearchTerms(unittest.TestCase):
    def test_music_app_uses_title_variants_and_artists(self):
        titles, artists = server.search_terms(track("Hey Jude - Remastered 2015", "The Beatles"))
        self.assertEqual(titles, ["Hey Jude - Remastered 2015", "Hey Jude"])
        self.assertEqual(artists, ["The Beatles"])

    def test_video_tries_both_sides_of_a_dash(self):
        titles, artists = server.search_terms(track("Arijit Singh - Tum Hi Ho (Official Video)", "T-Series", loose=True))
        self.assertEqual(titles, ["Tum Hi Ho", "Arijit Singh"])
        self.assertEqual(artists, ["Arijit Singh", "Tum Hi Ho", "T-Series"])

    def test_video_credits_and_handles_become_artists(self):
        titles, artists = server.search_terms(track("Kesariya | Brahmastra | @ArijitSingh", "Sony Music India VEVO", loose=True))
        self.assertEqual(titles, ["Kesariya"])
        self.assertEqual(artists[0], "@ArijitSingh")   # an @handle is tried first
        self.assertIn("Sony Music India", artists)


class Matching(unittest.TestCase):
    def check(self, item, playing):
        titles, names = server.search_terms(playing)
        return server.matches(item, playing, titles, names)

    def test_same_song_same_length_matches(self):
        self.assertTrue(self.check(record("Hey Jude", "The Beatles", 431), track("Hey Jude - Remastered 2015", "The Beatles", 431)))

    def test_wrong_artist_is_rejected(self):
        self.assertFalse(self.check(record("Hey Jude", "Someone Else", 431), track("Hey Jude", "The Beatles", 431)))

    def test_wrong_title_is_rejected(self):
        self.assertFalse(self.check(record("Let It Be", "The Beatles", 243), track("Hey Jude", "The Beatles", 243)))

    def test_music_app_allows_five_seconds_of_difference(self):
        playing = track("Hey Jude", "The Beatles", 431)
        self.assertTrue(self.check(record("Hey Jude", "The Beatles", 426), playing))
        self.assertFalse(self.check(record("Hey Jude", "The Beatles", 420), playing))

    def test_video_allows_twelve_seconds(self):
        playing = track("The Beatles - Hey Jude", "The Beatles", 431, loose=True)
        self.assertTrue(self.check(record("Hey Jude", "The Beatles", 420), playing))

    def test_longer_music_video_needs_exact_title_and_singer(self):
        song = record("Tum Hi Ho", "Arijit Singh", 262)
        self.assertTrue(self.check(song, track("Arijit Singh - Tum Hi Ho", "T-Series", 262 + 90, loose=True)))
        self.assertFalse(self.check(song, track("Arijit Singh - Tum Hi Ho", "T-Series", 262 + 200, loose=True)))
        self.assertFalse(self.check(record("Tum Hi Ho", "Someone Else", 262), track("Arijit Singh - Tum Hi Ho", "T-Series", 262 + 90, loose=True)))

    def test_label_channel_passes_on_exact_title_and_length(self):
        playing = track("Kanmani Anbodu (Official Video)", "Some Label", 300, loose=True)
        self.assertTrue(self.check(record("Kanmani Anbodu", "Ilaiyaraaja", 298), playing))
        self.assertFalse(self.check(record("Kanmani Anbodu", "Ilaiyaraaja", 298), {**playing, "duration": 0}))

    def test_record_without_title_never_matches(self):
        self.assertFalse(self.check(record("", "The Beatles", 431), track("Hey Jude", "The Beatles", 431)))


class LrcTiming(unittest.TestCase):
    def test_parse_lrc_reads_every_stamp_and_sorts(self):
        lines = server.parse_lrc("[ar:Someone]\n[00:12.50]Hello\n[01:02:30][00:05.00]Again\nno stamp here")
        self.assertEqual(lines, [
            {"t": 5.0, "text": "Again"},
            {"t": 12.5, "text": "Hello"},
            {"t": 62.3, "text": "Again"},
        ])

    def test_timing_fits_rejects_a_longer_cut(self):
        item = record("Song", "Artist", 300, synced="[00:01.00]a\n[04:50.00]b")
        self.assertTrue(server.timing_fits(item, track("Song", duration=300)))
        self.assertFalse(server.timing_fits(item, track("Song", duration=240)))
        self.assertTrue(server.timing_fits(item, track("Song", duration=0)))

    def test_rank_prefers_synced_then_closest_length(self):
        playing = track("Song", duration=200)
        plain_exact = record("Song", "A", 200, plain="words")
        synced_off = record("Song", "A", 204, synced="[00:01.00]x")
        synced_close = record("Song", "A", 201, synced="[00:01.00]x")
        best = min([plain_exact, synced_off, synced_close], key=lambda i: server.rank(i, playing))
        self.assertIs(best, synced_close)


class SameLyrics(unittest.TestCase):
    ENGLISH = "\n".join([
        "[00:01.00]Walking down the river by the morning light",
        "[00:05.00]Every little thing we said was gold tonight",
        "[00:09.00]Never gonna let the summer slip away",
    ])
    OTHER = "\n".join([
        "[00:01.00]Quiero bailar contigo bajo la luna llena",
        "[00:05.00]Corazon perdido busca su camino nuevo",
        "[00:09.00]Mañana cantaremos juntos otra vez",
    ])

    def test_same_words_match(self):
        a = {"syncedLyrics": self.ENGLISH}
        b = {"plainLyrics": self.ENGLISH.replace("[00:01.00]", "")}
        self.assertTrue(server.same_lyrics(a, b))

    def test_different_language_does_not(self):
        self.assertFalse(server.same_lyrics({"syncedLyrics": self.ENGLISH}, {"syncedLyrics": self.OTHER}))

    def test_too_few_words_to_tell_counts_as_same(self):
        self.assertTrue(server.same_lyrics({"plainLyrics": "la la"}, {"syncedLyrics": self.OTHER}))

    def test_spelling_differences_still_match(self):
        a = {"plainLyrics": "Kannaana kanne kannaana kanne en meedhu saaya vaa\nNee thoongum varai naan paadum paattu thaan"}
        b = {"plainLyrics": "Kanaana kane kanaana kane en meethu saya va\nNee thungum varai nan padum pattu than"}
        self.assertTrue(server.same_lyrics(a, b))


class LookupLrclib(unittest.TestCase):
    """The whole lookup, with LRCLIB answered from here instead of the network."""

    def lookup(self, playing, get=None, searches=()):
        def fake(path, params):
            return get if path == "get" else list(searches)
        with mock.patch.object(server, "lrclib", side_effect=fake) as called:
            return server.lookup_lrclib(playing), called

    def test_direct_hit_with_synced_lyrics_stops_searching(self):
        hit = record("Hey Jude", "The Beatles", 431, synced="[00:01.00]Hey Jude")
        found, called = self.lookup(track("Hey Jude", "The Beatles", 431, album="Past Masters"), get=hit)
        self.assertIs(found, hit)
        self.assertEqual(called.call_count, 1)

    def test_prefers_synced_over_plain(self):
        plain = record("Hey Jude", "The Beatles", 431, plain="Hey Jude")
        synced = record("Hey Jude", "The Beatles", 430, synced="[00:01.00]Hey Jude")
        found, _ = self.lookup(track("Hey Jude", "The Beatles", 431), searches=[plain, synced])
        self.assertIs(found, synced)

    def test_no_lyrics_beats_wrong_lyrics(self):
        wrong = record("Hey Jude", "A Cover Band", 431, synced="[00:01.00]Hey Jude")
        found, _ = self.lookup(track("Hey Jude", "The Beatles", 431), searches=[wrong])
        self.assertIsNone(found)

    def test_instrumental_is_accepted(self):
        quiet = record("Interlude", "The Band", 60, instrumental=True)
        found, _ = self.lookup(track("Interlude", "The Band", 60), searches=[quiet])
        self.assertIs(found, quiet)

    def test_other_language_version_is_dropped_when_the_album_record_says_which(self):
        # The album's own record has only plain words, so the search goes on and finds a
        # synced record with the same title, sung in another language.
        reference = record("Song", "Singer", 200, plain=SameLyrics.ENGLISH)
        dubbed = record("Song", "Singer", 200, synced=SameLyrics.OTHER)
        found, called = self.lookup(track("Song", "Singer", 200, album="Album"), get=reference, searches=[dubbed])
        self.assertGreater(called.call_count, 1)
        self.assertIs(found, reference)


class SafariTabs(unittest.TestCase):
    def test_video_id(self):
        self.assertEqual(server.video_id("https://www.youtube.com/watch?v=abc123&t=4"), "abc123")
        self.assertEqual(server.video_id("https://youtube.com/shorts/xyz9"), "xyz9")

    def test_tab_title_drops_counts_marks_and_site_name(self):
        self.assertEqual(server.tab_title("‎(3) Song Name - YouTube Music"), "Song Name")
        self.assertEqual(server.tab_title(None), "")

    def test_choose_tab_prefers_a_playing_video_then_the_front_tab(self):
        paused_front = {"front": True, "video": {"paused": True}}
        playing_back = {"front": False, "video": {"paused": False}}
        ad_front = {"front": True, "video": {"paused": False, "ad": True}}
        self.assertIs(server.choose_tab([paused_front, playing_back, ad_front]), playing_back)
        self.assertIs(server.choose_tab([{"front": False}, paused_front]), paused_front)
        self.assertIsNone(server.choose_tab([]))


if __name__ == "__main__":
    unittest.main()
