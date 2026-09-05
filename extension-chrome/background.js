// Service worker: coordinates the pieces. The popup (popup.js) drives
// start/stop and the strength slider; audio capture/processing/playback lives
// in the offscreen document (offscreen.js), since service workers have no DOM
// / AudioContext / MediaStream support; delayed-video display lives in
// content.js, injected into the captured tab.

importScripts("config.js");

const NATIVE_HOST = "com.angusa1210.streamerisolate";

// Starts the backend via the native messaging host if it isn't already up, so
// using this never requires a terminal. Non-fatal: if the host isn't
// installed, capture still proceeds in case the backend was started by hand.
async function ensureBackend() {
  try {
    return await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: "ensure-backend" });
  } catch (e) {
    return { ok: false, error: `Native host unavailable (${e.message})` };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "get-state") {
    chrome.storage.session
      .get([`capturing_${message.tabId}`, `phase_${message.tabId}`])
      .then((r) =>
        sendResponse({
          capturing: !!r[`capturing_${message.tabId}`],
          phase: r[`phase_${message.tabId}`] || "off",
        })
      );
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
  if (message.type === "capture-phase") {
    // Offscreen tells us when the first processed audio arrives, i.e. when
    // the initial buffering wait is over.
    setPhase(message.tabId, message.phase);
    setBadge(message.tabId, message.phase === "running" ? "ON" : "···");
    return false;
  }
  if (message.type === "measured-delay") {
    // Offscreen measured the real audio latency; hand it to the video
    // overlay so it delays by the same amount instead of a fixed guess.
    if (message.tabId) {
      chrome.tabs
        .sendMessage(message.tabId, {
          type: "set-delay",
          targetDelaySeconds: message.targetDelaySeconds,
        })
        .catch(() => {});
    }
    return false;
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

// Badge and tab messaging both reject once a tab is gone, which happens
// routinely on the tab-closed path. Keep those failures quiet.
function setBadge(tabId, text) {
  chrome.action.setBadgeText({ text, tabId }).catch(() => {});
  if (text) {
    chrome.action.setBadgeBackgroundColor({ color: "#2e7d32", tabId }).catch(() => {});
  }
}

async function startCapture(tabId, streamId, vocalStrength) {
  await ensureBackend();
  await ensureOffscreenDocument();
  chrome.runtime.sendMessage({ type: "start-capture", streamId, tabId, vocalStrength });

  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    chrome.tabs
      .sendMessage(tabId, { type: "start-sync", targetDelaySeconds: TARGET_DELAY_SECONDS })
      .catch(() => {});
  } catch (err) {
    // Video sync is a bonus on top of audio isolation -- don't block capture
    // on it (e.g. the tab may be a chrome:// page or otherwise unscriptable).
    console.warn("[StreamerIsolate] could not start video sync:", err);
  }

  setBadge(tabId, "···");
  await setCapturing(tabId, true);
  await setPhase(tabId, "buffering");
}

async function stopCapture(tabId) {
  chrome.runtime.sendMessage({ type: "stop-capture", tabId });
  chrome.tabs.sendMessage(tabId, { type: "stop-sync" }).catch(() => {});
  setBadge(tabId, "");
  await setCapturing(tabId, false);
  await setPhase(tabId, "off");
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

async function setPhase(tabId, phase) {
  // storage.session (rather than a variable) so the popup can read it fresh
  // and subscribe to changes -- the service worker may be torn down between
  // popup opens.
  await chrome.storage.session.set({ [`phase_${tabId}`]: phase });
}

// If the captured tab is closed, make sure we don't leave capture running.
chrome.tabs.onRemoved.addListener((tabId) => {
  getCapturing(tabId).then((capturing) => {
    if (capturing) stopCapture(tabId);
  });
});
