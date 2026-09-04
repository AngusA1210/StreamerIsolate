// Runs in the offscreen document, which is the only MV3 context with DOM /
// AudioContext / getUserMedia support. This is where the actual interception
// happens: chrome.tabCapture mutes the source tab the moment we grab its
// stream, and it stays muted because we deliberately never connect the raw
// captured audio to the AudioContext's destination -- only the processed
// audio coming back from the backend gets connected, which is what makes
// this a true replace rather than the additive tap ScreenCaptureKit gave us.

const BACKEND_URL = "ws://127.0.0.1:8765";

let audioContext = null;
let mediaStream = null;
let sourceNode = null;
let workletNode = null;
let gainNode = null;
let socket = null;
let playCursor = 0;
let vocalStrength = 0.85;

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "start-capture") {
    if (typeof message.vocalStrength === "number") vocalStrength = message.vocalStrength;
    startCapture(message.streamId).catch((err) => console.error("[StreamerIsolate] start failed:", err));
  } else if (message.type === "stop-capture") {
    stopCapture();
  } else if (message.type === "set-strength") {
    vocalStrength = message.vocalStrength;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "settings", vocalStrength }));
    }
  }
});

async function startCapture(streamId) {
  stopCapture(); // clean up any previous session first

  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
      },
    },
    video: false,
  });

  audioContext = new AudioContext();
  await audioContext.audioWorklet.addModule("pcm-forwarder.js");

  sourceNode = audioContext.createMediaStreamSource(mediaStream);
  workletNode = new AudioWorkletNode(audioContext, "pcm-forwarder", { outputChannelCount: [2] });
  sourceNode.connect(workletNode);
  // Deliberately not connecting sourceNode/workletNode onward to
  // audioContext.destination -- that silence is what "replaces" the tab's
  // original audio instead of merely adding a copy on top of it.

  gainNode = audioContext.createGain();
  gainNode.connect(audioContext.destination);
  playCursor = audioContext.currentTime + 0.1;

  socket = new WebSocket(BACKEND_URL);
  socket.binaryType = "arraybuffer";

  socket.addEventListener("open", () => {
    socket.send(
      JSON.stringify({
        type: "start",
        sampleRate: audioContext.sampleRate,
        channels: 2,
        chunkSeconds: CHUNK_SECONDS,
        overlapSeconds: OVERLAP_SECONDS,
        gain: 1.0,
        vocalStrength,
      })
    );
  });

  socket.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      const msg = JSON.parse(event.data);
      if (msg.type === "error") {
        console.error("[StreamerIsolate] server error:", msg.message);
      }
      return;
    }
    playProcessedChunk(event.data);
  });

  socket.addEventListener("error", (err) => {
    console.error("[StreamerIsolate] websocket error (is `streamerisolate serve` running?):", err);
  });

  workletNode.port.onmessage = (event) => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(event.data.buffer);
    }
  };
}

function playProcessedChunk(arrayBuffer) {
  if (!audioContext) return;
  const interleaved = new Float32Array(arrayBuffer);
  const frameCount = interleaved.length / 2;
  if (frameCount <= 0) return;

  const audioBuffer = audioContext.createBuffer(2, frameCount, audioContext.sampleRate);
  const left = audioBuffer.getChannelData(0);
  const right = audioBuffer.getChannelData(1);
  for (let i = 0; i < frameCount; i++) {
    left[i] = interleaved[i * 2];
    right[i] = interleaved[i * 2 + 1];
  }

  const source = audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(gainNode);

  const now = audioContext.currentTime;
  if (playCursor < now) playCursor = now + 0.05;
  source.start(playCursor);
  playCursor += audioBuffer.duration;
}

function stopCapture() {
  if (workletNode) workletNode.port.onmessage = null;
  if (sourceNode) sourceNode.disconnect();
  if (workletNode) workletNode.disconnect();
  if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());
  if (socket) {
    try {
      socket.send(JSON.stringify({ type: "stop" }));
    } catch (e) {
      // socket may already be closed/closing
    }
    socket.close();
  }
  if (audioContext) audioContext.close();

  audioContext = null;
  mediaStream = null;
  sourceNode = null;
  workletNode = null;
  gainNode = null;
  socket = null;
}
