// chat-uses stream layout (PLAN.md 5.5, 5.6).
//
// - Shows the target's screen through noVNC, view only: input goes through
//   brain, never through this page.
// - Polls control/scene.json (written by `chat-bcast-scene` in bcast) for
//   full-frame scenes, so "starting soon" and "be right back" work without
//   brain.
// - Connects to brain's websocket for live HUD data (Phase 4) and shows
//   "Controls offline" while it can't.

import RFB from "./novnc/core/rfb.js";

const GRID_COLS = 16;
const GRID_ROWS = 9;

// Encodings noVNC offers that would hurt the stream:
// - the cursor sent separately: noVNC would show it as the local CSS cursor,
//   which never appears in the capture. Without these, TigerVNC draws the
//   real cursor into the picture.
// - JPEG and H.264: lossy, they blur text before our own encoder does.
//   bcast and the target share a host, so bandwidth doesn't matter.
const DROPPED_ENCODINGS = new Set([-239, 0x574d5664, 21, 50]);
for (let q = -32; q <= -23; q++) DROPPED_ENCODINGS.add(q); // JPEG quality levels
const sendEncodings = RFB.messages.clientEncodings;
RFB.messages.clientEncodings = function (sock, encodings) {
  return sendEncodings.call(this, sock, encodings.filter((e) => !DROPPED_ENCODINGS.has(e)));
};

const $ = (id) => document.getElementById(id);

function drawGridLabels() {
  for (let c = 0; c < GRID_COLS; c++) {
    const span = document.createElement("span");
    span.textContent = String.fromCharCode(65 + c);
    $("cols").append(span);
  }
  for (let r = 1; r <= GRID_ROWS; r++) {
    const span = document.createElement("span");
    span.textContent = String(r);
    $("rows").append(span);
  }
}

function connectScreen() {
  const rfb = new RFB($("screen"), `ws://${location.host}/websockify`, { shared: true });
  rfb.viewOnly = true;
  rfb.focusOnClick = false;
  rfb.compressionLevel = 1; // same host: spend little CPU on compression
  rfb.background = "#000";
  rfb.addEventListener("connect", () => { $("screen-lost").hidden = true; });
  rfb.addEventListener("disconnect", () => {
    $("screen-lost").hidden = false;
    setTimeout(connectScreen, 2000);
  });
}

const SCENES = {
  starting: { title: "Starting soon", subtitle: "Get your !commands ready" },
  brb: { title: "We'll be right back", subtitle: "" },
};

function applyScene(state) {
  const preset = SCENES[state.scene];
  if (!preset) {
    $("scene").hidden = true;
    return;
  }
  $("scene-title").textContent = state.title || preset.title;
  $("scene-subtitle").textContent = state.subtitle || preset.subtitle;
  $("scene").hidden = false;
}

async function pollScene() {
  try {
    const res = await fetch("control/scene.json", { cache: "no-store" });
    if (res.ok) applyScene(await res.json());
  } catch {
    // Keep the current scene until the file is readable again.
  }
  setTimeout(pollScene, 1000);
}

function setControls(online) {
  const badge = $("controls");
  badge.textContent = online ? "Controls online" : "Controls offline";
  badge.className = `badge ${online ? "online" : "offline"}`;
}

// Phase 4 defines the messages; until then the badge is all this does.
function connectBrain(url) {
  if (!url) {
    setControls(false);
    return;
  }
  let ws;
  try {
    ws = new WebSocket(url);
  } catch {
    setControls(false);
    setTimeout(() => connectBrain(url), 5000);
    return;
  }
  ws.addEventListener("open", () => setControls(true));
  ws.addEventListener("close", () => {
    setControls(false);
    setTimeout(() => connectBrain(url), 5000);
  });
}

function tickClock() {
  $("clock").textContent = new Date().toLocaleTimeString("en-GB", { hour12: false });
}

async function main() {
  drawGridLabels();
  tickClock();
  setInterval(tickClock, 1000);
  connectScreen();
  pollScene();
  let config = {};
  try {
    config = await (await fetch("control/config.json", { cache: "no-store" })).json();
  } catch {
    // No config: no brain to connect to.
  }
  connectBrain(config.brainUrl);
}

main();
