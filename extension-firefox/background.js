// Firefox background script.
//
// Firefox has no tabCapture equivalent, so unlike the Chrome version this
// extension never touches the tab's audio. Instead it *drives* the desktop
// backend over the same local websocket: the backend captures from a virtual
// audio device, isolates speech, and plays the result itself. This script
// relays start/stop and the strength slider, and feeds the backend's reported
// delay to the video overlay so the picture still lines up with the sound.

const api = globalThis.browser ?? globalThis.chrome;
const BACKEND_URL = "ws://127.0.0.1:8765";

let socket = null;
let activeTabId = null;
let devices = null;
let state = { connected: false, capturing: false, phase: "off", error: null };
let reportedDelay = null;

function connect() {
  return new Promise((resolve, reject) => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      resolve();
      return;
    }
    socket = new WebSocket(BACKEND_URL);

    socket.addEventListener("open", () => {
      state.connected = true;
      state.error = null;
      socket.send(JSON.stringify({ type: "control-hello" }));
      resolve();
    });

    socket.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      handleServerMessage(msg);
    });

    socket.addEventListener("close", () => {
      state.connected = false;
      state.capturing = false;
      state.phase = "off";
      stopOverlay();
    });

    socket.addEventListener("error", () => {
      state.connected = false;
      state.error = "Can't reach the StreamerIsolate backend. Is it running?";
      reject(new Error(state.error));
    });
  });
}

function handleServerMessage(msg) {
  if (msg.type === "devices") {
    devices = msg;
  } else if (msg.type === "control-started") {
    state.capturing = true;
    state.phase = "buffering";
    reportedDelay = null;
    startOverlay();
  } else if (msg.type === "control-stopped") {
    state.capturing = false;
    state.phase = "off";
    stopOverlay();
  } else if (msg.type === "status") {
    state.phase = msg.phase;
    state.error = msg.error || null;
    if (msg.error) {
      state.capturing = false;
      stopOverlay();
    } else if (activeTabId != null && msg.delaySeconds > 0) {
      // Firefox can't measure this itself (it never sees the audio), so it
      // takes the backend's estimate. Only forward meaningful changes --
      // retargeting the overlay on every small wobble would make the picture
      // visibly jump.
      if (reportedDelay === null || Math.abs(msg.delaySeconds - reportedDelay) > 0.15) {
        reportedDelay = msg.delaySeconds;
        api.tabs
          .sendMessage(activeTabId, { type: "set-delay", targetDelaySeconds: msg.delaySeconds })
          .catch(() => {});
      }
    }
  } else if (msg.type === "error") {
    state.error = msg.message;
    state.capturing = false;
    stopOverlay();
  }
}

async function startOverlay() {
  if (activeTabId == null) return;
  try {
    await api.scripting.executeScript({ target: { tabId: activeTabId }, files: ["content.js"] });
    api.tabs
      .sendMessage(activeTabId, { type: "start-sync", targetDelaySeconds: 3.5 })
      .catch(() => {});
  } catch (err) {
    // Video sync is a bonus; audio isolation works regardless.
    console.warn("[StreamerIsolate] could not start video sync:", err);
  }
}

function stopOverlay() {
  if (activeTabId == null) return;
  api.tabs.sendMessage(activeTabId, { type: "stop-sync" }).catch(() => {});
}

api.runtime.onMessage.addListener((message) => {
  if (message.type === "get-state") {
    return Promise.resolve({ ...state, devices });
  }

  if (message.type === "connect") {
    return connect()
      .then(() => ({ ok: true }))
      .catch((err) => ({ ok: false, error: err.message }));
  }

  if (message.type === "start") {
    activeTabId = message.tabId;
    return connect()
      .then(() => {
        socket.send(
          JSON.stringify({
            type: "control-start",
            inputDevice: message.inputDevice,
            outputDevice: message.outputDevice,
            vocalStrength: message.vocalStrength,
          })
        );
        return { ok: true };
      })
      .catch((err) => ({ ok: false, error: err.message }));
  }

  if (message.type === "stop") {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "control-stop" }));
    }
    stopOverlay();
    state.capturing = false;
    state.phase = "off";
    return Promise.resolve({ ok: true });
  }

  if (message.type === "set-strength") {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "settings", vocalStrength: message.vocalStrength }));
    }
    return Promise.resolve({ ok: true });
  }

  return false;
});

api.tabs.onRemoved.addListener((tabId) => {
  if (tabId === activeTabId && socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "control-stop" }));
    state.capturing = false;
    state.phase = "off";
    activeTabId = null;
  }
});
