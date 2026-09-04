// Injected into the captured tab on start (see background.js). Buffers
// video frames and displays them with a delay via a canvas overlay, while
// the underlying <video> element keeps playing completely undisturbed at
// full speed.
//
// That last part matters: chrome.tabCapture taps the same clock the video
// element plays on, so pausing or slowing the video itself to hold it back
// would also pause/distort the audio feeding the Demucs backend. Instead,
// this leaves the real video alone and shows the user a deliberately-late
// *copy* of its frames on top, timed to roughly match when the
// corresponding (slower, processed) audio actually plays.
//
// Known tradeoffs: frames are captured at a modest rate/resolution to keep
// memory bounded over several seconds of buffering, so the overlay is
// softer than the native stream. The delay target is a fixed estimate
// (config.js), not measured per-session, so it won't be sample-accurate.

(() => {
  if (window.__streamerIsolateStop) {
    window.__streamerIsolateStop();
  }

  let video = null;
  let canvas = null;
  let ctx = null;
  let scratch = null;
  let scratchCtx = null;
  let resizeObserver = null;
  let running = false;
  let targetDelaySeconds = 4.0;
  let lastCaptureTime = 0;
  let rafHandle = null;

  const frameBuffer = []; // { bitmap, capturedAt } oldest first
  const MAX_BUFFER_SECONDS = 8;
  const CAPTURE_INTERVAL_MS = 1000 / 12; // ~12fps capture bounds memory/CPU
  const MAX_CAPTURE_WIDTH = 640; // downscale before buffering, same reason

  function findVideo() {
    return document.querySelector("video");
  }

  function positionCanvas() {
    if (!video || !canvas) return;
    const rect = video.getBoundingClientRect();
    canvas.style.left = `${rect.left}px`;
    canvas.style.top = `${rect.top}px`;
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    canvas.style.display = rect.width > 0 && rect.height > 0 ? "block" : "none";

    // Internal pixel resolution is separate from the CSS display size above
    // -- without setting this, canvas defaults to a blurry 300x150 buffer
    // stretched to fit.
    const newWidth = Math.max(1, Math.round(rect.width));
    const newHeight = Math.max(1, Math.round(rect.height));
    if (canvas.width !== newWidth) canvas.width = newWidth;
    if (canvas.height !== newHeight) canvas.height = newHeight;
  }

  async function captureFrame(now) {
    if (now - lastCaptureTime < CAPTURE_INTERVAL_MS) return;
    lastCaptureTime = now;
    if (!video.videoWidth || !video.videoHeight) return;

    const scale = Math.min(1, MAX_CAPTURE_WIDTH / video.videoWidth);
    const w = Math.max(1, Math.round(video.videoWidth * scale));
    const h = Math.max(1, Math.round(video.videoHeight * scale));
    if (scratch.width !== w || scratch.height !== h) {
      scratch.width = w;
      scratch.height = h;
    }

    try {
      scratchCtx.drawImage(video, 0, 0, w, h);
      const bitmap = await createImageBitmap(scratch);
      frameBuffer.push({ bitmap, capturedAt: performance.now() });

      const cutoff = performance.now() - MAX_BUFFER_SECONDS * 1000;
      while (frameBuffer.length && frameBuffer[0].capturedAt < cutoff) {
        frameBuffer.shift().bitmap.close();
      }
    } catch (e) {
      // Frame not ready / video mid-seek -- skip this tick.
    }
  }

  function captureLoop(now) {
    if (!running) return;
    captureFrame(now);
    if (video.requestVideoFrameCallback) {
      video.requestVideoFrameCallback(captureLoop);
    } else {
      requestAnimationFrame(captureLoop);
    }
  }

  function renderLoop() {
    if (!running) return;
    const targetTime = performance.now() - targetDelaySeconds * 1000;

    let chosen = null;
    for (let i = frameBuffer.length - 1; i >= 0; i--) {
      if (frameBuffer[i].capturedAt <= targetTime) {
        chosen = frameBuffer[i];
        break;
      }
    }
    if (chosen && ctx && canvas.width > 0 && canvas.height > 0) {
      ctx.drawImage(chosen.bitmap, 0, 0, canvas.width, canvas.height);
    }
    rafHandle = requestAnimationFrame(renderLoop);
  }

  function start(delaySeconds) {
    video = findVideo();
    if (!video) {
      console.warn("[StreamerIsolate] no <video> element found on this page -- video sync not started");
      return;
    }
    targetDelaySeconds = delaySeconds || targetDelaySeconds;

    canvas = document.createElement("canvas");
    canvas.style.position = "fixed";
    canvas.style.zIndex = "2147483647";
    canvas.style.pointerEvents = "none";
    document.documentElement.appendChild(canvas);
    ctx = canvas.getContext("2d");

    scratch = document.createElement("canvas");
    scratchCtx = scratch.getContext("2d");

    positionCanvas();
    resizeObserver = new ResizeObserver(positionCanvas);
    resizeObserver.observe(video);
    window.addEventListener("scroll", positionCanvas, true);
    window.addEventListener("resize", positionCanvas);

    running = true;
    if (video.requestVideoFrameCallback) {
      video.requestVideoFrameCallback(captureLoop);
    } else {
      requestAnimationFrame(captureLoop);
    }
    rafHandle = requestAnimationFrame(renderLoop);
  }

  function stop() {
    running = false;
    if (resizeObserver) resizeObserver.disconnect();
    window.removeEventListener("scroll", positionCanvas, true);
    window.removeEventListener("resize", positionCanvas);
    if (rafHandle) cancelAnimationFrame(rafHandle);
    for (const f of frameBuffer) f.bitmap.close();
    frameBuffer.length = 0;
    if (canvas) canvas.remove();
    canvas = null;
    ctx = null;
    scratch = null;
    scratchCtx = null;
    video = null;
  }

  window.__streamerIsolateStop = stop;

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "start-sync") {
      start(message.targetDelaySeconds);
    } else if (message.type === "stop-sync") {
      stop();
    }
  });
})();
