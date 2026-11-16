// brain's admin dashboard: talks to /api/* with the token (dashboard.py).
// Names, typed text and reasons can come from chat: they only ever go on
// the page as textContent.
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN = "chat-uses-admin-token";
const NAME = "chat-uses-admin-name";
let auditOldest = null;

function stored(key) {
  try { return sessionStorage.getItem(key) || ""; } catch { return ""; }
}
function store(key, value) {
  try { value ? sessionStorage.setItem(key, value) : sessionStorage.removeItem(key); } catch { /* private mode */ }
}

async function api(path, body) {
  const headers = { Authorization: `Bearer ${stored(TOKEN)}` };
  const name = stored(NAME);
  if (name) headers["X-Moderator"] = name;
  const init = { headers, cache: "no-store" };
  if (body !== undefined) {
    init.method = "POST";
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  if (res.status === 401) {
    showLogin("That token didn't work.");
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error((await res.text()).trim() || `HTTP ${res.status}`);
  return res.json();
}

function toast(text, ok = true) {
  const m = $("message");
  m.textContent = text;
  m.className = `toast ${ok ? "good" : "bad"}`;
  m.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { m.hidden = true; }, 6000);
}

async function act(path, body) {
  try {
    const answer = await api(path, body);
    toast(answer.text, answer.ok);
    await refresh();
    return answer.ok;
  } catch (err) {
    if (err.message !== "unauthorized") toast(err.message, false);
    return false;
  }
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function when(ts) {
  if (!ts) return "forever";
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString() : d.toLocaleString();
}

function userLink(id, label) {
  const a = el("a", label || id);
  a.href = "#";
  a.addEventListener("click", (e) => {
    e.preventDefault();
    $("audit-user").value = id;
    loadAudit(true);
  });
  return a;
}

function renderState(s) {
  const panic = s.panic;
  $("panic-card").classList.toggle("panicking", Boolean(panic));
  $("panic-state").textContent = panic
    ? `PANIC since ${when(panic.since)}, by ${panic.by}${panic.reason ? `: ${panic.reason}` : ""}`
    : "Live: chat has control";
  $("panic").hidden = Boolean(panic);
  $("panic-reason").hidden = Boolean(panic);
  $("resume").hidden = !panic;
  $("status").textContent = s.status;
  const locked = Boolean(s.lockdown);
  $("lockdown-state").textContent = locked
    ? `Lockdown on since ${when(s.lockdown.since)}, by ${s.lockdown.by}`
    : "Lockdown off: everyone can send commands";
  $("lockdown").textContent = locked ? "End lockdown" : "Lockdown";
  $("lockdown").dataset.on = locked ? "1" : "";
  const rows = s.banned.map((b) => {
    const tr = el("tr");
    const who = el("td");
    who.append(userLink(b.user, b.name ? `${b.name} (${b.user})` : b.user));
    const lift = el("button", "Unban", "quiet");
    lift.addEventListener("click", () => act("/api/unban", { who: b.user }));
    const cell = el("td");
    cell.append(lift);
    tr.append(who, el("td", when(b.until)), cell);
    return tr;
  });
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "Nobody", "muted");
    td.colSpan = 3;
    tr.append(td);
    rows.push(tr);
  }
  $("bans").tBodies[0].replaceChildren(...rows);
}

function auditRow(c) {
  const tr = el("tr", null, `outcome-${c.outcome}`);
  const who = el("td");
  who.append(userLink(c.user, c.display_name || "?"));
  tr.append(el("td", when(c.ts)), who, el("td", c.user, "id"), el("td", c.raw, "raw"),
    el("td", c.reason ? `${c.outcome}: ${c.reason}` : c.outcome));
  return tr;
}

async function loadAudit(fresh) {
  const params = new URLSearchParams({ limit: "100" });
  const user = $("audit-user").value.trim();
  if (user) params.set("user", user);
  if (!fresh && auditOldest) params.set("before", auditOldest);
  const { commands } = await api(`/api/commands?${params}`);
  const body = $("audit").tBodies[0];
  const rows = commands.map(auditRow);
  if (fresh) body.replaceChildren(...rows);
  else body.append(...rows);
  if (commands.length) auditOldest = String(commands[commands.length - 1].id);
  else if (fresh) auditOldest = null;
  $("audit-more").hidden = commands.length < 100;
}

function lasted(seconds) {
  return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

function eventDetails(e) {
  const parts = [];
  if (e.user) parts.push(e.user);
  if (e.until) parts.push(`until ${when(e.until > 253402300000 ? 0 : e.until)}`);
  if (e.on !== undefined) parts.push(e.on ? "on" : "off");
  if (e.by) parts.push(`by ${e.by}`);
  if (e.name) parts.push(e.name);
  if (e.instance) parts.push(`of ${e.instance}`);
  if (e.result && e.result !== "ok") parts.push(e.result);
  if (e.seconds !== undefined) parts.push(`after ${lasted(e.seconds)}`);
  let text = parts.join(" ");
  if (e.reason) text += `: ${e.reason}`;
  if (e.error) text += `: ${e.error}`;
  if (e.mode) text += `${text ? " " : ""}(${e.mode})`;
  return text;
}

// What screen OCR saw holds a slur: blurred until hovered, and the screenshot on demand.
function seenOnScreen(e) {
  const cell = el("span");
  const lines = e.found || e.lines || [];
  if (lines.length) cell.append(el("span", lines.join(" · "), "spoiler"));
  if (e.screenshot) {
    const link = el("a", "screenshot");
    link.href = "#";
    link.addEventListener("click", async (ev) => {
      ev.preventDefault();
      try {
        const res = await fetch(`/api/screens/${encodeURIComponent(e.screenshot)}`, {
          headers: { Authorization: `Bearer ${stored(TOKEN)}` },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        window.open(URL.createObjectURL(await res.blob()), "_blank", "noopener");
      } catch (err) {
        toast(`Screenshot: ${err.message}`, false);
      }
    });
    cell.append(" ", link);
  }
  return cell;
}

async function loadEvents() {
  const { events } = await api("/api/events?limit=50");
  $("events").tBodies[0].replaceChildren(...events.map((e) => {
    const tr = el("tr", null, `event-${e.type}`);
    const details = el("td", eventDetails(e));
    if (e.type === "ocr_hit" || e.type === "hold") details.append(" ", seenOnScreen(e));
    tr.append(el("td", when(e.ts)), el("td", e.type), details);
    return tr;
  }));
}

async function refresh() {
  renderState(await api("/api/state"));
  await loadEvents();
}

let timer;
async function tick() {
  try {
    await refresh();
    if (!auditOldest || $("audit").tBodies[0].rows.length <= 100) await loadAudit(true);
  } catch (err) {
    if (err.message === "unauthorized") return;
    toast(`Can't reach brain: ${err.message}`, false);
  }
  timer = setTimeout(tick, 3000);
}

function showLogin(error) {
  clearTimeout(timer);
  store(TOKEN, "");
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-name").value = stored(NAME);
  $("login-error").textContent = error || "";
  $("login-error").hidden = !error;
  $("login-token").focus();
}

function start() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("who").textContent = stored(NAME) ? `as ${stored(NAME)}` : "";
  tick();
}

$("login").addEventListener("submit", (e) => {
  e.preventDefault();
  store(TOKEN, $("login-token").value.trim());
  store(NAME, $("login-name").value.trim());
  $("login-token").value = "";
  start();
});
$("logout").addEventListener("click", () => showLogin());
$("panic").addEventListener("click", () => act("/api/panic", { reason: $("panic-reason").value }).then((ok) => {
  if (ok) $("panic-reason").value = "";
}));
$("resume").addEventListener("click", () => {
  if (confirm("Is the screen safe? Resume puts the machine back on stream.")) act("/api/resume", {});
});
$("lockdown").addEventListener("click", () => act("/api/lockdown", { on: !$("lockdown").dataset.on }));
$("ban-form").addEventListener("submit", (e) => {
  e.preventDefault();
  act("/api/ban", { who: $("ban-who").value, duration: $("ban-duration").value, reason: $("ban-reason").value })
    .then((ok) => { if (ok) $("ban-form").reset(); });
});
$("audit-user").addEventListener("change", () => loadAudit(true).catch((err) => toast(err.message, false)));
$("audit-clear").addEventListener("click", () => {
  $("audit-user").value = "";
  loadAudit(true).catch((err) => toast(err.message, false));
});
$("audit-more").addEventListener("click", () => loadAudit(false).catch((err) => toast(err.message, false)));

if (stored(TOKEN)) start();
else showLogin();
