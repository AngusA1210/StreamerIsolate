// Pure frame-presentation scheduling, split out from content.js so it can be
// tested without a browser (see tests/test_frame_scheduler.js).
//
// The rule: a decoded frame is shown at its own due time, not whenever it
// happens to finish decoding. Decode latency varies, and painting on decode
// completion hands that variance straight to the viewer as uneven motion.
// When several frames are already due -- i.e. we fell behind -- only the
// newest is worth painting; the older ones are stale and would just stutter.

(() => {
  /**
   * Chooses which buffered frame to paint now.
   *
   * @param {Array<{presentAt: number}>} readyFrames oldest first; due frames
   *   are removed from this array.
   * @param {number} now current time, same clock as presentAt.
   * @returns {{due: object|null, dropped: Array<object>}} the frame to paint
   *   (if any) and any stale frames the caller must release.
   */
  function pickDueFrame(readyFrames, now) {
    let due = null;
    const dropped = [];
    while (readyFrames.length && readyFrames[0].presentAt <= now) {
      if (due) dropped.push(due);
      due = readyFrames.shift();
    }
    return { due, dropped };
  }

  globalThis.__streamerIsolateScheduler = { pickDueFrame };
})();
