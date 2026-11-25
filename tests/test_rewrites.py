"""Writing built on a stand-in translation: the Natural translation, the context notes and the
Story card are marked weak while Google rests, and written again once Google's translation
replaces the stand-in. The weak version keeps showing until then."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server

LINES = ["onnu", "rendu"]


def run_now(target, args, daemon):
    thread = mock.Mock()
    thread.start.side_effect = lambda: target(*args)
    return thread


class Rewrites(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.literal = {"status": "ready", "lines": {"onnu": "local one"}, "source": "local"}
        self.written_from = []
        self.stories_from = []
        self.translations = mock.Mock()
        self.translations.ensure.side_effect = lambda key, lines: self.literal
        self.naturals = server.WrittenStore("natural", self.write_natural)
        self.stories = server.StoryStore()
        for patcher in (
            mock.patch.object(server, "CACHE", self.folder),
            mock.patch.object(server, "translations", self.translations),
            mock.patch.object(server, "naturals", self.naturals),
            mock.patch.object(server, "choose_writer", return_value="model"),
            mock.patch.object(server, "song_story", side_effect=self.write_story),
            mock.patch.object(server, "ask_google", side_effect=OSError("no network in tests")),
            mock.patch.object(server.threading, "Thread", side_effect=run_now),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_natural(self, model, lines, literal, track, progress):
        self.written_from.append(dict(literal))
        return {line: f"natural from {literal.get(line, 'nothing')}" for line in lines}

    def write_story(self, model, lines, english, track):
        self.stories_from.append(dict(english))
        return {"about": f"A song about {english.get('onnu', 'nothing')}, written for the tests."}

    @staticmethod
    def ask(store):
        """What the phone sees after polling a few times, as each step finishes in turn."""
        for _ in range(4):
            entry = store.ensure("song", LINES, {})
        return entry

    def google_arrives(self):
        self.literal = {"status": "ready", "lines": {"onnu": "One"}, "source": "google"}

    # --- Natural ---

    def test_natural_from_a_stand_in_is_marked_weak(self):
        entry = self.ask(self.naturals)
        self.assertTrue(entry["weak"])
        self.assertEqual(entry["lines"]["onnu"], "natural from local one")
        self.assertTrue(json.loads((self.folder / "song.natural.json").read_text("utf-8"))["weak"])

    def test_natural_from_google_is_not_weak(self):
        self.google_arrives()
        entry = self.ask(self.naturals)
        self.assertNotIn("weak", entry)
        self.ask(self.naturals)
        self.assertEqual(len(self.written_from), 1)

    def test_weak_natural_stays_while_google_still_rests(self):
        self.ask(self.naturals)
        entry = self.ask(self.naturals)
        self.assertEqual(entry["lines"]["onnu"], "natural from local one")
        self.assertEqual(len(self.written_from), 1)

    def test_natural_is_rewritten_once_google_answers(self):
        self.ask(self.naturals)
        self.google_arrives()
        self.ask(self.naturals)
        entry = self.ask(self.naturals)
        self.assertEqual(entry["lines"]["onnu"], "natural from One")
        self.assertNotIn("weak", entry)
        self.assertEqual(self.written_from[-1], {"onnu": "One"})
        saved = json.loads((self.folder / "song.natural.json").read_text("utf-8"))
        self.assertNotIn("weak", saved)
        self.assertEqual(saved["lines"]["onnu"], "natural from One")

    def test_the_weak_natural_shows_while_it_is_rewritten(self):
        self.ask(self.naturals)
        self.google_arrives()
        shown = []
        with mock.patch.object(server.threading, "Thread", side_effect=lambda target, args, daemon: mock.Mock()):
            shown.append(self.naturals.ensure("song", LINES, {}))
            shown.append(self.naturals.ensure("song", LINES, {}))   # a second poll doesn't start a second rewrite
        self.assertTrue(all(entry["lines"]["onnu"] == "natural from local one" for entry in shown))
        self.assertEqual(len(self.written_from), 1)

    def test_a_failed_rewrite_keeps_the_weak_natural_and_tries_later(self):
        self.ask(self.naturals)
        self.google_arrives()
        with mock.patch.object(self.naturals, "writer", side_effect=RuntimeError("ollama stopped")):
            entry = self.ask(self.naturals)
        entry = self.ask(self.naturals)
        self.assertEqual(entry["lines"]["onnu"], "natural from local one")
        self.assertGreater(entry["retry_at"], time.time())
        self.assertNotIn("rewriting", entry)
        entry["retry_at"] = 0
        self.assertEqual(self.ask(self.naturals)["lines"]["onnu"], "natural from One")

    def test_natural_written_with_no_translation_at_all_is_weak(self):
        self.literal = {"status": "failed"}
        self.assertTrue(self.ask(self.naturals)["weak"])
        self.google_arrives()
        self.ask(self.naturals)
        self.assertEqual(self.written_from, [{}, {"onnu": "One"}])

    def test_a_song_google_finds_english_drops_the_weak_natural(self):
        self.ask(self.naturals)
        self.literal = {"status": "none", "lines": {}, "source": "google"}
        self.assertEqual(self.ask(self.naturals), {"status": "none"})
        self.assertFalse((self.folder / "song.natural.json").exists())

    def test_a_weak_natural_saved_before_a_restart_is_rewritten(self):
        self.ask(self.naturals)
        self.google_arrives()
        fresh = server.WrittenStore("natural", self.write_natural)
        self.ask(fresh)
        self.assertEqual(self.ask(fresh)["lines"]["onnu"], "natural from One")

    def test_natural_saved_before_this_change_is_rewritten_if_google_came_later(self):
        natural = self.folder / "song.natural.json"
        literal = self.folder / "song.en.json"
        natural.write_text(json.dumps({"status": "ready", "lines": {"onnu": "old"}, "version": server.WRITING_VERSION}), "utf-8")
        literal.write_text(json.dumps({"status": "ready", "lines": {"onnu": "One"}, "source": "google"}), "utf-8")
        os.utime(natural, (1000, 1000))
        self.google_arrives()
        self.ask(self.naturals)
        self.assertEqual(self.ask(self.naturals)["lines"]["onnu"], "natural from One")

    def test_natural_saved_before_this_change_after_google_is_kept(self):
        natural = self.folder / "song.natural.json"
        literal = self.folder / "song.en.json"
        literal.write_text(json.dumps({"status": "ready", "lines": {"onnu": "One"}, "source": "google"}), "utf-8")
        os.utime(literal, (1000, 1000))
        natural.write_text(json.dumps({"status": "ready", "lines": {"onnu": "old"}, "version": server.WRITING_VERSION}), "utf-8")
        self.google_arrives()
        self.assertEqual(self.ask(self.naturals)["lines"]["onnu"], "old")
        self.assertEqual(self.written_from, [])

    def test_context_notes_are_rewritten_too(self):
        contexts = server.WrittenStore("context", self.write_natural)
        self.assertTrue(self.ask(contexts)["weak"])
        self.google_arrives()
        self.ask(contexts)
        self.assertNotIn("weak", self.ask(contexts))

    # --- Story ---

    def test_story_from_a_stand_in_is_weak(self):
        entry = self.ask(self.stories)
        self.assertEqual(entry["status"], "ready")
        self.assertTrue(entry["weak"])
        self.assertEqual(self.stories_from, [{"onnu": "natural from local one", "rendu": "natural from nothing"}])

    def test_story_waits_for_the_rewritten_natural_then_is_rewritten(self):
        self.ask(self.stories)
        self.google_arrives()
        with mock.patch.object(server.threading, "Thread", side_effect=lambda target, args, daemon: mock.Mock()):
            entry = self.stories.ensure("song", LINES, {})   # the Natural rewrite has only been asked for
        self.assertIn("local one", entry["about"])
        self.assertEqual(len(self.stories_from), 1)
        self.naturals.entries["song"].pop("rewriting")
        self.ask(self.stories)   # the Natural is rewritten now; the story follows
        entry = self.ask(self.stories)
        self.assertIn("One", entry["about"])
        self.assertNotIn("weak", entry)
        self.assertEqual(self.stories_from[-1]["onnu"], "natural from One")
        saved = json.loads((self.folder / "song.story.json").read_text("utf-8"))
        self.assertNotIn("weak", saved)

    def test_story_from_google_is_not_rewritten(self):
        self.google_arrives()
        self.ask(self.stories)
        self.assertNotIn("weak", self.ask(self.stories))
        self.assertEqual(len(self.stories_from), 1)

    def test_a_failed_story_rewrite_keeps_the_weak_story(self):
        self.ask(self.stories)
        self.google_arrives()
        self.ask(self.naturals)   # the Natural is rewritten first
        with mock.patch.object(server, "song_story", return_value={}):
            self.ask(self.stories)
        entry = self.ask(self.stories)
        self.assertIn("local one", entry["about"])
        self.assertGreater(entry["retry_at"], time.time())


if __name__ == "__main__":
    unittest.main()
