# Roadmap

What's being worked on, what's next, and good things to pick up. If you take something, say so in an issue so two people don't build the same thing.

## Now

- **Better translation for Indian languages.** Try AI4Bharat's IndicTrans2 next to NLLB on the same songs and keep whichever reads better. It needs a free Hugging Face login on the Mac (`translate/.venv/bin/hf auth login`) and accepting the model's terms.

## Next

1. **Story tab, with meaning.** Rename Tidbits to Story. Open it with what the song is about, in two or three sentences written from the Natural translation, then the key line and what it means, then the tidbits.
2. **Show people what's possible.** One-time glass hints where they apply ("Double-tap for controls", "Swipe for more styles", "Try Natural"), plus a small "?" in the controls that opens a one-screen guide. Each hint goes away for good once the feature has been used.
3. **The phone finds the Mac by itself.** No address to type: the server announces itself on the network and the page finds it.
4. **Timing fixes saved on the Mac.** The − / + lyric nudge is remembered per phone today. Keep it on the Mac so every screen gets it and it survives reinstalls.

## Later

5. **Exact word timing with Whisper.** Lyric sites only time whole lines, so the word highlight is an estimate. Listening to the song with Whisper (on the Mac, after separating the vocals) could time each word.
6. **An idle screen worth looking at.** When nothing plays, a slow wall of recently played album art, or a clock in the last album's colours.
7. **More visual styles.** Album-art mosaic, ridgelines, constellation, rain on glass, and picking a style automatically by the song's mood.
8. **More from the phone.** Volume, a look at what's up next, "play more like this".
9. **Windows support.** See below.

## Windows support

The phone side already works anywhere: it's a web page that can be added to the Home Screen. The Mac server is what needs a Windows version. Each Mac-only part has a Windows replacement:

| Job | On the Mac today | On Windows |
|---|---|---|
| What's playing, position, album art | `media-control` (Apple's MediaRemote) and Safari's tabs | System Media Transport Controls, which apps like Spotify, Chrome, Edge and Apple Music for Windows report to. The `winrt-Windows.Media.Control` Python packages read them, and this covers YouTube in the browser without any per-browser code. |
| Play, pause, next, previous | `media-control` and Safari JavaScript | The same Transport Controls session. |
| Hearing the audio for the visualizer | `visualizer/audio_levels.swift`, a Core Audio tap | WASAPI loopback capture, for example with the `soundcard` or `PyAudioWPatch` Python packages. It must print the same JSON lines (`b`, `w`, `v`) so the page doesn't change. |
| Transliteration | macOS's ICU through CoreFoundation (`to_latin` in `server.py`) | ICU through PyICU, or Windows' own `icu.dll`. |
| Starting at login | LaunchAgent in `install.sh` | Task Scheduler or the Startup folder, from an `install.ps1`. |
| Menu bar helper | `mac/DeskLyricsMenu.swift` | A system tray icon, for example with `pystray`. |
| Phone finding the computer | `Your-Mac.local` | The PC's IP address from `ipconfig`, and allowing Python through Windows Firewall on private networks. |

Lyrics, translation, NLLB, the local model through Ollama, tidbits, and the whole phone interface carry over as they are.

A sensible way in: put the Mac-only parts of `server.py` behind small functions (now playing, controls, audio levels, transliteration), then add Windows versions beside them, one row of the table at a time.
