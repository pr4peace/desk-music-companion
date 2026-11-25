"""Stand-ins for macos.py on systems the server doesn't support yet.

They let server.py load, and its tests run, on Linux or Windows: nothing is playing,
the controls do nothing, the visualizer has no sound, and transliteration uses PyICU
when it is installed. A real Windows version would replace these one at a time
(see the Windows table in ROADMAP.md).
"""
import sys

NOW_PLAYING_MISSING = f"Following what's playing isn't supported on {sys.platform} yet."
LEVELS_MISSING = f"The visualizer can't hear the sound on {sys.platform} yet."


def now_playing_command():
    return None


def send_media_command(action):
    return False


def safari_is_audible():
    return False


def read_safari_tabs():
    return None


def run_in_safari_tab(url, script):
    return False


def audio_levels_command():
    return None


try:
    import icu
    ANY_LATIN = icu.Transliterator.createInstance("Any-Latin")
except ImportError:
    ANY_LATIN = None


def to_latin(text):
    """Latin letters through PyICU, the same ICU transform macOS uses. Unchanged without it."""
    return ANY_LATIN.transliterate(text) if ANY_LATIN else text


def read_secret(service, account):
    """No Keychain here: secrets come from environment variables instead."""
    return None
