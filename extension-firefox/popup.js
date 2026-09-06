// Firefox popup: device pickers, start/stop and the strength slider. All of
// it is relayed through background.js to the StreamerIsolate backend, which owns the
// actual audio pipeline (see background.js for why).

const api = globalThis.browser ?? globalThis.chrome;

const statusEl = document.getElementById("status");
const toggleEl = document.getElementById("toggle");
const inputEl = document.getElementById("input");
const outputEl = document.getElementById("output");
const strengthEl = document.getElementById("strength");
const strengthValueEl = document.getElementById("strengthValue");

let activeTab = null;
let state = { connected: false, capturing: false, phase: "off", error: null };
let pollTimer = null;
let retryTimer = null;
let storedSettings = null;
let connectAttempts = 0;

async function init() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  activeTab = tab;

  const stored = await api.storage.local.get(["vocalStrength", "inputDevice", "outputDevice"]);
  const strength = typeof stored.vocalStrength === "number" ? stored.vocalStrength : 1.0;
  strengthEl.value = Math.round(strength * 100);
  strengthValueEl.textContent = `${Math.round(strength * 100)}%`;

  storedSettings = stored;
  await tryConnect();
}

async function tryConnect() {
  statusEl.textContent = "Connecting to backend…";
  statusEl.className = "status";

  const result = await api.runtime.sendMessage({ type: "connect" }).catch((e) => ({
    ok: false,
    error: `Extension background not responding: ${e.message}`,
  }));

  if (!result || !result.ok) {
    connectAttempts += 1;
    showConnectFailure(result, connectAttempts);
    if (!retryTimer) retryTimer = setInterval(tryConnect, 2000);
    return;
  }
  connectAttempts = 0;

  if (retryTimer) {
    clearInterval(retryTimer);
    retryTimer = null;
  }
  await refresh();
  if (!pollTimer) pollTimer = setInterval(refresh, 700);
}

// Each failure mode needs a different action from the user, so name the step
// that actually failed instead of a blanket "starting backend".
function showConnectFailure(result, attempts) {
  const stage = result?.stage;
  const detail = result?.error ? `<span class='detail'>${escapeHtml(result.error)}</span>` : "";

  if (stage === "host-unavailable") {
    statusEl.innerHTML =
      "Can't reach the launcher, so the backend can't be started for you. Run " +
      "<code>./scripts/install.sh</code> once, then reload this extension.<br>" +
      detail;
    statusEl.className = "status err";
    return;
  }

  if (stage === "socket-blocked") {
    statusEl.innerHTML =
      "The backend is running, but the connection to it was refused or blocked." +
      "<br>" +
      detail;
    statusEl.className = "status err";
    return;
  }

  // Still loading models is the normal case for the first ~20s.
  if (attempts < 15) {
    statusEl.textContent = "Starting backend… (loads models, ~20s)";
    statusEl.className = "status";
    return;
  }
  statusEl.innerHTML =
    "Backend still not reachable after 30s. Check <code>backend.log</code> in the " +
    "project folder.<br>" + detail;
  statusEl.className = "status err";
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

async function refresh() {
  // Firefox can unload the background event page, which makes this reject.
  // Unhandled, that kills the polling loop and the popup appears frozen.
  const next = await api.runtime.sendMessage({ type: "get-state" }).catch(() => null);
  if (!next) return;
  state = next;

  // Read the saved selection from module state, not a parameter: the device
  // list arrives asynchronously, so the call that finally populates the
  // dropdowns is usually an interval tick, which passes no arguments.
  if (next.devices && inputEl.options.length === 0) {
    populate(inputEl, next.devices.inputs, storedSettings?.inputDevice ?? next.devices.defaultInput);
    populate(outputEl, next.devices.outputs, storedSettings?.outputDevice ?? next.devices.defaultOutput);
    toggleEl.disabled = false;
  }
  render();
}

function populate(select, items, selected) {
  select.innerHTML = "";
  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.index);
    option.textContent = item.name;
    if (item.index === selected) option.selected = true;
    select.appendChild(option);
  }
}

function render() {
  toggleEl.textContent = state.capturing ? "Stop" : "Start";
  inputEl.disabled = state.capturing;
  outputEl.disabled = state.capturing;

  if (state.error) {
    statusEl.textContent = state.error;
    statusEl.className = "status err";
  } else if (!state.capturing) {
    statusEl.textContent = "Ready";
    statusEl.className = "status";
  } else if (state.phase === "running") {
    statusEl.textContent = "Running — playing isolated speech";
    statusEl.className = "status on";
  } else {
    statusEl.textContent = "Buffering… (takes a few seconds)";
    statusEl.className = "status";
  }
}

toggleEl.addEventListener("click", async () => {
  toggleEl.disabled = true;
  try {
    if (state.capturing) {
      await api.runtime.sendMessage({ type: "stop" });
    } else {
      await api.storage.local.set({
        inputDevice: Number(inputEl.value),
        outputDevice: Number(outputEl.value),
      });
      const result = await api.runtime.sendMessage({
        type: "start",
        tabId: activeTab?.id,
        inputDevice: Number(inputEl.value),
        outputDevice: Number(outputEl.value),
        vocalStrength: Number(strengthEl.value) / 100,
      });
      if (result && !result.ok) {
        statusEl.textContent = result.error;
        statusEl.className = "status err";
      }
    }
    await refresh();
  } finally {
    toggleEl.disabled = false;
  }
});

// Persist device choices as soon as they're made, not only on Start -- the
// popup closes without warning and the selection was being lost.
for (const [select, key] of [
  [inputEl, "inputDevice"],
  [outputEl, "outputDevice"],
]) {
  select.addEventListener("change", async () => {
    const value = Number(select.value);
    storedSettings = { ...(storedSettings || {}), [key]: value };
    await api.storage.local.set({ [key]: value });
  });
}

strengthEl.addEventListener("input", async () => {
  const strength = Number(strengthEl.value) / 100;
  strengthValueEl.textContent = `${strengthEl.value}%`;
  await api.storage.local.set({ vocalStrength: strength });
  api.runtime.sendMessage({ type: "set-strength", vocalStrength: strength }).catch(() => {});
});

window.addEventListener("unload", () => {
  if (pollTimer) clearInterval(pollTimer);
  if (retryTimer) clearInterval(retryTimer);
});

// A throw here leaves the popup blank, which reads as the extension not
// opening at all -- surface it instead.
init().catch((e) => {
  statusEl.textContent = `Popup error: ${e.message}`;
  statusEl.className = "status err";
});

// Show the loaded build, so it's always clear which version is running.
const versionEl = document.getElementById("si-version");
if (versionEl) versionEl.textContent = `v${api.runtime.getManifest().version}`;
