/*
 * LiveRecall browser client: local caption state (the rules and the data).
 *
 * This file never touches the DOM or the network, so it can be exercised on
 * its own: tests/test_client.py drives it through the page, and you can poke
 * at it from the browser console as `liverecall.store`.
 *
 * It mirrors the server's CaptionState (liverecall/state.py):
 *   - one entry per segment: the latest revision we have accepted;
 *   - a final is durable: no later message may replace it;
 *   - an older or equal revision is never shown after a newer one;
 *   - history is bounded by time (a window) and by count.
 *
 * Time is measured on the *lecture clock* (the source_ts the source stamps on
 * every caption), never on the browser's clock. The browser and the server may
 * run on different machines whose clocks disagree, and a final recovered via
 * HISTORY keeps its original timestamp, so "the last 30 s" means the last 30 s
 * of lecture time before the newest caption we hold.
 */
"use strict";

const STORE_WINDOW_S = 120;   // keep finals for 120 s of lecture time (spec: at least 60 s)
const STORE_MAX_ITEMS = 600;  // ...and never more than this many segments, whatever the rate

class CaptionStore {
  constructor(windowS = STORE_WINDOW_S, maxItems = STORE_MAX_ITEMS) {
    this.windowS = windowS;
    this.maxItems = maxItems;
    this.segments = new Map();  // segment number -> {segment, revision, final, text, source_ts}
    this.newestTs = 0;          // newest source_ts seen: the lecture clock
    this.floor = 0;             // every segment <= floor has been pruned; stragglers are rejected
  }

  /**
   * Validate and apply one CAPTION_UPDATE (live, or from HISTORY when fromHistory is true).
   * Returns "accepted", "restart", "duplicate", "stale" or "invalid"; "duplicate",
   * "stale" and "invalid" change nothing.
   */
  accept(msg, fromHistory = false) {
    const entry = CaptionStore.parse(msg);
    if (!entry) return "invalid";
    let verdict = "accepted";
    if (!fromHistory && entry.segment <= this.topFinalSegment()) {
      // Within one run the server only forwards revisions newer than its own
      // state, which includes everything it already sent us, so with a source
      // that finalizes segments in order (the provided one) a *live* update for
      // a segment at or below our newest final cannot happen. It means the
      // server (and with it the source) restarted and numbering began again at
      // 1. Start over instead of rejecting the whole new run as stale.
      this.reset();
      verdict = "restart";
    }
    if (entry.segment <= this.floor) return "stale";  // already pruned: too old to matter
    const current = this.segments.get(entry.segment);
    if (current) {
      if (entry.revision === current.revision) return "duplicate";
      if (current.final || entry.revision < current.revision) return "stale";
    }
    this.segments.set(entry.segment, entry);
    if (entry.source_ts > this.newestTs) this.newestTs = entry.source_ts;
    if (entry.final) this.prune();  // once per segment is often enough
    return verdict;
  }

  reset() {
    this.segments.clear();
    this.newestTs = 0;
    this.floor = 0;
  }

  /** Highest segment number we hold as final, 0 if none. */
  topFinalSegment() {
    let top = 0;
    for (const entry of this.segments.values()) {
      if (entry.final && entry.segment > top) top = entry.segment;
    }
    return top;
  }

  /** Drop entries outside the time window, then the oldest beyond the count cap. */
  prune() {
    const cutoff = this.newestTs - this.windowS;
    for (const [segment, entry] of this.segments) {
      if (entry.source_ts < cutoff) this.forget(segment);
    }
    if (this.segments.size > this.maxItems) {
      const oldest = [...this.segments.keys()].sort((a, b) => a - b);
      for (const segment of oldest.slice(0, this.segments.size - this.maxItems)) this.forget(segment);
    }
  }

  forget(segment) {
    this.segments.delete(segment);
    if (segment > this.floor) this.floor = segment;
  }

  /**
   * What CLIENT_HELLO reports: the final with the highest segment number.
   * Never a provisional: the server sends every final after this point, so
   * reporting a provisional would skip finals we have not seen.
   * Known limit of the protocol's single (segment, revision) tuple: a final
   * missing *below* this point could not be asked for. It cannot happen with
   * a source that finalizes segments in order, like the provided one.
   */
  resumeState() {
    let best = null;
    for (const entry of this.segments.values()) {
      if (entry.final && (best === null || entry.segment > best.segment)) best = entry;
    }
    return best
      ? { last_segment: best.segment, last_revision: best.revision }
      : { last_segment: 0, last_revision: 0 };
  }

  /** Finals from the last `seconds` of lecture time, oldest first. Local data only. */
  finalsWithin(seconds) {
    const cutoff = this.newestTs - seconds;
    return this.sorted().filter((e) => e.final && e.source_ts >= cutoff);
  }

  /** The newest `count` segments (provisional or final), oldest first: the live view. */
  latest(count) {
    const all = this.sorted();
    return all.slice(Math.max(0, all.length - count));
  }

  sorted() {
    return [...this.segments.values()].sort((a, b) => a.segment - b.segment);
  }

  /** Field validation, same rules as the server's make_caption(); unknown fields are ignored. */
  static parse(msg) {
    if (!msg || typeof msg !== "object") return null;
    const { segment, revision, source_ts, text } = msg;
    if (!Number.isInteger(segment) || segment <= 0) return null;
    if (!Number.isInteger(revision) || revision <= 0) return null;
    if (typeof source_ts !== "number" || !Number.isFinite(source_ts)) return null;
    if (typeof text !== "string") return null;
    return { segment, revision, final: msg.final === true, text, source_ts };
  }
}

// Make the class reachable from the console and from test scripts (a top-level
// `class` in a classic script is not a property of window by itself).
if (typeof window !== "undefined") window.CaptionStore = CaptionStore;
