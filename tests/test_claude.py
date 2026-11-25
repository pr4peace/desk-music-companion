"""Claude as the writer of the Natural translation, the notes and the Story, when it has a key.
Every test here uses a fake key and a fake network: nothing is sent to Anthropic."""
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import server
from tests.test_rewrites import run_now

LINES = ["onnu", "rendu"]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(text, stop="end_turn"):
    return FakeResponse(json.dumps({
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
        "stop_reason": stop,
    }).encode("utf-8"))


def streamed(*pieces, stop="end_turn"):
    events = [{"type": "message_start", "message": {}}, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    events += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}} for piece in pieces]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop}}, {"type": "message_stop"}]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return FakeResponse(body.encode("utf-8"))


class WithKey(unittest.TestCase):
    """Claude switched on with a fake key; the network is whatever each test says."""

    def setUp(self):
        self.sent = []
        self.answers = []
        for patcher in (
            mock.patch.object(server, "CLAUDE_ON", True),
            mock.patch.object(server, "claude", server.ClaudeAccess()),
            mock.patch.dict(os.environ, {"DESK_MUSIC_CLAUDE_KEY": "sk-ant-test"}),
            mock.patch.object(server.urllib.request, "urlopen", side_effect=self.urlopen),
            mock.patch.object(server, "choose_writer", return_value="qwen3.5:9b"),
            mock.patch.object(server, "ollama_chat", return_value="from the Mac"),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def urlopen(self, request, timeout=None):
        if not str(getattr(request, "full_url", request)).startswith(server.CLAUDE_API):
            raise AssertionError(f"unexpected request to {request}")
        self.sent.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Choosing(WithKey):
    def test_claude_writes_when_it_has_a_key(self):
        self.assertEqual(server.choose_song_writer(), "claude:claude-haiku-5-5")

    def test_the_local_model_writes_without_a_key(self):
        with mock.patch.dict(os.environ, {"DESK_MUSIC_CLAUDE_KEY": ""}), mock.patch.object(server, "claude", server.ClaudeAccess()):
            self.assertEqual(server.choose_song_writer(), "qwen3.5:9b")

    def test_switched_off_means_local_even_with_a_key(self):
        with mock.patch.object(server, "CLAUDE_ON", False), mock.patch.object(server, "claude", server.ClaudeAccess()):
            self.assertEqual(server.choose_song_writer(), "qwen3.5:9b")

    def test_tidbits_and_the_stand_in_keep_to_the_local_model(self):
        self.assertEqual(server.choose_writer(), "qwen3.5:9b")


class Asking(WithKey):
    def test_a_plain_reply_is_its_text(self):
        self.answers.append(reply('{"lines": []}'))
        text = server.write_with("claude:claude-haiku-5-5", "system words", "the prompt", 3000, temperature=0.2)
        self.assertEqual(text, '{"lines": []}')
        self.assertEqual(server.written_by.name, "claude")
        request = self.sent[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertEqual(body["system"], "system words")
        self.assertEqual(body["messages"], [{"role": "user", "content": "the prompt"}])
        self.assertNotIn("temperature", body)   # current models refuse sampling settings
        self.assertEqual(request.get_header("X-api-key"), "sk-ant-test")
        self.assertEqual(request.get_header("Anthropic-version"), server.CLAUDE_API_VERSION)

    def test_a_streamed_reply_shows_progress(self):
        self.answers.append(streamed('{"lines": [{"n": 1', ', "en": "one"}]}'))
        seen = []
        text = server.write_with("claude:claude-haiku-5-5", "s", "p", 3000, on_text=seen.append)
        self.assertEqual(text, '{"lines": [{"n": 1, "en": "one"}]}')
        self.assertEqual(seen, ['{"lines": [{"n": 1', text])
        self.assertTrue(json.loads(self.sent[0].data)["stream"])

    def test_a_refusal_falls_back_to_the_mac(self):
        self.answers.append(reply("", stop="refusal"))
        self.assertEqual(server.write_with("claude:claude-haiku-5-5", "s", "p", 3000), "from the Mac")
        self.assertEqual(server.written_by.name, "local")
        self.assertFalse(server.claude.ready())   # rests a while before trying Claude again

    def test_a_refused_key_rests_claude_for_an_hour(self):
        self.answers.append(urllib.error.HTTPError(server.CLAUDE_API, 401, "unauthorized", {}, None))
        self.assertEqual(server.write_with("claude:claude-haiku-5-5", "s", "p", 3000), "from the Mac")
        self.assertGreater(server.claude.rest_until, time.time() + 3000)
        self.assertEqual(server.choose_song_writer(), "qwen3.5:9b")

    def test_no_network_rests_it_briefly(self):
        self.answers.append(urllib.error.URLError("offline"))
        server.write_with("claude:claude-haiku-5-5", "s", "p", 3000)
        self.assertLess(server.claude.rest_until, time.time() + 600)

    def test_without_a_local_model_the_failure_is_reported(self):
        self.answers.append(urllib.error.URLError("offline"))
        with mock.patch.object(server, "choose_writer", return_value=None), self.assertRaises(urllib.error.URLError):
            server.write_with("claude:claude-haiku-5-5", "s", "p", 3000)

    def test_a_local_writer_never_touches_claude(self):
        self.assertEqual(server.write_with("qwen3.5:9b", "s", "p", 3000), "from the Mac")
        self.assertEqual(self.sent, [])


class Upgrading(WithKey):
    """Writing saved from the local model is written again by Claude once it has a key."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.translations = mock.Mock()
        self.translations.ensure.return_value = {"status": "ready", "lines": {"onnu": "one"}, "source": "google"}
        for patcher in (
            mock.patch.object(server, "CACHE", self.folder),
            mock.patch.object(server, "translations", self.translations),
            mock.patch.object(server.threading, "Thread", side_effect=run_now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, model, lines, literal, track, progress):
        return {"onnu": server.write_with(model, "s", "p", 3000)}

    def test_new_writing_says_who_wrote_it(self):
        self.answers.append(reply("by Claude"))
        store = server.WrittenStore("natural", self.write)
        self.assertEqual(store.ensure("song", LINES, {})["status"], "loading")
        entry = store.ensure("song", LINES, {})
        self.assertEqual((entry["lines"], entry["writer"]), ({"onnu": "by Claude"}, "claude"))
        saved = json.loads((self.folder / "song.natural.json").read_text("utf-8"))
        self.assertEqual(saved["writer"], "claude")

    def test_local_writing_shows_until_claude_rewrites_it(self):
        local = {"status": "ready", "lines": {"onnu": "by the Mac"}, "version": server.WRITING_VERSION}
        (self.folder / "song.natural.json").write_text(json.dumps(local), "utf-8")
        self.answers.append(reply("by Claude"))
        store = server.WrittenStore("natural", self.write)
        first = store.ensure("song", LINES, {})
        self.assertEqual(first["lines"], {"onnu": "by the Mac"})   # shown while Claude writes
        entry = store.ensure("song", LINES, {})
        self.assertEqual((entry["lines"], entry["writer"]), ({"onnu": "by Claude"}, "claude"))
        self.assertNotIn("weak", entry)

    def test_without_a_key_local_writing_stays(self):
        local = {"status": "ready", "lines": {"onnu": "by the Mac"}, "version": server.WRITING_VERSION}
        (self.folder / "song.natural.json").write_text(json.dumps(local), "utf-8")
        with mock.patch.object(server, "CLAUDE_ON", False), mock.patch.object(server, "claude", server.ClaudeAccess()):
            entry = server.WrittenStore("natural", self.write).ensure("song", LINES, {})
        self.assertEqual(entry["lines"], {"onnu": "by the Mac"})
        self.assertNotIn("weak", entry)
        self.assertEqual(self.sent, [])

    def test_a_failed_rewrite_keeps_the_local_writing(self):
        local = {"status": "ready", "lines": {"onnu": "by the Mac"}, "version": server.WRITING_VERSION}
        (self.folder / "song.natural.json").write_text(json.dumps(local), "utf-8")
        self.answers.append(urllib.error.URLError("offline"))
        with mock.patch.object(server, "choose_writer", return_value=None):
            store = server.WrittenStore("natural", self.write)
            store.ensure("song", LINES, {})
            entry = store.ensure("song", LINES, {})
        self.assertEqual(entry["lines"], {"onnu": "by the Mac"})


class InTests(unittest.TestCase):
    def test_the_suite_keeps_claude_off(self):
        self.assertFalse(server.CLAUDE_ON)
        self.assertIsNone(server.claude.current_key())


if __name__ == "__main__":
    unittest.main()
