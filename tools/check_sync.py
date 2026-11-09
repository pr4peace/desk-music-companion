#!/usr/bin/env python3
"""Checks whether the lyrics line up with the song playing now. Prints no lyrics.

It compares when the lyric lines say singing happens with when the Mac actually
hears a voice, for about 30 seconds. Run it during a part of the song with singing:

    python3 tools/check_sync.py
"""
import json
import subprocess
import sys
import time
import urllib.request

SERVER = "http://127.0.0.1:8787"
LISTEN_SECONDS = 30


def get(path):
    with urllib.request.urlopen(SERVER + path, timeout=5) as response:
        return json.load(response)


def music_app_position():
    script = 'tell application "Music" to if player state is playing then return player position'
    try:
        output = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5).stdout.strip()
        return float(output) if output else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def listen(seconds, start_position):
    began = time.time()
    samples = []
    with urllib.request.urlopen(SERVER + "/api/levels", timeout=seconds + 10) as stream:
        while time.time() - began < seconds:
            line = stream.readline().decode().strip()
            if line.startswith("data:"):
                frame = json.loads(line[5:])
                if "v" in frame:
                    samples.append((start_position + time.time() - began, frame["v"]))
    return samples


def correlation(samples, starts, shift):
    ends = starts[1:] + [starts[-1] + 5]
    sung = [int(any(a <= t + shift < a + min(b - a, 5) for a, b in zip(starts, ends))) for t, _ in samples]
    mean_sung = sum(sung) / len(sung)
    if mean_sung in (0, 1):
        return 0.0
    mean_voice = sum(v for _, v in samples) / len(samples)
    top = sum((s - mean_sung) * (v - mean_voice) for s, (_, v) in zip(sung, samples))
    bottom = (sum((s - mean_sung) ** 2 for s in sung) * sum((v - mean_voice) ** 2 for _, v in samples)) ** 0.5
    return top / bottom if bottom else 0.0


def main():
    state = get("/api/state")
    if state.get("state") != "playing":
        sys.exit("Nothing is playing.")
    track = state["track"]
    print(f"Song:    {track['name']} by {track['artist']}")
    if state["lyrics"] != "synced":
        sys.exit(f"Lyrics:  {state['lyrics']}, so there is no line timing to check.")

    lines = [line for line in get(f"/api/lyrics?key={track['key']}")["lines"] if line.get("text")]
    starts = [line["t"] for line in lines]
    length = track["duration"]
    print(f"Lyrics:  {len(lines)} lines, last one at {starts[-1]:.0f}s of a {length:.0f}s song")
    if length and starts[-1] > length + 3:
        print("Result:  WRONG VERSION. The lyrics run past the end of the song.")
        return

    music = music_app_position()
    if music is not None:
        print(f"Clock:   Music says {music:.1f}s, the server says {get('/api/state')['position']:.1f}s")

    state = get("/api/state")
    seconds = min(LISTEN_SECONDS, max(0, (length or 1e9) - state["position"] - 2))
    print(f"Listening for {seconds:.0f} seconds...")
    samples = listen(seconds, state["position"])
    if get("/api/state").get("track", {}).get("key") != track["key"]:
        sys.exit("Result:  The song changed while listening. Try again.")
    heard = sum(samples[0][0] <= t <= samples[-1][0] for t in starts) if samples else 0
    if heard < 4:
        print(f"Result:  CAN'T TELL. Only {heard} lyric lines passed. Try during a part with singing.")
        return

    scores = [(i / 2, correlation(samples, starts, i / 2)) for i in range(-24, 25)]
    best_shift, best = max(scores, key=lambda pair: pair[1])
    here = dict(scores)[0.0]
    print(f"Match:   {here:.2f} at the current timing; best {best:.2f} at {best_shift:+.1f}s")
    # A song with a known good timing scores about 0.4, and repeated lines can give weak false peaks.
    if abs(best_shift) <= 1 and best >= 0.25:
        print("Result:  IN SYNC.")
    elif best < 0.35 or best - here < 0.15:
        print("Result:  CAN'T TELL. Trust your ears here, or run it again during another part of the song.")
    else:
        direction = "late" if best_shift > 0 else "early"
        print(f"Result:  OFF by about {abs(best_shift):.1f}s; the lyrics look {direction}. Double-tap and use − or + to nudge.")


if __name__ == "__main__":
    main()
