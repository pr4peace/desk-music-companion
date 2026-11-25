"""Keeps the Mac's copy of Desk Music Companion on the latest main from GitHub.

Every so often it fetches origin/main. When there's something new, it tries the new version's
tests in a scratch copy first, then fast-forwards this checkout and restarts the server. It
leaves the checkout alone when it's on another branch, has uncommitted changes, or holds
commits GitHub doesn't, so working on the code here is never disturbed.
Only the standard library, like the server.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

CHECK_EVERY = 15 * 60     # seconds between checks
FIRST_CHECK = 60          # after the server starts, so a restart loop can't hammer GitHub
WAIT_RECHECK = 60         # while an update waits for the music to stop
WAIT_FOR_PAUSE = 60 * 60  # longest an update waits for the music to stop
GIT_TIMEOUT = 90
TEST_TIMEOUT = 300

# Changes here only take effect after ./install.sh runs again, since it builds or copies them.
INSTALL_PATHS = ("install.sh", "mac/", "visualizer/")
# Changes here don't need a restart: the phone reloads index.html by itself.
NO_RESTART_PATHS = ("index.html", "README.md", "ROADMAP.md", "docs/", ".impeccable/", "tests/")


def needs_install(paths):
    return any(path.startswith(INSTALL_PATHS) for path in paths)


def needs_restart(paths):
    return any(not path.startswith(NO_RESTART_PATHS) for path in paths)


class Updater:
    def __init__(self, root, settings_path, installed_path, restart, busy=lambda: False,
                 verify=None, log=print):
        self.root = root
        self.settings_path = settings_path
        self.installed_path = installed_path   # the commit install.sh last ran on
        self.restart = restart
        self.busy = busy
        self.verify = verify or self.run_tests
        self.log = log
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.manual = False
        self.rejected = None       # a commit whose tests failed, skipped until main moves on
        self.waiting_since = None  # when an update started waiting for the music to stop
        self.settings = {"auto": True}
        try:
            saved = json.loads(settings_path.read_text("utf-8"))
            if isinstance(saved, dict):
                self.settings.update(saved)
        except (OSError, ValueError):
            pass
        self.state = {"state": "idle", "message": "Not checked yet", "checked": None}

    # --- what the menu bar shows -------------------------------------------------------

    def status(self):
        with self.lock:
            result = {**self.state, "auto": bool(self.settings.get("auto"))}
            last = self.settings.get("last")
        if isinstance(last, dict):
            result["updated"] = last.get("at")
            result["updated_to"] = last.get("version")
        result["version"] = self.short(self.git("rev-parse", "HEAD")[1])
        result["needs_install"] = self.install_pending()
        return result

    def set_auto(self, on):
        with self.lock:
            self.settings["auto"] = bool(on)
        self.save()
        if on:
            self.wake.set()

    def check_now(self):
        self.manual = True
        self.wake.set()

    # --- the loop ----------------------------------------------------------------------

    def start(self):
        if not (self.root / ".git").exists():
            self.set_state("off", "Not a git checkout, so it can't update itself")
            return
        if not self.installed_path.exists():   # installed before this was tracked: count it as now
            self.record_install()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.wake.wait(FIRST_CHECK)
        while True:
            self.wake.clear()
            manual, self.manual = self.manual, False
            if manual or self.settings.get("auto"):
                try:
                    self.check(manual=manual)
                except Exception as err:   # never let the updater take the server down
                    self.set_state("failed", f"Update check failed: {err}")
            self.wake.wait(WAIT_RECHECK if self.state["state"] == "waiting" else CHECK_EVERY)

    def check(self, manual=False):
        """One check. Returns the state it ends in."""
        self.set_state("checking", "Checking for updates")
        ok, branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        if not ok:
            return self.set_state("failed", "Couldn't read the git checkout")
        if branch != "main":
            return self.set_state("skipped", f"On branch {branch}, so not updating")
        ok, changes = self.git("status", "--porcelain", "--untracked-files=no")
        if not ok or changes:
            return self.set_state("skipped", "Local changes, so not updating")

        ok, _ = self.git("fetch", "--quiet", "origin", "main")
        if not ok:
            return self.set_state("failed", "Couldn't reach GitHub")
        _, head = self.git("rev-parse", "HEAD")
        _, target = self.git("rev-parse", "FETCH_HEAD")
        if head == target:
            self.waiting_since = None
            return self.set_state("current", "Up to date")
        if not self.git("merge-base", "--is-ancestor", head, target)[0]:
            return self.set_state("skipped", "This Mac has commits GitHub doesn't, so not updating")
        if target == self.rejected:
            return self.set_state("failed", f"Version {self.short(target)} failed its tests, waiting for a fix")

        if not manual and self.busy():
            self.waiting_since = self.waiting_since or time.time()
            if time.time() - self.waiting_since < WAIT_FOR_PAUSE:
                return self.set_state("waiting", f"Version {self.short(target)} is ready, waiting for a pause")

        self.set_state("checking", f"Testing version {self.short(target)}")
        if not self.verify(target):
            self.rejected = target
            self.log(f"Update {self.short(target)} failed its tests, staying on {self.short(head)}", flush=True)
            return self.set_state("failed", f"Version {self.short(target)} failed its tests, waiting for a fix")

        _, names = self.git("diff", "--name-only", head, target)
        paths = names.splitlines()
        ok, _ = self.git("merge", "--ff-only", "--quiet", target)
        if not ok:
            return self.set_state("failed", "Couldn't move to the new version")
        self.waiting_since = None
        with self.lock:
            self.settings["last"] = {"version": self.short(target), "at": time.time()}
        self.save()
        self.log(f"Updated from {self.short(head)} to {self.short(target)}", flush=True)
        self.set_state("updated", f"Updated to {self.short(target)}")
        if needs_restart(paths):
            self.restart()
        return "updated"

    # --- helpers -----------------------------------------------------------------------

    def run_tests(self, commit):
        """Runs the new version's tests in a scratch worktree, so this checkout isn't touched."""
        scratch = tempfile.mkdtemp(prefix="desk-music-update-")
        tree = os.path.join(scratch, "tree")
        try:
            if not self.git("worktree", "add", "--detach", "--quiet", tree, commit)[0]:
                return False
            try:
                done = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=tree,
                                      capture_output=True, timeout=TEST_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return done.returncode == 0
        finally:
            self.git("worktree", "remove", "--force", tree)
            shutil.rmtree(scratch, ignore_errors=True)
            self.git("worktree", "prune")

    def install_pending(self):
        try:
            installed = self.installed_path.read_text("utf-8").strip()
        except OSError:
            return False
        ok, names = self.git("diff", "--name-only", installed, "HEAD")
        return ok and needs_install(names.splitlines())

    def record_install(self):
        ok, head = self.git("rev-parse", "HEAD")
        if ok:
            try:
                self.installed_path.write_text(head + "\n", "utf-8")
            except OSError:
                pass

    def git(self, *args):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            done = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return False, ""
        return done.returncode == 0, done.stdout.strip()

    def set_state(self, state, message):
        with self.lock:
            self.state = {"state": state, "message": message, "checked": time.time()}
        return state

    def save(self):
        with self.lock:
            data = json.dumps(self.settings, indent=1)
        temp = self.settings_path.with_suffix(".tmp")
        try:
            temp.write_text(data, "utf-8")
            os.replace(temp, self.settings_path)
        except OSError:
            pass

    @staticmethod
    def short(commit):
        return (commit or "")[:7]
