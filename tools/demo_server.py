"""Serves the phone page with a made-up song, so you can work on the interface or take screenshots
without a Mac playing music, and without showing anyone's real lyrics or album art.

    python3 tools/demo_server.py            then open http://localhost:8790 on the Mac or phone

The song, its words, translations, cover and tidbits are all invented for this demo. The music the
visualizer hears is made up too: a steady beat, with a voice whenever a line is being sung.

    /demo/seek?t=42     jumps the song to 42 seconds
    /demo/tidbits?status=loading|ready   shows the tidbits loading or ready
"""
import json
import math
import random
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8790
KEY = "demo-chai-ki-pyaali"
DURATION = 158.0

# (start time, original, in English letters, word for word, written for meaning)
SONG = [
    (8.0, "सुबह की धूप खिड़की पे आई", "subah ki dhoop khidki pe aayi", "Morning's sunlight came to the window", "The morning sun has found my window"),
    (12.5, "चाय की प्याली हाथों में", "chai ki pyaali haathon mein", "Cup of tea in the hands", "A cup of tea, warm in my hands"),
    (17.0, "धीरे धीरे गाना बजे", "dheere dheere gaana baje", "Slowly slowly the song plays", "A song begins, soft and slow"),
    (21.5, "मेज़ पे बैठा छोटा सा फ़ोन", "mez pe baitha chhota sa phone", "On the desk sits a small phone", "A little phone keeps me company on the desk"),
    (26.0, "हर एक शब्द चमकता है", "har ek shabd chamakta hai", "Every single word shines", "and every word lights up as it's sung"),
    (31.0, "", "", "", ""),
    (38.0, "गाओ मेरे साथ", "gaao mere saath", "Sing with me", "Sing along with me"),
    (41.5, "ताल पे नाचे रंग", "taal pe naache rang", "On the beat dance the colours", "the colours dance to the beat"),
    (45.0, "गाओ मेरे साथ", "gaao mere saath", "Sing with me", "Sing along with me"),
    (48.5, "दिल में बजे मृदंग", "dil mein baje mridang", "In the heart plays a mridang", "my heart keeps time like a drum"),
    (53.0, "", "", "", ""),
    (60.0, "शाम ढले तो भी ये गीत", "shaam dhale to bhi ye geet", "Even when evening falls this song", "Even as the evening fades"),
    (64.5, "मेज़ पे जलता रहे", "mez pe jalta rahe", "On the desk keeps burning", "this song stays alight on my desk"),
    (69.0, "गाओ मेरे साथ", "gaao mere saath", "Sing with me", "Sing along with me"),
    (72.5, "ताल पे नाचे रंग", "taal pe naache rang", "On the beat dance the colours", "the colours dance to the beat"),
    (76.0, "गाओ मेरे साथ", "gaao mere saath", "Sing with me", "Sing along with me"),
    (79.5, "दिल में बजे मृदंग", "dil mein baje mridang", "In the heart plays a mridang", "my heart keeps time like a drum"),
    (84.0, "", "", "", ""),
]

TIDBITS = [
    {"kind": "fact", "headline": "Written for a desk",
     "text": "This song doesn't exist. It was written for these screenshots, so nobody's real lyrics end up in a README.",
     "source": "a made-up page for this demo"},
    {"kind": "funny", "headline": "The band has never toured",
     "text": "The Demo Band has played to exactly one audience: a phone propped against a monitor.",
     "source": "a made-up page for this demo"},
    {"kind": "fact", "headline": "Tea, sunlight, a drum",
     "text": "A mridang is a South Indian drum. Here it stands in for a heartbeat keeping time.",
     "source": "a made-up page for this demo"},
    {"kind": "fact", "headline": "Real songs get real facts",
     "text": "With music playing on the Mac, this tab researches the song, film and artist on Wikipedia in the background.",
     "source": "a made-up page for this demo"},
]

COVER = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#16324a"/><stop offset=".55" stop-color="#b8543a"/><stop offset="1" stop-color="#f2b05e"/>
    </linearGradient>
    <radialGradient id="sun" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="#fff2c9"/><stop offset=".6" stop-color="#ffd27a"/><stop offset="1" stop-color="#f29a4a" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="600" height="600" fill="url(#sky)"/>
  <circle cx="300" cy="360" r="170" fill="url(#sun)"/>
  <path d="M0 430 C 120 400 200 470 300 440 S 480 400 600 430 V600 H0Z" fill="#7a2f2a" opacity=".85"/>
  <path d="M0 480 C 140 455 230 515 330 488 S 500 455 600 482 V600 H0Z" fill="#3d1a24"/>
  <g fill="none" stroke="#fff2c9" stroke-width="3" opacity=".55">
    <path d="M250 250 q 12 -24 0 -48 q -12 -24 0 -48"/><path d="M300 240 q 12 -24 0 -48 q -12 -24 0 -48"/><path d="M350 250 q 12 -24 0 -48 q -12 -24 0 -48"/>
  </g>
</svg>"""


class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time() - 20.0
        self.paused_at = None

    def position(self):
        with self.lock:
            now = self.paused_at or time.time()
            return (now - self.started) % DURATION

    def seek(self, seconds):
        with self.lock:
            self.started = (self.paused_at or time.time()) - seconds

    def toggle(self):
        with self.lock:
            if self.paused_at:
                self.started += time.time() - self.paused_at
                self.paused_at = None
            else:
                self.paused_at = time.time()


player = Player()
tidbit_status = {"value": "ready"}


def singing(position):
    for i, (start, text, *_ignored) in enumerate(SONG):
        end = SONG[i + 1][0] if i + 1 < len(SONG) else DURATION
        if start <= position < end:
            return bool(text) and position < end - .6
    return False


def levels_frame(position):
    """A made-up mix: a kick on every beat at 96 bpm, a shimmer on top, and a voice while a line is sung."""
    beat = (position * 96 / 60) % 1
    kick = math.exp(-beat * 7)
    voice = singing(position)
    bands = []
    for i in range(32):
        low = kick * 255 * max(0, 1 - i / 7)
        mid = (150 + 60 * math.sin(position * 3 + i)) * (1 if voice and 6 <= i <= 18 else .25)
        high = 60 * random.random() * (i / 31)
        bands.append(int(min(255, low + mid * .7 + high + 20)))
    wave = [int(127.5 + 110 * (kick * .6 + .3) * math.sin(position * 40 + n / 96 * math.pi * 6) * math.sin(n / 96 * math.pi)) for n in range(96)]
    vocal = int(170 + 60 * math.sin(position * 5)) if voice else 0
    return {"b": bands, "w": wave, "v": vocal}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content_type, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, value):
        self.send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            self.send((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self.send_json({
                "state": "paused" if player.paused_at else "playing",
                "position": player.position(), "age": 0, "lyrics": "synced", "art": "demo",
                "track": {"key": KEY, "name": "Chai Ki Pyaali", "artist": "The Demo Band", "album": "Desk Songs", "duration": DURATION},
            })
        elif url.path == "/api/lyrics":
            self.send_json({
                "status": "synced",
                "lines": [{"t": start, "text": text} for start, text, *_ignored in SONG],
                "roman": {text: roman for _start, text, roman, *_ignored in SONG if text},
            })
        elif url.path == "/api/translation":
            column = 4 if query.get("style") == ["natural"] else 3
            self.send_json({"status": "ready", "lines": {row[1]: row[column] for row in SONG if row[1]}})
        elif url.path == "/api/tidbits":
            if tidbit_status["value"] == "loading":
                self.send_json({"status": "loading", "step": 2, "steps": 4, "label": "Reading about the song…"})
            else:
                self.send_json({"status": "ready", "items": TIDBITS})
        elif url.path == "/api/levels":
            self.stream_levels()
        elif url.path == "/art":
            self.send(COVER.encode(), "image/svg+xml")
        elif url.path == "/demo/seek":
            player.seek(float(query.get("t", ["0"])[0]))
            self.send_json({"position": player.position()})
        elif url.path == "/demo/tidbits":
            tidbit_status["value"] = query.get("status", ["ready"])[0]
            self.send_json(tidbit_status)
        elif url.path in ("/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"):
            self.send((ROOT / "assets" / url.path.lstrip("/")).read_bytes(), "image/png")
        else:
            self.send(b"Not found", "text/plain", 404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        action = urllib.parse.parse_qs(url.query).get("action", [""])[0]
        if action == "toggle":
            player.toggle()
        elif action in ("next", "previous"):
            player.seek(0)
        self.send_json({"ok": True})

    def stream_levels(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                frame = {"b": [0] * 32, "w": [128] * 96, "v": 0} if player.paused_at else levels_frame(player.position())
                self.wfile.write(b"data: " + json.dumps(frame).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(1 / 30)
        except OSError:
            pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"Demo song on http://localhost:{port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
