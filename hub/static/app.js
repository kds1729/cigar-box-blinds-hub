// Copyright (C) 2026 Keith Sprochi
// SPDX-License-Identifier: GPL-2.0-or-later
// One tab per room; a mock window shows the blind where it last reported itself.
// Click the window to send the blind to that height. Positions are percent open
// (100 = fully open, 0 = closed) on the blind's own scale.

const FULL = 0xFFFF;
let rooms = {};
let current = null;
let token = 0;  // bumped on every new action so stale polling loops stop

const $ = (id) => document.getElementById(id);
const j = async (url, opts) => (await fetch(url, opts)).json();
const post = (url, body) => j(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});

function status(msg, bad) {
  $("status").textContent = msg || "";
  $("status").className = bad ? "bad" : "";
}

function limitPct(r) {
  return r.min_raw == null ? null : r.min_raw / FULL * 100;
}

function topPct(r) {
  return r.max_raw == null ? null : r.max_raw / FULL * 100;
}

// Why pct is off-limits for room r, or null if it's allowed.
function outside(r, pct) {
  const lp = limitPct(r), tp = topPct(r);
  if (lp != null) { if (pct < lp) return "That's inside the no-go zone."; }
  if (tp != null) { if (pct > tp) return "That's above the top line."; }
  return null;
}

function draw() {
  const r = rooms[current];
  const reading = r.last_read;
  $("fabric").style.height = reading ? (100 - reading.pct) + "%" : "0";
  const lp = limitPct(r);
  $("nogo").style.display = lp == null ? "none" : "block";
  if (lp != null) $("nogo").style.height = lp + "%";
  $("limit-text").textContent = lp == null ? "none set" : lp.toFixed(1) + "% open (raw " + r.min_raw.toString(16) + ")";
  const tp = topPct(r);
  $("top-text").textContent = tp == null ? "none set" : tp.toFixed(1) + "% open (raw " + r.max_raw.toString(16) + ")";
}

function showTarget(pct) {
  const t = $("target");
  if (pct == null) { t.style.display = "none"; return; }
  t.style.display = "block";
  t.style.top = (100 - pct) + "%";
}

async function readOnce() {
  const res = await j(`/api/rooms/${current}/position`);
  if (res.error) throw new Error(res.error);
  rooms[current].last_read = {pct: res.pct, raw: res.raw, at: res.at};
  draw();
  return res;
}

// Read until two readings in a row match (the blind has stopped), or give up.
async function settle(myToken, maxReads = 15) {
  let prev = null;
  for (let i = 0; i < maxReads; i++) {
    if (myToken !== token) return;
    status(i === 0 ? "reading position..." : "moving... " + rooms[current].last_read.pct + "%");
    let res;
    try { res = await readOnce(); } catch (e) { if (myToken === token) status(e.message, true); return; }
    if (myToken !== token) return;
    if (maxReads === 1 || (prev !== null && res.raw === prev)) {
      showTarget(null);
      status("at " + res.pct + "%");
      return;
    }
    prev = res.raw;
    await new Promise(r => setTimeout(r, 1000));
  }
  if (myToken === token) status("still changing after " + maxReads + " reads; last " + rooms[current].last_read.pct + "%", true);
}

function selectRoom(id) {
  current = id;
  token++;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.room === id));
  showTarget(null);
  status("");
  draw();
  settle(token, 1);
}

$("window").onclick = (e) => {
  const inner = $("glass").getBoundingClientRect();
  const f = Math.min(1, Math.max(0, (e.clientY - inner.top) / inner.height));
  const pct = Math.round((1 - f) * 1000) / 10;
  // the top line is invisible, so a click above it just goes to the top line (same as Open)
  const tp = topPct(rooms[current]);
  goTo(tp != null && pct > tp ? Math.floor(tp * 10) / 10 : pct);
};

// Open = the highest position allowed: the top line (rounded down so it never lands
// above it), or the motor's own top limit if the room has no line.
$("open").onclick = () => {
  const tp = topPct(rooms[current]);
  goTo(tp == null ? 100 : Math.floor(tp * 10) / 10);
};

// Close = the lowest position allowed: the no-go line (rounded up so it never lands
// below it), or fully closed if the room has no line.
$("close").onclick = () => {
  const lp = limitPct(rooms[current]);
  goTo(lp == null ? 0 : Math.ceil(lp * 10) / 10);
};

async function goTo(pct) {
  const why = outside(rooms[current], pct);
  if (why) { status(why, true); return; }
  const myToken = ++token;
  showTarget(pct);
  status("sending " + pct + "%...");
  const res = await post(`/api/rooms/${current}/position`, {percent: pct});
  if (res.error) { showTarget(null); status(res.error, true); return; }
  settle(myToken);
};

$("stop").onclick = async () => {
  const myToken = ++token;
  status("sending stop...");
  const res = await post(`/api/rooms/${current}/command`, {cmd: "stop"});
  if (res.error) { status(res.error, true); return; }
  showTarget(null);
  settle(myToken);
};

async function setLine(kind, key, name) {
  token++;
  status("reading position to set the " + name + "...");
  const res = await post(`/api/rooms/${current}/limit/${kind}`);
  if (res.error) { status(res.error, true); return; }
  rooms[current][key] = parseInt(res.raw, 16);
  rooms[current].last_read = {pct: res.pct, raw: res.raw, at: res.at};
  draw();
  status(name + " set at " + res.pct + "%");
}

async function clearLine(kind, key, name) {
  await j(`/api/rooms/${current}/limit/${kind}`, {method: "DELETE"});
  delete rooms[current][key];
  draw();
  status(name + " removed");
}

$("set-limit").onclick = () => setLine("bottom", "min_raw", "no-go line");
$("clear-limit").onclick = () => clearLine("bottom", "min_raw", "no-go line");
$("set-top").onclick = () => setLine("top", "max_raw", "top line");
$("clear-top").onclick = () => clearLine("top", "max_raw", "top line");

async function loadSchedule() {
  const rules = await j("/api/schedule");
  const el = $("schedule-grid");
  if (rules.length === 0) { el.innerHTML = "<p>No rules yet.</p>"; return; }
  let html = "<table><tr><th>Room</th><th>Days</th><th>Time</th><th>Action</th><th>On</th><th></th></tr>";
  for (const r of rules) {
    const label = rooms[r.room] ? rooms[r.room].label : r.room;
    html += `<tr><td>${label}</td><td>${r.days.join(", ")}</td><td>${r.time}</td>
      <td>${r.action === "position" ? r.percent + "% open" : r.action}</td>
      <td><input type="checkbox" ${r.enabled ? "checked" : ""} data-toggle="${r.id}"></td>
      <td><button data-del="${r.id}">Delete</button></td></tr>`;
  }
  el.innerHTML = html + "</table>";
  el.querySelectorAll("[data-toggle]").forEach(cb => {
    cb.onchange = () => j(`/api/schedule/${cb.dataset.toggle}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: cb.checked})});
  });
  el.querySelectorAll("[data-del]").forEach(btn => {
    btn.onclick = async () => { await j(`/api/schedule/${btn.dataset.del}`, {method: "DELETE"}); loadSchedule(); };
  });
}

$("add-rule").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  const days = Array.from(form.querySelectorAll('input[name="days"]:checked')).map(c => c.value);
  if (days.length === 0) { status("pick at least one day", true); return; }
  const pct = Number(form.percent.value);
  const why = outside(rooms[form.room.value], pct);
  if (why) { status("Schedule refused: " + why, true); return; }
  await post("/api/schedule", {room: form.room.value, days, time: form.time.value, action: "position", percent: pct});
  loadSchedule();
};

(async function init() {
  rooms = await j("/api/rooms");
  const tabs = $("tabs");
  for (const [id, r] of Object.entries(rooms)) {
    const b = document.createElement("button");
    b.textContent = r.label;
    b.dataset.room = id;
    b.onclick = () => selectRoom(id);
    tabs.appendChild(b);
    const opt = document.createElement("option");
    opt.value = id; opt.textContent = r.label;
    $("rule-room").appendChild(opt);
  }
  selectRoom(Object.keys(rooms)[0]);
  loadSchedule();
})();

// Hover readout: a thin guide line plus the percent-open the cursor is pointing at,
// so a click can be aimed precisely.
(function hoverReadout() {
  const win = $("window");
  win.removeAttribute("title");
  const guide = document.createElement("div");
  guide.id = "hover-guide";
  const tag = document.createElement("div");
  tag.id = "hover-tag";
  win.append(guide, tag);
  win.addEventListener("mousemove", (e) => {
    const inner = $("glass").getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientY - inner.top) / inner.height));
    const pct = Math.round((1 - f) * 1000) / 10;
    const lp = limitPct(rooms[current]);
    guide.style.display = tag.style.display = "block";
    guide.style.top = tag.style.top = (f * 100) + "%";
    tag.textContent = pct.toFixed(1) + "% open" + (lp != null && pct < lp ? " (no-go)" : "");
  });
  win.addEventListener("mouseleave", () => { guide.style.display = tag.style.display = "none"; });
})();
