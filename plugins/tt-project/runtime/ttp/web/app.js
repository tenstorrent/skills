// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
"use strict";
const $ = (s) => document.querySelector(s);
// Page updates go through render.js: unchanged parts are left alone, held ones wait for the user.
const R = window.TTPRender, put = (el, html) => R.patch(el, html), text = (el, s) => R.text(el, s);
const agoH = R.agoHtml;
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ago = R.ago;
const money = (x) => `$${(+x || 0).toFixed(2)}`;
const at = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";

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
    const detail = g.detail || (g.regime === "windows" ? `${n.window}: ${n.utilization}% of account used · project stops at ${n.limit}%`
      : `project ${money(n.spent_24h)} / ${money(n.daily_cap)} per 24h${n.estimated_24h ? ` (~${money(n.estimated_24h)} estimated)` : ""} · ${money(n.spent_7d)} / ${money(n.weekly_cap)} per 7d`);
    return `<div class="row"><b>${esc(k)}</b><span class="pill lv-${g.level}">${g.level}</span><span class="meta">${esc(detail)}</span>
      <span class="meta">${esc((g.reasons || []).join("; "))}</span></div>`; }).join("");
}

// A queued task deferred by the coordinator waits for its start time or start_when probe.
const deferred = (t) => t.status === "queued" && !!t.starts;

// A queued task with a future retry time is waiting on a busy resource, not idle in the queue.
const waiting = (t) => t.status === "queued" && !deferred(t) && t.not_before && t.not_before > Date.now() / 1000;

// A queued task on a logged-out provider is held until a run on it works again; one on a provider
// whose API host does not resolve is held until it does.
const heldAs = (t) => t.status !== "queued" ? "" : (t.blocked_reason || "").startsWith("held: logged out") ? "held: logged out"
  : (t.blocked_reason || "").startsWith("held: network,") ? "held: network" : "";
const held = (t) => !!heldAs(t);

function taskRow(t) {
  const label = deferred(t) ? esc(t.starts) : waiting(t) ? "waiting" : held(t) ? heldAs(t) : t.review_since ? `review ${agoH(t.review_since)}` : t.status === "pushing" ? "approved, pushing" : esc(t.status);
  const noteLabel = t.status === "blocked" ? "Blocked" : waiting(t) ? "Waiting" : held(t) ? "Held" : "Note";
  return `<details class="row"><summary><span class="id">#${t.id}</span> <span class="st st-${t.status}">${label}</span>
    <span class="title">${esc(t.title)}</span> <span class="meta">${esc(t.tier)} · ${money(t.spent_usd)}${t.budget_usd ? " / " + money(t.budget_usd) : ""} · ${agoH(t.updated)} ago${t.pr_url ? ` · <a href="${esc(t.pr_url)}" target="_blank" rel="noopener">PR</a>` : ""}</span></summary>
    ${t.blocked_reason ? `<p><b>${noteLabel}:</b> ${esc(t.blocked_reason)}</p>` : ""}${t.result ? `<p>${esc(t.result)}</p>` : ""}
    ${(t.pushed || []).map((x) => `<p class="meta">${x.status === "landed" ? "already on the branch" : "pushed"} ${esc((x.sha || "").slice(0, 7))}${x.version ? " as " + esc(x.version) : ""} (${esc(x.branch || "?")})</p>`).join("")}
    <p class="meta">origin ${esc(t.origin)} · kind ${esc(t.kind)} · attempts ${t.attempts}${t.branch ? " · branch " + esc(t.branch) : ""}</p>
    ${["queued", "running", "blocked", "pushing"].includes(t.status) ? `<button class="ghost" onclick="taskAct(${t.id},'cancelled')">Cancel</button>` : ""}
    ${["blocked", "failed"].includes(t.status) ? `<button class="ghost" onclick="taskAct(${t.id},'queued')">Retry</button>` : ""}</details>`;
}
window.taskAct = async (id, status) => { const r = await api(`/api/task/${id}`, { status }); if (r.error) alert(r.error); refresh(); };

function bars(el, rows, key, label, cap) {
  if (!rows.length) { put(el, `<p class="muted">No data yet.</p>`); return; }
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
  put(el, s + `</svg>`);
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
  // An item is its text, cut to fit, then an optional HTML tail (an age).
  const cols = [
    ["you", "Waiting on you", st.tasks.filter((t) => t.status === "blocked").map((t) => [`#${t.id} ${t.title}${t.blocked_reason ? ` — ${t.blocked_reason}` : ""}`])
      .concat(asks.map((m) => [`${m.text}`, "", `ask ${m.id}, ${agoH(m.ts)} ago: `]))],
    ["review", "Ready for review", st.tasks.filter((t) => t.status === "review" || (t.pr_url && t.status === "done")).map((t) => [`#${t.id} ${t.title}`, t.review_since ? ` (in review ${agoH(t.review_since)})` : ""])],
    ["work", "Working", st.tasks.filter((t) => t.status === "running").map((t) => [`#${t.id} ${t.title}`])],
    ["queued", "Queued", st.tasks.filter((t) => t.status === "queued").map((t) => [`#${t.id} ${t.title}${deferred(t) ? ` (${t.starts})` : waiting(t) ? ` (waiting, next try ${at(t.not_before)})` : ""}`])],
  ];
  R.rows($("#board"), cols.map(([cls, name, items]) => ({ key: cls, html: `<div class="col ${cls}"><h3>${name}<span class="n">${items.length}</span></h3>` +
    (items.slice(0, 6).map(([x, tail, head]) => `<div class="item">${head || ""}${esc(x.slice(0, cls === "you" ? 320 : 160))}${tail || ""}</div>`).join("") || `<div class="item muted">—</div>`) + `</div>` })));
}

let lastOk = 0;
// `cmd` in server text becomes <code>cmd</code>; the rest is escaped.
const codes = (t) => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>");
function banner(html) {
  put($("#banner"), html);
  $("#banner").hidden = !html;
}

// The push queue card: what waits, the live batch, and the recent batches with their deploy.
function pushQueueHtml(q, now) {
  const ent = (q.entries || []).map((e) => `<div class="row"><span class="id">#${e.task}</span><span class="st st-${esc(e.status)}">${esc(e.status)}</span>` +
    `<span class="title">${esc(e.title || "")}</span><span class="meta">${esc(e.branch || "?")} ${esc(e.head)} · ${agoH(Math.round(now - e.age_s))} old${e.pushed_sha ? ` · ${esc(e.pushed_sha)}` : ""}</span></div>`).join("");
  const b = (q.last || []).map((x) => `<div class="row"><span class="id">${esc(x.id)}</span><span class="st ${x.outcome === "pushed" ? "st-done" : ["pushed", "landed", "nothing"].includes(x.outcome) ? "" : "st-failed"}">${esc(x.outcome || "?")}</span>` +
    `<span class="meta">${x.pushed_sha ? esc(x.pushed_sha.slice(0, 7)) + (x.version ? " as " + esc(x.version) : "") + " · " : ""}${x.check_runs != null ? `checks ${x.check_runs} run${x.check_runs === 1 ? "" : "s"}${x.check_s != null ? ` in ${Math.round(x.check_s)} s` : ""} · ` : ""}` +
    `deploy ${esc(x.after_push || (q.live && q.live.id === x.id ? "running" : "pending"))} · ${agoH(x.ended || x.started)} ago</span></div>`).join("");
  return (ent ? `<h3>Entries</h3>${ent}` : `<p class="muted">Nothing waiting.</p>`) + (b ? `<h3>Recent batches</h3>${b}` : "");
}

function healthHtml(h) {
  const c = h.coordinator, parts = [];
  parts.push(c.last_turn ? `last turn ${agoH(c.last_turn)} ago${c.last_status ? ` (${esc(c.last_status)})` : ""}` : "no turn yet");
  if (c.failures) parts.push(`<span class="lv-red">${c.failures} failed in a row</span>`);
  if (c.backoff_until) parts.push(`retry at ${at(c.backoff_until)}`);
  if (c.idle_wake) parts.push(`next idle check ${at(c.idle_wake)}`);
  else if (c.idle_held) parts.push(`idle check ${esc(c.idle_held)}`);
  return `<div class="row"><span class="meta">${parts.join(" · ")}</span></div>` + h.providers_paused.map((p) =>
    `<div class="row"><b>${esc(p.provider)}</b><span class="pill lv-red">paused until ${at(p.until)}</span><span>${esc(p.note)}</span><span class="meta">fix: ${esc(p.fix)}</span></div>`).join("") +
    (h.breakers || []).map((b) => `<div class="row"><b>${esc(b.provider)}</b><span class="pill lv-red">logged out</span><span>${esc(b.line)}</span></div>`).join("") +
    (h.schedules_broken ? `<div class="row"><span class="pill lv-red">${esc(h.schedules_broken)}</span></div>` : "") +
    (h.upstream ? `<div class="row"><span class="meta">${esc(h.upstream)}</span></div>` : "") +
    (h.resources_paused || []).map((r) =>
    `<div class="row"><b>${esc(r.resource)}</b><span class="pill lv-orange">resource paused</span><span>${esc(r.reason || "")}</span><span class="meta">since ${at(r.since)} by ${esc(r.by || "user")}: its tasks wait</span><button data-resume-resource="${esc(r.resource)}">Resume</button></div>`).join("");
}

let lastKey = "";
// One delegated listener: rows patched later (held updates, R.flush) keep working.
$("#chealth").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-resume-resource]");
  if (!b) return;
  await api("/api/pause", { resource: b.dataset.resumeResource, paused: false }); refresh();
});

async function refresh() {
  let st;
  try { st = await api("/api/state"); } catch (e) {
    if (e.message === "auth") return;
    lastKey = "";
    // The page keeps its last data; say so rather than let it look current.
    // Only this computer can reopen a tunnel, so the page says how; the daemon's service restarts it by itself.
    const help = localStorage.getItem("ttp_offline_help") || "If the project runs on another machine, the SSH tunnel from this computer is down: `ttp web <project> --tunnel` reopens it, and `--keep` keeps it up. If it runs on this computer, its daemon is down and its service restarts it within a few minutes.";
    banner(`Cannot reach the project's daemon (${esc(e.message || e)}). What you see is from ${lastOk ? ago(lastOk / 1000) + " ago" : "earlier"} and may be out of date.<br>${codes(help)}`);
    text($("#daemon"), "unreachable"); $("#daemon").className = "pill lv-red";
    return;
  }
  lastOk = Date.now();
  const hb = st.heartbeat;
  const stuck = hb && hb.age > st.heartbeat_stale_s;
  // Nothing new (ticking ages aside): leave the page alone and only move the ages on. The minute
  // in the key still re-renders once a minute for what the page derives from the clock.
  const key = R.key(st, `${!!stuck}|${Math.floor(Date.now() / 60000)}`);
  if (key === lastKey) { R.flush(); R.tick(); return; }
  lastKey = key;
  if (st.offline_help) localStorage.setItem("ttp_offline_help", st.offline_help);
  // A daemon started before an upgrade serves this file without the health fields until it restarts.
  const h = st.health || { spend: {}, coordinator: {}, providers_paused: [], asks: [], running: 0, why_idle: "" };
  const cfg = st.project.config || {};
  const disk = st.disk_low;
  // The daemon's service restarts a stuck daemon by itself; a command is named only where none does.
  const svc = st.service, fix = !svc ? `<code>ttp restart ${esc(st.project.name)}</code> restarts it.`
    : svc.watchdog ? `Its watchdog restarts it after ${Math.round((st.watchdog_s || 600) / 60)} min without a tick.`
    : `Its service predates the watchdog: <code>ttp restart ${esc(st.project.name)}</code> restarts it and adds one.`;
  banner([stuck ? `The daemon has not completed a tick for ${Math.round(hb.age / 60)} min: nothing new starts. ${fix}` : "",
          disk ? `Only ${disk.free_gb} GB free under ${esc(disk.path)}: only questions and plans start until ${st.disk && st.disk.resume_gb ? st.disk.resume_gb + " GB are" : "space is"} free.` : "",
          h.undelivered ? `${h.undelivered.asks} question(s) not delivered to any chat since ${at(h.undelivered.since)}: is the chat relay running? Answer here meanwhile.${h.undelivered.below_floor ? ` ${h.undelivered.below_floor} of them are below every chat's severity floor.` : ""}` : ""].filter(Boolean).join("<br>"));
  text($("#pname"), st.project.name);
  text($("#daemon"), st.paused ? "paused" : stuck ? "stuck" : (st.daemon && st.daemon.pid ? `running on ${st.daemon.host}` : "stopped"));
  $("#daemon").className = "pill " + (stuck ? "lv-red" : st.paused ? "lv-orange" : "lv-green");
  text($("#spend"), h.spend.headline || `24h ${money(h.spend.spent_24h)}`);
  $("#spend").title = h.spend.detail || "all providers";
  const needs = st.tasks.filter((t) => t.status === "blocked").length + (st.attention || []).length;
  $("#needs").hidden = !needs; text($("#needs"), `${needs} need${needs === 1 ? "s" : ""} you`);
  $("#hostline").hidden = !h.host; text($("#hostline"), h.host || "");
  $("#sleepline").hidden = !h.idle_sleep; text($("#sleepline"), h.idle_sleep || "");
  $("#relline").hidden = !h.release; text($("#relline"), h.release || "");
  $("#localline").hidden = !h.local_only; text($("#localline"), h.local_only || "");
  const dk = st.disk, kept = Object.keys(st.worktrees_kept || {});
  $("#disk").hidden = !dk;
  if (dk) {
    text($("#disk"), `disk ${dk.free_gb} GB free${dk.low ? " · guard on" : ""}`);
    $("#disk").className = "pill " + (dk.low ? "lv-red" : dk.free_gb < 2 * dk.threshold_gb ? "lv-orange" : "lv-green");
    $("#disk").title = `${dk.free_gb} of ${dk.total_gb} GB free under ${dk.path}; the guard holds new tasks below ${dk.threshold_gb} GB` +
      (kept.length ? `\nFinished tasks' worktrees kept: ${kept.map(t => `#${t} (${st.worktrees_kept[t]})`).join(", ")}` : "");
  }
  put($("#why"), h.why_idle ? `<b>Idle:</b> ${esc(h.why_idle)}` : `${h.running} run${h.running === 1 ? "" : "s"} working.` +
    (h.held ? ` <b>Held:</b> ${esc(h.held)}` : ""));
  text($("#top"), [h.spend.in_flight ? `~${money(h.spend.in_flight)} so far in running work` : "",
    h.spend.top_7d ? `Top spender, 7 days: ${h.spend.top_7d.source} ${money(h.spend.top_7d.usd)}` : ""].filter(Boolean).join(" · "));
  put($("#chealth"), healthHtml(h));
  $("#pqcard").hidden = !st.push_queue;
  if (st.push_queue) { text($("#pqline"), st.push_queue.line || ""); put($("#pq"), pushQueueHtml(st.push_queue, st.now || Date.now() / 1000)); }
  announce(st.attention || [], st.project.name);
  document.title = `${unseen ? "(" + unseen + ") " : ""}${st.project.name} · tt-project`;
  board(st);
  const muted = (s) => [{ key: "empty", html: `<p class="muted">${s}</p>` }];
  const attn = (st.attention || []).map((m) => ({ key: `${m.kind}:${m.id}`, html: `<div class="attn"><span class="when">${agoH(m.ts)} ago · ${m.kind === "ask" ? "ask " + m.id : "problem"}</span><div>${esc(m.text)}</div></div>` }));
  R.rows($("#attention"), attn.length ? attn : muted("Nothing needs you right now."));
  const feed = (st.feed || []).map((m) => ({ key: m.id != null ? `id:${m.id}` : `${m.kind}:${m.ts}:${m.text}`, html: `<div class="row"><span class="when">${agoH(m.ts)} ago</span>` +
    `<span class="meta">${m.state === "cleared" ? "cleared" + (m.cleared_at ? " " + at(m.cleared_at) : "") : esc(m.kind === "alert" ? "note" : m.kind)}</span>` +
    `<span class="title">${esc(m.text)}</span></div>` }));
  R.rows($("#feed"), feed.length ? feed : muted("Nothing yet."));
  put($("#gates2"), gateHtml(st.gates));
  const running = h.working || st.runs.filter((r) => r.status === "running").map((r) => ({ ...r, run: r.id }));
  R.rows($("#running"), running.length ? running.map((r) => ({ key: r.run, html: `<div class="row"><span class="id">run ${r.run}</span><span class="title">${r.task ? `#${r.task} ${esc(r.title || "")}` : esc(r.role)}</span><span class="meta">${esc(r.provider)} ${esc(r.model || "")} ${esc(r.effort || "")}${r.wake ? ` · ${esc(r.wake)} wake` : ""} · ${agoH(r.started)}${r.cost_usd ? ` · ~${money(r.cost_usd)} so far` : ""}</span>${r.note ? `<div class="meta">${esc(r.note)}</div>` : ""}</div>` })) : muted("Idle."));
  text($("#coord"), (st.coordinator && st.coordinator.summary) || "—");
  const done = st.tasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status)).slice(0, 8);
  const taskRows = (ts) => ts.map((t) => ({ key: t.id, html: taskRow(t) }));
  R.rows($("#recent"), done.length ? taskRows(done) : muted("Nothing yet."));
  R.rows($("#tasklist"), st.tasks.length ? taskRows(st.tasks) : muted("No tasks."));
  R.rows($("#issuelist"), st.issues.length ? st.issues.map((i) => ({ key: i.id, html: `<div class="row"><span class="id">${esc(i.severity)}</span><span class="title">${esc(i.title)}</span><span class="meta">${esc(i.source)} · seen ${i.count}× · last ${agoH(i.last_seen)} ago${i.task ? " · task #" + i.task : ""}</span></div>` })) : muted("No open issues."));
  put($("#schedlist"), `<table><tr><th>Name</th><th>Kind</th><th>Every</th><th>Last run</th><th>7-day cost</th><th>Daily budget</th><th>On</th></tr>` +
    st.schedules.map((s) => `<tr><td><b>${esc(s.name)}</b><div class="meta">${esc(s.description)}</div></td><td>${esc(s.kind)}</td><td>${Math.round(s.every_s / 60)} min</td>
      <td>${agoH(s.last_run)} ago<div class="meta">${esc((s.last_status || "").startsWith("skipped: budget ") ? "waiting for budget" : s.last_status || "")}</div></td><td>${money(s.cost_7d)}</td>
      <td><input type="number" min="0" step="0.5" value="${s.budget_usd_day == null ? "" : s.budget_usd_day}" style="width:6em" onchange="setSched('${esc(s.name)}',{budget_usd_day:this.value})"></td>
      <td><input type="checkbox" ${s.enabled ? "checked" : ""} onchange="setSched('${esc(s.name)}',{enabled:this.checked})"></td></tr>`).join("") + `</table>`);
  put($("#accounts"), (st.accounts || []).map((a) => `<div class="row"><b>${esc(a.provider)}</b><span class="meta">${esc(a.account || "unknown")}</span><span class="meta">last used ${agoH(a.last)} ago</span></div>`).join("") || `<p class="muted">No runs yet.</p>`);
  bars($("#spendchart"), st.budget.daily_spend, "usd", "daily spend", cfg.budget && cfg.budget.daily_usd);
  bars($("#peakchart"), st.budget.window_peaks, "peak", "window peaks", cfg.budget ? 100 - cfg.budget.reserve_pct : 90);
  put($("#sources"), `<table>${st.budget.top_sources_7d.map((r) => `<tr><td>${esc(r.source)}</td><td>${esc(r.provider)}</td><td>${money(r.usd)}</td><td class="meta">${r.n} runs</td></tr>`).join("")}</table>`);
  if (document.activeElement !== $("#daily")) $("#daily").value = cfg.budget ? cfg.budget.daily_usd : "";
  if (document.activeElement !== $("#weekly")) $("#weekly").value = cfg.budget ? cfg.budget.weekly_usd : "";
  text($("#pause"), st.paused ? "Resume project" : "Pause project");
  $("#pause").onclick = async () => { await api("/api/pause", { paused: !st.paused }); refresh(); };
  R.tick();
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
    d.innerHTML = `<div class="b">${esc(m.text)}</div><div class="meta">${agoH(m.ts)} ago${where}</div>`;
    log.appendChild(d); });
  R.tick();
  if (rows.length && atBottom) log.scrollTop = log.scrollHeight;
}
$("#say").onsubmit = async (e) => { e.preventDefault(); const t = $("#msg").value.trim(); if (!t) return; $("#msg").value = ""; await api("/api/say", { text: t }); pollChat(); };

refresh(); pollChat();
setInterval(() => { if (!document.hidden) { refresh(); pollChat(); } }, 4000);
