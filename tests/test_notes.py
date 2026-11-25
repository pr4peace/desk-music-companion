"""The "What it means" notes and the story's key line: longer, read with the lines around them, and
written again when they were saved by the older, shorter prompt."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server

LINES = ["Vanjam seigiraai", "Kanavil vandhaai"]
LONG_NOTE = ("Vanjam is deceit, but sung right after a line about love it lands as a lover's tease, not an accusation. "
             "The next line answers it: you trick me, yet you still come to my dreams.")


class Notes(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.prompts = []
        for patcher in (mock.patch.object(server, "CACHE", self.folder),
                        mock.patch.object(server, "ollama_chat", side_effect=self.reply)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def reply(self, model, system, prompt, max_tokens, **kwargs):
        self.prompts.append(prompt)
        if '"key"' in prompt:
            return json.dumps({"about": "A lover is accused of deceit, half in jest, and forgiven by the end.", "key": 1, "meaning": LONG_NOTE})
        return json.dumps({"lines": [{"n": 1, "note": LONG_NOTE}]})

    def test_a_note_of_a_few_sentences_is_kept(self):
        notes = server.context_notes("model", LINES, {}, {"key": "song"})
        self.assertEqual(notes, {LINES[0]: LONG_NOTE})

    def test_the_prompt_asks_for_the_lines_around_and_wordplay(self):
        server.context_notes("model", LINES, {}, {"key": "song"})
        self.assertIn("lines just before and after", self.prompts[-1])
        self.assertIn("play on words", self.prompts[-1])

    def test_the_story_keeps_a_longer_key_line_meaning(self):
        story = server.song_story("model", LINES, {}, {"key": "song"})
        self.assertEqual(story["key"]["meaning"], LONG_NOTE)

    def test_notes_saved_by_the_short_prompt_are_written_again(self):
        (self.folder / "song.context.json").write_text(json.dumps({"status": "ready", "lines": {LINES[0]: "short"}}), "utf-8")
        store = server.WrittenStore("context", server.context_notes, server.NOTES_VERSION)
        literal = {"status": "ready", "lines": {}}
        run_now = lambda target, args, daemon: mock.Mock(start=lambda: target(*args))
        with mock.patch.object(server.translations, "ensure", return_value=literal), \
             mock.patch.object(server, "choose_writer", return_value="model"), \
             mock.patch.object(server.threading, "Thread", side_effect=run_now):
            store.ensure("song", LINES, {"key": "song"})
            entry = store.ensure("song", LINES, {"key": "song"})
        self.assertEqual(entry["lines"], {LINES[0]: LONG_NOTE})
        self.assertEqual(json.loads((self.folder / "song.context.json").read_text("utf-8"))["version"], server.NOTES_VERSION)

    def test_natural_translations_are_not_written_again(self):
        self.assertEqual(server.naturals.version, server.WRITING_VERSION)


if __name__ == "__main__":
    unittest.main()
