# Design notes

Background on how StreamerIsolate works and why it's built this way,
including approaches that were tried and abandoned. See the README for
installation and use.

## Separation

Audio is processed in overlapping chunks (default 3s with 0.75s overlap)
through a pretrained [Demucs](https://github.com/facebookresearch/demucs)
model, which splits a mix into drums/bass/other/vocals. Only the `vocals`
stem is kept, and chunks are crossfaded at the seams.

That's an approximation of "speech, not music" — Demucs separates *singing*
from instrumentals, and has no notion of a streamer talking versus a song's
vocals, so both land in the same stem.

## The singing classifier

To reduce that bleed-through, a second pretrained model
([PANNs](https://github.com/qiuqiangkong/panns_inference) Cnn14, trained on
AudioSet) runs over short windows of the isolated vocals and ducks the ones
that look more like singing than speech.

Two things about the scoring are worth knowing:

- It compares the *relative dominance* of singing over speech, not the raw
  difference. AudioSet's "Speech" label fires on singing too, so an earlier
  rule of "attenuate when singing beats speech by a fixed margin" almost
  never triggered strongly — singing bled through even at maximum strength.
- The strength setting scales both the trigger threshold and how fast
  attenuation ramps, rather than just lowering a floor the score rarely
  reached.

Scores are EMA-smoothed across chunk boundaries (`ClassifierState`, kept
per-session since one classifier instance serves all connections) so the
attenuation doesn't wobble chunk to chunk.

A bug worth remembering: the gain envelope used to be resampled with
`resample_poly`, whose FIR ringing left a ~24% level dip at every chunk edge
— an all-1.0 "don't attenuate" curve came back dipping to 0.756. It's now
interpolated straight onto the output grid, since the curve is smooth and
slowly varying.

## Why Chrome needs no audio setup, and Firefox does

The first approach used macOS
[ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
(still in `native/capture-app-audio/`) to capture one app's audio without
touching system output. That's a dead end here: ScreenCaptureKit gives you a
*copy* of audio already on its way to the speakers. It can't silence the
original, so you always hear the untouched original layered over the
processed result. Muting the tab doesn't help either — browsers stop
producing audio for a muted tab, so there's nothing left to capture.

True interception needs the browser's own API. Chrome's `chrome.tabCapture`
mutes the tab the instant capture starts, and the extension reconnects only
the processed audio to the speakers. Firefox has no equivalent (Mozilla bugs
[1443484](https://bugzilla.mozilla.org/show_bug.cgi?id=1443484) and
[1391223](https://bugzilla.mozilla.org/show_bug.cgi?id=1391223)), which is
why the Firefox path routes audio through a virtual device to the desktop
backend instead. The Firefox extension drives that backend over the same
local websocket and handles the video overlay.

## Video sync

The tab's video is never paused or slowed, because `chrome.tabCapture` taps
the same clock the video plays on — holding the video back would also stall
the audio feeding Demucs. Instead `content.js` buffers the video's frames and
draws a deliberately-delayed copy on a canvas over the real video, which
keeps playing undisturbed underneath.

Frames are buffered **encoded** (WebCodecs: `VideoEncoder` → chunk queue →
`VideoDecoder`). A few seconds of raw 1080p frames would be hundreds of
megabytes to gigabytes; the same span of H.264 is single-digit megabytes,
which is what makes native resolution and frame rate affordable. An earlier
version buffered raw bitmaps and had to downscale to 640px at 12fps, which
looked visibly soft and choppy.

How the delay target is chosen differs per browser:

- **Chrome** measures it. The processed stream returns in the order it was
  sent, so output frame N corresponds to input frame N; comparing when that
  frame was captured against when its processed counterpart is scheduled to
  play gives the true latency.
- **Firefox** can't — it never sees the audio — so the backend reports an
  estimate (chunk length + processing time + queued output), smoothed before
  sending.

Both only retarget the overlay when the delay changes meaningfully (>150ms),
since retargeting makes the picture jump.

Known behavior: switching away from the tab freezes the browser's animation
callbacks, so capture and playback stall. On return the buffer resets and the
delay rebuilds over a few seconds rather than replaying a stale backlog.

## Layout

| Path | What it does |
| --- | --- |
| `src/streamerisolate/separator.py` | Demucs wrapper, returns the vocals stem |
| `src/streamerisolate/vocal_classifier.py` | PANNs singing-vs-speech gate |
| `src/streamerisolate/pipeline.py` | Device capture → separate → playback |
| `src/streamerisolate/server.py` | Local websocket: audio streaming (Chrome) and pipeline control (Firefox) |
| `src/streamerisolate/gui.py` | PySide6 desktop app |
| `extension-chrome/` | Chrome extension (tabCapture audio + video overlay) |
| `extension-firefox/` | Firefox extension (backend control + video overlay) |
| `native/capture-app-audio/` | ScreenCaptureKit experiment, superseded |

`extension-firefox/content.js` is a copy of the Chrome one — the logic is
browser-agnostic. Keep them in sync.

## CLI

`streamerisolate serve` runs the backend. `streamerisolate list-devices`
lists audio devices. `streamerisolate run --input X --output Y` runs the
pipeline directly, with `--vocal-strength` (0..1), `--chunk-seconds`,
`--overlap-seconds`, `--gain`, `--model`, and `--no-vocal-classifier`.
