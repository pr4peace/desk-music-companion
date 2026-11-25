"""The Mac-only parts sit behind the same small functions on every system."""
import unittest
from unittest import mock

import macos
import other_systems
import server

FUNCTIONS = (
    "now_playing_command", "send_media_command", "safari_is_audible", "read_safari_tabs",
    "run_in_safari_tab", "audio_levels_command", "to_latin",
)
MESSAGES = ("NOW_PLAYING_MISSING", "LEVELS_MISSING")


class SameShape(unittest.TestCase):
    def test_every_system_module_offers_the_same_functions(self):
        for module in (macos, other_systems):
            for name in FUNCTIONS:
                self.assertTrue(callable(getattr(module, name, None)), f"{module.__name__}.{name}")
            for name in MESSAGES:
                self.assertIsInstance(getattr(module, name, None), str, f"{module.__name__}.{name}")

    def test_server_uses_one_of_them(self):
        self.assertIn(server.system, (macos, other_systems))


class Controls(unittest.TestCase):
    def test_music_app_controls_go_through_the_system(self):
        playing = {"state": "playing", "name": "Song"}
        with mock.patch.object(server, "current_player", return_value=playing), \
             mock.patch.object(server.system, "send_media_command", return_value=True) as send:
            self.assertTrue(server.control("next"))
        send.assert_called_once_with("next")

    def test_safari_tab_gets_the_page_script(self):
        tab = {"state": "playing", "source": "safari", "timed": True, "url": "https://www.youtube.com/watch?v=x"}
        with mock.patch.object(server, "current_player", return_value=tab), \
             mock.patch.object(server.system, "run_in_safari_tab", return_value=True) as run, \
             mock.patch.object(server.safari_youtube, "toggled") as toggled:
            self.assertTrue(server.control("toggle"))
        run.assert_called_once_with(tab["url"], server.SAFARI_ACTIONS["toggle"])
        toggled.assert_called_once()

    def test_nothing_playing_sends_nothing(self):
        with mock.patch.object(server, "current_player", return_value={"state": "stopped"}), \
             mock.patch.object(server.system, "send_media_command") as send:
            self.assertFalse(server.control("toggle"))
        send.assert_not_called()

    def test_untimed_safari_tab_cannot_be_reached(self):
        tab = {"state": "playing", "source": "safari", "timed": False, "url": "u"}
        with mock.patch.object(server, "current_player", return_value=tab), \
             mock.patch.object(server.system, "run_in_safari_tab") as run:
            self.assertFalse(server.control("toggle"))
        run.assert_not_called()


class MacCommands(unittest.TestCase):
    def test_media_control_names(self):
        self.assertEqual(macos.now_playing_command()[1:], ["stream", "--micros"])
        with mock.patch.object(macos.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(macos.send_media_command("previous"))
        self.assertEqual(run.call_args.args[0][1], "previous-track")

    def test_missing_tools_are_not_a_crash(self):
        with mock.patch.object(macos.subprocess, "run", side_effect=OSError("not found")):
            self.assertFalse(macos.send_media_command("toggle"))
            self.assertIsNone(macos.read_safari_tabs())
            self.assertFalse(macos.run_in_safari_tab("u", "s"))


class NowPlayingFeed(unittest.TestCase):
    """Reading `media-control stream` lines, which a Windows reader would also print."""

    def test_full_then_diff(self):
        feed = server.NowPlaying()
        feed._feed(b'{"type": "data", "payload": {"title": "Song", "artist": "Singer", "playing": true,'
                   b' "durationMicros": 200000000, "elapsedTimeMicros": 10000000,'
                   b' "bundleIdentifier": "com.apple.Music"}}')
        feed._feed(b'{"type": "data", "diff": true, "payload": {"playing": false, "artist": null}}')
        feed._feed(b'not json')
        state = feed.snapshot()
        self.assertEqual(state["state"], "paused")
        self.assertEqual(state["name"], "Song")
        self.assertEqual(state["artist"], "")
        self.assertEqual(state["duration"], 200)
        self.assertFalse(state["loose"])

    def test_unavailable_shows_the_systems_message(self):
        feed = server.NowPlaying()
        feed.error = server.system.NOW_PLAYING_MISSING
        self.assertEqual(feed.snapshot(), {"state": "unavailable", "detail": server.system.NOW_PLAYING_MISSING})


if __name__ == "__main__":
    unittest.main()
