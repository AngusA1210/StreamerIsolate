// Service worker: owns the toggle-on-click UX and the offscreen document
// lifecycle. Actual audio capture/processing/playback happens in the
// offscreen document (offscreen.js), since service workers have no DOM /
// AudioContext / MediaStream support.

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;
  const capturing = await getCapturing(tab.id);
  if (capturing) {
    await stopCapture(tab.id);
  } else {
    await startCapture(tab);
  }
});

async function ensureOffscreenDocument() {
  const existing = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (existing.length > 0) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capture a tab's audio via chrome.tabCapture and play back the processed result.",
  });
}

async function startCapture(tab) {
  await ensureOffscreenDocument();
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  chrome.runtime.sendMessage({ type: "start-capture", streamId, tabId: tab.id });
  chrome.action.setBadgeText({ text: "ON", tabId: tab.id });
  chrome.action.setBadgeBackgroundColor({ color: "#2e7d32", tabId: tab.id });
  await setCapturing(tab.id, true);
}

async function stopCapture(tabId) {
  chrome.runtime.sendMessage({ type: "stop-capture", tabId });
  chrome.action.setBadgeText({ text: "", tabId });
  await setCapturing(tabId, false);
}

async function getCapturing(tabId) {
  const key = `capturing_${tabId}`;
  const result = await chrome.storage.session.get(key);
  return !!result[key];
}

async function setCapturing(tabId, value) {
  const key = `capturing_${tabId}`;
  await chrome.storage.session.set({ [key]: value });
}

// If the captured tab is closed, make sure we don't leave capture running.
chrome.tabs.onRemoved.addListener((tabId) => {
  stopCapture(tabId);
});
