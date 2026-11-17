// chat-uses stream layout (PLAN.md 5.5, 5.6).
//
// - Shows the target's screen through noVNC, view only: input goes through
//   brain, never through this page.
// - Polls control/scene.json (written by `chat-bcast-scene` in bcast) for
//   full-frame scenes, so "starting soon" and "be right back" work without
//   brain.
// - Connects to brain's websocket for the command log, the grid flash and
//   the name at the pointer (Phase 4), and shows "Controls offline" while
//   it can't.
// - Shows the power vote running (PLAN.md 6.4): what, the count, the time
//   left, then how it ended.
// - Shows the mode meter, and in democracy the commands being voted with
//   their counts instead of the log (PLAN.md 6.3).
// - Shows chat's snapshots (PLAN.md 6.6): the saves to !load and the latest
//   auto snapshots to !rewind to, with thumbnails brain serves.
// - Covers everything the moment brain's feed says "panic" (PLAN.md 7.5),
//   and while brain is unreachable keeps to control/panic.json, which
//   bcast's chat-bcast-panic keeps from the same feed: a panic ends only
//   when brain says so.

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
  hold: { title: "Content check", subtitle: "A moderator is having a look" }, // screen OCR's hold
};

let scene = { scene: "live" };
let feedPanic = null; // what brain's feed says: false, "panic" or "hold" (null: no feed right now)
let filePanic = false; // control/panic.json, for when there's no feed

function panicKind(on, kind) {
  return on === true ? (kind === "hold" ? "hold" : "panic") : false;
}

function applyScene() {
  const panic = feedPanic ?? filePanic;
  // A panic looks like any break; a content hold says what it is.
  const state = panic ? { scene: panic === "hold" ? "hold" : "brb" } : scene;
  const preset = SCENES[state.scene];
  if (!preset) {
    $("scene").hidden = true;
    return;
  }
  $("scene-title").textContent = state.title || preset.title;
  $("scene-subtitle").textContent = state.subtitle || preset.subtitle;
  $("scene").hidden = false;
}

async function readControl(name) {
  try {
    const res = await fetch(`control/${name}`, { cache: "no-store" });
    return res.ok ? await res.json() : null;
  } catch {
    return null; // keep the current state until the file is readable again
  }
}

async function pollScene() {
  const [sceneFile, panicFile] = await Promise.all([readControl("scene.json"), readControl("panic.json")]);
  if (sceneFile) scene = sceneFile;
  if (panicFile) filePanic = panicKind(panicFile.panic, panicFile.kind);
  applyScene();
  setTimeout(pollScene, 1000);
}

// --- brain's feed (control/src/chatuses/overlay.py) -------------------------
//
// Everything in it can come from chat (names, typed text), so it is only
// ever put on the page with textContent, never as HTML.

const LOG_SLOTS = 6; // two columns of three
const FLASH_MS = 3000;
const STATE_MARK = { queued: "…", done: "✓", dropped: "dropped", busy: "busy", later: "soon", refused: "" };
const lines = new Map(); // id -> {line, el}
let brainUp = false;
let targetUp = false;

function setLockdown(on) {
  $("lockdown").hidden = !on;
}

function setControls() {
  const badge = $("controls");
  const [text, cls] = !brainUp
    ? ["Controls offline", "offline"]
    : targetUp
      ? ["Controls online", "online"]
      : ["Machine not taking input", "offline"];
  badge.textContent = text;
  badge.className = `badge ${cls}`;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

// The public site's rules page, bottom right: playing means accepting them.
function setNotice(site) {
  const notice = $("notice");
  const host = typeof site === "string" ? site.replace(/^https?:\/\//, "") : "";
  notice.hidden = !host;
  $("notice-site").textContent = host ? `${host}/rules.html` : "";
}

function renderLine(line) {
  const row = el("div", `log-line ${line.state}`);
  row.append(el("span", `plat ${line.platform}`), el("span", "who", line.user));
  if (line.text) row.append(el("span", "what", line.text));
  if (line.reason) row.append(el("span", "why", line.reason));
  row.append(el("span", "mark", STATE_MARK[line.state] ?? ""));
  return row;
}

function addLine(line) {
  const log = $("log");
  if (!lines.size) log.replaceChildren();
  const entry = { line, el: renderLine(line) };
  lines.set(line.id, entry);
  log.prepend(entry.el); // newest first
  while (lines.size > LOG_SLOTS) {
    const [oldest] = lines.keys();
    lines.get(oldest).el.remove();
    lines.delete(oldest);
  }
}

function setLineState(id, state, reason) {
  const entry = lines.get(id);
  if (!entry) return;
  entry.line.state = state;
  if (reason) entry.line.reason = reason; // brain's fixed wording, e.g. a !click "text" not found
  const fresh = renderLine(entry.line);
  entry.el.replaceWith(fresh);
  entry.el = fresh;
}

let gridTimer;
function flashGrid(cell) {
  const grid = $("grid");
  grid.querySelector(".hit")?.classList.remove("hit");
  if (cell) {
    const [col, row] = cell;
    grid.children[row * GRID_COLS + col]?.classList.add("hit");
  }
  grid.classList.toggle("labels", !cell); // !grid shows every cell's name
  grid.hidden = false;
  clearTimeout(gridTimer);
  gridTimer = setTimeout(() => { grid.hidden = true; }, FLASH_MS);
}

let pointerTimer;
function showPointer(x, y, user) {
  const p = $("pointer");
  p.style.left = `${x}px`;
  p.style.top = `${y}px`;
  p.classList.toggle("left", x > 1300); // keep the label on screen
  p.classList.toggle("below", y < 60);
  $("pointer-who").textContent = user;
  p.hidden = false;
  clearTimeout(pointerTimer);
  pointerTimer = setTimeout(() => { p.hidden = true; }, FLASH_MS);
  flashGrid([Math.floor(x / 100), Math.floor(y / 100)]);
}

// --- the power vote (control/src/chatuses/referee/votes.py) ---------------------

const VOTE_ENDED = {
  running: "Passed: happening now",
  done: "Done",
  error: "Didn't work",
  failed: "Failed",
  vetoed: "Vetoed",
  cancelled: "Ended",
};
let vote = null;
let voteAt = 0; // when it arrived: ends_in counts from then
let voteTimer;

function voteBar(yes, no) {
  const bar = el("div", "vote-bar");
  const part = el("div", "vote-yes");
  part.style.width = yes + no ? `${(100 * yes) / (yes + no)}%` : "0";
  bar.append(part);
  return bar;
}

function renderVote() {
  const panel = $("vote-panel");
  const box = $("vote");
  const death = runView?.death;
  panel.classList.toggle("dying", Boolean(death));
  if (death) {
    // The machine died (game/runs.py): the death vote takes the vote panel's place.
    const left = death.ends_in === null ? null : Math.max(0, Math.ceil(death.ends_in - (performance.now() - runAt) / 1000));
    panel.classList.remove("active");
    box.className = "value";
    box.replaceChildren(
      el("div", "vote-what", "💀 The machine died"),
      el("div", "death-cause", sentence(death.cause)),
      ...(left === null
        ? [el("div", "vote-need", "No lives left: the run is over")]
        : [
            el("div", "vote-time", `${left} s left`),
            voteBar(death.revive, death.rip),
            el("div", "vote-need", `${death.revive} revive · ${death.rip} rip · a revive costs a life`),
            el("div", "vote-how", "!revive   !rip"),
          ]),
    );
    return;
  }
  if (!vote) {
    panel.classList.remove("active");
    box.className = "value muted";
    box.textContent = "No vote running";
    return;
  }
  const open = vote.phase === "voting" || vote.phase === "veto";
  const left = Math.max(0, Math.ceil(vote.ends_in - (performance.now() - voteAt) / 1000));
  panel.classList.toggle("active", open);
  box.className = "value";
  const parts = [el("div", "vote-what", vote.text), el("div", "vote-by", `by ${vote.by}`)];
  if (vote.phase === "voting") {
    const needs = `needs ${vote.quorum} yes and ${Math.round(vote.threshold * 100)}%`;
    parts.push(
      el("div", "vote-time", `${left} s left`),
      voteBar(vote.yes, vote.no),
      el("div", "vote-need", `${vote.yes} yes · ${vote.no} no · ${needs}`),
      el("div", "vote-how", "!yes   !no"),
    );
  } else if (vote.phase === "veto") {
    parts.push(
      el("div", "vote-time", `Passed: happens in ${left} s`),
      el("div", "vote-need", `!veto to stop it (${vote.vetoes} of ${vote.veto_needed})`),
    );
  } else {
    const how = VOTE_ENDED[vote.phase] ?? vote.phase;
    const detail = vote.result && vote.phase !== "running" ? `: ${vote.result}` : "";
    parts.push(el("div", `vote-result ${vote.phase}`, how + detail));
  }
  box.replaceChildren(...parts);
}

// --- the run (control/src/chatuses/game/runs.py) ----------------------------------

let runView = null;
let runAt = 0; // when it arrived: uptime and the death vote's time count from then
let runTimer;

function sentence(text) {
  return text ? `${text[0].toUpperCase()}${text.slice(1)}.` : "";
}

function hhmmss(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n) => String(n).padStart(2, "0");
  return `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

function renderRun() {
  const v = runView;
  $("run").textContent = v ? `#${v.number}` : "#—";
  if (!v) {
    $("lives").textContent = "";
    $("uptime").textContent = "—";
  } else {
    const max = Math.min(10, v.lives_max ?? 0);
    $("lives").textContent = v.lives === null ? "∞" : "♥".repeat(Math.min(v.lives, 10)) + "♡".repeat(Math.max(0, max - v.lives));
    $("uptime").textContent = hhmmss(v.uptime + (performance.now() - runAt) / 1000);
  }
  if (v?.death) renderVote(); // its countdown
}

function setRun(v) {
  runView = v ?? null;
  runAt = performance.now();
  renderRun();
  renderVote();
  clearInterval(runTimer);
  if (runView) runTimer = setInterval(renderRun, 250);
}

// --- chaos (control/src/chatuses/game/chaos.py) ------------------------------------------

let chaosView = null;
let chaosAt = 0;
let chaosTimer = null;
const CHAOS_LOOKS = ["flip", "quake", "blackout"]; // done here; the rest is brain's

function relabelGrid(grid) {
  const cols = grid?.cols ?? Array.from({ length: GRID_COLS }, (_, c) => String.fromCharCode(65 + c));
  const rows = grid?.rows ?? Array.from({ length: GRID_ROWS }, (_, r) => String(r + 1));
  [...$("cols").children].forEach((span, i) => { span.textContent = cols[i] ?? ""; });
  [...$("rows").children].forEach((span, i) => { span.textContent = rows[i] ?? ""; });
}

function renderChaos() {
  const v = chaosView;
  const left = v ? Math.max(0, v.ends_in - (performance.now() - chaosAt) / 1000) : 0;
  const on = v && left > 0 ? v.key : null;
  for (const look of CHAOS_LOOKS) document.body.classList.toggle(`chaos-${look}`, on === look);
  $("blackout").hidden = on !== "blackout";
  $("chaos-panel").hidden = !on;
  if (!on) return;
  $("chaos-name").textContent = `🌀 ${v.name}`;
  $("chaos-left").textContent = `${Math.ceil(left)} s`;
  $("chaos-by").textContent = v.source === "random" ? "by chance" : `from ${v.by}`;
}

function setChaos(v) {
  chaosView = v ?? null;
  chaosAt = performance.now();
  relabelGrid(chaosView?.grid ?? null);
  renderChaos();
  clearInterval(chaosTimer);
  if (chaosView) chaosTimer = setInterval(renderChaos, 250);
}

// --- the driver's seat (control/src/chatuses/game/driver.py) ---------------------------

let driverView = null;
let driverAt = 0;
let driverTimer = null;

function renderDriver() {
  const v = driverView;
  const panel = $("driver-panel");
  const left = v ? Math.max(0, v.ends_in - (performance.now() - driverAt) / 1000) : 0;
  if (!v || left <= 0) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  panel.classList.toggle("offered", v.state === "offered");
  $("driver-name").textContent = `🚗 ${v.name}`;
  $("driver-left").textContent = `${Math.ceil(left)} s`;
  $("driver-what").textContent =
    v.state === "offered" ? "Any command takes the wheel" : "Only their commands run · !impeach";
}

function setDriver(v) {
  driverView = v ?? null;
  driverAt = performance.now();
  renderDriver();
  clearInterval(driverTimer);
  if (driverView) driverTimer = setInterval(renderDriver, 250);
}

// --- achievements (control/src/chatuses/game/achievements.py) -------------------------

const TOAST_SECONDS = 8;
const toasts = []; // shown one after the other
let toastTimer;

function showToast(a) {
  if (a) toasts.push(a);
  if (toastTimer || !toasts.length) return;
  const next = toasts.shift();
  $("toast-title").textContent = `🏆 ${next.name}`;
  $("toast-text").textContent = [next.description, ...(next.rewards || [])].join(" · ");
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
    toastTimer = null;
    showToast(null);
  }, TOAST_SECONDS * 1000);
}

let cardTimer;

function showCard(card) {
  clearTimeout(cardTimer);
  const box = $("card");
  if (!card) {
    box.hidden = true;
    return;
  }
  const death = card.kind === "death";
  box.className = `card-scene ${death ? "death" : "start"}`;
  $("card-title").textContent = death ? `💀 RUN #${card.number} IS DEAD` : `RUN #${card.number} BEGINS`;
  $("card-cause").textContent = death ? sentence(card.cause) : "";
  const lives = card.lives === null || card.lives === undefined ? "" : `${"♥".repeat(Math.min(card.lives, 10))} ${card.lives} lives`;
  $("card-facts").textContent = death
    ? `Lived ${hhmmss(card.lived)} · ${card.lives_used} ${card.lives_used === 1 ? "life" : "lives"} used`
    : `${lives}${lives ? " · " : ""}a fresh machine · good luck`;
  const blow = death ? card.final_blow : null;
  $("card-blow").textContent = blow ? `Probably killed by ${blow.name} (${blow.text}). No offence.` : "";
  const last = death ? (card.last_commands || []).slice(0, 10) : [];
  $("card-last").replaceChildren(...last.map((c) => {
    const li = el("li");
    li.append(el("span", "who", c.name), ` ${c.text}`);
    return li;
  }));
  box.hidden = false;
  cardTimer = setTimeout(() => { box.hidden = true; }, Math.max(1, card.seconds) * 1000);
}

function setVote(v) {
  vote = v ?? null;
  voteAt = performance.now();
  clearInterval(voteTimer);
  renderVote();
  if (vote && (vote.phase === "voting" || vote.phase === "veto")) voteTimer = setInterval(renderVote, 250);
}

// --- anarchy and democracy (control/src/chatuses/referee/modes.py) --------------

let modeView = null;
let modeAt = 0; // when it arrived: locked_for and ends_in count from then
let modeTimer;

function renderTally(tally, elapsed) {
  const box = $("tally");
  if (!tally) {
    box.replaceChildren();
    return;
  }
  const left = tally.ends_in === null ? null : Math.max(0, Math.ceil(tally.ends_in - elapsed));
  const head = tally.total
    ? `Democracy · the most voted runs in ${left} s · ${tally.total} vote${tally.total === 1 ? "" : "s"}`
    : "Democracy · type a command: the most voted runs";
  const rows = [el("div", "tally-head", head)];
  tally.options.forEach((option, i) => {
    const row = el("div", `tally-row${i === 0 ? " lead" : ""}`);
    const fill = el("div", "fill");
    fill.style.width = `${(100 * option.votes) / Math.max(1, tally.total)}%`;
    row.append(fill, el("span", "what", option.text), el("span", "votes", String(option.votes)));
    rows.push(row);
  });
  box.replaceChildren(...rows);
}

function renderMode() {
  const v = modeView;
  const elapsed = (performance.now() - modeAt) / 1000;
  const democracy = v?.mode === "democracy";
  $("meter-marker").style.left = `${v ? v.meter : 50}%`;
  if (v) {
    $("tick-anarchy").style.left = `${v.anarchy_at}%`;
    $("tick-democracy").style.left = `${v.democracy_at}%`;
  }
  $("mode-name").textContent = democracy ? "Democracy" : "Anarchy";
  $("mode-name").className = `value ${democracy ? "democracy" : "anarchy"}`;
  const lock = v ? Math.max(0, Math.ceil(v.locked_for - elapsed)) : 0;
  $("mode-lock").textContent = lock ? `locked ${lock} s` : "";
  $("log").hidden = democracy;
  $("tally").hidden = !democracy;
  $("ticker").hidden = democracy;
  $("ticker-democracy").hidden = !democracy;
  renderTally(democracy ? v.tally : null, elapsed);
}

function setMode(view) {
  modeView = view ?? null;
  modeAt = performance.now();
  clearInterval(modeTimer);
  renderMode();
  if (modeView && (modeView.locked_for || modeView.tally?.ends_in)) modeTimer = setInterval(renderMode, 250);
}

// --- time travel (control/src/chatuses/snapshots.py) ----------------------------

const REWINDS = 3; // auto snapshots shown: !rewind, !rewind 2, !rewind 3
let thumbsBase = ""; // brain's address over HTTP: thumbnails are at /thumbs/<id>.png

function shot(snap, caption, kind) {
  const box = el("div", `shot ${kind}${snap ? " taken" : ""}`);
  const pic = el("div", "pic");
  if (snap && snap.thumb && thumbsBase) {
    const img = document.createElement("img");
    img.alt = "";
    img.src = `${thumbsBase}/thumbs/${snap.id}.png`;
    pic.append(img);
  }
  box.append(pic, el("div", "cap", caption));
  return box;
}

function setTimeline(view) {
  if (!view) {
    $("saves").replaceChildren();
    $("timeline").replaceChildren();
    return;
  }
  const slots = Math.min(3, view.slots ?? 3);
  const saves = [];
  for (let i = 0; i < slots; i++) {
    const save = view.saves[i];
    saves.push(shot(save, save ? save.label : "empty", "save"));
  }
  $("saves").replaceChildren(...saves);
  const autos = view.timeline.filter((s) => s.kind === "auto").slice(0, REWINDS);
  const rewinds = [];
  for (let i = 0; i < REWINDS; i++) {
    rewinds.push(shot(autos[i], i ? `!rewind ${i + 1}` : "!rewind", "auto"));
  }
  $("timeline").replaceChildren(...rewinds);
}

function onMessage(msg) {
  switch (msg.type) {
    case "hello":
      feedPanic = panicKind(msg.panic, msg.panic_kind);
      applyScene();
      lines.clear();
      $("log").replaceChildren(el("div", "log-line muted", "Waiting for chat…"));
      for (const line of msg.log) addLine(line);
      targetUp = msg.target;
      setControls();
      setLockdown(msg.lockdown === true);
      setVote(msg.vote);
      setTimeline(msg.timeline);
      setMode(msg.mode);
      setRun(msg.run);
      showCard(msg.card);
      setDriver(msg.driver);
      setChaos(msg.chaos);
      setNotice(msg.site);
      break;
    case "chaos":
      setChaos(msg.chaos);
      break;
    case "driver":
      setDriver(msg.driver);
      break;
    case "run":
      setRun(msg.run);
      break;
    case "card":
      showCard(msg);
      break;
    case "achievement":
      showToast(msg);
      break;
    case "mode":
      setMode(msg);
      break;
    case "timeline":
      setTimeline(msg);
      break;
    case "vote":
      setVote(msg.vote);
      break;
    case "panic":
      feedPanic = panicKind(msg.on, msg.kind);
      applyScene();
      break;
    case "lockdown":
      setLockdown(msg.on === true);
      break;
    case "log":
      addLine(msg);
      break;
    case "log-state":
      setLineState(msg.id, msg.state, msg.reason);
      break;
    case "pointer":
      showPointer(msg.x, msg.y, msg.user);
      break;
    case "grid":
      flashGrid(null);
      break;
    case "status":
      targetUp = msg.target;
      setControls();
      break;
  }
}

function connectBrain(url) {
  if (!url) {
    setControls();
    return;
  }
  let ws;
  try {
    ws = new WebSocket(url);
  } catch {
    setTimeout(() => connectBrain(url), 5000);
    return;
  }
  ws.addEventListener("open", () => { brainUp = true; setControls(); });
  ws.addEventListener("message", (event) => {
    try {
      onMessage(JSON.parse(event.data));
    } catch (err) {
      console.error("bad message from brain", err);
    }
  });
  ws.addEventListener("close", () => {
    brainUp = false;
    feedPanic = null; // back to panic.json until brain answers again
    applyScene();
    setVote(null); // the vote lives in brain
    setRun(null); // so does the run
    setDriver(null); // and the driver's seat
    setChaos(null); // chaos too: brain ends it
    showCard(null);
    setMode(null); // so does the mode: anarchy's look until it's back
    setControls();
    setTimeout(() => connectBrain(url), 3000);
  });
}

function drawGrid() {
  const grid = $("grid");
  for (let r = 1; r <= GRID_ROWS; r++) {
    for (let c = 0; c < GRID_COLS; c++) {
      grid.append(el("span", "", `${String.fromCharCode(65 + c)}${r}`));
    }
  }
}

function tickClock() {
  $("clock").textContent = new Date().toLocaleTimeString("en-GB", { hour12: false });
}

async function main() {
  drawGridLabels();
  drawGrid();
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
  if (config.brainUrl) {
    thumbsBase = config.brainUrl.replace(/^ws/, "http").replace(/\/ws\/overlay$/, "");
  }
  connectBrain(config.brainUrl);
}

main();
