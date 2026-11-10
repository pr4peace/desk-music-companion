#!/usr/bin/env python3
"""Desk Music Companion: shows album art and time-synced lyrics on a phone on your desk.

Follows whatever macOS reports as Now Playing (Apple Music, Spotify, and so on)
through the `media-control` tool. Safari does not always report to Now Playing,
so while Safari is making sound the server also reads any open YouTube tab,
which needs Safari > Settings > Developer > Allow JavaScript from Apple Events
for lyrics timing. Finds lyrics on LRCLIB and serves a full-screen page over the
local network. It only touches playback when a control on the phone is pressed.

Needs:  brew install media-control
Run:    python3 server.py        (PORT=9000 python3 server.py to change the port)
"""
import atexit
import base64
import ctypes
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
PORT = int(os.environ.get("PORT", "8787"))
USER_AGENT = "DeskLyrics/1.0"
MEDIA_CONTROL = shutil.which("media-control") or "/opt/homebrew/bin/media-control"

# These apps report clean song metadata. Anything else (usually a browser playing
# a video) gets its title untangled and looser matching.
MUSIC_APPS = {"com.apple.Music", "com.spotify.client"}

# Let the feed settle before searching, because duration can arrive just after the title.
DURATION_GRACE = 3.0

# How much longer than the song a music video may be (seconds), for intros and outros.
VIDEO_EXTRA = 120

# Most characters of lyrics to send for translation in one request.
TRANSLATE_CHUNK = 1500


class NowPlaying:
    """Keeps the latest system Now Playing state from a `media-control stream`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.art = None
        self.art_id = None
        self.art_type = "image/jpeg"
        self.error = None
        self.process = None
        self.first_seen = {}

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _run(self):
        command = [MEDIA_CONTROL, "stream", "--micros"]
        while True:
            try:
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                with self.lock:
                    self.error = "Install media-control on the Mac: brew install media-control"
                time.sleep(30)
                continue
            with self.lock:
                self.error = None
            for line in self.process.stdout:
                self._feed(line)
            self.process.wait()
            with self.lock:
                self.data = {}
            time.sleep(3)

    def _feed(self, line):
        try:
            message = json.loads(line)
        except ValueError:
            return
        if message.get("type") != "data":
            return
        payload = message.get("payload") or {}
        has_art = "artworkData" in payload
        art = payload.pop("artworkData", None)
        art_type = payload.pop("artworkMimeType", None)

        with self.lock:
            if not message.get("diff"):
                self.data = {}
            for key, value in payload.items():
                if value is None:
                    self.data.pop(key, None)
                else:
                    self.data[key] = value
            if has_art or not message.get("diff"):
                self._set_art(art, art_type)

    def _set_art(self, encoded, art_type):
        if not encoded:
            self.art = self.art_id = None
            return
        try:
            self.art = base64.b64decode(encoded)
        except ValueError:
            self.art = self.art_id = None
            return
        self.art_id = hashlib.sha1(self.art).hexdigest()[:16]
        self.art_type = art_type or "image/jpeg"

    def snapshot(self):
        with self.lock:
            data = dict(self.data)
            art_id = self.art_id
            error = self.error
        if error:
            return {"state": "unavailable", "detail": error}

        name = (data.get("title") or "").strip()
        if not name:
            return {"state": "stopped"}

        playing = bool(data.get("playing"))
        duration = (data.get("durationMicros") or 0) / 1_000_000
        position = (data.get("elapsedTimeMicros") or 0) / 1_000_000
        stamp = data.get("timestampEpochMicros")
        if playing and stamp:
            position += max(0.0, time.time() - stamp / 1_000_000) * (data.get("playbackRate") or 1)
        if duration:
            position = min(position, duration)

        apps = {data.get("bundleIdentifier"), data.get("parentApplicationBundleIdentifier")}
        return {
            "state": "playing" if playing else "paused",
            "position": max(0.0, position),
            "name": name,
            "artist": (data.get("artist") or "").strip(),
            "album": (data.get("album") or "").strip(),
            "duration": duration,
            "loose": not (apps & MUSIC_APPS),
            "art": art_id,
        }

    def artwork(self, art_id):
        with self.lock:
            return (self.art, self.art_type) if art_id and art_id == self.art_id else (None, None)

    def settled(self, key, has_duration):
        """True once a track is ready to look up."""
        with self.lock:
            first = self.first_seen.setdefault(key, time.monotonic())
            if len(self.first_seen) > 50:
                self.first_seen = {key: first}
        return has_duration or time.monotonic() - first >= DURATION_GRACE


# --- YouTube in Safari ------------------------------------------------------

CORE_AUDIO = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
CORE_FOUNDATION = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
CORE_FOUNDATION.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
CORE_FOUNDATION.CFStringGetCString.restype = ctypes.c_bool
CORE_FOUNDATION.CFRelease.argtypes = [ctypes.c_void_p]
UTF8 = 0x08000100
AUDIO_SYSTEM_OBJECT = 1


class PropertyAddress(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def audio_property(audio_object, code, buffer):
    """Reads a Core Audio property into buffer. Returns (ok, bytes written)."""
    address = PropertyAddress(int.from_bytes(code.encode("ascii"), "big"), int.from_bytes(b"glob", "big"), 0)
    size = ctypes.c_uint32(ctypes.sizeof(buffer))
    status = CORE_AUDIO.AudioObjectGetPropertyData(
        audio_object, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(buffer),
    )
    return status == 0, size.value


def safari_is_audible():
    """True while Safari's media process is sending sound to an output."""
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
        found = CORE_FOUNDATION.CFStringGetCString(bundle, text, 256, UTF8)
        CORE_FOUNDATION.CFRelease(bundle)
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
    """Returns the open YouTube tabs, or None if Safari could not be asked."""
    try:
        done = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", SAFARI_SCRIPT],
            capture_output=True, text=True, timeout=5,
        )
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def choose_tab(rows):
    def preference(row):
        video = row.get("video") or {}
        return (0 if video and not video.get("paused") and not video.get("ad") else 1, 0 if row.get("front") else 1)
    return min(rows, key=preference) if rows else None


def video_id(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.path.startswith("/shorts/"):
        return parsed.path.split("/")[2]
    return urllib.parse.parse_qs(parsed.query).get("v", [""])[0]


def tab_title(name):
    name = re.sub(r"[‎‏‪-‮]", "", name or "")
    name = re.sub(r"^\(\d+\)\s*", "", name)
    return re.sub(r"\s+-\s+YouTube(?:\s+Music)?$", "", name).strip()


class SafariYouTube:
    """Follows a YouTube tab while Safari is audible and Now Playing has nothing."""

    POLL = 2.0
    KEEP_PAUSED = 300   # how long after the sound stops to keep showing the tab

    def __init__(self):
        self.lock = threading.Lock()
        self.tab = None
        self.video = None
        self.art = None
        self.art_id = None
        self.last_audible = float("-inf")

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                self._step()
            except Exception as err:   # keep following after anything unexpected
                print(f"Safari check failed: {err}", flush=True)
            time.sleep(self.POLL)

    def _step(self):
        if now_playing.snapshot()["state"] == "playing":
            self._clear()
            return
        audible = safari_is_audible()
        if audible:
            self.last_audible = time.monotonic()
        elif time.monotonic() - self.last_audible > self.KEEP_PAUSED:
            self._clear()
            return
        rows = read_safari_tabs()
        if rows is not None:
            self._update(choose_tab(rows), audible)

    def _clear(self):
        with self.lock:
            self.tab = self.video = self.art = self.art_id = None

    def _update(self, row, audible):
        if not row:
            self._clear()
            return
        video = row.get("video")
        if video and video.get("ad"):   # an ad is not the song: hold the last reading, paused
            with self.lock:
                if self.tab:
                    self.tab["playing"] = False
            return

        timed = bool(video)
        name = ((video or {}).get("title") or "").strip() or tab_title(row.get("name"))
        if not name:
            self._clear()
            return
        tab = {
            "name": name,
            "artist": ((video or {}).get("artist") or "").strip(),
            "album": ((video or {}).get("album") or "").strip(),
            "duration": float((video or {}).get("duration") or 0),
            "position": float((video or {}).get("time") or 0),
            "rate": float((video or {}).get("rate") or 1),
            "playing": not video.get("paused") if video else audible,
            "timed": timed,
            "url": row["url"],
            "read_at": time.monotonic(),
        }
        identifier = video_id(row["url"])
        with self.lock:
            self.tab = tab
            changed = identifier != self.video
            if changed:
                self.video = identifier
                self.art = self.art_id = None
        if changed and identifier:
            threading.Thread(target=self._fetch_art, args=(identifier,), daemon=True).start()

    def _fetch_art(self, identifier):
        for size in ("maxresdefault", "hqdefault"):
            url = f"https://i.ytimg.com/vi/{urllib.parse.quote(identifier)}/{size}.jpg"
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    data = response.read(4_000_000)
            except (OSError, urllib.error.URLError):
                continue
            with self.lock:
                if self.video == identifier:
                    self.art = data
                    self.art_id = hashlib.sha1(data).hexdigest()[:16]
            return

    def snapshot(self):
        with self.lock:
            tab = dict(self.tab) if self.tab else None
            art_id = self.art_id
        if not tab:
            return None
        position = tab["position"]
        if tab["playing"]:
            position += (time.monotonic() - tab["read_at"]) * tab["rate"]
        if tab["duration"]:
            position = min(position, tab["duration"])
        return {
            "state": "playing" if tab["playing"] else "paused",
            "position": position,
            "name": tab["name"],
            "artist": tab["artist"],
            "album": tab["album"],
            "duration": tab["duration"],
            "loose": True,
            "timed": tab["timed"],
            "source": "safari",
            "url": tab["url"],
            "art": art_id,
        }

    def toggled(self):
        """Shows a play or pause straight away instead of at the next check."""
        with self.lock:
            if self.tab:
                now = time.monotonic()
                if self.tab["playing"]:
                    self.tab["position"] += (now - self.tab["read_at"]) * self.tab["rate"]
                self.tab["read_at"] = now
                self.tab["playing"] = not self.tab["playing"]

    def artwork(self, art_id):
        with self.lock:
            return (self.art, "image/jpeg") if art_id and art_id == self.art_id else (None, None)


# --- Lyrics lookup ---------------------------------------------------------

def norm(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(c for c in text if c.isalnum())


def unique(items):
    seen, result = set(), []
    for item in items:
        key = norm(item)
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def title_variants(name):
    cleaned = re.sub(
        r"\s*[\(\[][^\)\]]*\b(feat|ft|with|from|remaster|remastered|version|edit|mono|stereo|live|bonus|deluxe)\b[^\)\]]*[\)\]]",
        "", name, flags=re.I,
    )
    cleaned = re.sub(
        r"\s+-\s+.*\b(remaster|remastered|version|edit|mono|stereo|live)\b.*$",
        "", cleaned, flags=re.I,
    ).strip()
    return unique([name, cleaned])


def artist_names(artist):
    parts = re.split(r"\s*(?:,|;|&|\band\b|\bfeat\.?|\bft\.?|\bwith\b|\bx\b)\s*", artist, flags=re.I)
    return [p for p in (part.strip() for part in parts) if p]


def clean_video_title(title):
    """Reduces 'Song | Movie | Singer (Official Video) 4K' to 'Song'."""
    text = re.sub(
        r"[\(\[][^\)\]]*\b(official|video|audio|lyrics?|lyrical|hd|4k|visuali[sz]er|full song|teaser|remaster(ed)?)\b[^\)\]]*[\)\]]",
        "", title, flags=re.I,
    )
    text = text.split("|")[0]
    text = re.sub(r"[\(\[]\s*(tamil|telugu|hindi|malayalam|kannada|bengali|punjabi|marathi)\s*[\)\]]", "", text, flags=re.I)
    text = re.sub(
        r"\b(official\s+)?(music\s+|lyrics?\s+|lyrical\s+|full\s+)?(video|audio|song)(\s+song)?\s*$",
        "", text.strip(), flags=re.I,
    )
    text = re.sub(r"\b(4k|hd|lyrics?|lyrical)\s*$", "", text.strip(), flags=re.I)
    return text.strip(" -–—:~@")


def clean_channel(channel):
    return re.sub(r"(\s*-\s*topic|vevo|\s+official)\s*$", "", channel, flags=re.I).strip()


def search_terms(track):
    """Returns (titles, artists) to try for this track."""
    name, artist = track["name"], track["artist"]
    if not track["loose"]:
        return title_variants(name), unique([artist, *artist_names(artist)])

    cleaned = clean_video_title(name) or name
    titles, artists = [], []
    pieces = [p.strip() for p in re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)]
    if len(pieces) == 2:
        # 'A - B' is usually 'Artist - Song', sometimes 'Song - Artist'.
        titles += [pieces[1], pieces[0]]
        artists += [pieces[0], pieces[1]]
    else:
        titles.append(cleaned)
    # Credits after the first '|' often name the singer, and an @handle almost always does.
    credits = [piece.strip() for piece in name.split("|")[1:]]
    artists += sorted(credits, key=lambda piece: not piece.startswith("@"))
    channel = clean_channel(artist)
    artists += [channel, *artist_names(channel)]
    return unique(titles), unique(artists)


def lrclib(path, params):
    url = f"https://lrclib.net/api/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        if err.code in (400, 404):
            return None
        raise


def artist_overlaps(item, names):
    candidate = norm(item.get("artistName"))
    if not candidate:
        return False
    for name in names:
        n = norm(name)
        if len(n) >= 3 and (n in candidate or candidate in n):
            return True
    return False


def matches(item, track, titles, names):
    """Only accept a result that is clearly this song. No lyrics beats wrong lyrics."""
    title = norm(item.get("trackName"))
    if not title:
        return False
    wanted = [norm(t) for t in titles]
    exact = title in wanted
    close = exact or any(len(w) >= 3 and (title in w or w in title) for w in wanted)
    if not close:
        return False

    overlap = artist_overlaps(item, names)
    if track["duration"]:
        extra = track["duration"] - (item.get("duration") or 0)
        fits = abs(extra) <= (12 if track["loose"] else 5)
        # A music video can run well past the song, so it may be longer, but only
        # when the exact title and the singer both confirm it is the same song.
        if not fits and not (track["loose"] and exact and overlap and 0 < extra <= VIDEO_EXTRA):
            return False

    if overlap:
        return True
    # A channel name like a record label says nothing about the singer, so a video
    # can pass without an artist match, but only on an exact title and known length.
    return track["loose"] and exact and bool(track["duration"])


def parse_lrc(text):
    lines = []
    for raw in text.splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", raw)
        if not stamps:
            continue
        words = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for minutes, seconds in stamps:
            lines.append({"t": int(minutes) * 60 + float(seconds.replace(":", ".")), "text": words})
    lines.sort(key=lambda line: line["t"])
    return lines


def rank(item, track):
    return (
        0 if item.get("syncedLyrics") else 1,
        abs((item.get("duration") or 0) - track["duration"]),
    )


def timing_fits(item, track):
    """False for lyrics timed to a longer cut of the song than the one playing."""
    if not item.get("syncedLyrics") or not track["duration"]:
        return True
    lines = parse_lrc(item["syncedLyrics"])
    return not lines or lines[-1]["t"] <= track["duration"] + 3


@functools.lru_cache(maxsize=128)
def sound_words(lyrics):
    """The consonants of each word, so the same lyrics match across scripts and spellings."""
    found = set()
    for raw in lyrics.splitlines():
        line = re.sub(r"\[[^\]]*\]", "", raw).strip()
        latin = (romanize(line) if not_latin(line) else line) or ""
        for word in re.findall(r"[^\W\d_]+", unicodedata.normalize("NFD", latin.lower())):
            bare = re.sub(r"[aeiouy\u0300-\u036f]", "", word)
            bare = re.sub(r"(?<=[kgcjtdpbs])h", "", re.sub(r"(.)\1", r"\1", bare))
            if len(bare) >= 2:
                found.add(bare)
    return frozenset(found)


def same_lyrics(item, reference):
    """False when two records are the same title sung in different languages."""
    words = [sound_words(i.get("syncedLyrics") or i.get("plainLyrics") or "") for i in (item, reference)]
    smaller = min(len(w) for w in words)
    return smaller < 8 or len(words[0] & words[1]) / smaller >= 0.3


def lookup_lrclib(track):
    """Returns the best LRCLIB record, or None. Raises on network failure."""
    titles, names = search_terms(track)
    pool = []
    reference = None   # the record for this exact album and length settles which version it is

    if track["duration"] and not track["loose"]:
        direct = lrclib("get", {
            "track_name": track["name"],
            "artist_name": track["artist"],
            "album_name": track["album"],
            "duration": round(track["duration"]),
        })
        if direct:
            pool.append(direct)
            if direct.get("syncedLyrics") or direct.get("plainLyrics"):
                reference = direct

    def acceptable(item):
        return matches(item, track, titles, names) and timing_fits(item, track) and (not reference or same_lyrics(item, reference))

    def synced_match():
        return any(i.get("syncedLyrics") and acceptable(i) for i in pool)

    searches = []
    specific = names[1:3] if not track["loose"] and len(names) > 1 else names[:2]
    for title in titles:
        for name in specific:
            searches.append({"track_name": title, "artist_name": name})
        if names:
            searches.append({"q": f"{title} {specific[0] if specific else names[0]}"})
        searches.append({"track_name": title})

    for params in searches[:8]:
        if synced_match():
            break
        pool.extend(lrclib("search", params) or [])

    usable = [i for i in pool if acceptable(i) and (i.get("syncedLyrics") or i.get("plainLyrics") or i.get("instrumental"))]
    return min(usable, key=lambda i: rank(i, track)) if usable else None


LOOKUP_VERSION = 2   # songs saved by an earlier version of the matching rules are looked up again


class LyricsStore:
    NONE_RETRY = 3600
    ERROR_RETRY = 30

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def ensure(self, key, track):
        with self.lock:
            entry = self.entries.get(key)
            if entry and time.time() < entry.get("retry_at", float("inf")):
                return entry
            if not entry:
                saved = CACHE / f"{key}.json"
                if saved.exists():
                    entry = json.loads(saved.read_text("utf-8"))
                    if entry.get("lookup") == LOOKUP_VERSION:
                        self.entries[key] = entry
                        return entry
            entry = {"status": "loading"}
            self.entries[key] = entry
        threading.Thread(target=self._fetch, args=(key, track), daemon=True).start()
        return entry

    def get(self, key):
        with self.lock:
            return self.entries.get(key)

    def _fetch(self, key, track):
        try:
            record = lookup_lrclib(track)
        except Exception as err:  # network trouble: keep the screen alive, try again soon
            print(f"lyrics lookup failed for {track['name']}: {err}", flush=True)
            self._set(key, {"status": "error", "retry_at": time.time() + self.ERROR_RETRY})
            return

        if record and record.get("syncedLyrics"):
            entry = {"status": "synced", "lines": parse_lrc(record["syncedLyrics"])}
        elif record and record.get("plainLyrics"):
            entry = {"status": "plain", "text": record["plainLyrics"]}
        elif record and record.get("instrumental"):
            entry = {"status": "instrumental"}
        else:
            self._set(key, {"status": "none", "retry_at": time.time() + self.NONE_RETRY})
            return

        entry["lookup"] = LOOKUP_VERSION
        CACHE.mkdir(exist_ok=True)
        saved = CACHE / f"{key}.json"
        if saved.exists() and lyric_texts(json.loads(saved.read_text("utf-8"))) != lyric_texts(entry):
            (CACHE / f"{key}.en.json").unlink(missing_ok=True)   # the old translations were of other words
            (CACHE / f"{key}.natural.json").unlink(missing_ok=True)
            (CACHE / f"{key}.context.json").unlink(missing_ok=True)
            translations.entries.pop(key, None)
            naturals.entries.pop(key, None)
            contexts.entries.pop(key, None)
        saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
        self._set(key, entry)

    def _set(self, key, entry):
        with self.lock:
            self.entries[key] = entry


# --- Transliteration and translation ----------------------------------------

CORE_FOUNDATION.CFStringCreateWithCString.restype = ctypes.c_void_p
CORE_FOUNDATION.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
CORE_FOUNDATION.CFStringCreateMutableCopy.restype = ctypes.c_void_p
CORE_FOUNDATION.CFStringCreateMutableCopy.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
CORE_FOUNDATION.CFStringTransform.restype = ctypes.c_bool
CORE_FOUNDATION.CFStringTransform.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]


def to_latin(text):
    """Converts any script to Latin letters with macOS's built-in ICU transform."""
    cf = CORE_FOUNDATION
    source = cf.CFStringCreateWithCString(None, text.encode("utf-8"), UTF8)
    mutable = cf.CFStringCreateMutableCopy(None, 0, source)
    transform = cf.CFStringCreateWithCString(None, b"Any-Latin", UTF8)
    cf.CFStringTransform(mutable, None, transform, False)
    output = ctypes.create_string_buffer(len(text.encode("utf-8")) * 6 + 64)
    found = cf.CFStringGetCString(mutable, output, len(output), UTF8)
    for handle in (source, mutable, transform):
        cf.CFRelease(handle)
    return output.value.decode("utf-8") if found else text


VOWELS = "aāiīuūeēoō"
TAMIL = re.compile(r"[஀-௿]")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
KANA = re.compile(r"[぀-ヿ]")
HAN = re.compile(r"[一-鿿]")
TAMIL_SOUNDS = re.compile(
    rf"ṟṟ|ṉṟ|ṅk|ñc|ṇṭ|nt|mp|kk|cc|ṭṭ|tt|pp"
    rf"|(?P<medial>(?<=[{VOWELS}ṇṉ])[kcṭtp](?=[{VOWELS}]))"
    rf"|(?P<first>^[tc])"
    rf"|[āīūṉṇṟḷḻṅñṭṣ]"
)
TAMIL_SPELLING = {
    "ṟṟ": "tr", "ṉṟ": "ndr", "ṅk": "ng", "ñc": "nj", "ṇṭ": "nd", "nt": "ndh", "mp": "mb",
    "kk": "kk", "cc": "chch", "ṭṭ": "tt", "tt": "tth", "pp": "pp",
    "ā": "aa", "ī": "ee", "ū": "oo", "ṉ": "n", "ṇ": "n", "ṟ": "r", "ḷ": "l", "ḻ": "zh",
    "ṅ": "ng", "ñ": "nj", "ṭ": "t", "ṣ": "sh",
}
TAMIL_MEDIAL = {"k": "g", "c": "s", "ṭ": "d", "t": "dh", "p": "b"}
TAMIL_FIRST = {"t": "th", "c": "s"}


def tamil_sound(match):
    """Spells Tamil the way it is sung (magale, not makaḷē)."""
    if match["medial"]:
        return TAMIL_MEDIAL[match["medial"]]
    if match["first"]:
        return TAMIL_FIRST[match["first"]]
    return TAMIL_SPELLING[match[0]]


def not_latin(text):
    return any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in text)


def romanize_word(word):
    if not not_latin(word):
        return word   # an English word inside a Tamil line stays as it is
    latin = unicodedata.normalize("NFC", to_latin(word))
    if TAMIL.search(word):
        latin = TAMIL_SOUNDS.sub(tamil_sound, latin)
    elif DEVANAGARI.search(word):
        latin = re.sub(rf"(?<=[^{VOWELS}])a$", "", latin)   # Hindi drops the final 'a' sound
        latin = re.sub("ṁ(?![pbm])|m̐", "n", latin)     # nasal: main, hain, kahan
        latin = re.sub("c(?!h)", "ch", latin)
        latin = re.sub("[śṣ]", "sh", latin)
        latin = re.sub(r"(?<=\w)'(?=\w)", "", latin)     # the converter's mark between vowels isn't sung
    return "".join(c for c in unicodedata.normalize("NFD", latin) if not unicodedata.combining(c))


def romanize(text):
    """Returns the line in Latin letters, or None if it needs none or can't be read."""
    if not not_latin(text):
        return None
    if KANA.search(text) and HAN.search(text):
        return None   # the transform would read Japanese kanji as Chinese
    return " ".join(romanize_word(word) for word in text.split(" "))


def lyric_texts(entry):
    """The distinct, non-empty lines of a lyrics entry, in order."""
    if entry.get("lines"):
        rows = [line["text"] for line in entry["lines"]]
    else:
        rows = (entry.get("text") or "").split("\n")
    return list(dict.fromkeys(row.strip() for row in rows if row.strip()))


def google_translate(lines):
    """Returns (English lines, detected language)."""
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t",
        data=urllib.parse.urlencode({"q": "\n".join(lines)}).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as err:   # Google has brief hiccups: try again shortly
            if err.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 + attempt * 3)
    text = "".join(segment[0] for segment in data[0] if segment[0])
    return [row.strip() for row in text.split("\n")], data[2]


def translate_rows(rows):
    """Like google_translate, but always returns one English row for each row sent."""
    english, language = google_translate(rows)
    if language != "en" and len(english) != len(rows):   # the lines came back merged: go one at a time
        english = [google_translate([row])[0][0] for row in rows]
    return english, language


# Google's typing tools, by language, for lyrics published in English letters.
NATIVE_INPUT = {code: f"{code}-t-i0-und" for code in ("hi", "ta", "te", "ml", "kn", "bn", "pa", "gu", "mr", "ur")}
ENGLISH_WORDS = None


def reads_as_english(line):
    global ENGLISH_WORDS
    if ENGLISH_WORDS is None:
        try:
            ENGLISH_WORDS = {word.strip().lower() for word in open("/usr/share/dict/words", encoding="utf-8") if len(word.strip()) > 2}
        except OSError:
            ENGLISH_WORDS = set()
        ENGLISH_WORDS |= set("i a an am is to of in on my me we he it so no do be go by at as or if up".split())
    words = re.findall(r"[a-z']+", line.lower())
    known = sum(word in ENGLISH_WORDS or word.rstrip("s") in ENGLISH_WORDS for word in words)
    return known / max(1, len(words)) >= 0.75


def spell_natively(line, language):
    """Writes a lyric typed in English letters in its own script, or returns None."""
    query = urllib.parse.urlencode({"text": line, "itc": NATIVE_INPUT[language], "num": 1, "cp": 0, "cs": 1, "ie": "utf-8", "oe": "utf-8", "app": "desklyrics"})
    request = urllib.request.Request("https://inputtools.google.com/request?" + query, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    if data[0] != "SUCCESS":
        return None
    return "".join(part[1][0] for part in data[1] if part[1]) or None


def improve_romanized(chunk, english, language):
    """Google translates Hindi typed in English letters poorly, and often just copies it.
    Spelling those lines in their own script first fixes most of them."""
    try:
        spelled = {}
        for index, line in enumerate(chunk):
            if not not_latin(line) and not reads_as_english(line):
                native = spell_natively(line, language)
                if native:
                    spelled[index] = native
        if not spelled:
            return english
        better, _ = translate_rows(list(spelled.values()))
    except (OSError, ValueError, IndexError, TypeError):   # keep the plain translation
        return english
    merged = list(english)
    for index, row in zip(spelled, better):
        if row:
            merged[index] = row
    return merged


def translate_lines(lines):
    """Maps each line to English. Empty when the song is already in English."""
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) > TRANSLATE_CHUNK:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)

    result = {}
    for chunk in chunks:
        english, language = translate_rows(chunk)
        if language == "en":
            return {}
        if language in NATIVE_INPUT:
            english = improve_romanized(chunk, english, language)
        result.update({a: b for a, b in zip(chunk, english) if b and b != a})
    return result


# Translation models installed by translate/setup.sh. When one is present it translates songs
# written in Indian scripts on this Mac, before Google is asked. IndicTrans2, built for Indian
# languages, is preferred; Meta's NLLB is used when IndicTrans2 isn't downloaded.
NLLB_PYTHON = ROOT / "translate" / ".venv" / "bin" / "python"
NLLB_SCRIPT = ROOT / "translate" / "nllb.py"
INDIC_SCRIPT = ROOT / "translate" / "indictrans.py"
HF_MODELS = Path.home() / ".cache" / "huggingface" / "hub"


# In a blind test on these songs IndicTrans2 did better on Tamil, Telugu and Punjabi, and NLLB
# on Hindi, so Hindi and Urdu go to NLLB when it's installed.
PREFER_NLLB = {"hin_Deva", "urd_Arab"}


def mac_translator(language=None):
    """The helper script for this language, or None if no translation model is installed."""
    if not NLLB_PYTHON.exists():
        return None
    indic = (HF_MODELS / "models--ai4bharat--indictrans2-indic-en-dist-200M").exists()
    nllb = (HF_MODELS / "models--facebook--nllb-200-distilled-600M").exists()
    if nllb and (language in PREFER_NLLB or not indic):
        return NLLB_SCRIPT
    return INDIC_SCRIPT if indic else None
SCRIPT_LANGUAGES = {
    "TAMIL": "tam_Taml", "DEVANAGARI": "hin_Deva", "TELUGU": "tel_Telu", "MALAYALAM": "mal_Mlym",
    "KANNADA": "kan_Knda", "BENGALI": "ben_Beng", "GURMUKHI": "pan_Guru", "GUJARATI": "guj_Gujr",
    "ORIYA": "ory_Orya", "ARABIC": "urd_Arab",
}


def main_script(line):
    counts = {}
    for c in line:
        if c.isalpha():
            name = unicodedata.name(c, "").split(" ")[0]
            counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else None


def translate_with_nllb(lines, script=None):
    """English from a model on this Mac, or None when none is installed or the song isn't mostly
    in an Indian script."""
    groups = {}
    for line in lines:
        language = SCRIPT_LANGUAGES.get(main_script(line))
        if language:
            groups.setdefault(language, []).append(line)
    if sum(len(group) for group in groups.values()) < len(lines) * .5:
        return None
    main_language = max(groups, key=lambda language: len(groups[language]))
    script = script or mac_translator(main_language)
    if not script:
        return None
    payload = json.dumps({"groups": [{"lang": lang, "lines": group} for lang, group in groups.items()]}, ensure_ascii=False)
    done = subprocess.run(
        [str(NLLB_PYTHON), str(script)], input=payload, capture_output=True, text=True, timeout=240,
        env={**os.environ, "HF_HUB_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"},
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr.strip().splitlines() or ["NLLB stopped"])[-1][:200])
    return {line: text for line, text in json.loads(done.stdout)["lines"].items() if text and text != line}


GOOGLE_REST = 600   # after Google refuses with "too many requests", leave it alone this long
google_resting_until = 0.0


def numbered_lines(reply, count, field="en"):
    """Reads a model's {"lines": [{"n": 1, "en": "..."}]} reply into {n: text}."""
    rows = {}
    decoder = json.JSONDecoder()
    at = reply.find("{")
    while at != -1:
        try:
            value, end = decoder.raw_decode(reply, at)
        except ValueError:
            at = reply.find("{", at + 1)
            continue
        for item in value.get("lines", [value]) if isinstance(value, dict) else []:
            try:
                n, text = int(item.get("n")), str(item.get(field, "")).strip()
            except (AttributeError, TypeError, ValueError):
                continue
            if 1 <= n <= count and text:
                rows[n] = text
        at = reply.find("{", end)
    return rows


def translate_locally(lines):
    """A faithful line-by-line translation from the model on this Mac, for when Google won't answer."""
    if sum(reads_as_english(line) for line in lines) > len(lines) * .6:
        return {}   # already English
    model = choose_writer()
    if not model:
        raise RuntimeError("no local model installed")
    numbered = "\n".join(f"{n}. {line} | {romanize(line) or line}" for n, line in enumerate(lines, 1))
    prompt = (
        "Each numbered line is a song lyric, followed by how it sounds.\n\n"
        f"{numbered}\n\n"
        "Translate every line into plain English, staying close to the words. One entry per line, in order.\n"
        'Reply with only JSON: {"lines": [{"n": 1, "en": "..."}]}'
    )
    with tidbits.writing:   # one job at a time on the local model
        reply = ollama_chat(model, "You translate song lyrics accurately into English.", prompt, 3000)
    rows = numbered_lines(reply, len(lines))
    return {lines[n - 1]: text for n, text in rows.items() if text != lines[n - 1]}


def english_for(lines):
    """Tries NLLB on this Mac, then Google, then the local model. None if all of them fail."""
    global google_resting_until
    try:
        english = translate_with_nllb(lines)
        if english is not None:
            return english
    except Exception as err:
        print(f"on-Mac translation failed: {err}", flush=True)
    try:
        if time.time() < google_resting_until:
            raise RuntimeError("Google is rate-limiting, giving it a rest")
        return translate_lines(lines)
    except Exception as err:
        if isinstance(err, urllib.error.HTTPError) and err.code == 429:
            google_resting_until = time.time() + GOOGLE_REST
        print(f"translation failed: {err}; trying the local model", flush=True)
    try:
        return translate_locally(lines)
    except Exception as err:   # no translation is fine: the lyrics still show
        print(f"local translation failed: {err}", flush=True)
        return None


class TranslationStore:
    FAILED_RETRY = 45

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def ensure(self, key, lines):
        saved = CACHE / f"{key}.en.json"
        with self.lock:
            entry = self.entries.get(key)
            if entry and time.time() < entry.get("retry_at", float("inf")):
                return entry
            if not entry and saved.exists():
                entry = json.loads(saved.read_text("utf-8"))
                self.entries[key] = entry
                return entry
            entry = {"status": "loading", "total": len(lines), "since": time.time()}
            self.entries[key] = entry
        threading.Thread(target=self._fetch, args=(key, lines, saved), daemon=True).start()
        return entry

    def _fetch(self, key, lines, saved):
        english = english_for(lines)
        if english is None:
            entry = {"status": "failed", "retry_at": time.time() + self.FAILED_RETRY}
        else:
            entry = {"status": "ready" if english else "none", "lines": english}
            CACHE.mkdir(exist_ok=True)
            saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
        with self.lock:
            self.entries[key] = entry


class WrittenStore:
    """Writing the local model does with the whole song as context, starting from the literal
    translation: natural English for each line, or notes on what lines mean in the song.
    Optional: the literal translation always shows first. While it writes, the entry says how
    far through the song it has got, so the phone can show real progress."""
    FAILED_RETRY = 120
    UNAVAILABLE_RETRY = 600

    def __init__(self, name, writer):
        self.name = name
        self.writer = writer
        self.lock = threading.Lock()
        self.entries = {}

    def ensure(self, key, lines, track):
        saved = CACHE / f"{key}.{self.name}.json"
        with self.lock:
            entry = self.entries.get(key)
            if entry and time.time() < entry.get("retry_at", float("inf")):
                return entry
            if not entry and saved.exists():
                entry = json.loads(saved.read_text("utf-8"))
                self.entries[key] = entry
                return entry
        literal = translations.ensure(key, lines)
        if literal.get("status") == "none":
            return {"status": "none"}
        if literal.get("status") not in ("ready", "failed"):
            return {"status": "loading"}
        literal = {"lines": literal.get("lines") or {}}   # a failed literal one just means no hint
        with self.lock:
            if self.entries.get(key, {}).get("status") == "loading":
                return self.entries[key]
            entry = {"status": "loading", "done": 0, "total": len(lines)}
            self.entries[key] = entry
        threading.Thread(target=self._write, args=(key, lines, literal["lines"], track, saved), daemon=True).start()
        return entry

    def _progress(self, key, done):
        with self.lock:
            entry = self.entries.get(key)
            if entry and entry.get("status") == "loading":
                entry["done"] = max(entry.get("done", 0), done)

    def _write(self, key, lines, literal, track, saved):
        model = choose_writer()
        if not model:
            entry = {"status": "unavailable", "retry_at": time.time() + self.UNAVAILABLE_RETRY}
        else:
            try:
                with tidbits.writing:   # one job at a time on the local model
                    written = self.writer(model, lines, literal, track, lambda done: self._progress(key, done))
                entry = {"status": "ready" if written else "none", "lines": written}
                CACHE.mkdir(exist_ok=True)
                saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
            except Exception as err:
                print(f"{self.name} writing failed: {err}", flush=True)
                entry = {"status": "failed", "retry_at": time.time() + self.FAILED_RETRY}
        with self.lock:
            self.entries[key] = entry


def lines_written(text):
    """How far through the numbered lines a reply has got, while it is still being written."""
    numbers = [int(n) for n in re.findall(r'"n"\s*:\s*(\d+)', text)]
    return max(numbers) if numbers else 0


def natural_lines(model, lines, literal, track, progress=None):
    numbered = "\n".join(
        f"{n}. {line} | {romanize(line) or line}" + (f" | literal: {literal[line]}" if line in literal else "") for n, line in enumerate(lines, 1)
    )
    prompt = (
        f"Song: \"{track.get('name', '')}\" by {track.get('artist', '')}.\n\n"
        "Each numbered line shows the lyric, how it sounds, and where available a word-for-word machine translation.\n\n"
        f"{numbered}\n\n"
        "Write each line in natural English that says what it means and how it feels, the way a friend who knows "
        "the language would explain it while the song plays. Read the whole song first, so repeated words, "
        "idioms and metaphors make sense in context. Keep it short like a lyric, never longer than needed. "
        "Keep names and words with no good English equivalent. One entry for every line, in order.\n"
        'Reply with only JSON: {"lines": [{"n": 1, "en": "..."}]}'
    )
    reply = ollama_chat(model, "You translate song lyrics so they make sense to an English speaker. You never add lines or invent meaning that isn't there.", prompt, 3000,
                        on_text=progress and (lambda text: progress(lines_written(text))))
    rows = numbered_lines(reply, len(lines))
    result = {}
    for n, text in rows.items():
        line = lines[n - 1]
        if len(text) <= 3 * len(literal.get(line, line)) + 60 and text != line:
            result[line] = text
    return result


def context_notes(model, lines, literal, track, progress=None):
    """Short notes on what lines mean in the song as a whole: the idiom, the image, the reference or
    the double meaning a listener would miss from a translation alone. Only lines that need one get one."""
    natural = {}
    saved = CACHE / f"{track.get('key', '')}.natural.json"
    if track.get("key") and saved.exists():
        natural = json.loads(saved.read_text("utf-8")).get("lines") or {}
    numbered = "\n".join(
        f"{n}. {line} | {romanize(line) or line}"
        + (f" | literal: {literal[line]}" if line in literal else "")
        + (f" | natural: {natural[line]}" if line in natural else "")
        for n, line in enumerate(lines, 1)
    )
    prompt = (
        f"Song: \"{track.get('name', '')}\" by {track.get('artist', '')}.\n\n"
        "Each numbered line shows the lyric, how it sounds, and English translations where available.\n\n"
        f"{numbered}\n\n"
        "Read the whole song first. Then, for the lines where a translation alone loses something, write one short note "
        "(at most 18 words) on what the line really means here: an idiom, a metaphor and what it stands for, a cultural "
        "or religious reference, wordplay or a double meaning, who is speaking to whom, or how it answers an earlier line. "
        "Say the meaning, not the words again. Don't repeat the translation, don't praise the song, and skip lines that "
        "are plain or already clear. A repeated line gets a note only the first time. Never invent facts about the singers or the film.\n"
        'Reply with only JSON: {"lines": [{"n": 3, "note": "..."}]}'
    )
    reply = ollama_chat(model, "You explain song lyrics to an English speaker the way a friend who grew up with the language would.", prompt, 2500,
                        on_text=progress and (lambda text: progress(lines_written(text))))
    rows = numbered_lines(reply, len(lines), field="note")
    result = {}
    for n, text in rows.items():
        line = lines[n - 1]
        if line not in result and 8 <= len(text) <= 160:
            result[line] = text
    return result


def ollama_chat(model, system, prompt, max_tokens, on_text=None):
    """Asks the local model. With on_text, the reply is streamed and on_text sees it grow."""
    body = json.dumps({
        "model": model, "stream": bool(on_text), "think": False, "keep_alive": "30s",
        "options": {"temperature": 0.4, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }).encode("utf-8")
    request = urllib.request.Request(f"{OLLAMA}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=240) as response:
        if not on_text:
            return json.load(response)["message"]["content"]
        text = ""
        for raw in response:
            if not raw.strip():
                continue
            part = json.loads(raw)
            text += part.get("message", {}).get("content", "")
            on_text(text)
            if part.get("done"):
                break
        return text


# --- Tidbits -----------------------------------------------------------------
# Facts come from Wikipedia. A model running on this Mac picks the good ones and
# writes the funny ones, and anything it says that isn't in the articles is dropped.

WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
# Wikipedia asks apps to say who they are; generic names get rate-limited sooner.
WIKI_AGENT = "DeskMusicCompanion/1.0 (https://github.com/pr4peace/desk-music-companion)"
OLLAMA = "http://127.0.0.1:11434"
WRITERS = ("qwen3.5:9b", "gemma4:e4b")   # the first of these that is installed writes; kept small so the Mac stays responsive
TIDBIT_COUNT = 5
TIDBITS_AFTER = 20   # seconds into a song before its tidbits are researched
SKIP_SECTIONS = ("cast", "track listing", "personnel", "see also", "references", "external links", "notes", "further reading", "filmography", "discography", "bibliography")
MUSIC_WORDS = ("singer", "musician", "band", "composer", "rapper", "vocalist", "songwriter", "duo", "group", "producer", "playback")
INTERESTING_WORDS = ("award", "won ", "first", "record", "inspired", "named", "famous", "originally", "debut", "nominated", "chart", "best", "reportedly", "banned", "only")


def plain_words(text):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKD", text).lower()).strip()


def base_title(title):
    return plain_words(re.sub(r"\s*\(.*?\)\s*$", "", title))


def wiki_api(**params):
    params.update(format="json", formatversion=2)
    request = urllib.request.Request(f"{WIKIPEDIA}?{urllib.parse.urlencode(params)}", headers={"User-Agent": WIKI_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:   # asked to slow down: wait as long as it says
            if err.code != 429 or attempt == 2:
                raise
            try:
                time.sleep(min(20, float(err.headers.get("Retry-After") or 3)))
            except ValueError:
                time.sleep(3)


def tidy_article(text):
    parts = re.split(r"\n==+ *(.*?) *==+\n", text)
    kept = [parts[0]]
    for heading, body in zip(parts[1::2], parts[2::2]):
        if heading.strip().lower() not in SKIP_SECTIONS:
            kept.append(body)
    return re.sub(r"\n+", " ", " ".join(kept)).strip()


def about_song(text, name, seen):
    """Only the sentences about this song, and a following one that carries on about it.
    Sentences already in the seen set are skipped, as film and soundtrack articles often repeat."""
    wanted = f" {plain_words(name)} "
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", text)
    keep = set()
    for index, sentence in enumerate(sentences):
        if wanted in f" {plain_words(sentence)} ":
            keep.add(index)
            if index + 1 < len(sentences) and re.match(r"(It|This|The song|The track|She|He|They)\b", sentences[index + 1]):
                keep.add(index + 1)
    fresh = []
    for index in sorted(keep):
        marker = plain_words(sentences[index])[:70]
        if marker not in seen:
            seen.add(marker)
            fresh.append(sentences[index])
    return " ".join(fresh)[:3000]


def find_articles(query, title_fits, text_fits, want=1):
    """Returns [(title, text)] for search results that are really about the subject."""
    found = []
    for hit in wiki_api(action="query", list="search", srsearch=query, srlimit=4)["query"]["search"]:
        title = hit["title"]
        if re.match(r"list of|discography|awards and", title, re.I) or not title_fits(title):
            continue
        text = wiki_api(action="query", prop="extracts", explaintext=1, titles=title, redirects=1)["query"]["pages"][0].get("extract", "")
        if text and "may refer to" not in text[:300] and text_fits(text):
            found.append((title, tidy_article(text)))
            if len(found) == want:
                break
    return found


def mentions_any(text, words):
    haystack = f" {plain_words(text)} "
    return any(f" {word} " in haystack for word in words)


def clean_album(album):
    album = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", album or "")
    album = re.sub(r"\s+-\s*(ep|single)\s*$", "", album, flags=re.I)
    return re.sub(r"\b(original motion picture soundtrack|soundtrack|ost)\b", "", album, flags=re.I).strip(" -")


def film_of(*texts):
    """The film a song comes from, when the title or album says so: 'Magale (From "Baththa")'."""
    for text in texts:
        found = re.search(r"\(\s*from\s+[\"“']?([^\"”')]+?)[\"”']?\s*\)", text or "", re.I)
        if found:
            return found.group(1).strip()
    return ""


def grounded(text, notes):
    """True when every number and proper name in the text also appears in the notes."""
    lowered = notes.lower()
    if any(number.strip(".,") not in notes for number in re.findall(r"\d[\d,.]*", text)):
        return False
    return all(name.lower() in lowered for name in re.findall(r"(?<=[a-z,;:'] )[A-Z][\w'-]+", text))


def headline_is_grounded(headline, notes):
    lowered = notes.lower()
    if any(number.strip(".,") not in notes for number in re.findall(r"\d[\d,.]*", headline)):
        return False
    return all(word.lower() in lowered or reads_as_english(word) for word in re.findall(r"[A-Z][\w'-]+", headline))


def choose_writer():
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as response:
            installed = {model["name"] for model in json.load(response)["models"]}
    except (OSError, ValueError, KeyError):
        return None
    return next((name for name in WRITERS if name in installed), None)


def parse_tidbits(reply):
    """Reads the model's JSON, whether it sends one object or several in a row."""
    decoder = json.JSONDecoder()
    items = []
    at = reply.find("{")
    while at != -1:
        try:
            value, end = decoder.raw_decode(reply, at)
        except ValueError:
            at = reply.find("{", at + 1)
            continue
        if isinstance(value, dict):
            items.extend(value.get("tidbits", [value]))
        at = reply.find("{", end)
    return [item for item in items if isinstance(item, dict)]


def ask_writer(model, sources, track):
    notes = "\n\n".join(f"[{title}]\n{text}" for title, text in sources)
    prompt = (
        f"Now playing: \"{track['name']}\" by {track['artist']}.\n\n{notes}\n\n"
        f"Write up to {TIDBIT_COUNT} tidbits to show on screen while this song plays. Every one must be about this song itself: how it "
        "was made, who sings it, what it means, where it appears, what happened to it. Ignore anything about the rest of the album, "
        "film or career. Fewer is better than padding. Most are \"fact\": the most surprising, specific things in the notes. Include one "
        "or two \"funny\" if the notes allow: a deadpan aside that would make a friend laugh, teasing one concrete detail. "
        "A funny one must be a joke, not a summary. The style, from other subjects: "
        "\"Recorded in one take, mostly because the studio bill had already made everyone cry.\" and "
        "\"The band named itself after a typo, then spent forty years pretending it was deep.\" "
        "Never joke about anyone's body, family, health or religion. "
        "Give each a headline: a punchy hook of 2 to 5 words in sentence case that makes you want to read on, not a label. "
        "The text is one plain sentence under 24 words that pays it off without repeating the headline. "
        "Reply with only JSON in this shape, nothing else: "
        '{"tidbits": [{"kind": "fact or funny", "headline": "...", "text": "...", "source": "the bracketed title it came from"}]}'
    )
    body = json.dumps({
        "model": model, "stream": False, "think": False, "keep_alive": "30s",
        "options": {"temperature": 0.7, "num_predict": 800},
        "messages": [
            {"role": "system", "content": "You write short tidbits for a music display. Use only what is in the notes. Never add a name, number, or event that is not in the notes. Write fewer if the notes are thin."},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(f"{OLLAMA}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=150) as response:
        reply = json.load(response)["message"]["content"]
    return parse_tidbits(reply)


def sentences_as_tidbits(sources):
    """Used when no local model is available: the most interesting sentences, as written."""
    picks = []
    for title, text in sources:
        sentences = [s.strip() for s in re.split(r"(?<=[a-z\)]\.)\s+(?=[A-Z])", text) if 50 <= len(s.strip()) <= 260 and s.count(",") <= 4]
        ranked = sorted(sentences, key=lambda s: -sum(word in s.lower() for word in INTERESTING_WORDS))
        picks.extend({"kind": "fact", "headline": "", "text": s, "source": title} for s in ranked[:3])
    return picks[:TIDBIT_COUNT]


def write_tidbits(sources, track):
    notes = " ".join(text for _, text in sources) + f" {track['name']} {track['artist']} {track.get('album') or ''}"
    titles = [title for title, _ in sources]
    model = choose_writer()
    items = []
    if model:
        try:
            written = ask_writer(model, sources, track)
        except (OSError, ValueError, KeyError, TypeError):
            written = []
        seen = set()
        for item in written:
            text = str(item.get("text", "")).strip()
            headline = str(item.get("headline", "")).strip().rstrip(".")
            if len(headline.split()) > 7 or not headline_is_grounded(headline, notes):
                headline = ""
            if headline:
                headline = headline[0].upper() + headline[1:]
            if re.search(r"\bnotes?\b|other tracks?", text, re.I):   # talking about its source, or about another song
                continue
            if text and text not in seen and len(text.split()) <= 34 and grounded(text, notes):
                seen.add(text)
                items.append({"kind": "funny" if item.get("kind") == "funny" else "fact", "headline": headline, "text": text, "source": item.get("source") if item.get("source") in titles else titles[0]})
    if not items:
        items = sentences_as_tidbits(sources)
    facts = [i for i in items if i["kind"] == "fact"]
    funny = [i for i in items if i["kind"] == "funny"]
    mixed = []
    while facts or funny:   # a funny one after every two facts
        mixed.extend(facts[:2])
        facts = facts[2:]
        mixed.extend(funny[:1])
        funny = funny[1:]
    return mixed[:TIDBIT_COUNT]


class TidbitStore:
    STEPS = 4
    FAILED_RETRY = 120
    NOTHING_RETRY = 3600

    def __init__(self):
        self.lock = threading.Lock()
        self.writing = threading.Lock()   # one song at a time on the local model
        self.entries = {}

    def ensure(self, track):
        key = track["key"]
        saved = CACHE / f"{key}.tidbits.json"
        with self.lock:
            entry = self.entries.get(key)
            if entry and time.time() < entry.get("retry_at", float("inf")):
                return entry
            if not entry and saved.exists():
                entry = json.loads(saved.read_text("utf-8"))
                self.entries[key] = entry
                return entry
            entry = {"status": "loading", "step": 0, "steps": self.STEPS, "label": "Looking up the song"}
            self.entries[key] = entry
        threading.Thread(target=self._build, args=(track, saved), daemon=True).start()
        return entry

    def _progress(self, key, step, label):
        with self.lock:
            self.entries[key] = {"status": "loading", "step": step, "steps": self.STEPS, "label": label}

    def _build(self, track, saved):
        key = track["key"]
        try:
            entry = self._research(track, key)
            if entry["status"] == "ready":
                CACHE.mkdir(exist_ok=True)
                saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
        except Exception as err:   # no tidbits is fine: everything else still works
            print(f"tidbits failed: {err}", flush=True)
            entry = {"status": "failed", "retry_at": time.time() + self.FAILED_RETRY}
        with self.lock:
            self.entries[key] = entry

    def _research(self, track, key):
        name = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", track["name"]).strip()
        artist = re.split(r",|&| feat\.?| ft\.?| x | and ", track["artist"] or "")[0].strip()
        album = clean_album(track.get("album"))
        film = film_of(track["name"], track.get("album"))
        artist_words = [word for word in plain_words(artist).split() if len(word) > 2]
        sources = []

        own_article = find_articles(
            f"{name} {artist} song",
            lambda title: base_title(title) == plain_words(name),
            lambda text: mentions_any(text, artist_words + plain_words(album).split()),
        )
        sources += own_article
        if album and plain_words(album) != plain_words(name):
            self._progress(key, 1, f"Reading about {album}")
            song_words = [word for word in plain_words(name).split() if len(word) > 3]
            sources += find_articles(
                album,
                lambda title: base_title(title) == plain_words(album),
                lambda text: mentions_any(text, artist_words + song_words),
                want=2,
            )
        if film and plain_words(film) not in (plain_words(name), plain_words(album)):
            # A single from a film is usually written up in the film's soundtrack article, not its own.
            self._progress(key, 1, f"Reading about {film}")
            song_words = [word for word in plain_words(name).split() if len(word) > 3]
            sources += find_articles(
                f"{film} soundtrack",
                lambda title: base_title(title) == plain_words(film),
                lambda text: mentions_any(text, artist_words + song_words),
                want=2,
            )
        if artist:
            self._progress(key, 2, f"Reading about {artist}")
            sources += find_articles(
                f"{artist} singer musician",
                lambda title: plain_words(artist) in plain_words(title),
                lambda text: mentions_any(text[:800], MUSIC_WORDS),
            )
        # A song's own article is all about it. Elsewhere, only what ties back to the song counts.
        own = dict(own_article)
        unique = []
        seen = set()
        for title, text in dict(sources).items():
            notes = text[:3000] if title in own else about_song(text, name, seen)
            if len(notes) >= 40:
                unique.append((title, notes))
        if not unique:
            return {"status": "none", "retry_at": time.time() + self.NOTHING_RETRY}
        self._progress(key, 3, "Picking the good bits")
        with self.writing:
            items = write_tidbits(unique, track)
        if not items:
            return {"status": "none", "retry_at": time.time() + self.NOTHING_RETRY}
        return {"status": "ready", "items": items}


# --- Visualizer --------------------------------------------------------------

LEVELS_DIR = ROOT / "visualizer"
LEVELS_SOURCE = LEVELS_DIR / "audio_levels.swift"
LEVELS_HELPER = LEVELS_DIR / "audio_levels"


def levels_helper_ready():
    """Builds the helper if it is missing or out of date."""
    if LEVELS_HELPER.exists() and LEVELS_HELPER.stat().st_mtime >= LEVELS_SOURCE.stat().st_mtime:
        return True
    command = [
        "swiftc", "-O", "-o", str(LEVELS_HELPER), str(LEVELS_SOURCE),
        "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
        "-Xlinker", str(LEVELS_DIR / "Info.plist"),
    ]
    try:
        return subprocess.run(command, capture_output=True, timeout=180).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class AudioLevels:
    """Runs the level helper only while a phone has the visualizer open."""

    def __init__(self):
        self.changed = threading.Condition()
        self.clients = 0
        self.frame = None
        self.count = 0
        self.thread = None
        self.process = None

    def join(self):
        with self.changed:
            self.clients += 1
            if not self.thread:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def leave(self):
        with self.changed:
            self.clients -= 1

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def wait(self, seen):
        """Returns (newest frame or None, its number), waiting briefly for a new one."""
        with self.changed:
            self.changed.wait_for(lambda: self.count != seen, timeout=2)
            return (self.frame if self.count != seen else None), self.count

    def _publish(self, frame):
        with self.changed:
            self.frame = frame
            self.count += 1
            self.changed.notify_all()

    def _run(self):
        while True:
            with self.changed:
                if not self.clients:
                    self.thread = None
                    return
            if not levels_helper_ready():
                self._publish(b'{"error":"The visualizer helper could not be built on the Mac."}')
                time.sleep(10)
                continue
            self.process = subprocess.Popen(
                [str(LEVELS_HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            for line in self.process.stdout:
                self._publish(line.strip())
                with self.changed:
                    if not self.clients:
                        break
            self.stop()
            self.process.wait()
            time.sleep(1)


now_playing = NowPlaying()
audio_levels = AudioLevels()
safari_youtube = SafariYouTube()
lyrics_store = LyricsStore()
translations = TranslationStore()
naturals = WrittenStore("natural", natural_lines)
contexts = WrittenStore("context", context_notes)
tidbits = TidbitStore()


def current_player():
    """A playing app wins, then a playing Safari tab, then whatever is paused."""
    system = now_playing.snapshot()
    if system["state"] == "playing":
        return system
    tab = safari_youtube.snapshot()
    if tab and (tab["state"] == "playing" or system["state"] != "paused"):
        return tab
    return system


def build_state():
    player = current_player()
    if player["state"] in ("stopped", "unavailable"):
        return player

    key = hashlib.sha1(f"{player['name']}\0{player['artist']}".encode("utf-8")).hexdigest()[:16]
    status = "loading"
    if not player.get("timed", True):
        status = "needs_setup"
    elif now_playing.settled(key, bool(player["duration"])):
        status = lyrics_store.ensure(key, player)["status"]
    track = {
        "key": key,
        "name": (clean_video_title(player["name"]) if player["loose"] else "") or player["name"],
        "artist": player["artist"],
        "album": player["album"],
        "duration": player["duration"],
    }
    # Research tidbits in the background once a song has played a while, so they're ready
    # when the tab opens. Songs skipped in the first seconds aren't worth the work.
    if player["state"] == "playing" and player["position"] > TIDBITS_AFTER:
        tidbits.ensure(track)
    return {
        "state": player["state"],
        "position": player["position"],
        "age": 0,
        "track": track,
        "lyrics": status,
        "art": player["art"],
    }


MEDIA_COMMANDS = {"toggle": "toggle-play-pause", "next": "next-track", "previous": "previous-track"}
SAFARI_ACTIONS = {
    "toggle": "(()=>{const v=document.querySelector('video');if(v)v.paused?v.play():v.pause()})()",
    "next": "document.querySelector('.ytp-next-button, .next-button')?.click()",
    "previous": (
        "(()=>{const v=document.querySelector('video');"
        "const b=document.querySelector('.ytp-prev-button, .previous-button');"
        "if(v&&(v.currentTime>3||!b||!b.offsetParent))v.currentTime=0;else if(b)b.click()})()"
    ),
}
SAFARI_CONTROL = """
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


def control(action):
    """Sends a command to whichever player is on screen. Returns True if it went through."""
    player = current_player()
    if player["state"] in ("stopped", "unavailable"):
        return False
    try:
        if player.get("source") != "safari":
            return subprocess.run([MEDIA_CONTROL, MEDIA_COMMANDS[action]], capture_output=True, timeout=5).returncode == 0
        if not player["timed"]:
            return False   # Safari's Apple Events setting is off, so the tab can't be reached
        done = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", SAFARI_CONTROL, player["url"], SAFARI_ACTIONS[action]],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    sent = done.returncode == 0 and done.stdout.strip() == "ok"
    if sent and action == "toggle":
        safari_youtube.toggled()
    return sent


MANIFEST = json.dumps({
    "name": "Desk Music Companion",
    "short_name": "Desk Music",
    "display": "fullscreen",
    "background_color": "#0b0b0d",
    "theme_color": "#0b0b0d",
    "start_url": "/",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}).encode("utf-8")
# Icons for adding the page to a phone's home screen.
HOME_ICONS = {"/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content_type, status=200, cache="no-store"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the phone went away mid-reply, which is routine

    def send_json(self, value, status=200):
        self.send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def stream_levels(self):
        audio_levels.join()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = 0
            while True:
                frame, seen = audio_levels.wait(seen)
                self.wfile.write(b"data: " + frame + b"\n\n" if frame else b": waiting\n\n")
                self.wfile.flush()
        except OSError:
            pass   # the phone closed the visualizer
        finally:
            audio_levels.leave()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        action = urllib.parse.parse_qs(url.query).get("action", [""])[0]
        if url.path != "/api/control" or action not in MEDIA_COMMANDS:
            self.send(b"Not found", "text/plain", 404)
        elif self.headers.get("X-Desk-Lyrics") != "1":   # stops other websites from sending commands
            self.send(b"Forbidden", "text/plain", 403)
        else:
            self.send_json({"ok": control(action)})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)

        if url.path == "/":
            self.send((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self.send_json(build_state())
        elif url.path == "/api/lyrics":
            entry = lyrics_store.get(query.get("key", [""])[0])
            if entry:
                body = {k: v for k, v in entry.items() if k != "retry_at"}
                body["roman"] = {text: romanize(text) for text in lyric_texts(entry) if not_latin(text)}
                self.send_json(body)
            else:
                self.send_json({"status": "missing"}, 404)
        elif url.path == "/api/levels":
            self.stream_levels()
        elif url.path == "/api/translation":
            key = query.get("key", [""])[0]
            entry = lyrics_store.get(key)
            lines = lyric_texts(entry) if entry else []
            style = query.get("style", [""])[0]
            if lines and style in ("natural", "context"):
                track = build_state().get("track") or {}
                store = naturals if style == "natural" else contexts
                result = store.ensure(key, lines, track if track.get("key") == key else {})
                self.send_json({k: v for k, v in result.items() if k != "retry_at"})
            elif lines:
                result = dict(translations.ensure(key, lines))
                if "since" in result:   # how long it has been going, for the phone's progress ring
                    result["elapsed"] = round(time.time() - result.pop("since"), 1)
                self.send_json({k: v for k, v in result.items() if k != "retry_at"})
            elif not entry or entry.get("status") == "loading":   # lyrics are still on their way
                self.send_json({"status": "loading"})
            else:
                self.send_json({"status": "none"})
        elif url.path == "/api/tidbits":
            track = build_state().get("track") or {}
            if track.get("key") == query.get("key", [""])[0]:
                result = tidbits.ensure(track)
                self.send_json({k: v for k, v in result.items() if k != "retry_at"})
            else:
                self.send_json({"status": "none"})
        elif url.path == "/art":
            art_id = query.get("id", [""])[0]
            data, content_type = now_playing.artwork(art_id)
            if not data:
                data, content_type = safari_youtube.artwork(art_id)
            if data:
                self.send(data, content_type, cache="max-age=86400")
            else:
                self.send(b"Not found", "text/plain", 404)
        elif url.path == "/manifest.webmanifest":
            self.send(MANIFEST, "application/manifest+json")
        elif url.path in HOME_ICONS:
            self.send((ROOT / "assets" / url.path.lstrip("/")).read_bytes(), "image/png", cache="max-age=86400")
        else:
            self.send(b"Not found", "text/plain", 404)


def lan_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.168.0.1", 9))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    atexit.register(now_playing.stop)
    atexit.register(audio_levels.stop)
    now_playing.start()
    safari_youtube.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Desk Music Companion is running. Open on your phone: http://{lan_address()}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
