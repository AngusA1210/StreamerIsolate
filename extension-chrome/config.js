// Shared tunables, loaded as a classic (non-module) script by both
// background.js (via importScripts) and offscreen.html (via <script src>).
// content.js gets these values by message instead, since it's injected
// standalone into the page.

const CHUNK_SECONDS = 3.0;
const OVERLAP_SECONDS = 0.75;

// Starting estimate for end-to-end latency: a full chunk must buffer before
// the first inference can even start, plus slack for inference and the
// WebSocket round trip. The video overlay holds frames back by this much at
// first; once audio is flowing, offscreen.js measures the real latency and
// corrects this (see "measured-delay"), so this only has to be close.
const TARGET_DELAY_SECONDS = 3.5;
