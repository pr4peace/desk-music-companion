"""Sharing tapped timing with LRCLIB. Nothing here reaches LRCLIB: every request is stood in for."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server

KEY = "0123456789abcdef"
PLAIN = "O kadhale\n\nEn uyire\nNee varuvaai\n"
LINES = [{"t": 12.3, "text": "O kadhale"}, {"t": 16.0, "text": "En uyire"}, {"t": 61.05, "text": "Nee varuvaai"}]
SOURCE = {"trackName": "O Kadhale", "artistName": "Singer", "albumName": "Film", "duration": 241.6}
EASY = "0FFF" + "F" * 60   # about one try in sixteen


def answers(prefix, target, nonce):
    """LRCLIB's own check, byte by byte, as its server does it."""
    digest = hashlib.sha256(f"{prefix}{nonce}".encode()).digest()
    for got, limit in zip(digest, bytes.fromhex(target)):
        if got != limit:
            return got < limit
    return True


class RunAtOnce:
    """Stands in for threading.Thread so a share finishes before the test looks."""
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class FakeLrclib:
    def __init__(self, publish_status=201):
        self.publish_status = publish_status
        self.calls = []

    def __call__(self, path, body=None, headers=None):
        self.calls.append((path, body, headers))
        if path == "request-challenge":
            return 200, {"prefix": "abcDEF123", "target": EASY}
        if path == "publish":
            prefix, nonce = headers["X-Publish-Token"].split(":")
            if not answers(prefix, EASY, nonce):
                return 400, {"message": "IncorrectPublishTokenError"}
            return self.publish_status, None if self.publish_status == 201 else {"message": "nope"}
        raise AssertionError(path)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        guard = mock.patch("urllib.request.urlopen", side_effect=AssertionError("tests must not reach the network"))
        guard.start()
        self.addCleanup(guard.stop)


class LrcText(unittest.TestCase):
    def test_stamps(self):
        self.assertEqual(server.lrc_stamp(0), "[00:00.00]")
        self.assertEqual(server.lrc_stamp(61.05), "[01:01.05]")
        self.assertEqual(server.lrc_stamp(-3), "[00:00.00]")
        self.assertEqual(server.lrc_stamp(3599.999), "[60:00.00]")

    def test_synced_lyrics_follow_the_nudge(self):
        self.assertEqual(server.synced_lrc(LINES), "[00:12.30] O kadhale\n[00:16.00] En uyire\n[01:01.05] Nee varuvaai")
        # a nudge of +0.5 shows every line half a second earlier
        self.assertTrue(server.synced_lrc(LINES, 0.5).startswith("[00:11.80] O kadhale\n[00:15.50]"))
        self.assertEqual(server.parse_lrc(server.synced_lrc(LINES))[2], {"t": 61.05, "text": "Nee varuvaai"})


class ProofOfWork(unittest.TestCase):
    def test_answer_passes_lrclibs_check(self):
        for prefix in ("abc", "XyZ0987654321", "q" * 32):
            nonce = server.solve_challenge(prefix, EASY)
            self.assertTrue(answers(prefix, EASY, nonce))

    def test_harder_target_still_solved(self):
        target = "000FFF" + "0" * 58
        nonce = server.solve_challenge("prefix", target)
        self.assertTrue(answers("prefix", target, nonce))

    def test_impossible_target_gives_up(self):
        with mock.patch.object(server, "MOST_TRIES", 50):
            with self.assertRaises(ValueError):
                server.solve_challenge("prefix", "0" * 64)


class Publishing(NoNetwork):
    PAYLOAD = {**SOURCE, "plainLyrics": "a", "syncedLyrics": "[00:01.00] a"}

    def test_challenge_then_publish_with_its_answer(self):
        fake = FakeLrclib()
        with mock.patch.object(server, "lrclib_post", fake):
            self.assertIsNone(server.publish_to_lrclib(self.PAYLOAD))
        self.assertEqual([c[0] for c in fake.calls], ["request-challenge", "publish"])
        self.assertEqual(fake.calls[1][1], self.PAYLOAD)
        self.assertTrue(fake.calls[1][2]["X-Publish-Token"].startswith("abcDEF123:"))

    def test_refusal_is_reported(self):
        with mock.patch.object(server, "lrclib_post", FakeLrclib(publish_status=400)):
            self.assertIn("400", server.publish_to_lrclib(self.PAYLOAD))

    def test_broken_challenge_is_reported(self):
        with mock.patch.object(server, "lrclib_post", return_value=(500, None)):
            self.assertIn("challenge", server.publish_to_lrclib(self.PAYLOAD))


class Sharing(NoNetwork):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.store = server.ShareStore(self.folder / "tapped" / "shared.json")
        self.fake = FakeLrclib()
        for patch in (
            mock.patch.object(server, "lrclib_post", self.fake),
            mock.patch.object(server, "CACHE", self.folder / "cache"),
            mock.patch.object(server.threading, "Thread", RunAtOnce),
            mock.patch.object(server.timing, "get", return_value=0),
            mock.patch.object(server, "current_player", return_value={"state": "stopped"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(server.lyrics_store.entries.pop, KEY, None)

    def tapped(self, source=SOURCE):
        server.lyrics_store._set(KEY, {"status": "synced", "lines": LINES, "tapped": True, "text": PLAIN, "source": source})

    def test_share_publishes_once_and_remembers(self):
        self.tapped()
        self.assertEqual(self.store.status(KEY), {"status": "ready"})
        self.assertTrue(self.store.start(KEY))
        self.assertEqual(self.store.status(KEY), {"status": "shared"})
        published = self.fake.calls[1][1]
        self.assertEqual(published["trackName"], "O Kadhale")
        self.assertEqual(published["albumName"], "Film")
        self.assertEqual(published["duration"], 242)
        self.assertEqual(published["plainLyrics"], "O kadhale\nEn uyire\nNee varuvaai")
        self.assertEqual(published["syncedLyrics"], server.synced_lrc(LINES))
        # it lasts past a restart of the Mac server
        again = server.ShareStore(self.folder / "tapped" / "shared.json")
        self.assertEqual(again.status(KEY), {"status": "shared"})

    def test_nudge_or_retiming_after_sharing_offers_it_again(self):
        self.tapped()
        self.store.start(KEY)
        with mock.patch.object(server.timing, "get", return_value=0.5):
            self.assertEqual(self.store.status(KEY), {"status": "ready"})
            self.store.start(KEY)
            self.assertTrue(self.fake.calls[-1][1]["syncedLyrics"].startswith("[00:11.80]"))
            self.assertEqual(self.store.status(KEY), {"status": "shared"})

    def test_failure_is_shown_and_can_be_retried(self):
        self.tapped()
        self.fake.publish_status = 500
        self.store.start(KEY)
        status = self.store.status(KEY)
        self.assertEqual(status["status"], "failed")
        self.assertIn("500", status["error"])
        self.fake.publish_status = 201
        self.store.start(KEY)
        self.assertEqual(self.store.status(KEY), {"status": "shared"})

    def test_network_trouble_is_a_failure_not_a_crash(self):
        self.tapped()
        with mock.patch.object(server, "lrclib_post", side_effect=OSError("offline")):
            self.store.start(KEY)
        self.assertEqual(self.store.status(KEY)["status"], "failed")

    def test_only_tapped_timing_can_be_shared(self):
        server.lyrics_store._set(KEY, {"status": "synced", "lines": LINES, "source": SOURCE})   # from a lyrics site
        self.assertEqual(self.store.status(KEY), {"status": "unavailable"})
        self.assertFalse(self.store.start(KEY))
        server.lyrics_store._set(KEY, {"status": "plain", "text": PLAIN, "source": SOURCE})
        self.assertFalse(self.store.start(KEY))
        self.assertFalse(self.store.start("ffffffffffffffff"))   # a song the Mac doesn't have
        self.assertEqual(self.fake.calls, [])

    def test_songs_saved_before_records_were_kept_need_the_song_playing(self):
        self.tapped(source=None)
        self.assertEqual(self.store.status(KEY), {"status": "unavailable"})
        self.assertFalse(self.store.start(KEY))

    def player(self, name="O Kadhale", artist="Singer", loose=False):
        return {"state": "playing", "name": name, "artist": artist, "album": "Film", "duration": 241.6, "loose": loose}

    def key_for(self, player):
        return hashlib.sha1(f"{player['name']}\0{player['artist']}".encode()).hexdigest()[:16]

    def test_older_song_finds_its_lrclib_record_again(self):
        player = self.player()
        record = {"trackName": "O Kadhale (From Film)", "artistName": "Singer", "albumName": "Film OST",
                  "duration": 241, "plainLyrics": PLAIN}
        key = self.key_for(player)
        self.addCleanup(server.lyrics_store.entries.pop, key, None)
        server.lyrics_store._set(key, {"status": "synced", "lines": LINES, "tapped": True, "text": PLAIN})
        with mock.patch.object(server, "current_player", return_value=player), \
             mock.patch.object(server, "lookup_lrclib", return_value=record):
            self.assertEqual(self.store.status(key), {"status": "ready"})
            self.store.start(key)
            self.assertEqual(self.store.status(key), {"status": "shared"})
        self.assertEqual(self.fake.calls[1][1]["trackName"], "O Kadhale (From Film)")
        saved = json.loads((self.folder / "cache" / f"{key}.json").read_text("utf-8"))
        self.assertEqual(saved["source"]["albumName"], "Film OST")

    def test_another_cut_playing_now_isnt_shared_as_the_record(self):
        player = {**self.player(), "duration": 300}
        key = self.key_for(player)
        self.addCleanup(server.lyrics_store.entries.pop, key, None)
        server.lyrics_store._set(key, {"status": "synced", "lines": LINES, "tapped": True, "text": PLAIN, "source": SOURCE})
        with mock.patch.object(server, "current_player", return_value=player):
            self.assertTrue(self.store.start(key))
            self.assertEqual(self.store.status(key)["status"], "failed")
        self.assertEqual(self.fake.calls, [])
        with mock.patch.object(server, "current_player", return_value=self.player()):   # the record's own cut
            self.store.start(key)
            self.assertEqual(self.store.status(key), {"status": "shared"})

    def test_older_video_with_no_record_isnt_shared_under_its_video_title(self):
        player = self.player(name="O Kadhale | Official Video 4K", artist="Label", loose=True)
        key = self.key_for(player)
        self.addCleanup(server.lyrics_store.entries.pop, key, None)
        server.lyrics_store._set(key, {"status": "synced", "lines": LINES, "tapped": True, "text": PLAIN})
        with mock.patch.object(server, "current_player", return_value=player), \
             mock.patch.object(server, "lookup_lrclib", return_value=None):
            self.store.start(key)
            self.assertEqual(self.store.status(key)["status"], "failed")
        self.assertEqual(self.fake.calls, [])


class LyricsKeepTheirRecord(NoNetwork):
    def test_plain_lyrics_remember_which_lrclib_record_they_came_from(self):
        record = {**SOURCE, "id": 7, "plainLyrics": PLAIN, "syncedLyrics": None}
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(server, "CACHE", Path(folder)), \
             mock.patch.object(server, "lookup_lyrics", return_value=record), \
             mock.patch.object(server.tapped, "get", return_value=None):
            server.lyrics_store._fetch(KEY, {})
            self.assertEqual(server.lyrics_store.get(KEY)["source"], SOURCE)
            # and keep it once tapped
            with mock.patch.object(server.tapped, "set"), mock.patch.object(server.timing, "forget"):
                self.assertTrue(server.save_tapped(KEY, LINES))
            self.assertEqual(server.lyrics_store.get(KEY)["source"], SOURCE)
        server.lyrics_store.entries.pop(KEY, None)


if __name__ == "__main__":
    unittest.main()
