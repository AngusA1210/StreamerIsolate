// Injected into the captured tab on start (see background.js). Shows a
// delayed copy of the tab's video so it lines up with the processed audio,
// while the real <video> element keeps playing undisturbed at full speed.
//
// Why not just delay the video element: chrome.tabCapture taps the same
// clock the video plays on, so pausing or slowing it would also pause and
// distort the audio feeding the Demucs backend.
//
// Frames are buffered *encoded* (WebCodecs VideoEncoder -> EncodedVideoChunk
// queue -> VideoDecoder), not as raw bitmaps. A few seconds of raw frames at
// full resolution would be hundreds of megabytes to gigabytes; the same span
// of H.264 is single-digit megabytes, which is what makes it affordable to
// keep the stream's native resolution and frame rate.

(() => {
  if (window.__streamerIsolateStop) {
    window.__streamerIsolateStop();
  }

  const KEYFRAME_INTERVAL_MS = 2000;
  const MAX_QUEUE_SECONDS = 12; // safety valve if the pump stalls
  const BITS_PER_PIXEL = 0.15; // generous: this is a local few-second buffer
  const TARGET_FRAMERATE = 60; // encoder hint; actual rate follows the source

  let video = null;
  let canvas = null;
  let ctx = null;
  let encoder = null;
  let decoder = null;
  let resizeObserver = null;
  let running = false;
  let configuring = false;
  let targetDelayMs = 4000;
  let configuredWidth = 0;
  let configuredHeight = 0;
  let lastKeyframeAt = 0;
  let pumpHandle = null;
  let codecUnavailable = false;

  // Holds only not-yet-displayed chunks, i.e. roughly `targetDelayMs` worth.
  let queue = [];

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
    // Canvas *resolution* is set from decoded frames (see drawDecodedFrame),
    // not from this CSS box -- object-fit letterboxes it like the video does.
  }

  async function pickEncoderConfig(width, height, framerate) {
    const bitrate = Math.min(
      25_000_000,
      Math.max(4_000_000, Math.round(width * height * framerate * BITS_PER_PIXEL))
    );
    const codecs = [
      "avc1.640034", // H.264 High 5.2 -- hardware-encoded on most Macs
      "avc1.4D4034", // H.264 Main 5.2
      "avc1.42E034", // H.264 Baseline 5.2
      "vp8",
      "vp09.00.10.08",
    ];
    for (const hardwareAcceleration of ["prefer-hardware", "no-preference"]) {
      for (const codec of codecs) {
        const config = {
          codec,
          width,
          height,
          bitrate,
          framerate,
          latencyMode: "realtime",
          hardwareAcceleration,
        };
        try {
          const support = await VideoEncoder.isConfigSupported(config);
          if (support && support.supported) return support.config || config;
        } catch (e) {
          // codec string not recognised here; try the next one
        }
      }
    }
    return null;
  }

  function handleEncodedChunk(chunk, metadata) {
    // Keep any new decoderConfig with the chunk it belongs to rather than
    // applying it immediately -- chunks already queued were encoded with the
    // previous config and still have to decode with it.
    queue.push({
      chunk,
      capturedAt: performance.now(),
      decoderConfig: (metadata && metadata.decoderConfig) || null,
    });
  }

  function drawDecodedFrame(frame) {
    try {
      if (ctx && canvas) {
        if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas.width = frame.displayWidth;
          canvas.height = frame.displayHeight;
        }
        ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
      }
    } finally {
      frame.close();
    }
  }

  function resetDecoder() {
    if (decoder && decoder.state !== "closed") {
      try {
        decoder.close();
      } catch (e) {
        /* already gone */
      }
    }
    decoder = new VideoDecoder({
      output: drawDecodedFrame,
      error: (e) => console.warn("[StreamerIsolate] decoder error:", e),
    });
    dropToNextKeyframe();
  }

  function dropToNextKeyframe() {
    // A decoder can only start (or restart) on a keyframe, so discard
    // anything before the next one rather than feeding it a broken chain.
    while (queue.length && queue[0].chunk.type !== "key") queue.shift();
  }

  async function setupEncoder(width, height, framerate) {
    if (configuring) return;
    configuring = true;
    try {
      const config = await pickEncoderConfig(width, height, framerate);
      if (!config) {
        // Latch this, or every single frame would re-probe every codec.
        codecUnavailable = true;
        console.warn("[StreamerIsolate] no supported video codec for buffering -- video sync disabled");
        return;
      }
      if (encoder && encoder.state !== "closed") {
        try {
          encoder.close();
        } catch (e) {
          /* already gone */
        }
      }
      encoder = new VideoEncoder({
        output: handleEncodedChunk,
        error: (e) => console.warn("[StreamerIsolate] encoder error:", e),
      });
      encoder.configure(config);
      configuredWidth = width;
      configuredHeight = height;
      lastKeyframeAt = 0; // force a keyframe on the next captured frame
    } finally {
      configuring = false;
    }
  }

  function onVideoFrame() {
    if (!running) return;
    try {
      const width = video.videoWidth;
      const height = video.videoHeight;
      if (width && height && !codecUnavailable) {
        if (width !== configuredWidth || height !== configuredHeight) {
          setupEncoder(width, height, TARGET_FRAMERATE);
        } else if (encoder && encoder.state === "configured" && encoder.encodeQueueSize < 4) {
          // Skipping while the encoder is backed up keeps a slow machine from
          // building an ever-growing encode backlog.
          const nowMs = performance.now();
          const keyFrame = nowMs - lastKeyframeAt >= KEYFRAME_INTERVAL_MS;
          if (keyFrame) lastKeyframeAt = nowMs;
          // Our own monotonic clock, so a stream hiccup can't hand the
          // encoder a non-increasing timestamp.
          const frame = new VideoFrame(video, { timestamp: Math.round(nowMs * 1000) });
          try {
            encoder.encode(frame, { keyFrame });
          } finally {
            frame.close();
          }
        }
      }
    } catch (e) {
      // Frame not ready, mid-reconfigure, etc -- skip this one.
    }
    if (running && video.requestVideoFrameCallback) {
      video.requestVideoFrameCallback(onVideoFrame);
    }
  }

  function pump() {
    if (!running) return;
    const now = performance.now();
    const cutoff = now - targetDelayMs;

    while (queue.length && queue[0].capturedAt <= cutoff) {
      const item = queue.shift();
      try {
        if (item.decoderConfig) decoder.configure(item.decoderConfig);
        if (decoder.state === "configured") decoder.decode(item.chunk);
      } catch (e) {
        console.warn("[StreamerIsolate] decode failed, resetting decoder:", e);
        resetDecoder();
        break;
      }
    }

    // If the pump stalled (backgrounded tab, etc), don't grow without bound.
    const overflowCutoff = now - MAX_QUEUE_SECONDS * 1000;
    if (queue.length && queue[0].capturedAt < overflowCutoff) {
      while (queue.length && queue[0].capturedAt < overflowCutoff) queue.shift();
      dropToNextKeyframe();
    }

    pumpHandle = requestAnimationFrame(pump);
  }

  function onVisibilityChange() {
    // Backgrounding the tab freezes rAF/requestVideoFrameCallback, so both
    // capture and playback stall and the buffer goes stale. Rather than
    // dumping that backlog as a catch-up burst on return, start clean and
    // let the delay rebuild.
    if (!running || document.hidden) return;
    queue = [];
    lastKeyframeAt = 0;
    resetDecoder();
  }

  function start(delaySeconds) {
    if (typeof VideoEncoder === "undefined" || typeof VideoDecoder === "undefined") {
      console.warn("[StreamerIsolate] WebCodecs unavailable in this browser -- video sync disabled");
      return;
    }
    video = findVideo();
    if (!video) {
      console.warn("[StreamerIsolate] no <video> element found on this page -- video sync not started");
      return;
    }
    if (!video.requestVideoFrameCallback) {
      console.warn("[StreamerIsolate] requestVideoFrameCallback unavailable -- video sync disabled");
      return;
    }
    if (delaySeconds) targetDelayMs = delaySeconds * 1000;

    canvas = document.createElement("canvas");
    canvas.style.position = "fixed";
    canvas.style.zIndex = "2147483647";
    canvas.style.pointerEvents = "none";
    canvas.style.objectFit = "contain";
    document.documentElement.appendChild(canvas);
    ctx = canvas.getContext("2d");

    positionCanvas();
    resizeObserver = new ResizeObserver(positionCanvas);
    resizeObserver.observe(video);
    window.addEventListener("scroll", positionCanvas, true);
    window.addEventListener("resize", positionCanvas);
    document.addEventListener("visibilitychange", onVisibilityChange);

    queue = [];
    configuredWidth = 0;
    configuredHeight = 0;
    codecUnavailable = false;
    running = true;

    resetDecoder();
    video.requestVideoFrameCallback(onVideoFrame);
    pumpHandle = requestAnimationFrame(pump);
  }

  function stop() {
    running = false;
    if (resizeObserver) resizeObserver.disconnect();
    window.removeEventListener("scroll", positionCanvas, true);
    window.removeEventListener("resize", positionCanvas);
    document.removeEventListener("visibilitychange", onVisibilityChange);
    if (pumpHandle) cancelAnimationFrame(pumpHandle);
    if (encoder && encoder.state !== "closed") {
      try {
        encoder.close();
      } catch (e) {
        /* already gone */
      }
    }
    if (decoder && decoder.state !== "closed") {
      try {
        decoder.close();
      } catch (e) {
        /* already gone */
      }
    }
    queue = [];
    if (canvas) canvas.remove();
    encoder = null;
    decoder = null;
    canvas = null;
    ctx = null;
    video = null;
  }

  window.__streamerIsolateStop = stop;

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "start-sync") {
      start(message.targetDelaySeconds);
    } else if (message.type === "stop-sync") {
      stop();
    } else if (message.type === "set-delay") {
      // The offscreen document measured the real audio latency; match it
      // rather than sticking with the fixed estimate we started from.
      if (message.targetDelaySeconds > 0) targetDelayMs = message.targetDelaySeconds * 1000;
    }
  });
})();
