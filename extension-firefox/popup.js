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

async function init() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  activeTab = tab;

  const stored = await api.storage.local.get(["vocalStrength", "inputDevice", "outputDevice"]);
  const strength = typeof stored.vocalStrength === "number" ? stored.vocalStrength : 1.0;
  strengthEl.value = Math.round(strength * 100);
  strengthValueEl.textContent = `${Math.round(strength * 100)}%`;

  const connectResult = await api.runtime.sendMessage({ type: "connect" });
  if (!connectResult || !connectResult.ok) {
    // Name the exact command -- "start the backend" isn't actionable enough.
    statusEl.innerHTML =
      'Backend not running. In a terminal, run:<br><code>streamerisolate serve</code>';
    statusEl.className = "status err";
    return;
  }

  await refresh(stored);
  pollTimer = setInterval(refresh, 700);
}

async function refresh(stored) {
  const next = await api.runtime.sendMessage({ type: "get-state" });
  if (!next) return;
  state = next;

  if (next.devices && inputEl.options.length === 0) {
    populate(inputEl, next.devices.inputs, stored?.inputDevice ?? next.devices.defaultInput);
    populate(outputEl, next.devices.outputs, stored?.outputDevice ?? next.devices.defaultOutput);
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

strengthEl.addEventListener("input", async () => {
  const strength = Number(strengthEl.value) / 100;
  strengthValueEl.textContent = `${strengthEl.value}%`;
  await api.storage.local.set({ vocalStrength: strength });
  api.runtime.sendMessage({ type: "set-strength", vocalStrength: strength }).catch(() => {});
});

window.addEventListener("unload", () => {
  if (pollTimer) clearInterval(pollTimer);
});

init();
