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
    // The native host starts the backend for us, so the usual case here is
    // simply "still loading models" -- say that rather than crying error.
    // Only after a while do we suggest the manual fallback.
    connectAttempts += 1;
    if (connectAttempts < 15) {
      statusEl.textContent = "Starting backend… (loads models, ~20s)";
      statusEl.className = "status";
    } else {
      statusEl.innerHTML =
        "Backend still not reachable. Run <code>./scripts/install.sh</code> once, " +
        "or start it manually with <code>streamerisolate serve</code>." +
        "<br><span class='detail'></span>";
      statusEl.className = "status err";
      const detail = statusEl.querySelector(".detail");
      if (detail && result && result.error) detail.textContent = result.error;
    }
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

async function refresh() {
  const next = await api.runtime.sendMessage({ type: "get-state" });
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

init();
