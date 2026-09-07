// Tests for extension-chrome/frame-scheduler.js.
//
// Run with macOS's built-in JavaScriptCore (no npm needed):
//   tests/run_js_tests.sh

load("extension-chrome/frame-scheduler.js");
const { pickDueFrame } = globalThis.__streamerIsolateScheduler;

let failures = 0;

function check(name, condition, detail) {
  if (condition) {
    print(`  ok   ${name}`);
  } else {
    failures++;
    print(`  FAIL ${name}${detail ? " -- " + detail : ""}`);
  }
}

function frames(...presentTimes) {
  return presentTimes.map((presentAt, i) => ({ presentAt, id: i }));
}

print("frame scheduler");

// Nothing is due yet: hold the last painted frame rather than running ahead.
{
  const buf = frames(100, 200);
  const { due, dropped } = pickDueFrame(buf, 50);
  check("holds when nothing is due", due === null && dropped.length === 0);
  check("leaves undue frames buffered", buf.length === 2);
}

// Exactly one due: paint it.
{
  const buf = frames(100, 200);
  const { due, dropped } = pickDueFrame(buf, 150);
  check("paints the due frame", due && due.id === 0, due && `got id ${due.id}`);
  check("drops nothing", dropped.length === 0);
  check("keeps the future frame", buf.length === 1 && buf[0].id === 1);
}

// Several due at once (we fell behind): paint the newest, report the rest so
// the caller can release them. Painting the older ones would be a stutter.
{
  const buf = frames(100, 110, 120, 500);
  const { due, dropped } = pickDueFrame(buf, 200);
  check("paints the newest due frame", due && due.id === 2, due && `got id ${due.id}`);
  check("reports the stale ones", dropped.length === 2, `got ${dropped.length}`);
  check("never leaks a stale frame", dropped.map((f) => f.id).join(",") === "0,1");
  check("keeps the future frame", buf.length === 1 && buf[0].id === 3);
}

// A frame due exactly now counts as due.
{
  const buf = frames(100);
  const { due } = pickDueFrame(buf, 100);
  check("boundary is inclusive", due && due.id === 0);
}

// Empty buffer must not throw.
{
  const buf = [];
  const { due, dropped } = pickDueFrame(buf, 1000);
  check("handles an empty buffer", due === null && dropped.length === 0);
}

// Every frame taken from the buffer is either painted or reported, so the
// caller can always close exactly what it removed -- decoded frames are a
// scarce resource and leaking them stalls the decoder.
{
  const buf = frames(10, 20, 30, 40, 50);
  const before = buf.length;
  const { due, dropped } = pickDueFrame(buf, 35);
  const accounted = (due ? 1 : 0) + dropped.length + buf.length;
  check("accounts for every frame", accounted === before, `${accounted} vs ${before}`);
}

print(failures === 0 ? "\nframe scheduler: all passed" : `\nframe scheduler: ${failures} FAILED`);
if (failures > 0) throw new Error(`${failures} failing assertions`);
