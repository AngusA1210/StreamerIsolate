# StreamerIsolate

Removes background music from a livestream's audio in near-real-time, keeping
only the streamer's speech — so you can play your own music without the two
fighting.

Works with **Chrome** (extension only) or **Firefox** (extension + a virtual
audio device).

## Install (once)

```bash
./scripts/install.sh
```

That sets up Python, downloads the models (~400MB), and registers a native
messaging host so the extension can start the backend by itself. Needs
Homebrew's `python@3.12` — macOS's built-in Python 3.9 is too old for current
PyTorch (`brew install python@3.12`).

After this you never need a terminal: clicking the extension starts
everything.

## Chrome

1. Go to `chrome://extensions`, turn on **Developer mode**, click **Load
   unpacked**, pick the `extension-chrome/` folder.
2. Copy the extension ID it shows, and run `./scripts/install.sh <that-id>`
   so the extension is allowed to start the backend. (Chrome ties this
   permission to the ID, which it assigns on load; Firefox needs no such
   step.)
3. Open your stream, click the StreamerIsolate icon, hit **Start**.

No audio setup needed.

## Firefox

Firefox can't hand a tab's audio to an extension ([Mozilla bug
1443484](https://bugzilla.mozilla.org/show_bug.cgi?id=1443484)), so the
backend handles audio through a virtual audio device instead.

1. Install a virtual audio device — [BlackHole](https://github.com/ExistentialAudio/BlackHole)
   (`brew install blackhole-2ch`), or use Pro Tools Audio Bridge if you have
   Pro Tools.
2. Set that virtual device as your **system output** while the stream plays.
3. Go to `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on**
   → pick `extension-firefox/manifest.json`.
4. In the popup, set **Capture from** to the virtual device and **Play to**
   to your speakers, then hit **Start**.

## Good to know

- **Starting takes a few seconds.** The first click starts the backend, which
  loads models (~20s); after that it stays running and starts are quick.
  Either way it buffers a few seconds before audio comes out — the popup says
  so until it's ready.
- **The slider** controls how hard detected singing is cut, live. Song vocals
  can still bleed through when the streamer talks *over* music — both land in
  the same audio and no setting fully separates them.
- **Video is delayed to match the audio** so they stay in sync.
- **After changing backend code**, restart `streamerisolate serve` — Python
  doesn't hot-reload. For the extensions, hit reload in the browser.
- **Nothing playing?** Check that `streamerisolate serve` is running on port
  8765. In Chrome, `chrome://extensions` → StreamerIsolate → "Inspect views:
  offscreen.html" shows errors. In Firefox, `about:debugging` → Inspect.
- **Attenuation not doing anything?** Run the backend as
  `STREAMERISOLATE_DEBUG=1 streamerisolate serve` — it prints one line per
  chunk showing the strength in effect, what the classifier heard, and the
  gain it applied, which distinguishes "the slider isn't reaching it" from
  "it doesn't think there's singing".

## Status

Working: Chrome extension, Firefox extension, video sync, adjustable singing
attenuation, and one-click start with no terminal.

Not done: running fully in-browser with no install at all (Demucs would
have to be ported to ONNX/WebGPU, and existing browser ports are offline-only
so real-time isn't a given); Firefox audio interception (blocked on Firefox,
would remove the virtual-device step).

For how it works internally and why it's built this way, see
[docs/design-notes.md](docs/design-notes.md).
