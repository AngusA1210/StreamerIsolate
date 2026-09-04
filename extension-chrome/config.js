// Shared tunables, loaded as a classic (non-module) script by both
// background.js (via importScripts) and offscreen.html (via <script src>).
// content.js gets these values by message instead, since it's injected
// standalone into the page.

const CHUNK_SECONDS = 3.0;
const OVERLAP_SECONDS = 0.75;

// Rough end-to-end latency budget: a full chunk must buffer before the
// first inference can even start, plus some slack for Demucs/classifier
// inference and the WebSocket round trip. The video overlay holds frames
// back by this much so it roughly lines up with when the corresponding
// audio actually plays. Not measured per-session -- a fixed estimate.
const TARGET_DELAY_SECONDS = 4.0;
