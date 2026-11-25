"""The Mac-only parts of the server, behind a few small functions.

server.py asks this module what's playing, sends it play and pause, reads Safari's YouTube
tabs, starts the audio level helper and turns other scripts into Latin letters. A version
for another system provides the same functions with the same results (see other_systems.py
for the stand-ins used elsewhere, and the Windows table in ROADMAP.md).

Nothing here touches macOS until it is called, so the server can be imported anywhere.
"""
import ctypes
import functools
import json
import shutil
import subprocess

from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEDIA_CONTROL = shutil.which("media-control") or "/opt/homebrew/bin/media-control"
MEDIA_COMMANDS = {"toggle": "toggle-play-pause", "next": "next-track", "previous": "previous-track"}
UTF8 = 0x08000100
AUDIO_SYSTEM_OBJECT = 1


# --- Now Playing and its controls --------------------------------------------

# Shown on the phone when the now-playing reader can't be started.
NOW_PLAYING_MISSING = "Install media-control on the Mac: brew install media-control"


def now_playing_command():
    """A command that prints Now Playing as JSON lines, in `media-control stream` form."""
    return [MEDIA_CONTROL, "stream", "--micros"]


def send_media_command(action):
    """Sends toggle, next or previous to the app in Now Playing. True if it went through."""
    try:
        done = subprocess.run([MEDIA_CONTROL, MEDIA_COMMANDS[action]], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


# --- Core Audio and Core Foundation ------------------------------------------

class PropertyAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


@functools.cache
def core_audio():
    return ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")


@functools.cache
def core_foundation():
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateMutableCopy.restype = ctypes.c_void_p
    cf.CFStringCreateMutableCopy.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    cf.CFStringTransform.restype = ctypes.c_bool
    cf.CFStringTransform.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
    return cf


def audio_property(audio_object, code, buffer):
    """Reads a Core Audio property into buffer. Returns (ok, bytes written)."""
    address = PropertyAddress(int.from_bytes(code.encode("ascii"), "big"), int.from_bytes(b"glob", "big"), 0)
    size = ctypes.c_uint32(ctypes.sizeof(buffer))
    status = core_audio().AudioObjectGetPropertyData(
        audio_object, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(buffer),
    )
    return status == 0, size.value


# --- YouTube in Safari --------------------------------------------------------

def safari_is_audible():
    """True while Safari's media process is sending sound to an output."""
    cf = core_foundation()
    processes = (ctypes.c_uint32 * 512)()
    ok, size = audio_property(AUDIO_SYSTEM_OBJECT, "prs#", processes)
    if not ok:
        return False
    for process in processes[:size // ctypes.sizeof(ctypes.c_uint32)]:
        running = ctypes.c_uint32()
        if not audio_property(process, "piro", running)[0] or not running.value:
            continue
        bundle = ctypes.c_void_p()
        if not audio_property(process, "pbid", bundle)[0] or not bundle.value:
            continue
        text = ctypes.create_string_buffer(256)
        found = cf.CFStringGetCString(bundle, text, 256, UTF8)
        cf.CFRelease(bundle)
        if found and text.value.startswith((b"com.apple.WebKit.", b"com.apple.Safari")):
            return True
    return False


SAFARI_PROBE = (
    "(()=>{const v=document.querySelector('video');"
    "const m=navigator.mediaSession&&navigator.mediaSession.metadata;"
    "const p=document.querySelector('#movie_player');"
    "return JSON.stringify({paused:v?v.paused:true,time:v?v.currentTime:0,duration:v?v.duration:0,"
    "rate:v?v.playbackRate:1,ad:!!(p&&p.classList.contains('ad-showing')),"
    "title:m?m.title:'',artist:m?m.artist:'',album:m?m.album:''})})()"
)

SAFARI_SCRIPT = r"""
function run() {
  const safari = Application('Safari');
  if (!safari.running()) return '[]';
  const probe = %s;
  const rows = [];
  for (const win of safari.windows()) {
    let front = -1;
    try { front = win.currentTab().index(); } catch (e) { continue; }
    for (const tab of win.tabs()) {
      const url = tab.url() || '';
      if (!/^https:\/\/(www\.|m\.|music\.)?youtube\.com\/(watch\?|shorts\/)/.test(url)) continue;
      const row = {url: url, name: tab.name(), front: tab.index() === front};
      try { row.video = JSON.parse(safari.doJavaScript(probe, {in: tab})); } catch (e) {}
      rows.push(row);
    }
  }
  return JSON.stringify(rows);
}
""" % json.dumps(SAFARI_PROBE)


def read_safari_tabs():
    """Returns the open YouTube tabs as [{url, name, front, video}], or None if Safari
    could not be asked. `video` is missing when Safari won't run JavaScript from Apple Events."""
    try:
        done = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", SAFARI_SCRIPT],
            capture_output=True, text=True, timeout=5,
        )
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


SAFARI_RUN = """
function run(argv) {
  const safari = Application('Safari');
  for (const win of safari.windows()) {
    for (const tab of win.tabs()) {
      if (tab.url() === argv[0]) { safari.doJavaScript(argv[1], {in: tab}); return 'ok'; }
    }
  }
  return 'missing';
}
"""


def run_in_safari_tab(url, script):
    """Runs JavaScript in the Safari tab showing url. True if the tab was found."""
    try:
        done = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", SAFARI_RUN, url, script],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "ok"


# --- Audio levels for the visualizer -------------------------------------------

LEVELS_DIR = ROOT / "visualizer"
LEVELS_SOURCE = LEVELS_DIR / "audio_levels.swift"
LEVELS_HELPER = LEVELS_DIR / "audio_levels"

# Shown on the phone when the level helper can't be started.
LEVELS_MISSING = "The visualizer helper could not be built on the Mac."


def audio_levels_command():
    """A command that prints the sound's levels as JSON lines ({"b", "w", "v"}), or None.
    Builds the Swift helper first if it is missing or out of date."""
    if LEVELS_HELPER.exists() and LEVELS_HELPER.stat().st_mtime >= LEVELS_SOURCE.stat().st_mtime:
        return [str(LEVELS_HELPER)]
    command = [
        "swiftc", "-O", "-o", str(LEVELS_HELPER), str(LEVELS_SOURCE),
        "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
        "-Xlinker", str(LEVELS_DIR / "Info.plist"),
    ]
    try:
        built = subprocess.run(command, capture_output=True, timeout=180).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        built = False
    return [str(LEVELS_HELPER)] if built else None


# --- Transliteration -----------------------------------------------------------

def to_latin(text):
    """Converts any script to Latin letters with macOS's built-in ICU transform."""
    cf = core_foundation()
    source = cf.CFStringCreateWithCString(None, text.encode("utf-8"), UTF8)
    mutable = cf.CFStringCreateMutableCopy(None, 0, source)
    transform = cf.CFStringCreateWithCString(None, b"Any-Latin", UTF8)
    cf.CFStringTransform(mutable, None, transform, False)
    output = ctypes.create_string_buffer(len(text.encode("utf-8")) * 6 + 64)
    found = cf.CFStringGetCString(mutable, output, len(output), UTF8)
    for handle in (source, mutable, transform):
        cf.CFRelease(handle)
    return output.value.decode("utf-8") if found else text


# --- Secrets -------------------------------------------------------------------

def read_secret(service, account):
    """A password saved in the login Keychain, or None. tools/claude-key.sh saves the Claude key here."""
    try:
        found = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return found.stdout.strip() or None if found.returncode == 0 else None
