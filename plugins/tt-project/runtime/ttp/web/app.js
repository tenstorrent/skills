// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ago = (ts) => { if (!ts) return "—"; const s = Date.now() / 1000 - ts; return s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${(s / 3600).toFixed(1)}h` : `${Math.round(s / 86400)}d`; };
const money = (x) => `$${(+x || 0).toFixed(2)}`;

let TOKEN = new URLSearchParams(location.hash.slice(1)).get("token") || sessionStorage.getItem("ttp_token") || "";
if (TOKEN) { sessionStorage.setItem("ttp_token", TOKEN); document.cookie = `ttp_token=${TOKEN}; SameSite=Strict; path=/`; history.replaceState(null, "", location.pathname); }
const api = async (path, body) => {
  const r = await fetch(path, { method: body ? "POST" : "GET", headers: { "X-TTP-Token": TOKEN, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) { document.body.innerHTML = "<main><p>Open this page with the link printed by <code>ttp web</code> (it carries the access token).</p></main>"; throw new Error("auth"); }
  return r.json();
};

document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#tabs button, .tab").forEach((x) => x.classList.remove("on"));
  b.classList.add("on"); $("#" + b.dataset.tab).classList.add("on");
  if (b.dataset.tab === "chat") $("#msg").focus();
});

function gateHtml(gates) {
  const ks = Object.keys(gates || {});
  if (!ks.length) return `<p class="muted">No readings yet.</p>`;
  return ks.map((k) => { const g = gates[k], n = g.numbers || {};
    const detail = g.regime === "windows" ? `${n.window}: ${n.utilization}% of account used · project stops at ${n.limit}%`
      : `project ${money(n.spent_24h)} / ${money(n.daily_cap)} per 24h${n.estimated_24h ? ` (~${money(n.estimated_24h)} estimated)` : ""} · ${money(n.spent_7d)} / ${money(n.weekly_cap)} per 7d`;
    return `<div class="row"><b>${esc(k)}</b><span class="pill lv-${g.level}">${g.level}</span><span class="meta">${esc(detail)}</span>
      <span class="meta">${esc((g.reasons || []).join("; "))}</span></div>`; }).join("");
}

// A queued task with a future retry time is waiting on a busy resource, not idle in the queue.
const waiting = (t) => t.status === "queued" && t.not_before && t.not_before > Date.now() / 1000;

function taskRow(t) {
  const label = waiting(t) ? "waiting" : t.status;
  const noteLabel = t.status === "blocked" ? "Blocked" : waiting(t) ? "Waiting" : "Note";
  return `<details class="row"><summary><span class="id">#${t.id}</span> <span class="st st-${t.status}">${label}</span>
    <span class="title">${esc(t.title)}</span> <span class="meta">${esc(t.tier)} · ${money(t.spent_usd)}${t.budget_usd ? " / " + money(t.budget_usd) : ""} · ${ago(t.updated)} ago${t.pr_url ? ` · <a href="${esc(t.pr_url)}" target="_blank" rel="noopener">PR</a>` : ""}</span></summary>
    ${t.blocked_reason ? `<p><b>${noteLabel}:</b> ${esc(t.blocked_reason)}</p>` : ""}${t.result ? `<p>${esc(t.result)}</p>` : ""}
    <p class="meta">origin ${esc(t.origin)} · kind ${esc(t.kind)} · attempts ${t.attempts}${t.branch ? " · branch " + esc(t.branch) : ""}</p>
    ${["queued", "running", "blocked"].includes(t.status) ? `<button class="ghost" onclick="taskAct(${t.id},'cancelled')">Cancel</button>` : ""}
    ${["blocked", "failed"].includes(t.status) ? `<button class="ghost" onclick="taskAct(${t.id},'queued')">Retry</button>` : ""}</details>`;
}
window.taskAct = async (id, status) => { await api(`/api/task/${id}`, { status }); refresh(); };

function bars(el, rows, key, label, cap) {
  if (!rows.length) { el.innerHTML = `<p class="muted">No data yet.</p>`; return; }
  const days = [...new Set(rows.map((r) => r.d))].sort(), W = Math.max(360, days.length * 44), H = 170, P = 28;
  const tot = days.map((d) => rows.filter((r) => r.d === d).reduce((a, r) => Math.max(key === "usd" ? a + (+r.usd || 0) : Math.max(a, +r[key] || 0), a), 0));
  const max = Math.max(cap || 0, ...tot, 1), bw = (W - P * 2) / days.length * 0.7;
  const y = (v) => H - P - (H - P * 2) * v / max;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(label)}">`;
  s += `<line x1="${P}" x2="${W - P}" y1="${H - P}" y2="${H - P}" stroke="currentColor" opacity=".2"/>`;
  if (cap) s += `<line x1="${P}" x2="${W - P}" y1="${y(cap)}" y2="${y(cap)}" stroke="var(--red)" stroke-dasharray="4 3"/><text x="${W - P}" y="${y(cap) - 4}" text-anchor="end">limit ${key === "usd" ? money(cap) : cap + "%"}</text>`;
  days.forEach((d, i) => { const x = P + i * (W - P * 2) / days.length; const v = tot[i];
    s += `<rect x="${x}" y="${y(v)}" width="${bw}" height="${H - P - y(v)}" fill="var(--accent)" rx="2"><title>${d}: ${key === "usd" ? money(v) : v + "%"}</title></rect>`;
    s += `<text x="${x + bw / 2}" y="${H - 10}" text-anchor="middle">${d.slice(5)}</text>`; });
  el.innerHTML = s + `</svg>`;
}

// One-click desktop notifications while this page is open in any tab (localhost is a secure
// context, so no certificate or push service is needed).
const notifyBtn = $("#notify");
if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") notifyBtn.hidden = false;
notifyBtn.onclick = async () => { const r = await Notification.requestPermission(); notifyBtn.hidden = r === "granted" || r === "denied"; };
let seenAlert = +(sessionStorage.getItem("ttp_seen_alert") || 0), unseen = 0;
function announce(items, project) {
  const fresh = items.filter((m) => m.id > seenAlert);
  if (!fresh.length) return;
  if (seenAlert && "Notification" in window && Notification.permission === "granted")
    fresh.slice(-3).forEach((m) => new Notification(`${project}: ${m.kind === "ask" ? "needs you" : "alert"}`, { body: m.text.slice(0, 240), tag: `ttp-${m.id}` }));
  if (document.hidden) unseen += fresh.length;
  seenAlert = Math.max(...fresh.map((m) => m.id)); sessionStorage.setItem("ttp_seen_alert", seenAlert);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) unseen = 0; });

function board(st) {
  const asks = st.attention.filter((m) => m.kind === "ask");
  const cols = [
    ["you", "Waiting on you", st.tasks.filter((t) => t.status === "blocked").map((t) => `#${t.id} ${t.title}`).concat(asks.map((m) => m.text))],
    ["review", "Ready for review", st.tasks.filter((t) => t.status === "review" || (t.pr_url && t.status === "done")).map((t) => `#${t.id} ${t.title}`)],
    ["work", "Working", st.tasks.filter((t) => t.status === "running").map((t) => `#${t.id} ${t.title}`)],
    ["queued", "Queued", st.tasks.filter((t) => t.status === "queued").map((t) => `#${t.id} ${t.title}${waiting(t) ? " (waiting)" : ""}`)],
  ];
  $("#board").innerHTML = cols.map(([cls, name, items]) => `<div class="col ${cls}"><h3>${name}<span class="n">${items.length}</span></h3>` +
    (items.slice(0, 6).map((x) => `<div class="item">${esc(x).slice(0, 160)}</div>`).join("") || `<div class="item muted">—</div>`) + `</div>`).join("");
}

async function refresh() {
  let st;
  try { st = await api("/api/state"); } catch (e) { return; }
  const cfg = st.project.config || {};
  $("#pname").textContent = st.project.name;
  $("#daemon").textContent = st.paused ? "paused" : (st.daemon && st.daemon.pid ? `running on ${st.daemon.host}` : "stopped");
  $("#daemon").className = "pill " + (st.paused ? "lv-orange" : "lv-green");
  announce(st.attention || [], st.project.name);
  document.title = `${unseen ? "(" + unseen + ") " : ""}${st.project.name} · tt-project`;
  board(st);
  $("#attention").innerHTML = (st.attention || []).slice(0, 5).map((m) => `<div class="attn"><span class="when">${ago(m.ts)} ago · ${esc(m.kind)}</span><div>${esc(m.text)}</div></div>`).join("");
  $("#gates").innerHTML = $("#gates2").innerHTML = gateHtml(st.gates);
  const running = st.runs.filter((r) => r.status === "running");
  $("#running").innerHTML = running.length ? running.map((r) => `<div class="row"><span class="id">run ${r.id}</span><span class="title">${esc(r.role)}${r.task ? " · task #" + r.task : ""}</span><span class="meta">${esc(r.provider)} ${esc(r.model || "")} ${esc(r.effort || "")} · ${ago(r.started)}</span></div>`).join("") : `<p class="muted">Idle.</p>`;
  $("#coord").textContent = (st.coordinator && st.coordinator.summary) || "—";
  const done = st.tasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status)).slice(0, 8);
  $("#recent").innerHTML = done.map(taskRow).join("") || `<p class="muted">Nothing yet.</p>`;
  $("#tasklist").innerHTML = st.tasks.map(taskRow).join("") || `<p class="muted">No tasks.</p>`;
  $("#issuelist").innerHTML = st.issues.map((i) => `<div class="row"><span class="id">${esc(i.severity)}</span><span class="title">${esc(i.title)}</span><span class="meta">${esc(i.source)} · seen ${i.count}× · last ${ago(i.last_seen)} ago${i.task ? " · task #" + i.task : ""}</span></div>`).join("") || `<p class="muted">No open issues.</p>`;
  $("#schedlist").innerHTML = `<table><tr><th>Name</th><th>Kind</th><th>Every</th><th>Last run</th><th>7-day cost</th><th>Daily budget</th><th>On</th></tr>` +
    st.schedules.map((s) => `<tr><td><b>${esc(s.name)}</b><div class="meta">${esc(s.description)}</div></td><td>${esc(s.kind)}</td><td>${Math.round(s.every_s / 60)} min</td>
      <td>${ago(s.last_run)} ago<div class="meta">${esc(s.last_status || "")}</div></td><td>${money(s.cost_7d)}</td>
      <td><input type="number" min="0" step="0.5" value="${s.budget_usd_day ?? ""}" style="width:6em" onchange="setSched('${esc(s.name)}',{budget_usd_day:this.value})"></td>
      <td><input type="checkbox" ${s.enabled ? "checked" : ""} onchange="setSched('${esc(s.name)}',{enabled:this.checked})"></td></tr>`).join("") + `</table>`;
  $("#accounts").innerHTML = (st.accounts || []).map((a) => `<div class="row"><b>${esc(a.provider)}</b><span class="meta">${esc(a.account || "unknown")}</span><span class="meta">last used ${ago(a.last)} ago</span></div>`).join("") || `<p class="muted">No runs yet.</p>`;
  bars($("#spendchart"), st.budget.daily_spend, "usd", "daily spend", cfg.budget && cfg.budget.daily_usd);
  bars($("#peakchart"), st.budget.window_peaks, "peak", "window peaks", cfg.budget ? 100 - cfg.budget.reserve_pct : 90);
  $("#sources").innerHTML = `<table>${st.budget.top_sources_7d.map((r) => `<tr><td>${esc(r.source)}</td><td>${esc(r.provider)}</td><td>${money(r.usd)}</td><td class="meta">${r.n} runs</td></tr>`).join("")}</table>`;
  if (document.activeElement !== $("#daily")) $("#daily").value = cfg.budget ? cfg.budget.daily_usd : "";
  if (document.activeElement !== $("#weekly")) $("#weekly").value = cfg.budget ? cfg.budget.weekly_usd : "";
  $("#pause").textContent = st.paused ? "Resume project" : "Pause project";
  $("#pause").onclick = async () => { await api("/api/pause", { paused: !st.paused }); refresh(); };
}
window.setSched = async (name, body) => { await api(`/api/schedule/${encodeURIComponent(name)}`, body); refresh(); };
$("#savecaps").onclick = async () => {
  await api("/api/config", { key: "budget.daily_usd", value: $("#daily").value });
  await api("/api/config", { key: "budget.weekly_usd", value: $("#weekly").value }); refresh();
};

let lastMsg = 0;
async function pollChat() {
  let rows; try { rows = await api(`/api/messages?after=${lastMsg}`); } catch (e) { return; }
  const log = $("#log"), atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  rows.forEach((m) => { lastMsg = Math.max(lastMsg, m.id);
    const d = document.createElement("div"); d.className = `msg ${m.direction} sev-${m.severity}`;
    const where = m.chat === null ? (m.direction === "out" ? " · to everyone" : "")
      : m.chat === "web" ? "" : ` · chat ${esc(m.chat_label || m.chat)}`;
    d.innerHTML = `<div class="b">${esc(m.text)}</div><div class="meta">${ago(m.ts)} ago${where}</div>`;
    log.appendChild(d); });
  if (rows.length && atBottom) log.scrollTop = log.scrollHeight;
}
$("#say").onsubmit = async (e) => { e.preventDefault(); const t = $("#msg").value.trim(); if (!t) return; $("#msg").value = ""; await api("/api/say", { text: t }); pollChat(); };

refresh(); pollChat();
setInterval(() => { if (!document.hidden) { refresh(); pollChat(); } }, 4000);
