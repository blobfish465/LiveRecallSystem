/*
 * LiveRecall browser client: WebSocket wiring and rendering.
 * The rules (revision replacement, durability, bounded history) live in store.js.
 *
 * What this file does:
 *   - connects to the server, sends CLIENT_HELLO, feeds every message to the store;
 *   - renders one row per segment and updates it in place (no duplicate lines);
 *   - "What did I miss?" from local state only (no message to the server);
 *   - reconnects automatically with backoff, resuming from the latest final;
 *   - shows the connection state at all times.
 *
 * Configuration comes from the page URL, so the browser can run on a different
 * machine from the server:
 *   ?ws=ws://10.0.2.15:8765   server address (default: this page's host, port 8765)
 *   ?session=L1               session name (default L1)
 *   ?history=60               seconds of context asked for on a fresh join (1-60, default 60)
 */
"use strict";

const params = new URLSearchParams(location.search);
const WS_URL = params.get("ws") || `ws://${location.hostname || "127.0.0.1"}:8765`;
const SESSION = params.get("session") || "L1";
const HISTORY_SECONDS = clampInt(params.get("history"), 1, 60, 60);
const LIVE_ROWS = 10;           // segments shown in the live panel
const RECALL_DEFAULT_S = 30;    // spec: "What did I miss?" starts at 30 s
const RECONNECT_MIN_MS = 500;   // first retry after half a second...
const RECONNECT_MAX_MS = 5000;  // ...doubling up to 5 s between attempts
const DROP_MS = 5000;           // length of the demo's simulated network drop
const STALL_MS = 30000;         // no message for this long while "connected" => assume a dead link

const RECALL_HINT = "Shows finalized captions already stored in this page; nothing is requested from the server.";

const statusEl = document.getElementById("status");
const liveEl = document.getElementById("live");
const recallEl = document.getElementById("recall");
const recallTitleEl = document.getElementById("recall-title");
const statsEl = document.getElementById("stats");
const recallButtons = [...document.querySelectorAll("button[data-seconds]")];

const CLIENT_ID = numericClientId();
const store = new CaptionStore();
const rows = new Map();  // segment -> its <div> in the live panel
const counters = { connections: 0, live: 0, history: 0, duplicate: 0, stale: 0, invalid: 0, restarts: 0 };
let ws = null;
let reconnectTimer = null;
let reconnectDelay = RECONNECT_MIN_MS;
let holdUntil = 0;       // stay disconnected until this time (simulated network drop)
let recallSeconds = null;  // window of the open recall panel, null until one is chosen
let stallTimer = null;
let stalled = false;     // the watchdog (not the network) closed the connection
let resumedFrom = 0;     // last_segment sent in the current CLIENT_HELLO (0 = fresh join)

function clampInt(raw, min, max, fallback) {
  const n = Number.parseInt(raw, 10);
  return Number.isNaN(n) ? fallback : Math.min(max, Math.max(min, n));
}

// One id per tab (sessionStorage), so two tabs are two clients, as in the demo.
// If site storage is blocked, fall back to a fresh random id for this page load.
function numericClientId() {
  const key = "liverecall-client-id";
  const fresh = String(Math.floor(100000 + Math.random() * 900000));
  try {
    const stored = sessionStorage.getItem(key);
    if (stored) return Number(stored);
    sessionStorage.setItem(key, fresh);
  } catch {
    // storage unavailable: use the per-load id
  }
  return Number(fresh);
}

// ---------------------------------------------------------------- connection

function connect() {
  reconnectTimer = null;
  setStatus("connecting", `connecting to ${WS_URL}`);
  try {
    ws = new WebSocket(WS_URL);
  } catch (err) {
    // Only a malformed ?ws= value gets here; retrying would not help.
    setStatus("disconnected", `invalid server address ${WS_URL} (${err.message})`);
    return;
  }

  ws.onopen = () => {
    counters.connections += 1;
    reconnectDelay = RECONNECT_MIN_MS;
    const resume = store.resumeState();  // latest *final* we hold, or 0/0 for a fresh client
    resumedFrom = resume.last_segment;
    ws.send(JSON.stringify({
      type: "CLIENT_HELLO",
      client_id: CLIENT_ID,
      session: SESSION,
      last_segment: resume.last_segment,
      last_revision: resume.last_revision,
      history_seconds: HISTORY_SECONDS,
    }));
    setStatus("connected", resume.last_segment > 0
      ? `connected (resumed after segment ${resume.last_segment})`
      : `connected (fresh join, asked for ${HISTORY_SECONDS} s of context)`);
    renderStats();
    armStallWatchdog();
  };

  ws.onmessage = (event) => {
    armStallWatchdog();
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      counters.invalid += 1;
      return;
    }
    if (!msg || typeof msg !== "object") {
      counters.invalid += 1;
      return;
    }
    let newFinal = false;
    if (msg.type === "CAPTION_UPDATE") {
      newFinal = applyUpdate(msg, "live");
    } else if (msg.type === "HISTORY" && Array.isArray(msg.items)) {
      const restartsBefore = counters.restarts;
      const duplicatesBefore = counters.duplicate;
      const recovered = [];
      for (const item of msg.items) {  // oldest -> newest
        if (applyUpdate(item, "history")) {
          newFinal = true;
          recovered.push(item.segment);
        }
      }
      // Recovered context (late join or reconnect) is shown at once in the recall
      // panel; otherwise the student would only see the 10 newest live rows.
      if (msg.items.length > 0 && recallSeconds === null) recallSeconds = RECALL_DEFAULT_S;
      if (counters.restarts === restartsBefore) {
        reportRecovery(recovered, counters.duplicate - duplicatesBefore);
      }
    }
    // Any other message type is ignored on purpose.
    syncLiveView();
    if (newFinal && recallSeconds !== null) showRecall(recallSeconds);  // keep an open recall panel current
    renderStats();
  };

  ws.onclose = (event) => {
    ws = null;
    clearTimeout(stallTimer);
    if (stalled) {
      // Only quiet, not broken: shown as "connecting" (amber), not as a failure.
      stalled = false;
      scheduleReconnect("no captions for 30 s, re-checking the link", "connecting");
    } else {
      scheduleReconnect(describeClose(event));
    }
  };

  ws.onerror = () => {
    // The browser always fires a close event after an error; that schedules the reconnect.
  };
}

// Say on screen what the HISTORY reply brought, so a reconnect visibly recovers
// exactly the missed finals (and no duplicates) without reading the stats line.
function reportRecovery(segments, duplicates) {
  const range = segments.length > 0 ? ` (segments ${segments[0]}–${segments[segments.length - 1]})` : "";
  const plural = segments.length === 1 ? "" : "s";
  if (resumedFrom > 0) {
    setStatus("connected", `connected (resumed after segment ${resumedFrom}): recovered ` +
      `${segments.length} missed caption${plural}${range}, ${duplicates} duplicate${duplicates === 1 ? "" : "s"}`);
  } else {
    setStatus("connected", `connected (fresh join): ${segments.length} recent caption${plural} loaded${range}`);
  }
}

// HISTORY items and live updates go through the same rule, so a final recovered
// after a reconnect can never duplicate or overwrite one we already show.
// Returns true when a new final was accepted.
function applyUpdate(msg, origin) {
  const verdict = store.accept(msg, origin === "history");
  if (verdict === "restart") {
    // The server restarted (see CaptionStore.accept). Rejoin as a fresh client
    // so HISTORY brings the new run's recent context. Reset again because the
    // store kept the triggering update; if it was a final, the hello would
    // otherwise resume after it and skip the new run's earlier finals. The
    // update comes back through HISTORY (final) or its next revision.
    counters.restarts += 1;
    store.reset();
    if (ws !== null) ws.close(1000, "stream restarted, rejoining");
    return msg.final === true;
  }
  if (verdict !== "accepted") {
    counters[verdict] += 1;
    return false;
  }
  counters[origin] += 1;
  return msg.final === true;
}

// A browser cannot see WebSocket pings, so a link that dies silently (Wi-Fi
// off and on, NAT timeout) would leave the page "connected" with frozen
// captions. If nothing arrives for STALL_MS we close and rejoin; a merely
// silent lecture costs only a harmless resume from the latest final.
function armStallWatchdog() {
  clearTimeout(stallTimer);
  stallTimer = setTimeout(() => {
    if (ws !== null) {
      stalled = true;
      ws.close(1000, `no data for ${STALL_MS / 1000} s, checking the link`);
    }
  }, STALL_MS);
}

// A short human-readable cause for the status line. The server's own reason
// text is shown when it sent one (e.g. 1013 "client too far behind").
function describeClose(event) {
  if (event.reason) return event.reason;
  if (event.code === 1006) return "server unreachable";  // no close handshake: refused, reset or dropped
  if (event.code && event.code !== 1000) return `close code ${event.code}`;
  return "";
}

function scheduleReconnect(why, state = "disconnected") {
  if (reconnectTimer !== null) return;
  const hold = Math.max(0, holdUntil - Date.now());
  const delay = Math.max(hold, reconnectDelay);
  const retry = `reconnecting in ${(delay / 1000).toFixed(1)} s`;
  if (state === "connecting") {
    setStatus("connecting", `${why}; ${retry}`);
  } else {
    setStatus("disconnected", `disconnected${why ? ` (${why})` : ""}; ${retry}`);
  }
  reconnectTimer = setTimeout(connect, delay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

// Demo helper: behave like a network outage for `ms` milliseconds while the
// server and the source keep running. On reconnect the HISTORY reply must fill
// the gap exactly once. Recall keeps working meanwhile: it is local.
function dropConnection(ms = DROP_MS) {
  holdUntil = Date.now() + ms;
  if (ws !== null) {
    ws.close(1000, "simulated drop");  // the close event schedules the reconnect, honouring holdUntil
    return;
  }
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  scheduleReconnect("simulated drop");
}

// ----------------------------------------------------------------- rendering

function setStatus(state, text) {
  statusEl.dataset.state = state;
  statusEl.textContent = text;
}

// Bring the live panel in line with the store: one row per segment, updated in
// place, kept in segment order, oldest rows dropped beyond LIVE_ROWS.
function syncLiveView() {
  const visible = store.latest(LIVE_ROWS);
  const keep = new Set();
  let previous = null;
  for (const entry of visible) {
    keep.add(entry.segment);
    let row = rows.get(entry.segment);
    if (!row) {
      row = makeRow(entry.segment);
      rows.set(entry.segment, row);
    }
    updateRow(row, entry);
    const expectedAt = previous ? previous.nextSibling : liveEl.firstChild;
    if (row !== expectedAt) liveEl.insertBefore(row, expectedAt);  // moves it if already attached
    previous = row;
  }
  for (const [segment, row] of rows) {
    if (!keep.has(segment)) {
      row.remove();
      rows.delete(segment);
    }
  }
  liveEl.scrollTop = liveEl.scrollHeight;
}

function makeRow(segment) {
  const row = document.createElement("div");
  row.className = "caption";
  row.dataset.segment = String(segment);
  const meta = document.createElement("span");
  meta.className = "meta";
  // The state is spelled out in a badge, not only shown by colour, so it is
  // readable at a glance (and for colour-blind students).
  const badge = document.createElement("span");
  badge.className = "badge";
  const text = document.createElement("span");
  text.className = "text";
  row.append(meta, badge, text);
  return row;
}

function updateRow(row, entry) {
  row.classList.toggle("final", entry.final);
  row.classList.toggle("provisional", !entry.final);
  row.dataset.revision = String(entry.revision);
  row.querySelector(".meta").textContent = `#${entry.segment} r${entry.revision}`;
  row.querySelector(".badge").textContent = entry.final ? "✓ final" : "… provisional";
  row.querySelector(".text").textContent = entry.text;  // textContent, never innerHTML: caption text is untrusted
}

// "What did I miss?": the finals from the last `seconds` of lecture time,
// taken from the store. No message is sent to the server. The panel is
// refreshed whenever a new final arrives, so recovered finals show up in it.
function showRecall(seconds) {
  recallSeconds = seconds;
  const items = store.finalsWithin(seconds);
  for (const button of recallButtons) {
    button.classList.toggle("active", Number(button.dataset.seconds) === seconds);
  }
  const note = ws === null ? " Disconnected: this may be incomplete." : "";
  if (items.length === 0) {
    recallTitleEl.textContent = `Last ${seconds} s: no finalized captions yet.${note}`;
  } else {
    const first = items[0].segment;
    const last = items[items.length - 1].segment;
    recallTitleEl.textContent =
      `Last ${seconds} s: ${items.length} finalized caption${items.length === 1 ? "" : "s"} (segments ${first}–${last}).${note}`;
  }
  recallEl.replaceChildren(...items.map((entry) => {
    const p = document.createElement("p");
    p.dataset.segment = String(entry.segment);
    const age = document.createElement("span");
    age.className = "age";
    age.textContent = `${Math.round(store.newestTs - entry.source_ts)} s ago`;  // on the lecture clock
    const text = document.createElement("span");
    text.textContent = entry.text;  // textContent, never innerHTML: caption text is untrusted
    p.append(age, text);
    return p;
  }));
}

function hideRecall() {
  recallSeconds = null;
  for (const button of recallButtons) button.classList.remove("active");
  recallTitleEl.textContent = RECALL_HINT;
  recallEl.replaceChildren();
}

function renderStats() {
  statsEl.textContent =
    `client ${CLIENT_ID} · ${WS_URL} · session ${SESSION} · connections ${counters.connections}` +
    ` · live ${counters.live} · recovered ${counters.history} · duplicates ${counters.duplicate}` +
    ` · stale ${counters.stale} · invalid ${counters.invalid} · restarts ${counters.restarts}` +
    ` · segments held ${store.segments.size}`;
}

// ------------------------------------------------------------------- wiring

for (const button of recallButtons) {
  button.addEventListener("click", () => showRecall(Number(button.dataset.seconds)));
}
document.getElementById("recall-hide").addEventListener("click", hideRecall);
document.getElementById("drop").addEventListener("click", () => dropConnection());

// For tests and for the browser console (F12): liverecall.store, liverecall.counters, ...
window.liverecall = { store, counters, dropConnection, showRecall, get ws() { return ws; } };

renderStats();
connect();
