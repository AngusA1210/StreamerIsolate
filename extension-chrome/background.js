// Service worker: coordinates the pieces. The popup (popup.js) drives
// start/stop and the strength slider; audio capture/processing/playback lives
// in the offscreen document (offscreen.js), since service workers have no DOM
// / AudioContext / MediaStream support; delayed-video display lives in
// content.js, injected into the captured tab.

importScripts("config.js");

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "get-state") {
    getCapturing(message.tabId).then((capturing) => sendResponse({ capturing }));
    return true; // async response
  }
  if (message.type === "start-capture-request") {
    startCapture(message.tabId, message.streamId, message.vocalStrength)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (message.type === "stop-capture-request") {
    stopCapture(message.tabId)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  return false;
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

async function startCapture(tabId, streamId, vocalStrength) {
  await ensureOffscreenDocument();
  chrome.runtime.sendMessage({ type: "start-capture", streamId, tabId, vocalStrength });

  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    chrome.tabs.sendMessage(tabId, { type: "start-sync", targetDelaySeconds: TARGET_DELAY_SECONDS });
  } catch (err) {
    // Video sync is a bonus on top of audio isolation -- don't block capture
    // on it (e.g. the tab may be a chrome:// page or otherwise unscriptable).
    console.warn("[StreamerIsolate] could not start video sync:", err);
  }

  chrome.action.setBadgeText({ text: "ON", tabId });
  chrome.action.setBadgeBackgroundColor({ color: "#2e7d32", tabId });
  await setCapturing(tabId, true);
}

async function stopCapture(tabId) {
  chrome.runtime.sendMessage({ type: "stop-capture", tabId });
  chrome.tabs.sendMessage(tabId, { type: "stop-sync" }).catch(() => {});
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
  getCapturing(tabId).then((capturing) => {
    if (capturing) stopCapture(tabId);
  });
});
