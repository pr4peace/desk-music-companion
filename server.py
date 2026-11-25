#!/usr/bin/env python3
"""Desk Music Companion: shows album art and time-synced lyrics on a phone on your desk.

Follows whatever macOS reports as Now Playing (Apple Music, Spotify, and so on)
through the `media-control` tool. Safari does not always report to Now Playing,
so while Safari is making sound the server also reads any open YouTube tab,
which needs Safari > Settings > Developer > Allow JavaScript from Apple Events
for lyrics timing. Finds lyrics on LRCLIB (or NetEase, for timing LRCLIB lacks) and serves a full-screen page over the
local network. It only touches playback when a control on the phone is pressed.

Needs:  brew install media-control
Run:    python3 server.py        (PORT=9000 python3 server.py to change the port)
"""
import atexit
import base64
import functools
import hashlib
import json
import os
import re
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

import updater

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
PORT = int(os.environ.get("PORT", "8787"))
USER_AGENT = "DeskLyrics/1.0"

# The parts that only work on a Mac live in macos.py. Elsewhere, stand-ins let the server
# load and its tests run.
if sys.platform == "darwin":
    import macos as system
else:
    import other_systems as system

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
        while True:
            command = system.now_playing_command()
            try:
                if not command:
                    raise OSError("no now-playing reader")
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                with self.lock:
                    self.error = system.NOW_PLAYING_MISSING
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
        audible = system.safari_is_audible()
        if audible:
            self.last_audible = time.monotonic()
        elif time.monotonic() - self.last_audible > self.KEEP_PAUSED:
            self._clear()
            return
        rows = system.read_safari_tabs()
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
        titles, singers = title_variants(name), []
        # Soundtracks often name the singers in the title ('Song - Singer, Singer') and give
        # the composer as the artist. Lyrics sites list the singers, so try those too.
        pieces = re.split(r"\s+[-–—]\s+", titles[-1], maxsplit=1)
        if len(pieces) == 2 and pieces[0].strip() and pieces[1].strip():
            titles = [pieces[0].strip(), *titles]
            singers = artist_names(pieces[1])
        return unique(titles), unique([artist, *singers, *artist_names(artist)])

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


STRICT_CONFIRMED = 10   # seconds of difference allowed for a Music-app song when title and artist agree


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
        # The same recording is often listed a few seconds longer or shorter on lyrics sites. Allow
        # more when the exact title and the singer agree; timing_fits still rejects a longer cut.
        fits = abs(extra) <= (12 if track["loose"] else STRICT_CONFIRMED if exact and overlap else 5)
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


NETEASE = "https://music.163.com/api"
# NetEase puts the credits (lyricist, composer...) at the top as timed lines. They are not sung.
CREDIT_LINE = re.compile(r"^[^:：]{1,24}[:：]")
CREDIT_WORDS = re.compile(r"[\u4e00-\u9fff]|lyric|compos|produc|arrang|written|music|vocal|singer|mix|master|record", re.I)


def netease(path, params):
    url = f"{NETEASE}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://music.163.com/"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        if err.code in (400, 404):
            return None
        raise


def clean_netease_lrc(text):
    """Drops the credit lines NetEase adds, and the placeholder for songs nobody has timed."""
    kept = []
    for raw in (text or "").splitlines():
        words = re.sub(r"\[[^\]]*\]", "", raw).strip()
        match = CREDIT_LINE.match(words)
        if match and CREDIT_WORDS.search(match[0]):
            continue
        kept.append(raw)
    synced = "\n".join(kept)
    lines = [line for line in parse_lrc(synced) if line["text"]]
    if len(lines) < 4 or all(line["t"] == lines[0]["t"] for line in lines):
        return None   # untimed or nearly empty: no better than plain lyrics
    return synced


def netease_record(song):
    """A NetEase search result in the shape of an LRCLIB record, so the same checks apply."""
    return {
        "id": song.get("id"),
        "trackName": song.get("name"),
        "artistName": ", ".join(a.get("name") or "" for a in song.get("artists") or []),
        "albumName": (song.get("album") or {}).get("name"),
        "duration": (song.get("duration") or 0) / 1000,
        "syncedLyrics": None,
        "plainLyrics": None,
    }


def lookup_netease(track, reference=None):
    """Returns a NetEase record with synced lyrics, or None. Raises on network failure."""
    titles, names = search_terms(track)
    tried = set()
    for title in titles[:2]:
        for query in ([f"{title} {names[0]}"] if names else []) + [title]:
            found = netease("search/get", {"s": query, "type": 1, "limit": 10}) or {}
            songs = (found.get("result") or {}).get("songs") or []
            candidates = [netease_record(s) for s in songs]
            candidates = [c for c in candidates if c["id"] not in tried and matches(c, track, titles, names)]
            candidates.sort(key=lambda c: abs(c["duration"] - track["duration"]))
            for candidate in candidates[:3]:
                tried.add(candidate["id"])
                lyric = netease("song/lyric", {"id": candidate["id"], "lv": 1}) or {}
                candidate["syncedLyrics"] = clean_netease_lrc((lyric.get("lrc") or {}).get("lyric"))
                if not candidate["syncedLyrics"] or not timing_fits(candidate, track):
                    continue
                if reference and not same_lyrics(candidate, reference):
                    continue   # same title, other language
                return candidate
    return None


def lookup_lyrics(track):
    """LRCLIB first. When it has no timing, NetEase may. Raises only if LRCLIB can't be reached."""
    record = lookup_lrclib(track)
    if record and (record.get("syncedLyrics") or record.get("instrumental")):
        return record
    try:
        timed = lookup_netease(track, reference=record)
    except Exception as err:  # the second source is a bonus; plain lyrics still show
        print(f"NetEase lookup failed for {track['name']}: {err}", flush=True)
        timed = None
    return timed or record


LOOKUP_VERSION = 4   # songs saved by an earlier version of the matching rules are looked up again


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
            record = lookup_lyrics(track)
        except Exception as err:  # network trouble: keep the screen alive, try again soon
            print(f"lyrics lookup failed for {track['name']}: {err}", flush=True)
            self._set(key, {"status": "error", "retry_at": time.time() + self.ERROR_RETRY})
            return

        if record and record.get("syncedLyrics"):
            entry = {"status": "synced", "lines": parse_lrc(record["syncedLyrics"])}
        elif record and record.get("plainLyrics"):
            entry = with_tapped_timing(key, {"status": "plain", "text": record["plainLyrics"], "source": lrclib_source(record)})
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
            (CACHE / f"{key}.story.json").unlink(missing_ok=True)
            translations.entries.pop(key, None)
            naturals.entries.pop(key, None)
            contexts.entries.pop(key, None)
            stories.entries.pop(key, None)
            timing.forget(key)   # the nudge was for the old lines' timings
        saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
        self._set(key, entry)

    def _set(self, key, entry):
        with self.lock:
            self.entries[key] = entry


# --- Timing nudges -----------------------------------------------------------

# Kept beside server.py rather than in cache/, because these are choices someone made, not
# downloads, and clearing the cache shouldn't lose them.
TIMING = ROOT / "timing.json"
MAX_NUDGE = 30.0   # seconds either way; anything more is a wrong match, not drift


def song_key(text):
    return bool(re.fullmatch(r"[0-9a-f]{16}", text or ""))


def nudge_value(value):
    """A nudge in seconds, rounded to hundredths and kept in range, or None if it isn't a number."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(max(-MAX_NUDGE, min(MAX_NUDGE, value)), 2)


class TimingStore:
    """The − / + lyric nudge for each song, saved on the Mac so every phone gets it and it
    survives the page being removed from a phone's Home Screen."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.offsets = {}
        try:
            saved = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            for key, value in saved.items():
                value = nudge_value(value)
                if song_key(key) and value:
                    self.offsets[key] = value

    def get(self, key):
        with self.lock:
            return self.offsets.get(key, 0)

    def set(self, key, value):
        with self.lock:
            if value:
                self.offsets[key] = value
            elif self.offsets.pop(key, None) is None:
                return
            self._save()

    def adopt(self, offsets):
        """Takes nudges a phone remembered on its own, keeping any the Mac already has."""
        with self.lock:
            fresh = {k: v for k, v in offsets.items() if k not in self.offsets}
            if fresh:
                self.offsets.update(fresh)
                self._save()
            return len(fresh)

    def forget(self, key):
        self.set(key, 0)

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.offsets, indent=1, sort_keys=True), "utf-8")
            temporary.replace(self.path)   # a crash mid-write never leaves half a file
        except OSError as err:   # still right for this run; it just won't outlast a restart
            print(f"couldn't save lyric timing: {err}", flush=True)


# --- Timing tapped on a phone -------------------------------------------------

# When no lyrics site has timing for a song, someone can tap along on the phone once, line by
# line. Kept beside server.py like the nudges, since they are someone's work, not a download.
TAPPED = ROOT / "tapped"
LONGEST_SONG = 4 * 3600


def plain_rows(text):
    return [row.strip() for row in (text or "").split("\n") if row.strip()]


def tapped_lines(text, lines):
    """Checks a phone's taps against the plain lyrics: one time per line, in order, never going
    back. Returns the timed lines, or None if they don't fit these lyrics."""
    rows = plain_rows(text)
    if not rows or not isinstance(lines, list) or len(lines) != len(rows):
        return None
    timed, last = [], 0.0
    for row, line in zip(rows, lines):
        if not isinstance(line, dict) or line.get("text") != row:
            return None
        t = seconds_value(line.get("t"))
        if t is None or t < last or t > LONGEST_SONG:
            return None
        timed.append({"t": t, "text": row})
        last = t
    return timed


def seconds_value(value):
    """A time in seconds, rounded to hundredths, or None if it isn't a finite number."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(max(0.0, value), 2)


class TappedStore:
    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()

    def get(self, key):
        try:
            lines = json.loads((self.folder / f"{key}.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return lines if isinstance(lines, list) else None

    def set(self, key, lines):
        with self.lock:
            path = self.folder / f"{key}.json"
            try:
                if lines:
                    self.folder.mkdir(exist_ok=True)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(lines, ensure_ascii=False), "utf-8")
                    temporary.replace(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as err:
                print(f"couldn't save tapped timing: {err}", flush=True)


def with_tapped_timing(key, entry):
    """Plain lyrics become synced when someone has tapped along to them before."""
    lines = tapped_lines(entry.get("text"), tapped.get(key))
    if not lines:
        return entry
    return {"status": "synced", "lines": lines, "tapped": True, "text": entry["text"], "source": entry.get("source")}


def save_tapped(key, lines):
    """Stores taps from a phone and shows them at once. Empty taps go back to plain lyrics.
    Returns False if the song's lyrics aren't plain or the taps don't fit them."""
    entry = lyrics_store.get(key)
    if not entry or not (entry.get("status") == "plain" or entry.get("tapped")):
        return False
    plain = {"status": "plain", "text": entry["text"], "lookup": entry.get("lookup"), "source": entry.get("source")}
    if lines:
        timed = tapped_lines(entry["text"], lines)
        if not timed:
            return False
        tapped.set(key, timed)
        fresh = {**plain, "status": "synced", "lines": timed, "tapped": True}
    else:
        tapped.set(key, None)
        fresh = plain
    timing.forget(key)   # the taps already match the song as it plays
    CACHE.mkdir(exist_ok=True)
    (CACHE / f"{key}.json").write_text(json.dumps(fresh, ensure_ascii=False), "utf-8")
    lyrics_store._set(key, fresh)
    return True


# --- Sharing tapped timing with LRCLIB ---------------------------------------

# Timing someone tapped can go to LRCLIB, so everyone else gets these lyrics in time too. This
# publishes publicly, so it only ever happens when someone presses Share on the phone. LRCLIB asks
# for a small proof of work first: find a number whose SHA-256, after its prefix, is at most its target.
SHARED = TAPPED / "shared.json"
MOST_TRIES = 1 << 32   # far past any target LRCLIB sets; a guard against a broken challenge


def lrclib_source(record):
    """What LRCLIB calls a record, so a share lands on the same song."""
    return {k: record.get(k) for k in ("trackName", "artistName", "albumName", "duration")}


def lrc_stamp(seconds):
    hundredths = round(max(0.0, seconds) * 100)
    minutes, rest = divmod(hundredths, 6000)
    return f"[{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}]"


def synced_lrc(lines, offset=0):
    """Tapped lines as LRC. A − / + nudge moves every line, the way the phone shows them."""
    return "\n".join(f"{lrc_stamp(line['t'] - offset)} {line['text']}" for line in lines)


def solve_challenge(prefix, target):
    goal = bytes.fromhex(target)
    start = hashlib.sha256(prefix.encode("utf-8"))
    for nonce in range(MOST_TRIES):
        attempt = start.copy()
        attempt.update(str(nonce).encode("ascii"))
        if attempt.digest() <= goal:
            return str(nonce)
    raise ValueError("no answer to LRCLIB's challenge")


def lrclib_post(path, body=None, headers=None):
    """Returns LRCLIB's status code and JSON reply. Raises only if LRCLIB can't be reached."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
    request = urllib.request.Request(
        f"https://lrclib.net/api/{path}", data=data, method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def publish_to_lrclib(payload):
    """Asks for a challenge, answers it and publishes. Returns None, or what went wrong."""
    status, challenge = lrclib_post("request-challenge")
    if status != 200 or not isinstance(challenge, dict) or not challenge.get("prefix") or not challenge.get("target"):
        return f"LRCLIB didn't give a challenge ({status})"
    nonce = solve_challenge(challenge["prefix"], challenge["target"])
    status, reply = lrclib_post("publish", payload, {"X-Publish-Token": f"{challenge['prefix']}:{nonce}"})
    if status != 201:
        message = reply.get("message") if isinstance(reply, dict) else None
        return f"LRCLIB said no ({status}{': ' + message if message else ''})"
    return None


def share_fingerprint(payload):
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def share_payload(key, source):
    """What would be published for this song, or None if there's no tapped timing to share."""
    entry = lyrics_store.get(key)
    if not entry or not entry.get("tapped") or not entry.get("lines"):
        return None
    if not source or not source.get("trackName") or not source.get("artistName") or not source.get("duration"):
        return None
    return {
        "trackName": source["trackName"],
        "artistName": source["artistName"],
        "albumName": source.get("albumName") or "",
        "duration": round(float(source["duration"])),
        "plainLyrics": "\n".join(plain_rows(entry["text"])),
        "syncedLyrics": synced_lrc(entry["lines"], timing.get(key)),
    }


def playing_now(key):
    """The player, if it's playing this song."""
    player = current_player()
    if player["state"] in ("stopped", "unavailable"):
        return None
    if hashlib.sha1(f"{player['name']}\0{player['artist']}".encode("utf-8")).hexdigest()[:16] != key:
        return None
    return player


def other_recording(key, source):
    """Songs are kept by title and artist, so another cut of the same song shares their lyrics. When
    the one playing now is a different length from the LRCLIB record, the taps may be for that cut."""
    player = playing_now(key)
    if not player or player["loose"] or not player["duration"] or not source.get("duration"):
        return False
    return abs(player["duration"] - float(source["duration"])) > 5


def find_source(key, player):
    """The LRCLIB record these plain lyrics came from, found again from what's playing, and kept
    with the lyrics from then on. A song from a music app can fall back to its own details; a
    video's title isn't a song title, so a video can't."""
    record = lookup_lrclib({"name": player["name"], "artist": player["artist"], "album": player["album"],
                            "duration": player["duration"], "loose": player["loose"]})
    entry = lyrics_store.get(key)
    if not entry:
        return None
    if record and plain_rows(record.get("plainLyrics")) == plain_rows(entry.get("text")):
        source = lrclib_source(record)
    elif not player["loose"] and player["duration"]:
        source = {"trackName": player["name"], "artistName": player["artist"],
                  "albumName": player["album"], "duration": player["duration"]}
    else:
        return None
    kept = {**entry, "source": source}
    try:
        CACHE.mkdir(exist_ok=True)
        (CACHE / f"{key}.json").write_text(json.dumps(kept, ensure_ascii=False), "utf-8")
    except OSError as err:
        print(f"couldn't save the lyrics record: {err}", flush=True)
    lyrics_store._set(key, kept)
    return source


class ShareStore:
    """Which songs' tapped timing has gone to LRCLIB, and what's happening with the rest."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.jobs = {}   # key -> {"status": "working" | "failed", "error": ...}
        try:
            saved = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.shared = {k: v for k, v in saved.items() if song_key(k) and isinstance(v, str)} if isinstance(saved, dict) else {}

    def source(self, key):
        """(LRCLIB record, None), or for songs saved before lyrics kept their record, (None, the
        player) when it's playing this song so the record can be found again."""
        entry = lyrics_store.get(key) or {}
        if entry.get("source"):
            return entry["source"], None
        return None, playing_now(key)

    def status(self, key):
        """ready, working, shared, failed, or unavailable when there's nothing this Mac can share."""
        with self.lock:
            job = self.jobs.get(key)
            if job and job["status"] == "working":
                return dict(job)
        source, player = self.source(key)
        entry = lyrics_store.get(key) or {}
        if not entry.get("tapped") or not (source or player):
            return {"status": "unavailable"}
        payload = share_payload(key, source) if source else None
        with self.lock:
            if payload and self.shared.get(key) == share_fingerprint(payload):
                return {"status": "shared"}
            if job and job["status"] == "failed":
                return dict(job)
        return {"status": "ready"}

    def start(self, key):
        with self.lock:
            if self.jobs.get(key, {}).get("status") == "working":
                return True
        source, player = self.source(key)
        if not (source or player) or not (lyrics_store.get(key) or {}).get("tapped"):
            return False
        if source and other_recording(key, source):
            with self.lock:
                self.jobs[key] = {"status": "failed", "error": "this recording is a different length from LRCLIB's"}
            return True
        with self.lock:
            self.jobs[key] = {"status": "working"}
        threading.Thread(target=self._share, args=(key, source, player), daemon=True).start()
        return True

    def _share(self, key, source, player):
        payload = None
        try:
            if not source:
                source = find_source(key, player)
            payload = share_payload(key, source)
            if not payload:
                error = "LRCLIB needs the song's title, artist and length"
            else:
                error = publish_to_lrclib(payload)
        except Exception as err:   # network trouble; the phone offers to try again
            error = f"couldn't reach LRCLIB: {err}"
        with self.lock:
            if error:
                print(f"sharing timing for {key} failed: {error}", flush=True)
                self.jobs[key] = {"status": "failed", "error": error}
                return
            self.jobs.pop(key, None)
            self.shared[key] = share_fingerprint(payload)
            self._save()

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(exist_ok=True)
            temporary.write_text(json.dumps(self.shared, indent=1, sort_keys=True), "utf-8")
            temporary.replace(self.path)
        except OSError as err:
            print(f"couldn't save shared timing: {err}", flush=True)


# --- Transliteration and translation ----------------------------------------

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
    latin = unicodedata.normalize("NFC", system.to_latin(word))
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


class GoogleResting(Exception):
    """Google refused recently, so it isn't being asked again yet."""


def retry_after(err):
    """Seconds a refusal asks us to wait, or 0 when it doesn't say."""
    try:
        return max(0.0, float((err.headers or {}).get("Retry-After") or 0))
    except (TypeError, ValueError):
        return 0.0


class GoogleGate:
    """Google's free translate endpoint blocks a Mac that asks too often, and keeps blocking it while
    it keeps asking. So requests go one at a time with a gap between them, and after a "too many
    requests" refusal Google is left alone for a while: 10 minutes, then twice as long after each
    refusal in a row, up to 3 hours. Retrying sooner only makes the block last longer."""
    GAP = 1.0
    REST = 600
    MAX_REST = 3 * 3600

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = threading.Lock()   # held for a whole request, so no two are ever in flight
        self.last = 0.0
        self.resting_until = 0.0
        self.strikes = 0

    def resting(self):
        return time.time() < self.resting_until

    def wait_turn(self):
        with self.lock:
            if self.resting():
                raise GoogleResting(f"Google is rate-limiting, resting for {self.resting_until - time.time():.0f}s more")
            pause = self.last + self.GAP - time.time()
            if pause > 0:
                time.sleep(pause)
            self.last = time.time()

    def refused(self, wait=0.0):
        with self.lock:
            self.strikes += 1
            rest = min(self.MAX_REST, self.REST * 2 ** (self.strikes - 1))
            self.resting_until = time.time() + max(rest, wait)
            print(f"Google refused with too many requests; leaving it alone for {max(rest, wait) / 60:.0f} minutes", flush=True)

    def answered(self):
        with self.lock:
            self.strikes = 0


google_gate = GoogleGate()


def ask_google(request, timeout):
    """Sends one request to Google through the gate. Server hiccups get one more try."""
    for attempt in range(2):
        with google_gate.busy:
            google_gate.wait_turn()
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.load(response)
                google_gate.answered()
                return data
            except urllib.error.HTTPError as err:
                if err.code == 429:
                    google_gate.refused(retry_after(err))
                    raise
                if err.code not in (500, 502, 503, 504) or attempt == 1:
                    raise
        time.sleep(3)


def google_translate(lines):
    """Returns (English lines, detected language)."""
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t",
        data=urllib.parse.urlencode({"q": "\n".join(lines)}).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    data = ask_google(request, 15)
    text = "".join(segment[0] for segment in data[0] if segment[0])
    return [row.strip() for row in text.split("\n")], data[2]


def translate_rows(rows):
    """Like google_translate, but always returns one English row for each row sent. When Google
    merges lines, each half is asked again, which takes far fewer requests than one per line."""
    english, language = google_translate(rows)
    if language == "en" or len(english) == len(rows):
        return english, language
    if len(rows) == 1:
        return [" ".join(row for row in english if row)], language
    half = len(rows) // 2
    return translate_rows(rows[:half])[0] + translate_rows(rows[half:])[0], language


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
    data = ask_google(request, 10)
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


class LineCache:
    """Every line Google has translated, kept on disk, so a line is never sent twice: not when a song
    plays again, not when its chorus turns up in another song, and not when a song that was cut off
    halfway by a refusal is tried again. Maps a line to [English, the language Google detected]."""
    LIMIT = 50000

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.lines = None

    def _load(self):
        if self.lines is None:
            try:
                self.lines = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                self.lines = {}
        return self.lines

    def get(self, line):
        with self.lock:
            return self._load().get(line)

    def put(self, pairs):
        with self.lock:
            lines = self._load()
            lines.update(pairs)
            for old in list(lines)[:max(0, len(lines) - self.LIMIT)]:   # the oldest go first
                del lines[old]
            self.path.parent.mkdir(exist_ok=True)
            spare = self.path.with_suffix(".tmp")
            spare.write_text(json.dumps(lines, ensure_ascii=False), "utf-8")
            spare.replace(self.path)


google_lines = LineCache(CACHE / "google-lines.json")


def google_known(lines):
    """The English Google has already given for these lines."""
    known = {}
    for line in lines:
        hit = google_lines.get(line)
        if hit and hit[0] and hit[0] != line:
            known[line] = hit[0]
    return known


def translate_lines(lines):
    """Maps each line to English. Empty when the song is already in English. Lines Google has
    translated before come from the cache; only the rest are sent."""
    result = {}
    fresh = []
    cached_english = 0
    for line in lines:
        hit = google_lines.get(line)
        if not hit:
            fresh.append(line)
        elif hit[1] == "en":   # an English line, perhaps from another song: nothing to translate
            cached_english += 1
        elif hit[0] and hit[0] != line:
            result[line] = hit[0]
    if lines and cached_english == len(lines):
        return {}

    chunks, current, size = [], [], 0
    for line in fresh:
        if current and size + len(line) > TRANSLATE_CHUNK:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)

    for chunk in chunks:
        english, language = translate_rows(chunk)
        if language in NATIVE_INPUT:
            english = improve_romanized(chunk, english, language)
        google_lines.put({a: [b, language] for a, b in zip(chunk, english)})
        if language == "en":
            return {}
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


SCRIPT_NAMES = {"DEVANAGARI": "Hindi", "ARABIC": "Urdu", "GURMUKHI": "Punjabi", "ORIYA": "Odia", "HAN": "Chinese", "HANGUL": "Korean"}


def language_hint(lines, track=None):
    """A sentence telling the model which language the song is in, as far as the script shows it.
    Songs written in English letters can be any language, so the model is told to work it out."""
    scripts = [main_script(line) for line in lines]
    scripts = [name for name in scripts if name and name != "LATIN"]
    if len(scripts) >= len(lines) * .5:
        name = max(set(scripts), key=scripts.count)
        if name in ("HIRAGANA", "KATAKANA", "CJK"):
            return "The song is in Japanese or Chinese."
        return f"The song is in {SCRIPT_NAMES.get(name, name.title())}."
    return ("The lyrics may be in a language like Tamil, Hindi or Telugu written in English letters. "
            "Work out which language it is and read each word as that language, not as English.")


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
        f"{language_hint(lines)} Translate every line into plain English, staying close to the words. "
        "Translate what each word actually says; don't guess a meaning from how a word sounds in English. "
        "One entry per line, in order.\n"
        'Reply with only JSON: {"lines": [{"n": 1, "en": "..."}]}'
    )
    with tidbits.writing:   # one job at a time on the local model
        reply = ollama_chat(model, "You translate song lyrics accurately into English.", prompt, 3000, temperature=0.1)
    rows = numbered_lines(reply, len(lines))
    return {lines[n - 1]: text for n, text in rows.items() if text != lines[n - 1]}


def english_for(lines):
    """Tries the translation model on this Mac, then Google, then the local writing model.
    Returns (English, where it came from: "mac", "google" or "local"), or (None, None) if all fail.
    The local writing model is a stand-in: any lines Google translated before it refused are kept."""
    try:
        english = translate_with_nllb(lines)
        if english is not None:
            return english, "mac"
    except Exception as err:
        print(f"on-Mac translation failed: {err}", flush=True)
    try:
        return translate_lines(lines), "google"
    except Exception as err:
        print(f"translation failed: {err}; trying the local model", flush=True)
    known = google_known(lines)
    try:
        return {**translate_locally(lines), **known}, "local"
    except Exception as err:   # no translation is fine: the lyrics still show
        print(f"local translation failed: {err}", flush=True)
        return (known, "local") if known else (None, None)


class TranslationStore:
    """The word-for-word translation of each song. When Google was resting and the local writing
    model stood in, the song is shown with that, marked "source": "local", and Google is asked
    again once its rest is over. Its translation then replaces the stand-in, on disk too."""
    FAILED_RETRY = 45
    UPGRADE_RETRY = 600   # when Google fails for some other reason, try upgrading again after this

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def ensure(self, key, lines):
        saved = CACHE / f"{key}.en.json"
        with self.lock:
            entry = self.entries.get(key)
            if not entry and saved.exists():
                entry = json.loads(saved.read_text("utf-8"))
                if entry.get("source") == "local":
                    entry["retry_at"] = entry.get("better_at", 0)
                self.entries[key] = entry
            if entry and time.time() < entry.get("retry_at", float("inf")):
                return entry
            if entry and entry.get("source") == "local":   # time to ask Google again, while the stand-in shows
                entry["retry_at"] = float("inf")
                target, args = self._upgrade, (key, lines, saved, entry)
            else:
                entry = {"status": "loading", "total": len(lines), "since": time.time()}
                self.entries[key] = entry
                target, args = self._fetch, (key, lines, saved)
        threading.Thread(target=target, args=args, daemon=True).start()
        return entry

    def _save(self, saved, entry):
        CACHE.mkdir(exist_ok=True)
        saved.write_text(json.dumps({k: v for k, v in entry.items() if k != "retry_at"}, ensure_ascii=False), "utf-8")

    def _fetch(self, key, lines, saved):
        english, source = english_for(lines)
        if english is None:
            entry = {"status": "failed", "retry_at": time.time() + self.FAILED_RETRY}
        else:
            entry = {"status": "ready" if english else "none", "lines": english}
            if source == "local":
                entry.update(source="local", better_at=self._next_try())
                entry["retry_at"] = entry["better_at"]
            self._save(saved, entry)   # leaves retry_at out
        with self.lock:
            self.entries[key] = entry

    def _next_try(self):
        return max(google_gate.resting_until, time.time() + 60)

    def _upgrade(self, key, lines, saved, stand_in):
        try:
            english = translate_lines(lines)
        except Exception as err:
            print(f"translation upgrade waits: {err}", flush=True)
            later = google_gate.resting_until if google_gate.resting() else time.time() + self.UPGRADE_RETRY
            with self.lock:
                stand_in["better_at"] = later
                stand_in["retry_at"] = later
            self._save(saved, stand_in)
            return
        entry = {"status": "ready" if english else "none", "lines": english, "source": "google"}
        self._save(saved, entry)
        with self.lock:
            self.entries[key] = entry


WRITING_VERSION = 2   # raise when the prompts change enough that saved writing should be redone


def on_stand_in(literal):
    """True when a word-for-word translation is the local model's stand-in, or is missing because
    every translator failed. Writing built on it is weak and is redone once a proper one arrives."""
    return literal.get("source") == "local" or literal.get("status") == "failed"


def built_before_upgrade(saved, key):
    """For writing saved before it was marked weak or not: True when the song's word-for-word
    translation has since been replaced by Google's, so the writing was built on the stand-in."""
    literal = CACHE / f"{key}.en.json"
    try:
        if json.loads(literal.read_text("utf-8")).get("source") != "google":
            return False
        return literal.stat().st_mtime > saved.stat().st_mtime
    except (OSError, ValueError, AttributeError):
        return False


class WrittenStore:
    """Writing the local model does with the whole song as context, starting from the literal
    translation: natural English for each line, or notes on what lines mean in the song.
    Optional: the literal translation always shows first. While it writes, the entry says how
    far through the song it has got, so the phone can show real progress.
    Writing done from a stand-in translation is marked "weak". Once Google's translation replaces
    the stand-in, it is written again; the weak version keeps showing until the new one is ready."""
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
            if not entry and saved.exists():
                entry = json.loads(saved.read_text("utf-8"))
                if entry.get("version") == WRITING_VERSION or self.name != "natural":
                    if "weak" not in entry and built_before_upgrade(saved, key):
                        entry["weak"] = True
                    self.entries[key] = entry
                else:
                    entry = None
            if entry and not entry.get("weak") and time.time() < entry.get("retry_at", float("inf")):
                return entry
            weak = entry if entry and entry.get("weak") else None
            if weak and (weak.get("rewriting") or time.time() < weak.get("retry_at", 0)):
                return weak
        literal = translations.ensure(key, lines)
        if weak:
            if literal.get("status") == "none":   # Google says the song is English after all
                with self.lock:
                    self.entries.pop(key, None)
                saved.unlink(missing_ok=True)
                return {"status": "none"}
            if literal.get("status") != "ready" or on_stand_in(literal):
                return weak   # still the stand-in: keep showing what was written from it
            with self.lock:
                if weak.get("rewriting"):
                    return weak
                weak["rewriting"] = True
            threading.Thread(target=self._write, args=(key, lines, literal["lines"] or {}, track, saved, False), daemon=True).start()
            return weak
        if literal.get("status") == "none":
            return {"status": "none"}
        if literal.get("status") not in ("ready", "failed"):
            return {"status": "loading"}
        with self.lock:
            if self.entries.get(key, {}).get("status") == "loading":
                return self.entries[key]
            entry = {"status": "loading", "done": 0, "total": len(lines)}
            self.entries[key] = entry
        # a failed literal one just means no hint
        threading.Thread(target=self._write, args=(key, lines, literal.get("lines") or {}, track, saved, on_stand_in(literal)), daemon=True).start()
        return entry

    def _progress(self, key, done):
        with self.lock:
            entry = self.entries.get(key)
            if entry and entry.get("status") == "loading":
                entry["done"] = max(entry.get("done", 0), done)

    def _write(self, key, lines, literal, track, saved, weak):
        with self.lock:
            previous = self.entries.get(key)
        rewriting = previous is not None and previous.get("rewriting")
        model = choose_writer()
        if not model:
            entry = {"status": "unavailable", "retry_at": time.time() + self.UNAVAILABLE_RETRY}
        else:
            try:
                with tidbits.writing:   # one job at a time on the local model
                    written = self.writer(model, lines, literal, track, lambda done: self._progress(key, done))
                entry = {"status": "ready" if written else "none", "lines": written, "version": WRITING_VERSION}
                if weak:
                    entry["weak"] = True
                CACHE.mkdir(exist_ok=True)
                saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
            except Exception as err:
                print(f"{self.name} writing failed: {err}", flush=True)
                entry = {"status": "failed", "retry_at": time.time() + self.FAILED_RETRY}
        with self.lock:
            if rewriting and entry["status"] in ("failed", "unavailable"):   # keep the weak version up, try later
                previous.pop("rewriting", None)
                previous["retry_at"] = entry["retry_at"]
                return
            if self.entries.get(key) is previous or not rewriting:
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
        f"{language_hint(lines)}\n\n"
        "Rewrite each line in natural English that says what it means and how it feels, the way a friend who knows "
        "the language would explain it while the song plays. The word-for-word translation is your guide to what each "
        "line says: smooth it into good English and fix what it gets clumsy, but keep its meaning. Don't swap in a "
        "different idea, change who does what to whom, or turn a sad or angry line into a sweet one. Where there is no "
        "word-for-word translation, translate the words themselves; never guess from how a word sounds in English. "
        "Read the whole song first, so repeated words, idioms and metaphors make sense in context. Keep it short like "
        "a lyric. Keep names and words with no good English equivalent. One entry for every line, in order.\n"
        'Reply with only JSON: {"lines": [{"n": 1, "en": "..."}]}'
    )
    reply = ollama_chat(model, "You translate song lyrics so they make sense to an English speaker. You never add lines or invent meaning that isn't there.", prompt, 3000,
                        on_text=progress and (lambda text: progress(lines_written(text))), temperature=0.2)
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


def song_story(model, lines, english, track):
    """What the song is about, in two or three sentences, and the one line that carries it, with what
    that line means. Written from the Natural translation, so it follows the meaning, not the words."""
    notes = {}
    saved = CACHE / f"{track.get('key', '')}.context.json"
    if track.get("key") and saved.exists():
        notes = json.loads(saved.read_text("utf-8")).get("lines") or {}
    numbered = "\n".join(
        f"{n}. {line}"
        + (f" | means: {english[line]}" if line in english else "")
        + (f" | note: {notes[line]}" if line in notes else "")
        for n, line in enumerate(lines, 1)
    )
    prompt = (
        f"Song: \"{track.get('name', '')}\" by {track.get('artist', '')}.\n\n"
        "Each numbered line is a lyric, with what it means in English where available.\n\n"
        f"{numbered}\n\n"
        "Read the whole song. Then write:\n"
        "- about: two or three short sentences on what the song is about and how it feels, for someone who "
        "doesn't speak the language. Who is singing to whom, what happens, what it comes back to. Plain words, no praise.\n"
        "- key: the number of the one line that carries the song, usually the hook or the line everything turns on.\n"
        "- meaning: one or two sentences on what that line means in the song, beyond its translation.\n"
        "Use only what is in the lyrics. Never invent facts about the singers, the film or the story around it.\n"
        'Reply with only JSON: {"about": "...", "key": 3, "meaning": "..."}'
    )
    reply = ollama_chat(model, "You explain songs to an English speaker the way a friend who grew up with the language would.", prompt, 700)
    start, end = reply.find("{"), reply.rfind("}")
    try:
        found = json.loads(reply[start:end + 1])
    except ValueError:
        return {}
    story = {}
    about = str(found.get("about", "")).strip()
    if 40 <= len(about) <= 600:
        story["about"] = about
    try:
        n = int(found.get("key"))
    except (TypeError, ValueError):
        n = 0
    meaning = str(found.get("meaning", "")).strip()
    if 1 <= n <= len(lines) and 8 <= len(meaning) <= 300:
        line = lines[n - 1]
        story["key"] = {"line": line, "roman": (romanize(line) or "").strip() or None, "english": english.get(line), "meaning": meaning}
    return story


class StoryStore:
    """The opening of the Story tab. It waits for the Natural translation (asking for it if nobody has
    yet), then has the local model write the story from it. English songs go straight to the writing.
    A story written from weak translations is marked "weak" and written again, from the rewritten
    Natural translation, once Google's translation has replaced the stand-in."""
    FAILED_RETRY = 120
    UNAVAILABLE_RETRY = 600

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def ensure(self, key, lines, track):
        saved = CACHE / f"{key}.story.json"
        with self.lock:
            entry = self.entries.get(key)
            if not entry and saved.exists():
                entry = json.loads(saved.read_text("utf-8"))
                if entry.get("version") == WRITING_VERSION:
                    if "weak" not in entry and built_before_upgrade(saved, key):
                        entry["weak"] = True
                    self.entries[key] = entry
                else:
                    entry = None
            if entry and not entry.get("weak") and time.time() < entry.get("retry_at", float("inf")):
                return entry
            weak = entry if entry and entry.get("weak") else None
            if weak and (weak.get("rewriting") or time.time() < weak.get("retry_at", 0)):
                return weak
        literal = translations.ensure(key, lines)
        if literal.get("status") not in ("ready", "failed", "none"):
            return weak or {"status": "loading", "label": "Translating the song"}
        english, from_weak = {}, False
        if literal.get("status") != "none":   # not English, so the story is written from the Natural translation
            if weak and on_stand_in(literal):
                return weak   # Google hasn't replaced the stand-in yet
            natural = naturals.ensure(key, lines, track)
            if weak and (natural.get("weak") or natural.get("status") == "loading"):
                return weak   # the Natural translation is being written again first
            if natural.get("status") == "loading":
                return {"status": "loading", "label": "Working out what each line means", "done": natural.get("done", 0), "total": natural.get("total", len(lines))}
            # Without a Natural translation, the word-for-word one is the next best thing.
            english = natural.get("lines") or literal.get("lines") or {}
            from_weak = on_stand_in(literal) or bool(natural.get("weak")) or natural.get("status") == "failed"
            if not english:
                return weak or {"status": "none"}
        with self.lock:
            current = self.entries.get(key, {})
            if current.get("status") == "loading" or current.get("rewriting"):
                return current
            if weak:
                weak["rewriting"] = True
                entry = weak
            else:
                entry = {"status": "loading", "label": "Writing what the song is about"}
                self.entries[key] = entry
        threading.Thread(target=self._write, args=(key, lines, english, track, saved, from_weak), daemon=True).start()
        return entry

    def _write(self, key, lines, english, track, saved, weak=False):
        with self.lock:
            previous = self.entries.get(key)
        rewriting = previous is not None and previous.get("rewriting")
        model = choose_writer()
        if not model:
            entry = {"status": "unavailable", "retry_at": time.time() + self.UNAVAILABLE_RETRY}
        else:
            try:
                with tidbits.writing:   # one job at a time on the local model
                    story = song_story(model, lines, english, track)
                if not story:
                    raise ValueError("the reply had no usable story")
                entry = {"status": "ready", **story, "version": WRITING_VERSION}
                if weak:
                    entry["weak"] = True
                CACHE.mkdir(exist_ok=True)
                saved.write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
            except Exception as err:
                print(f"story writing failed: {err}", flush=True)
                entry = {"status": "failed", "retry_at": time.time() + self.FAILED_RETRY}
        with self.lock:
            if rewriting and entry["status"] in ("failed", "unavailable"):   # keep the weak story up, try later
                previous.pop("rewriting", None)
                previous["retry_at"] = entry["retry_at"]
                return
            if self.entries.get(key) is previous or not rewriting:
                self.entries[key] = entry


def ollama_chat(model, system, prompt, max_tokens, on_text=None, temperature=0.4):
    """Asks the local model. With on_text, the reply is streamed and on_text sees it grow."""
    body = json.dumps({
        "model": model, "stream": bool(on_text), "think": False, "keep_alive": "30s",
        "options": {"temperature": temperature, "num_predict": max_tokens},
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
# A bigger model writes better translations and stories, at the cost of a busier Mac. Name one to use it
# instead, for example DESK_MUSIC_WRITER=qwen3.5:27b. If it isn't installed, the small ones are used.
WRITER_CHOICE = os.environ.get("DESK_MUSIC_WRITER", "").strip()
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
    choices = ((WRITER_CHOICE,) if WRITER_CHOICE else ()) + WRITERS
    return next((name for name in choices if name in installed or f"{name}:latest" in installed), None)


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
            command = system.audio_levels_command()
            if not command:
                self._publish(json.dumps({"error": system.LEVELS_MISSING}).encode("utf-8"))
                time.sleep(10)
                continue
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
stories = StoryStore()
tidbits = TidbitStore()
timing = TimingStore(TIMING)
tapped = TappedStore(TAPPED)
shares = ShareStore(SHARED)


def restart_server():
    """Starts this server again in the same process, so it picks up new code at once."""
    print("Restarting on the new version", flush=True)
    now_playing.stop()
    audio_levels.stop()
    os.execv(sys.executable, [sys.executable, str(ROOT / "server.py"), *sys.argv[1:]])


# Pulls new versions of main from GitHub and restarts on them. updates.json keeps the on/off
# switch; installed-version is the commit install.sh last ran on, to tell when it must run again.
updates = updater.Updater(
    ROOT, ROOT / "updates.json", ROOT / "installed-version", restart=restart_server,
    busy=lambda: current_player()["state"] == "playing",
)


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
        "offset": timing.get(key),
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


CONTROL_ACTIONS = ("toggle", "next", "previous")
SAFARI_ACTIONS = {
    "toggle": "(()=>{const v=document.querySelector('video');if(v)v.paused?v.play():v.pause()})()",
    "next": "document.querySelector('.ytp-next-button, .next-button')?.click()",
    "previous": (
        "(()=>{const v=document.querySelector('video');"
        "const b=document.querySelector('.ytp-prev-button, .previous-button');"
        "if(v&&(v.currentTime>3||!b||!b.offsetParent))v.currentTime=0;else if(b)b.click()})()"
    ),
}


def control(action):
    """Sends a command to whichever player is on screen. Returns True if it went through."""
    player = current_player()
    if player["state"] in ("stopped", "unavailable"):
        return False
    if player.get("source") != "safari":
        return system.send_media_command(action)
    if not player["timed"]:
        return False   # Safari's Apple Events setting is off, so the tab can't be reached
    sent = system.run_in_safari_tab(player["url"], SAFARI_ACTIONS[action])
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

    def read_json(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 < length <= limit:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if self.headers.get("X-Desk-Lyrics") != "1":   # stops other websites from sending commands
            self.send(b"Forbidden", "text/plain", 403)
        elif url.path == "/api/control" and query.get("action", [""])[0] in CONTROL_ACTIONS:
            self.send_json({"ok": control(query["action"][0])})
        elif url.path == "/api/timing":
            key = query.get("key", [""])[0]
            value = nudge_value(query.get("offset", [""])[0])
            if not song_key(key) or value is None:
                self.send_json({"ok": False}, 400)
            else:
                timing.set(key, value)
                self.send_json({"ok": True, "offset": value})
        elif url.path == "/api/tapped":
            key = query.get("key", [""])[0]
            body = self.read_json(limit=512 * 1024)
            lines = body.get("lines") if isinstance(body, dict) else None
            if not song_key(key) or not isinstance(lines, list) or not save_tapped(key, lines):
                self.send_json({"ok": False}, 400)
            else:
                self.send_json({"ok": True})
        elif url.path == "/api/share":
            # Publishes this song's tapped timing on LRCLIB. Only ever sent by someone pressing Share.
            key = query.get("key", [""])[0]
            if not song_key(key) or not shares.start(key):
                self.send_json({"ok": False}, 400)
            else:
                self.send_json({"ok": True, **shares.status(key)})
        elif url.path == "/api/updates":
            auto = query.get("auto", [""])[0]
            if auto in ("0", "1"):
                updates.set_auto(auto == "1")
            if query.get("check", [""])[0] == "1":
                updates.check_now()
            self.send_json(updates.status())
        elif url.path == "/api/timing/import":
            # Phones from before nudges lived on the Mac hand over what they remembered, once.
            body = self.read_json()
            if not isinstance(body, dict):
                self.send_json({"ok": False}, 400)
            else:
                offsets = {k: nudge_value(v) for k, v in body.items() if song_key(k)}
                self.send_json({"ok": True, "added": timing.adopt({k: v for k, v in offsets.items() if v})})
        else:
            self.send(b"Not found", "text/plain", 404)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)

        if url.path == "/":
            self.send((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            # Which page the Mac is serving, so a phone left open for days can tell it should reload.
            self.send_json({**build_state(), "page": str((ROOT / "index.html").stat().st_mtime_ns)})
        elif url.path == "/api/updates":
            self.send_json(updates.status())
        elif url.path == "/api/lyrics":
            entry = lyrics_store.get(query.get("key", [""])[0])
            if entry:
                body = {k: v for k, v in entry.items() if k != "retry_at"}
                body["roman"] = {text: romanize(text) for text in lyric_texts(entry) if not_latin(text)}
                self.send_json(body)
            else:
                self.send_json({"status": "missing"}, 404)
        elif url.path == "/api/share":
            key = query.get("key", [""])[0]
            self.send_json(shares.status(key) if song_key(key) else {"status": "unavailable"})
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
        elif url.path == "/api/story":
            key = query.get("key", [""])[0]
            entry = lyrics_store.get(key)
            lines = lyric_texts(entry) if entry else []
            track = build_state().get("track") or {}
            if lines and track.get("key") == key:
                result = stories.ensure(key, lines, track)
                self.send_json({k: v for k, v in result.items() if k != "retry_at"})
            elif track.get("key") != key or (entry and entry.get("status") != "loading"):
                self.send_json({"status": "none"})
            else:   # lyrics are still on their way
                self.send_json({"status": "loading", "label": "Finding the lyrics"})
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
    updates.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Desk Music Companion is running. Open on your phone: http://{lan_address()}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
