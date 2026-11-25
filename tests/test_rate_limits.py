"""Google's rate limits: requests are spaced out and never repeated, Google is left alone after it
refuses, and a stand-in translation from the local model is replaced once Google answers again."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import server


def refusal(code=429, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("url", code, "refused", headers, None)


def reply(data):
    response = mock.MagicMock()
    response.__enter__.return_value = io.BytesIO(json.dumps(data).encode())
    return response


class Isolated(unittest.TestCase):
    """A fresh gate, an empty line cache and cache folder, and no network, for every test."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.gate = server.GoogleGate()
        self.slept = []
        for patcher in (
            mock.patch.object(server, "google_gate", self.gate),
            mock.patch.object(server, "google_lines", server.LineCache(self.folder / "lines.json")),
            mock.patch.object(server, "CACHE", self.folder),
            mock.patch.object(server.urllib.request, "urlopen", side_effect=OSError("no network in tests")),
            mock.patch.object(server.time, "sleep", side_effect=self.slept.append),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class Gate(Isolated):
    def test_refusal_rests_google_and_later_requests_are_not_sent(self):
        self.gate.refused()
        self.assertTrue(self.gate.resting())
        with self.assertRaises(server.GoogleResting):
            self.gate.wait_turn()

    def test_rest_doubles_after_each_refusal_in_a_row_up_to_a_cap(self):
        rests = []
        for _ in range(8):
            with mock.patch.object(server.time, "time", return_value=1000.0):
                self.gate.refused()
            rests.append(self.gate.resting_until - 1000.0)
        self.assertEqual(rests[:3], [600, 1200, 2400])
        self.assertEqual(max(rests), server.GoogleGate.MAX_REST)

    def test_an_answer_resets_the_rest(self):
        self.gate.refused()
        self.gate.refused()
        self.gate.answered()
        with mock.patch.object(server.time, "time", return_value=1000.0):
            self.gate.refused()
        self.assertEqual(self.gate.resting_until, 1000.0 + server.GoogleGate.REST)

    def test_a_longer_retry_after_is_honoured(self):
        with mock.patch.object(server.time, "time", return_value=1000.0):
            self.gate.refused(5000)
        self.assertEqual(self.gate.resting_until, 6000.0)

    def test_requests_are_spaced_out(self):
        self.gate.wait_turn()
        self.gate.wait_turn()
        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 0.5)


class AskGoogle(Isolated):
    def test_too_many_requests_is_not_retried_and_starts_a_rest(self):
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=refusal(429, 30)) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                server.ask_google("request", 5)
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(self.gate.resting())

    def test_a_server_hiccup_gets_one_more_try(self):
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=[refusal(503), reply([1])]) as urlopen:
            self.assertEqual(server.ask_google("request", 5), [1])
        self.assertEqual(urlopen.call_count, 2)
        self.assertFalse(self.gate.resting())

    def test_requests_never_overlap(self):
        held = []

        def urlopen(request, timeout):
            held.append(self.gate.busy.locked())
            return reply([1])
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=urlopen):
            server.ask_google("request", 5)
        self.assertEqual(held, [True])
        self.assertFalse(self.gate.busy.locked())

    def test_spelling_goes_through_the_gate_too(self):
        self.gate.refused()
        with self.assertRaises(server.GoogleResting):
            server.spell_natively("tum hi ho", "hi")


class FewerRequests(Isolated):
    def test_merged_lines_are_split_in_halves_not_one_by_one(self):
        calls = []

        def fake(rows):
            calls.append(rows)
            return (["merged"], "ta") if len(rows) > 2 else ([row.upper() for row in rows], "ta")
        with mock.patch.object(server, "google_translate", side_effect=fake):
            english, _ = server.translate_rows(["a", "b", "c", "d"])
        self.assertEqual(english, ["A", "B", "C", "D"])
        self.assertEqual(calls, [["a", "b", "c", "d"], ["a", "b"], ["c", "d"]])

    def test_a_song_heard_before_sends_nothing(self):
        with mock.patch.object(server, "translate_rows", return_value=(["One", "Two"], "ta")) as google:
            first = server.translate_lines(["onnu", "rendu"])
            second = server.translate_lines(["onnu", "rendu"])
        self.assertEqual(first, second)
        self.assertEqual(google.call_count, 1)

    def test_only_new_lines_are_sent(self):
        server.google_lines.put({"onnu": ["One", "ta"]})
        with mock.patch.object(server, "translate_rows", return_value=(["Two"], "ta")) as google:
            self.assertEqual(server.translate_lines(["onnu", "rendu"]), {"onnu": "One", "rendu": "Two"})
        google.assert_called_once_with(["rendu"])

    def test_a_cached_english_song_stays_untranslated(self):
        server.google_lines.put({"hello": ["hello", "en"], "world": ["world", "en"]})
        with mock.patch.object(server, "translate_rows") as google:
            self.assertEqual(server.translate_lines(["hello", "world"]), {})
        google.assert_not_called()

    def test_a_cached_english_line_does_not_stop_the_rest_of_a_song(self):
        server.google_lines.put({"I love you": ["I love you", "en"], "onnu": ["One", "ta"]})
        with mock.patch.object(server, "translate_rows", return_value=(["Two"], "ta")) as google:
            self.assertEqual(server.translate_lines(["I love you", "onnu", "rendu"]), {"onnu": "One", "rendu": "Two"})
        google.assert_called_once_with(["rendu"])

    def test_a_song_cut_off_by_a_refusal_resumes_where_it_stopped(self):
        lines = [f"{n:03d} " + "x" * 900 for n in range(3)]
        with mock.patch.object(server, "translate_rows", side_effect=[([lines[0].upper()], "ta"), server.GoogleResting("rest")]):
            with self.assertRaises(server.GoogleResting):
                server.translate_lines(lines)
        with mock.patch.object(server, "translate_rows", side_effect=lambda rows: ([row.upper() for row in rows], "ta")) as google:
            result = server.translate_lines(lines)
        self.assertEqual(len(result), 3)
        self.assertNotIn([lines[0]], [call.args[0] for call in google.call_args_list])

    def test_the_cache_survives_a_restart_and_drops_the_oldest(self):
        server.google_lines.put({"a": ["A", "ta"]})
        again = server.LineCache(self.folder / "lines.json")
        self.assertEqual(again.get("a"), ["A", "ta"])
        with mock.patch.object(server.LineCache, "LIMIT", 2):
            again.put({"b": ["B", "ta"], "c": ["C", "ta"]})
        self.assertIsNone(again.get("a"))
        self.assertEqual(again.get("c"), ["C", "ta"])


class Upgrade(Isolated):
    """A stand-in from the local model shows at once, and Google's translation replaces it later."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(server.threading, "Thread", side_effect=self.run_now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = server.TranslationStore()

    @staticmethod
    def run_now(target, args, daemon):
        thread = mock.Mock()
        thread.start.side_effect = lambda: target(*args)
        return thread

    def fall_back(self):
        self.gate.refused()
        with mock.patch.object(server, "translate_with_nllb", return_value=None), \
             mock.patch.object(server, "translate_locally", return_value={"onnu": "local one"}):
            self.store.ensure("song", ["onnu"])

    def test_the_stand_in_is_shown_and_marked(self):
        self.fall_back()
        entry = self.store.ensure("song", ["onnu"])
        self.assertEqual((entry["status"], entry["source"], entry["lines"]), ("ready", "local", {"onnu": "local one"}))
        self.assertGreaterEqual(entry["better_at"], self.gate.resting_until)

    def test_google_replaces_it_once_its_rest_is_over(self):
        self.fall_back()
        self.gate.resting_until = 0
        self.store.entries["song"]["retry_at"] = 0
        with mock.patch.object(server, "translate_lines", return_value={"onnu": "One"}):
            self.store.ensure("song", ["onnu"])
        entry = self.store.ensure("song", ["onnu"])
        self.assertEqual((entry["source"], entry["lines"]), ("google", {"onnu": "One"}))
        saved = json.loads((self.folder / "song.en.json").read_text("utf-8"))
        self.assertEqual(saved["lines"], {"onnu": "One"})

    def test_a_failed_upgrade_keeps_the_stand_in_and_waits_again(self):
        self.fall_back()
        self.store.entries["song"]["retry_at"] = 0
        with mock.patch.object(server, "translate_lines", side_effect=server.GoogleResting("rest")):
            entry = self.store.ensure("song", ["onnu"])
        self.assertEqual(entry["lines"], {"onnu": "local one"})
        self.assertEqual(entry["retry_at"], self.gate.resting_until)

    def test_a_stand_in_saved_before_a_restart_is_upgraded(self):
        self.fall_back()
        self.gate.resting_until = 0
        fresh = server.TranslationStore()
        saved = json.loads((self.folder / "song.en.json").read_text("utf-8"))
        saved["better_at"] = 0
        (self.folder / "song.en.json").write_text(json.dumps(saved), "utf-8")
        with mock.patch.object(server, "translate_lines", return_value={"onnu": "One"}) as google:
            fresh.ensure("song", ["onnu"])
        google.assert_called_once()
        self.assertEqual(fresh.entries["song"]["source"], "google")


if __name__ == "__main__":
    unittest.main()
