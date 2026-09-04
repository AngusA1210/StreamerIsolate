# StreamerIsolate

Removes background music from a livestream's audio in near-real-time, keeping
only detected speech — so you can play your own music underneath a stream
(Twitch, etc.) without the two fighting.

There are two ways to use it, and which one you want depends on your browser:

- **Chrome extension** (recommended if you're on Chrome): true interception —
  the stream tab goes silent and only the processed speech plays back. No
  virtual audio device, no system output changes.
- **Standalone app + virtual audio device** (works with Firefox, or any app):
  more setup, but works everywhere. This is the fallback for Firefox, which
  currently has no extension API for true tab-audio interception (see
  "Why two paths" below).

Both use the same Demucs backend under the hood.

## How separation works

Captured audio is processed in overlapping chunks (default 3s, 0.75s overlap)
through a pretrained [Demucs](https://github.com/facebookresearch/demucs)
model, which separates the mix into stems (drums/bass/other/vocals). Only the
`vocals` stem is kept — this is the v1 approximation of "speech, not music,"
not per-instrument removal, so a song's *sung* vocals can still bleed through
(Demucs doesn't distinguish singing from talking). The isolated speech is
crossfaded across chunk boundaries. This introduces a delay of roughly one
chunk length between the live stream and the processed speech, and currently
the video isn't delayed to match — both are known v1 limitations, not bugs.

## Install (backend, needed for both paths)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires Python 3.10+. A dedicated venv using Homebrew's `python@3.12` is
recommended (the system `/usr/bin/python3` on macOS is Python 3.9, which is
too old for current PyTorch/Demucs).

## Path 1: Chrome extension (recommended)

1. Start the local bridge server (loads the Demucs model once, then waits for
   the extension to connect):

   ```bash
   source .venv/bin/activate
   streamerisolate serve
   ```

2. Load the extension: open `chrome://extensions`, enable Developer mode
   (top right), click "Load unpacked," and select the `extension-chrome/`
   folder in this repo.
3. Open your Twitch stream in a tab, then click the StreamerIsolate icon in
   the toolbar. The tab should go silent, and after roughly a chunk-length
   delay, isolated speech should start playing. Click the icon again to stop
   (this restores the tab's normal audio).

**Debugging:** if nothing plays, check `chrome://extensions` → StreamerIsolate
→ "Inspect views: offscreen.html" for console errors (most likely cause:
`streamerisolate serve` isn't running, or isn't on port 8765).

### Why two paths

The first version of this used [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
(`native/capture-app-audio/`) to capture a specific app's audio on macOS
without touching system output. That turned out to be a dead end for this use
case: ScreenCaptureKit only gives you a *copy* of audio that's already going
to be played — it can't silence the original, so you'd always hear the
unprocessed original layered with the processed result. True interception
(silencing the source and substituting processed audio) needs the browser's
own `tabCapture` API, which mutes the tab the moment you start capturing.
Chrome has this (`chrome.tabCapture`); Firefox currently doesn't (tracked in
[Mozilla bug 1443484](https://bugzilla.mozilla.org/show_bug.cgi?id=1443484),
open/unresolved). Hence: a Chrome extension for true interception, and the
standalone-app path below as the option that still works on Firefox.

## Path 2: Standalone app + virtual audio device (Firefox, or any app)

To feed a stream's audio into the app, you need a virtual audio device that
your browser can output to, which this app can then read as an "input."
**Install this yourself** (it requires approving a system audio driver in
macOS Security settings, which isn't something to script):

- macOS: [BlackHole](https://github.com/ExistentialAudio/BlackHole)
  (`brew install blackhole-2ch`, then approve it in System Settings >
  Privacy & Security if prompted, and reboot/re-login if the device doesn't
  show up). If you already do audio engineering work with Pro Tools, its
  "Pro Tools Audio Bridge" virtual devices work the same way — no need to
  install BlackHole too.
- Route your browser/Twitch tab's audio output to the virtual device (this
  generally means setting it as your *system* output while the stream plays,
  since macOS has no built-in per-app output routing).
- Point `--output` at your real speakers/interface so you actually hear the
  cleaned result.

```bash
source .venv/bin/activate

# find your virtual input device and real output device
streamerisolate list-devices

# run the pipeline
streamerisolate run --input "BlackHole" --output "MacBook Pro Speakers"
```

Options for `run`:

- `--model` — Demucs model name (default `htdemucs`)
- `--chunk-seconds` / `--overlap-seconds` — processing window size and
  crossfade overlap (default 3s / 0.75s)
- `--gain` — output gain applied to the isolated speech
- `--capture-app` — macOS-only alternative to `--input` using ScreenCaptureKit
  (see `native/capture-app-audio/`). Kept for reference, but as explained
  above it can't silence the original source, so it adds a copy of the
  isolated speech rather than replacing the stream's audio. Not recommended
  for the actual use case; the Chrome extension is the real fix for that.

## Status / roadmap

- [x] Standalone Python pipeline: virtual-device capture -> Demucs separate -> playback
- [x] Local WebSocket bridge server (`streamerisolate serve`) for the extension
- [x] Chrome extension: true tab-audio interception via `chrome.tabCapture` + offscreen document
- [x] Reduced default chunk size (6s -> 3s) to shrink the audio/video gap, as
      a cheap partial mitigation
- [ ] Firefox extension (blocked on Mozilla shipping a tabCapture equivalent)
- [ ] Audio/video sync, properly: **not** as simple as delaying the tab's
      video element -- Twitch's video element is the same clock `tabCapture`
      taps for audio, so pausing/slowing it to hold video back also
      pauses/distorts the audio our pipeline depends on. The real fix would
      be a canvas overlay that buffers and redraws delayed video frames while
      the original video element keeps playing undisturbed underneath (so
      audio capture stays clean) -- not yet built, deferred in favor of the
      chunk-size reduction above.
- [ ] Reduce song-vocal bleed-through (e.g. a speech-vs-singing classifier on the vocals stem)
- [ ] Config for saving preferred devices/settings
