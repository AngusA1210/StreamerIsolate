# StreamerIsolate

Removes background music from a livestream's audio in near-real-time, keeping
only the streamer's speech — so you can play your own music without the two
fighting.

Works with **Chrome** (extension only) or **Firefox** (extension + a virtual
audio device).

## Install

Needs Python 3.10+ — use Homebrew's `python@3.12`, since macOS's built-in
Python 3.9 is too old for current PyTorch.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

First run downloads ~400MB of models.

Then start the backend and **leave it running** in that terminal — both
browser extensions talk to it, and nothing works without it:

```bash
streamerisolate serve
```

## Chrome

1. Go to `chrome://extensions`, turn on **Developer mode**, click **Load
   unpacked**, pick the `extension-chrome/` folder.
2. Open your stream, click the StreamerIsolate icon, hit **Start**.

That's it — no audio setup needed.

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

- **Starting takes a few seconds.** Models load once (~15s the first time),
  then it buffers a few seconds before audio comes out. The popup says
  "Buffering" until it's ready.
- **The slider** controls how hard detected singing is cut, live. Song vocals
  can still bleed through when the streamer talks *over* music — both land in
  the same audio and no setting fully separates them.
- **Video is delayed to match the audio** so they stay in sync.
- **After changing backend code**, restart `streamerisolate serve` — Python
  doesn't hot-reload. For the extensions, hit reload in the browser.
- **Nothing playing?** Check that `streamerisolate serve` is running on port
  8765. In Chrome, `chrome://extensions` → StreamerIsolate → "Inspect views:
  offscreen.html" shows errors.

## Status

Working: Chrome extension, Firefox extension, video sync, adjustable singing
attenuation.

Not done: launching the backend without a terminal; Firefox audio
interception (blocked on Firefox, would remove the virtual-device step).

For how it works internally and why it's built this way, see
[docs/design-notes.md](docs/design-notes.md).
