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
not per-instrument removal, so a song's *sung* vocals could still bleed
through (Demucs doesn't distinguish singing from talking). To reduce that, a
second pretrained model ([PANNs](https://github.com/qiuqiangkong/panns_inference),
trained on AudioSet) runs over the isolated vocals and attenuates stretches it
classifies as singing rather than speech — a soft, confidence-weighted gate,
not a hard cut, and not a perfect fix (expressive speech can still read as
singing-ish, and quiet singing can still read as speech-ish). Disable it with
`--no-vocal-classifier` (`run`) or when starting `serve`, if you'd rather
compare with it off, or want to skip its ~330MB checkpoint download. The
isolated speech is then crossfaded across chunk boundaries. This introduces a
delay of roughly one chunk length between the live stream and the processed
speech, and currently the video isn't delayed to match — both are known v1
limitations, not bugs (see "Status / roadmap").

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
   delay, isolated speech should start playing; the video should also visibly
   hold back to roughly match (see "Video sync" below). Click the icon again
   to stop (this restores the tab's normal audio and video).

**Updating after a code change:** the Python backend (`streamerisolate
serve`) does **not** hot-reload -- stop it (Ctrl+C) and start it again after
pulling/editing backend code. For the extension, click the reload icon on its
card in `chrome://extensions` (more reliable than repeating "Load unpacked").
The version number in the manifest is a quick sanity check that you're
looking at the build you expect.

**Debugging:** if nothing plays, check `chrome://extensions` → StreamerIsolate
→ "Inspect views: offscreen.html" for console errors (most likely cause:
`streamerisolate serve` isn't running, or isn't on port 8765). If audio works
but the video overlay doesn't appear, check the *page's* own console (F12 on
the Twitch tab, not the offscreen document) for `[StreamerIsolate]` warnings.

### Video sync

The tab's video isn't touched directly -- it keeps playing at full speed,
because `chrome.tabCapture` taps that same clock for audio, and holding video
back directly (pausing/slowing it) would also disrupt the audio feeding
Demucs. Instead, `content.js` is injected into the tab and buffers the
video's frames (via `requestVideoFrameCallback`, downscaled and captured at a
modest rate to bound memory), then draws a deliberately-delayed copy of them
on a transparent canvas positioned exactly over the real video -- so what you
*see* is delayed to roughly match the processed audio, without the real video
element ever being disturbed. The delay target is a fixed estimate
(`TARGET_DELAY_SECONDS` in `extension-chrome/config.js`, currently 4s to
cover the 3s chunk plus processing/network slack), not measured per-session,
so it's an approximation, not a sample-accurate sync. The core buffer/redraw
mechanism was verified against a synthetic timestamped video (measured delay
landed within ~0.1s of the 3s target in that test); the real-Twitch-page
integration (frame capture on the actual player, overlay positioning against
Twitch's DOM) needs your live test.

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
- [x] Reduce song-vocal bleed-through: PANNs speech-vs-singing classifier
      gating the vocals stem (built, integration-tested; user-tested live and
      confirmed it attenuates, but still struggles to cleanly separate the
      streamer's speech from a song's vocals, and the attenuation fluctuates
      noticeably -- likely because the classifier runs independently per
      chunk with no smoothing across chunk boundaries or window-to-window;
      revisit if this keeps being an issue)
- [x] Audio/video sync via a canvas overlay (`extension-chrome/content.js`):
      buffers and redraws delayed video frames on top of the real video,
      which keeps playing undisturbed underneath so audio capture stays
      clean. The buffer/redraw mechanism itself was verified against a
      synthetic timestamped video (measured delay within ~0.1s of target);
      the real-Twitch-page integration needs a live test. Delay target is a
      fixed estimate, not measured per-session.
- [ ] Firefox extension (blocked on Mozilla shipping a tabCapture equivalent)
- [ ] Config for saving preferred devices/settings
