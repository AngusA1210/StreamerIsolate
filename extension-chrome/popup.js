// Popup UI: start/stop toggle plus the live vocal-attenuation slider.
//
// chrome.tabCapture.getMediaStreamId is called from here rather than the
// service worker, because it requires the extension to have been invoked for
// the current tab -- opening this popup is that invocation.

const statusEl = document.getElementById("status");
const toggleEl = document.getElementById("toggle");
const strengthEl = document.getElementById("strength");
const strengthValueEl = document.getElementById("strengthValue");

let activeTab = null;
let capturing = false;
let phase = "off";

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tab;

  const stored = await chrome.storage.local.get("vocalStrength");
  const strength = typeof stored.vocalStrength === "number" ? stored.vocalStrength : 0.85;
  strengthEl.value = Math.round(strength * 100);
  strengthValueEl.textContent = `${Math.round(strength * 100)}%`;

  if (tab && tab.id) {
    const state = await chrome.runtime.sendMessage({ type: "get-state", tabId: tab.id });
    capturing = !!(state && state.capturing);
    phase = (state && state.phase) || "off";

    // Buffering can finish while the popup is open, so track it live.
    chrome.storage.session.onChanged.addListener((changes) => {
      const change = changes[`phase_${tab.id}`];
      if (change) {
        phase = change.newValue || "off";
        render();
      }
    });
  }
  render();
}

function render() {
  if (!capturing) {
    statusEl.textContent = "Off";
  } else if (phase === "running") {
    statusEl.textContent = "Running on this tab";
  } else {
    statusEl.textContent = "Buffering… (takes a few seconds)";
  }
  statusEl.classList.toggle("on", capturing && phase === "running");
  statusEl.classList.toggle("buffering", capturing && phase !== "running");
  toggleEl.textContent = capturing ? "Stop" : "Start on this tab";
}

toggleEl.addEventListener("click", async () => {
  if (!activeTab || !activeTab.id) return;
  toggleEl.disabled = true;
  try {
    if (capturing) {
      await chrome.runtime.sendMessage({ type: "stop-capture-request", tabId: activeTab.id });
      capturing = false;
    } else {
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: activeTab.id });
      await chrome.runtime.sendMessage({
        type: "start-capture-request",
        tabId: activeTab.id,
        streamId,
        vocalStrength: Number(strengthEl.value) / 100,
      });
      capturing = true;
      phase = "buffering";
    }
    render();
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message}`;
    console.error("[StreamerIsolate]", err);
  } finally {
    toggleEl.disabled = false;
  }
});

strengthEl.addEventListener("input", async () => {
  const strength = Number(strengthEl.value) / 100;
  strengthValueEl.textContent = `${strengthEl.value}%`;
  await chrome.storage.local.set({ vocalStrength: strength });
  // Nothing sends a response to this one; swallow the resulting
  // "message port closed" rejection rather than logging noise on every drag.
  chrome.runtime.sendMessage({ type: "set-strength", vocalStrength: strength }).catch(() => {});
});

init();
