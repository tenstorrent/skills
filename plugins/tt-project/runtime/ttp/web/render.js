// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
// Patches the page in place so a refresh keeps what the user is doing: a section or row is rewritten
// only when its own HTML changed, and never while it holds the user's text selection or the field
// they type in. That update waits until the selection clears or focus leaves. Ages are rendered as
// empty <span data-ago="ts"> and filled here, so the HTML of unchanged data stays the same.
"use strict";
(function (root) {
  const R = {};
  const pending = new Map();   // element -> function re-applying its held update
  // Fields that only tick with time; they do not make a payload new.
  const TICKING = new Set(["now", "age", "age_s"]);

  R.ago = (ts) => { if (!ts) return "—"; const s = Date.now() / 1000 - ts; return s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${(s / 3600).toFixed(1)}h` : `${Math.round(s / 86400)}d`; };
  R.agoHtml = (ts) => `<span data-ago="${+ts || 0}"></span>`;

  // A payload's identity without its ticking fields: equal keys need no render.
  R.key = (st, extra) => `${extra == null ? "" : extra}|` + JSON.stringify(st, (k, v) => TICKING.has(k) ? undefined : v);

  // What the user holds now: the ends of a non-collapsed selection and the focused element.
  R.env = () => {
    const sel = root.getSelection ? root.getSelection() : null, doc = root.document;
    return { sel: sel && !sel.isCollapsed && sel.rangeCount ? [sel.anchorNode, sel.focusNode] : [],
      active: doc ? doc.activeElement : null };
  };
  const editable = (n) => !!n && (/^(INPUT|TEXTAREA|SELECT)$/.test(n.tagName || "") || !!n.isContentEditable);
  // True when replacing `node` would drop the user's selection or the field they type in.
  R.holds = (node, e) => e.sel.some((n) => n && node.contains(n)) || (editable(e.active) && node.contains(e.active));

  // One element from HTML with a single root (a row).
  R.make = (html) => { const t = root.document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };

  const fill = (node) => {
    if (!node.querySelectorAll) return;
    node.querySelectorAll("[data-ago]").forEach((s) => { s.textContent = R.ago(+s.dataset.ago); });
  };

  // Set `el`'s content to `html` (or its text to it, with asText). Returns "same", "held" or "replaced".
  R.patch = (el, html, e, asText) => {
    e = e || R.env();
    if (el._ttpHtml === html) { pending.delete(el); return "same"; }
    if (R.holds(el, e)) { pending.set(el, (e2) => R.patch(el, html, e2, asText)); return "held"; }
    if (asText) el.textContent = html; else { el.innerHTML = html; fill(el); }
    el._ttpHtml = html; el._ttpRows = false; pending.delete(el);
    return "replaced";
  };
  R.text = (el, s, e) => R.patch(el, String(s), e, true);

  // Make `el`'s children the rows [{key, html}] in that order. A row is replaced only when its HTML
  // changed, an open <details> stays open, and a row the user holds is left as it is (not replaced,
  // moved or removed) until they let go. Returns the keys of the rows it replaced or added.
  R.rows = (el, rows, e) => {
    e = e || R.env();
    const seen = {};   // keys must be unique: a repeat gets a suffix
    rows = rows.map((r) => { const k = String(r.key); seen[k] = (seen[k] || 0) + 1; return seen[k] > 1 ? { key: `${k}~${seen[k]}`, html: r.html } : r; });
    const held = () => { pending.set(el, (e2) => R.rows(el, rows, e2)); };
    if (!el._ttpRows) {   // first keyed render: drop whatever was there
      if (el.children.length && R.holds(el, e)) { held(); return []; }
      el.innerHTML = ""; el._ttpRows = true; el._ttpHtml = undefined;
    }
    const want = new Set(rows.map((r) => r.key)), old = new Map(), holding = new Set();
    Array.from(el.children).forEach((c) => { old.set(c._ttpKey, c); if (R.holds(c, e)) holding.add(c); });
    // Held rows never move, so they cannot swap places among themselves: that waits for the user.
    const heldNow = Array.from(holding).filter((c) => want.has(c._ttpKey)).map((c) => c._ttpKey);
    const heldWant = rows.map((r) => r.key).filter((k) => heldNow.indexOf(k) >= 0);
    if (heldNow.some((k, i) => k !== heldWant[i])) { held(); return []; }
    let late = false;
    const changed = [];
    const nodes = rows.map((r) => {
      const c = old.get(r.key);
      if (c && c._ttpRow === r.html) return c;
      if (c && holding.has(c)) { late = true; return c; }
      const n = R.make(r.html); n._ttpKey = r.key; n._ttpRow = r.html;
      if (c) { if (c.open) n.open = true; el.replaceChild(n, c); }
      fill(n); changed.push(r.key);
      return n;
    });
    old.forEach((c, k) => { if (!want.has(k)) { if (holding.has(c)) late = true; else el.removeChild(c); } });
    // Put the rows in order without moving a held one: rows in order stay, new ones are inserted,
    // and rows ahead of a held row that belong after it are moved past it (they come back in turn).
    const keep = new Set(nodes);
    let cur = el.firstElementChild;
    nodes.forEach((n) => {
      while (cur && !keep.has(cur)) cur = cur.nextElementSibling;
      if (cur === n) { cur = n.nextElementSibling; return; }
      if (!holding.has(n)) { el.insertBefore(n, cur || null); return; }
      while (cur && cur !== n) { const next = cur.nextElementSibling; if (keep.has(cur)) el.insertBefore(cur, n.nextElementSibling); cur = next; }
      cur = n.nextElementSibling;
    });
    if (late) held(); else pending.delete(el);
    return changed;
  };

  // Apply held updates whose holder has let go.
  R.flush = (e) => {
    e = e || R.env();
    Array.from(pending.values()).forEach((f) => f(e));
  };
  R.pending = () => pending.size;

  // Move the ages on, except under the user's selection.
  R.tick = (e) => {
    e = e || R.env();
    root.document.querySelectorAll("[data-ago]").forEach((s) => {
      const t = R.ago(+s.dataset.ago);
      if (s.textContent !== t && !R.holds(s, e)) s.textContent = t;
    });
  };

  if (root.document && root.document.addEventListener) {
    root.document.addEventListener("selectionchange", () => R.flush());
    root.document.addEventListener("focusout", () => setTimeout(() => R.flush(), 0));
  }
  if (typeof module !== "undefined" && module.exports) module.exports = R; else root.TTPRender = R;
})(typeof window !== "undefined" ? window : globalThis);
