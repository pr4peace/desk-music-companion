"""The self-updater, against real git repositories in a temporary folder. No network: the
"GitHub" here is a bare repository on disk."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import updater

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


@unittest.skipUnless(shutil.which("git"), "needs git")
class SelfUpdate(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, GIT_ENV)
        self.env.start()
        self.folder = Path(tempfile.mkdtemp())
        self.hub = self.folder / "hub.git"     # stands in for GitHub
        self.work = self.folder / "work"       # where someone pushes new versions from
        self.mac = self.folder / "mac"         # the Mac's checkout the server runs from
        self.git(self.folder, "init", "--quiet", "--bare", "--initial-branch=main", str(self.hub))
        self.git(self.folder, "clone", "--quiet", str(self.hub), str(self.work))
        self.git(self.work, "checkout", "--quiet", "-B", "main")
        self.commit("server.py", "print('one')\n")
        self.git(self.folder, "clone", "--quiet", str(self.hub), str(self.mac))
        self.restarts = []
        self.playing = False
        self.passes = True
        self.updater = self.make()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.folder, ignore_errors=True)

    def make(self):
        return updater.Updater(
            self.mac, self.mac / "updates.json", self.mac / "installed-version",
            restart=lambda: self.restarts.append(True), busy=lambda: self.playing,
            verify=lambda commit: self.passes, log=lambda *a, **k: None,
        )

    def git(self, where, *args):
        return subprocess.run(["git", *args], cwd=where, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, name, text):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git(self.work, "add", name)
        self.git(self.work, "commit", "--quiet", "-m", f"change {name}")
        self.git(self.work, "push", "--quiet", "origin", "main")

    def mac_file(self, name):
        return (self.mac / name).read_text()

    def test_nothing_new_is_up_to_date(self):
        self.assertEqual(self.updater.check(), "current")
        self.assertEqual(self.restarts, [])

    def test_new_server_code_is_pulled_and_restarts(self):
        self.commit("server.py", "print('two')\n")
        self.assertEqual(self.updater.check(), "updated")
        self.assertEqual(self.mac_file("server.py"), "print('two')\n")
        self.assertEqual(self.restarts, [True])
        self.assertTrue(self.updater.status()["updated"])

    def test_page_only_change_needs_no_restart(self):
        self.commit("index.html", "<p>new</p>\n")
        self.assertEqual(self.updater.check(), "updated")
        self.assertEqual(self.mac_file("index.html"), "<p>new</p>\n")
        self.assertEqual(self.restarts, [])

    def test_local_changes_are_left_alone(self):
        (self.mac / "server.py").write_text("print('mine')\n")
        self.commit("server.py", "print('two')\n")
        self.assertEqual(self.updater.check(), "skipped")
        self.assertEqual(self.mac_file("server.py"), "print('mine')\n")
        self.assertIn("Local changes", self.updater.status()["message"])

    def test_untracked_files_dont_block_an_update(self):
        (self.mac / "notes.txt").write_text("scratch\n")
        self.commit("server.py", "print('two')\n")
        self.assertEqual(self.updater.check(), "updated")

    def test_another_branch_is_left_alone(self):
        self.git(self.mac, "checkout", "--quiet", "-b", "trying-something")
        self.commit("server.py", "print('two')\n")
        self.assertEqual(self.updater.check(), "skipped")
        self.assertEqual(self.mac_file("server.py"), "print('one')\n")

    def test_local_commits_are_left_alone(self):
        (self.mac / "extra.py").write_text("x = 1\n")
        self.git(self.mac, "add", "extra.py")
        self.git(self.mac, "commit", "--quiet", "-m", "local")
        self.commit("server.py", "print('two')\n")
        self.assertEqual(self.updater.check(), "skipped")
        self.assertEqual(self.mac_file("server.py"), "print('one')\n")

    def test_failing_tests_keep_the_old_version_until_main_moves(self):
        self.passes = False
        self.commit("server.py", "print('broken')\n")
        self.assertEqual(self.updater.check(), "failed")
        self.assertEqual(self.mac_file("server.py"), "print('one')\n")
        self.passes = True   # the same broken commit isn't tried again
        self.assertEqual(self.updater.check(), "failed")
        self.commit("server.py", "print('fixed')\n")
        self.assertEqual(self.updater.check(), "updated")
        self.assertEqual(self.mac_file("server.py"), "print('fixed')\n")

    def test_waits_for_a_pause_unless_asked(self):
        self.playing = True
        self.commit("server.py", "print('two')\n")
        self.assertEqual(self.updater.check(), "waiting")
        self.assertEqual(self.mac_file("server.py"), "print('one')\n")
        self.assertEqual(self.updater.check(manual=True), "updated")

    def test_stops_waiting_after_an_hour_of_music(self):
        self.playing = True
        self.commit("server.py", "print('two')\n")
        with mock.patch.object(updater.time, "time", return_value=1000.0):
            self.assertEqual(self.updater.check(), "waiting")
        with mock.patch.object(updater.time, "time", return_value=1000.0 + updater.WAIT_FOR_PAUSE + 1):
            self.assertEqual(self.updater.check(), "updated")

    def test_mac_helper_changes_ask_for_install(self):
        self.updater.record_install()
        self.assertFalse(self.updater.status()["needs_install"])
        self.commit("mac/DeskLyricsMenu.swift", "// new\n")
        self.updater.check()
        self.assertTrue(self.updater.status()["needs_install"])
        self.updater.record_install()   # what install.sh does
        self.assertFalse(self.updater.status()["needs_install"])

    def test_switch_is_remembered(self):
        self.updater.set_auto(False)
        self.assertFalse(self.make().status()["auto"])
        self.updater.set_auto(True)
        self.assertTrue(self.make().status()["auto"])

    def test_real_test_run_uses_a_scratch_copy(self):
        (self.work / "tests").mkdir()
        self.commit("tests/test_ok.py", "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n")
        self.commit("tests/__init__.py", "")
        target = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.mac, "fetch", "--quiet", "origin", "main")
        self.assertTrue(self.updater.run_tests(target))
        self.commit("tests/test_bad.py", "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self): self.fail()\n")
        self.git(self.mac, "fetch", "--quiet", "origin", "main")
        self.assertFalse(self.updater.run_tests(self.git(self.work, "rev-parse", "HEAD")))
        self.assertEqual(self.git(self.mac, "worktree", "list").count("\n"), 0)   # scratch copies removed
        self.assertFalse((self.mac / "tests").exists())                           # checkout untouched


class Paths(unittest.TestCase):
    def test_which_changes_restart_or_need_install(self):
        self.assertFalse(updater.needs_restart(["index.html", "README.md", "docs/a.png"]))
        self.assertTrue(updater.needs_restart(["index.html", "server.py"]))
        self.assertTrue(updater.needs_install(["mac/DeskLyricsMenu.swift"]))
        self.assertFalse(updater.needs_install(["server.py"]))


if __name__ == "__main__":
    unittest.main()
