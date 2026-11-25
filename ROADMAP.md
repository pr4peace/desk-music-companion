# Roadmap

What's being worked on, what's next, and good things to pick up. If you take something, say so in an issue so two people don't build the same thing.

## Now

Nothing is in progress. Pick something from Next.

## Done

- **A full UI and UX audit, and the fixes it found** (7ce20c2): readable lines from the desk, VoiceOver language tags and labels, 44 px targets, landscape controls beside the lyric, one-time gesture hints, plain-words timing, Tidbits paging, and lighter per-frame drawing.
- **The Mac-only parts of the server behind small functions.** `macos.py` holds now playing, the controls, Safari's tabs, the audio level helper and transliteration; `server.py` only calls them. The first step toward Windows (see below).
- **Automated tests** for lyric matching, transliteration, translation and the Mac functions, in `tests/`. Run `python3 -m unittest` from the project folder. They need no Mac, network or models.

## Next

1. **Story tab, with meaning.** Rename Tidbits to Story. Open it with what the song is about, in two or three sentences written from the Natural translation, then the key line and what it means, then the tidbits.
2. **Show people what's possible.** One-time glass hints where they apply ("Double-tap for controls", "Swipe for more styles", "Try Natural"), plus a small "?" in the controls that opens a one-screen guide. Each hint goes away for good once the feature has been used.
3. **The phone finds the Mac by itself.** No address to type: the server announces itself on the network and the page finds it.

## Later

4. **Exact word timing with Whisper.** Lyric sites only time whole lines, so the word highlight is an estimate. Listening to the song with Whisper (on the Mac, after separating the vocals) could time each word.
5. **An idle screen worth looking at.** When nothing plays, a slow wall of recently played album art, or a clock in the last album's colours.
6. **More visual styles.** See the ideas below.
7. **More from the phone.** Volume, a look at what's up next, "play more like this".
8. **Windows support.** See below.

## Visualizer ideas

The Visualizer has Rings, Kolam, Words, Diyas and Vinyl. Candidates for more, all in the album's colours:

- **Meaning, big.** The Natural English of each line fills the screen as it's sung, with the original small beneath it.
- **A picture per line.** Each line's meaning becomes an image behind the words, made ahead on the Mac by a local image model, like tidbits are.
- **Album mosaic.** The cover breaks into tiles that lift and ripple with the beat, then snap back into the whole cover on big moments.
- **Ridgelines.** The sound as rolling hills scrolling toward you, one ridge per moment, with the line floating above the horizon.
- **Constellation.** Each sung word leaves a star, so by the end the whole song hangs there as a sky.
- **Rain on glass.** The cover blurred behind wet glass; beats drop rain and the singing wipes a clear streak.
- **Rangoli bloom.** A radial, eight-way symmetric flower that opens a ring of petals on each beat and closes between verses.
- **Ink.** The waveform drawn as brush strokes that bleed and dry, like calligraphy on paper.
- **Garden.** Each line grows a plant; by the end of the song it's a garden, a different one every play.
- **Duets in two colours.** Tell the two voices apart by pitch and colour each singer's lines differently, in any style.
- **Chorus moments.** Spot the repeated lines and give the chorus a bigger moment in whichever style is on.
- **Pick by mood.** Choose the style for each song from its tempo and energy.

## Windows support

The phone side already works anywhere: it's a web page that can be added to the Home Screen. The Mac server is what needs a Windows version. Each Mac-only part has a Windows replacement:

| Job | On the Mac today | On Windows |
|---|---|---|
| What's playing, position, album art | `media-control` (Apple's MediaRemote) and Safari's tabs | System Media Transport Controls, which apps like Spotify, Chrome, Edge and Apple Music for Windows report to. The `winrt-Windows.Media.Control` Python packages read them, and this covers YouTube in the browser without any per-browser code. |
| Play, pause, next, previous | `media-control` and Safari JavaScript | The same Transport Controls session. |
| Hearing the audio for the visualizer | `visualizer/audio_levels.swift`, a Core Audio tap | WASAPI loopback capture, for example with the `soundcard` or `PyAudioWPatch` Python packages. It must print the same JSON lines (`b`, `w`, `v`) so the page doesn't change. |
| Transliteration | macOS's ICU through CoreFoundation (`to_latin` in `macos.py`) | ICU through PyICU, or Windows' own `icu.dll`. |
| Starting at login | LaunchAgent in `install.sh` | Task Scheduler or the Startup folder, from an `install.ps1`. |
| Menu bar helper | `mac/DeskLyricsMenu.swift` | A system tray icon, for example with `pystray`. |
| Phone finding the computer | `Your-Mac.local` | The PC's IP address from `ipconfig`, and allowing Python through Windows Firewall on private networks. |

Lyrics, translation, NLLB, the local model through Ollama, tidbits, and the whole phone interface carry over as they are.

The Mac-only parts of the server are now behind small functions in `macos.py`: `now_playing_command`, `send_media_command`, `safari_is_audible`, `read_safari_tabs`, `run_in_safari_tab`, `audio_levels_command` and `to_latin`. `server.py` picks that module on a Mac and `other_systems.py` elsewhere, which has stand-ins so the server loads and its tests run. The way in is a `windows.py` with the same functions, filled in one row of the table at a time, with `server.py` choosing it on Windows. `tests/test_system.py` checks that every system module offers the same functions.
