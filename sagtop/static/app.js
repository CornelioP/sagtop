"use strict";
// sagtop UI: snapshot + SSE deltas -> deterministic tree layout in Cytoscape.

const C = { fg: "#5cff8a", dim: "#9fd9ae", faint: "#3f8a55", edge: "#4fa866", amber: "#ffc53d", red: "#ff5c7a", cyan: "#62e4ff", bg: "#0d1610" };
const FONT = '"JBM", ui-monospace, monospace';
const NODE_W = 340, NODE_H = 60, COL_W = NODE_W + 70, ROW_H = NODE_H + 14, TREE_GAP = 0.6;
const WINDOWS = ["1h", "24h", "7d", "all"];
const STATUS_TAG = { running: "RUN", idle: "IDL", done: "DON", error: "ERR" };
const $ = (id) => document.getElementById(id);

const S = {
  nodes: new Map(), // id -> summary from server
  seq: 0,
  window: "24h",
  selected: null,
  order: [], // pre-order of visible nodes, for j/k
  visible: new Set(),
  filter: "",
  follow: false,
  home: "",
  dirs: [],
  userMoved: false,
  blink: true,
  evTimes: [],
  es: null,
  layoutQueued: false,
  panelTimer: null,
  lastToolCount: 0,
};

// ------------------------------------------------------------------ format
const ago = (t) => {
  if (!t) return "-";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${s | 0}s`;
  if (s < 3600) return `${(s / 60) | 0}m`;
  if (s < 86400) return `${(s / 3600) | 0}h`;
  return `${(s / 86400) | 0}d`;
};
const kfmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n || 0));
const hhmmss = (t) => (t ? new Date(t * 1000).toTimeString().slice(0, 8) : "--:--:--");
const fit = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s.padEnd(n));
const shortModel = (m) => (m || "?").replace(/^claude-/, "").replace(/-\d{8}$/, "");
const tilde = (p) => (S.home && (p === S.home || (p || "").startsWith(S.home + "/")) ? "~" + p.slice(S.home.length) : p || "");
const compactPath = (p) => {
  const parts = tilde(p).split("/");
  return parts.length > 6 ? [...parts.slice(0, 2), "…", ...parts.slice(-3)].join("/") : tilde(p);
};
const shortCwd = (p) => tilde(p).replace(/^.*\/(.{0,22})$/, (m, tail) => (m.length > 24 ? "…/" + tail : m));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function label(n) {
  const tool = n.tool === "codex" ? "CDX" : "CLD";
  const kind = n.kind === "session" ? "" : n.kind === "fork" ? " fork" : n.kind === "workflow" ? " WF" : " sub";
  const cursor = n.status === "running" ? (S.blink ? " █" : "  ") : "";
  const model = n.model ? ` · ${shortModel(n.model)}` : "";
  const l1 = fit(`[${STATUS_TAG[n.status] || "???"}] ${tool}${kind}${model}`, 40);
  const l2 = fit(n.title.replace(/\s+/g, " ") + cursor, 42);
  const br = n.branches ? ` br:${n.branches}` : "";
  const warn = n.failure || n.bad_lines ? " !" : "";
  const stats = n.kind === "workflow" ? `${n.tools} agents${n.tokens_out ? " ↑" + kfmt(n.tokens_out) : ""}` : `↑${kfmt(n.tokens_out)} ${n.tools}x`;
  const l3 = fit(`${fit(shortCwd(n.cwd), 16)} ${stats}${br}${warn}`, 36) + ago(n.last).padStart(5);
  return `${l1}\n${l2}\n${l3}`;
}

// ------------------------------------------------------------------- graph
const cy = cytoscape({
  container: $("cy"),
  minZoom: 0.08,
  maxZoom: 2.5,
  wheelSensitivity: 0.25,
  boxSelectionEnabled: false,
  autoungrabify: true,
  style: [
    {
      selector: "node",
      style: {
        shape: "rectangle",
        width: NODE_W,
        height: NODE_H,
        "background-color": C.bg,
        "border-width": 1.5,
        "border-color": C.faint,
        label: "data(label)",
        color: C.fg,
        "font-family": FONT,
        "font-size": 12,
        "text-wrap": "wrap",
        "text-max-width": NODE_W - 16,
        "text-valign": "center",
        "text-halign": "center",
        "text-justification": "left",
        "line-height": 1.35,
        "min-zoomed-font-size": 5,
      },
    },
    { selector: 'node[status="running"]', style: { "border-color": C.amber, color: C.amber, "border-width": 2 } },
    { selector: 'node[status="idle"]', style: { "border-color": C.fg, color: C.fg } },
    { selector: 'node[status="done"]', style: { "border-color": C.faint, color: C.dim } },
    { selector: 'node[status="error"]', style: { "border-color": C.red, color: C.red, "border-width": 2 } },
    { selector: 'node[tool="codex"]', style: { "border-style": "double", "border-width": 3 } },
    { selector: "node.sel", style: { "background-color": C.fg, color: "#04130a", "border-color": C.fg } },
    { selector: 'node.sel[status="running"]', style: { "background-color": C.amber, "border-color": C.amber } },
    { selector: 'node.sel[status="error"]', style: { "background-color": C.red, "border-color": C.red } },
    {
      selector: "edge",
      style: {
        width: 1.5,
        "line-color": C.edge,
        "curve-style": "taxi",
        "taxi-direction": "rightward",
        "taxi-turn": 30,
        "taxi-turn-min-distance": 8,
        "target-arrow-shape": "none",
      },
    },
    { selector: 'edge[edge="forked"]', style: { "line-style": "dashed", "line-dash-pattern": [4, 3] } },
    { selector: 'edge[status="running"]', style: { "line-color": C.amber, width: 2 } },
    { selector: "edge.sel", style: { "line-color": C.fg, width: 2 } },
  ],
});

function matches(n, q) {
  if (!q) return true;
  return q.split(/\s+/).every((tok) => {
    const m = tok.match(/^(status|tool|kind|model):(.+)$/);
    if (m) {
      const v = (n[m[1]] || "").toLowerCase();
      const want = m[2].toLowerCase();
      return m[1] === "model" ? v.includes(want) : v.startsWith(want); // model ids carry a vendor prefix
    }
    return `${n.title} ${n.cwd} ${n.model} ${n.tool} ${n.id}`.toLowerCase().includes(tok.toLowerCase());
  });
}

function computeVisible() {
  const vis = new Set();
  for (const n of S.nodes.values()) {
    if (!matches(n, S.filter)) continue;
    // keep ancestors so matches stay attached to their tree
    for (let cur = n, guard = 0; cur && !vis.has(cur.id) && guard < 64; cur = S.nodes.get(cur.parent), guard++) vis.add(cur.id);
  }
  return vis;
}

function layout() {
  S.layoutQueued = false;
  const vis = computeVisible();
  S.visible = vis;
  const kids = new Map();
  const roots = [];
  for (const id of vis) {
    const n = S.nodes.get(id);
    if (n.parent && vis.has(n.parent)) {
      if (!kids.has(n.parent)) kids.set(n.parent, []);
      kids.get(n.parent).push(id);
    } else roots.push(id);
  }
  const started = (id) => S.nodes.get(id).started || S.nodes.get(id).last;
  roots.sort((a, b) => started(b) - started(a));
  // 1) lay out each tree on its own: x = depth, y = leaf rows
  const order = [];
  const seen = new Set();
  const trees = roots.map((r) => {
    const local = new Map();
    let rows = 0, depthMax = 0;
    const place = (id, depth) => {
      seen.add(id);
      order.push(id);
      depthMax = Math.max(depthMax, depth);
      const ch = (kids.get(id) || []).filter((k) => !seen.has(k)).sort((a, b) => started(a) - started(b));
      let y;
      if (!ch.length) y = rows++;
      else {
        const ys = ch.map((k) => place(k, depth + 1));
        y = (ys[0] + ys[ys.length - 1]) / 2;
      }
      local.set(id, { d: depth, y });
      return y;
    };
    place(r, 0);
    return { local, rows, cols: depthMax + 1 };
  });
  S.order = order;

  // 2) pack trees newest-first into columns shaped like the viewport
  const totalRows = trees.reduce((a, t) => a + t.rows + TREE_GAP, 0);
  const avgCols = trees.length ? trees.reduce((a, t) => a + t.cols * t.rows, 0) / Math.max(1, totalRows) : 1;
  const aspect = Math.max(0.3, cy.height() / Math.max(1, cy.width()));
  const ideal = Math.sqrt((aspect * totalRows * avgCols * COL_W) / ROW_H);
  const budget = Math.max(8, Math.ceil(ideal / 4) * 4, ...trees.map((t) => t.rows)); // quantised so it rarely jumps
  const pos = new Map();
  let colX = 0, colW = 0, rowY = 0;
  for (const t of trees) {
    if (rowY > 0 && rowY + t.rows > budget) {
      colX += colW + 0.25;
      colW = 0;
      rowY = 0;
    }
    for (const [id, p] of t.local) pos.set(id, { x: (colX + p.d) * COL_W, y: (rowY + p.y) * ROW_H });
    colW = Math.max(colW, t.cols);
    rowY += t.rows + TREE_GAP;
  }

  cy.batch(() => {
    cy.nodes().forEach((el) => {
      if (!vis.has(el.id())) cy.remove(el);
    });
    let moves = 0;
    for (const [id, p] of pos) {
      const n = S.nodes.get(id);
      let el = cy.getElementById(id);
      if (el.empty()) {
        el = cy.add({ group: "nodes", data: { id, label: label(n), status: n.status, tool: n.tool }, position: p });
        if (id === S.selected) el.addClass("sel");
      } else {
        const cur = el.position();
        if (cur.x !== p.x || cur.y !== p.y) {
          if (moves++ < 250) el.animate({ position: p }, { duration: 220, easing: "ease-out-cubic" });
          else el.position(p);
        }
      }
    }
    // edges: one per visible child with a visible parent
    cy.edges().forEach((e) => {
      const n = S.nodes.get(e.target().id());
      if (!n || n.parent !== e.source().id() || !vis.has(n.parent)) cy.remove(e);
    });
    for (const id of vis) {
      const n = S.nodes.get(id);
      if (!n.parent || !vis.has(n.parent)) continue;
      const eid = `e:${id}`;
      if (cy.getElementById(eid).empty())
        cy.add({ group: "edges", data: { id: eid, source: n.parent, target: id, edge: n.edge, status: n.status } });
    }
  });
  if (S.selected && !vis.has(S.selected)) select(null);
  $("empty").hidden = vis.size > 0;
  updateCounts();
  setTimeout(autoView, 240); // after move animations settle
}

const queueLayout = () => {
  if (!S.layoutQueued) {
    S.layoutQueued = true;
    requestAnimationFrame(layout);
  }
};

function refreshNode(id) {
  const n = S.nodes.get(id);
  const el = cy.getElementById(id);
  if (!n || el.empty()) return;
  el.data({ label: label(n), status: n.status });
  const e = cy.getElementById(`e:${id}`);
  if (!e.empty()) e.data("status", n.status);
}

// -------------------------------------------------------------------- view
const visibleWidth = () => cy.width() - ($("panel").hidden ? 0 : $("panel").offsetWidth);

function autoView() {
  // until the user pans or zooms, keep everything in sight
  if (S.userMoved || !cy.nodes().length) return;
  cy.fit(cy.elements(), 40);
  if (cy.zoom() < 0.8) {
    cy.zoom(1); // too much to fit legibly: 1:1 on the newest trees, top-left
    const bb = cy.elements().boundingBox();
    cy.pan({ x: 30 - bb.x1, y: 30 - bb.y1 });
  } else if (cy.zoom() > 1.2) {
    cy.zoom(1.2);
    cy.center();
  }
}

function centerOn(el) {
  S.userMoved = true; // an explicit focus beats auto-fit
  const p = el.position(), z = cy.zoom();
  cy.animate({ pan: { x: visibleWidth() / 2 - p.x * z, y: cy.height() / 2 - p.y * z } }, { duration: 200 });
}

// ------------------------------------------------------------------ events
function upsert(n) {
  const prev = S.nodes.get(n.id);
  S.nodes.set(n.id, n);
  const structural = !prev || prev.parent !== n.parent || prev.started !== n.started || (S.filter && !matches(prev, S.filter)) || (S.filter && !matches(n, S.filter));
  if (structural) queueLayout();
  else refreshNode(n.id);
  if (n.id === S.selected) schedulePanel();
}

function apply(events) {
  for (const ev of events) {
    if (ev.op === "upsert") upsert(ev.node);
    else if (ev.op === "remove") {
      S.nodes.delete(ev.id);
      queueLayout();
    }
    S.seq = Math.max(S.seq, ev.seq);
  }
  const now = performance.now();
  for (let i = 0; i < events.length; i++) S.evTimes.push(now);
  if (S.follow) followNewest();
  updateCounts();
}

async function loadSnapshot() {
  const r = await fetch("/api/snapshot", { cache: "no-store" });
  const snap = await r.json();
  S.nodes = new Map(snap.nodes.map((n) => [n.id, n]));
  S.seq = snap.seq;
  S.window = snap.window;
  S.home = snap.home || "";
  S.dirs = [snap.claude_dir, snap.codex_dir].filter(Boolean);
  $("win").textContent = S.window;
  cy.elements().remove();
  layout();
  updateCounts();
  return snap;
}

function connect() {
  if (S.es) S.es.close();
  const es = new EventSource(`/api/events?since=${S.seq}`);
  S.es = es;
  es.addEventListener("open", () => setLink(true));
  es.addEventListener("batch", (m) => apply(JSON.parse(m.data)));
  es.addEventListener("reset", async () => {
    es.close();
    await loadSnapshot().catch(() => {});
    connect();
  });
  es.onerror = () => {
    // reconnect ourselves so `since` reflects what we already applied
    es.close();
    setLink(false);
    setTimeout(async () => {
      try {
        await loadSnapshot();
      } catch (_) {}
      connect();
    }, 1500);
  };
}

function setLink(ok) {
  const el = $("link");
  el.className = ok ? "ok" : "bad";
  el.textContent = ok ? "● LIVE" : "○ RECONNECTING";
}

function updateCounts() {
  const c = { running: 0, idle: 0, done: 0, error: 0 };
  for (const n of S.nodes.values()) c[n.status] = (c[n.status] || 0) + 1;
  $("counts").innerHTML =
    `<span class="run">▲ ${c.running} run</span> · <span class="idle">${c.idle} idle</span> · ` +
    `<span class="done">${c.done} done</span>` +
    (c.error ? ` · <span class="err">${c.error} err</span>` : "") +
    `<span class="dim"> · ${S.visible.size}/${S.nodes.size} shown</span>`;
}

// --------------------------------------------------------------- selection
function select(id, { center = false } = {}) {
  cy.$(".sel").removeClass("sel");
  S.selected = id;
  if (!id) {
    $("panel").hidden = true;
    return;
  }
  const el = cy.getElementById(id);
  el.addClass("sel");
  cy.getElementById(`e:${id}`).addClass("sel");
  if (center || !inView(el)) centerOn(el);
  if (!$("panel").hidden) renderPanel();
}

function inView(el) {
  if (el.empty()) return true;
  const bb = el.renderedBoundingBox();
  return bb.x1 >= 0 && bb.y1 >= 0 && bb.x2 <= visibleWidth() && bb.y2 <= cy.height();
}

function step(delta) {
  if (!S.order.length) return;
  const i = S.order.indexOf(S.selected);
  const next = i < 0 ? 0 : Math.min(S.order.length - 1, Math.max(0, i + delta));
  select(S.order[next]);
}

function followNewest() {
  let best = null;
  for (const id of S.visible) {
    const n = S.nodes.get(id);
    if (n.status === "running" && (!best || n.last > best.last || (n.last === best.last && n.started > best.started))) best = n;
  }
  if (best && best.id !== S.selected) select(best.id, { center: true });
}

// ------------------------------------------------------------------- panel
function schedulePanel() {
  if ($("panel").hidden || S.panelTimer) return;
  S.panelTimer = setTimeout(() => {
    S.panelTimer = null;
    renderPanel();
  }, 600);
}

async function renderPanel() {
  const id = S.selected;
  if (!id) return;
  let d;
  try {
    const r = await fetch(`/api/node/${encodeURIComponent(id)}`, { cache: "no-store" });
    if (!r.ok) return;
    d = await r.json();
  } catch (_) {
    return;
  }
  if (id !== S.selected) return;
  const freshFrom = id === S.panelId ? S.lastToolCount : d.tools;
  S.panelId = id;
  S.lastToolCount = d.tools;
  const st = `<span class="${d.status === "running" ? "run" : d.status === "error" ? "err" : d.status}">${d.status.toUpperCase()}</span>`;
  const kv = [
    ["id", esc(d.id)],
    ["agent", `${d.tool === "codex" ? '<span class="cdx">codex</span>' : "claude"} · ${esc(d.kind)}${d.edge ? " (" + esc(d.edge) + ")" : ""}`],
    ["status", st],
    ["model", esc(d.model || "-")],
    ["cwd", esc(d.cwd)],
    ["started", `${hhmmss(d.started)} <span class="dim">(${ago(d.started)} ago)</span>`],
    ["last", `${hhmmss(d.last)} <span class="dim">(${ago(d.last)} ago)</span>`],
    ["tokens", `out ${kfmt(d.tokens_out)} · ctx ${kfmt(d.ctx)} · tools ${d.tools} · branches ${d.branches}`],
    ["file", `<span class="dim" title="${esc(d.path)}">${esc(compactPath(d.path))}</span>`],
  ];
  if (d.bad_lines) kv.push(["warn", `<span class="err">${d.bad_lines} unparsable lines skipped</span>`]);
  if (d.failure) kv.push(["error", `<span class="err">${esc(d.failure)}</span>`]);
  const newCount = Math.max(0, d.tools - freshFrom);
  const tools = d.tool_events
    .slice()
    .reverse()
    .map(
      (t, i) =>
        `<div class="tool${i < newCount ? " new" : ""}"><span class="dim">${hhmmss(t.t)}</span><span class="n">${esc(t.name)}</span><span class="a" title="${esc(t.arg)}">${esc(t.arg)}</span></div>`
    )
    .join("");
  $("ptitle").textContent = `${d.tool}:${d.kind} ─ ${d.title}`.slice(0, 70);
  $("pbody").innerHTML =
    `<div class="kv">${kv.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("")}</div>` +
    `<h4>last tool calls</h4>${tools || '<span class="dim">none yet</span>'}` +
    `<h4>last message</h4><pre>${esc(d.last_text) || '<span class="dim">-</span>'}</pre>` +
    `<h4>prompt</h4><pre>${esc(d.prompt) || '<span class="dim">-</span>'}</pre>`;
}

function togglePanel(force) {
  const open = force ?? $("panel").hidden;
  $("panel").hidden = !open;
  if (open && S.selected) {
    renderPanel();
    const el = cy.getElementById(S.selected);
    if (!el.empty() && !inView(el)) centerOn(el);
  }
}

// ---------------------------------------------------------------- controls
async function cycleWindow() {
  const next = WINDOWS[(WINDOWS.indexOf(S.window) + 1) % WINDOWS.length];
  S.window = next;
  S.userMoved = false;
  $("win").textContent = next + (next === "all" ? " (scanning…)" : "");
  await fetch("/api/window", { method: "POST", body: JSON.stringify({ window: next }) });
  setTimeout(() => ($("win").textContent = S.window), 1500);
}

function setFilter(q) {
  S.filter = q.trim();
  queueLayout();
}

const filterEl = $("filter");
filterEl.addEventListener("input", () => setFilter(filterEl.value));
filterEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === "Escape") {
    if (e.key === "Escape") {
      filterEl.value = "";
      setFilter("");
    }
    filterEl.blur();
    e.preventDefault();
  }
  e.stopPropagation();
});
filterEl.addEventListener("focus", () => {
  $("bar").classList.add("editing");
  $("prompt").textContent = "/";
});
filterEl.addEventListener("blur", () => {
  $("bar").classList.remove("editing");
  $("prompt").textContent = S.filter ? "[filter]" : "[sagtop]";
});

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const help = $("help");
  const k = e.key;
  if (!help.hidden && k !== "?") {
    help.hidden = true;
    return;
  }
  switch (k) {
    case "j": case "ArrowDown": step(1); break;
    case "k": case "ArrowUp": step(-1); break;
    case "h": case "ArrowLeft": {
      const n = S.nodes.get(S.selected);
      if (n && n.parent && S.visible.has(n.parent)) select(n.parent);
      break;
    }
    case "l": case "ArrowRight": {
      const i = S.order.indexOf(S.selected);
      const nxt = S.nodes.get(S.order[i + 1]);
      if (nxt && nxt.parent === S.selected) select(nxt.id);
      break;
    }
    case "Enter": if (!S.selected) step(1); togglePanel(); break;
    case "Escape":
      if (!$("panel").hidden) togglePanel(false);
      else if (S.filter) { filterEl.value = ""; setFilter(""); filterEl.blur(); }
      else select(null);
      break;
    case "/": filterEl.focus(); break;
    case "f":
      S.follow = !S.follow;
      $("follow").textContent = `follow:${S.follow ? "on" : "off"}`;
      $("follow").className = S.follow ? "run" : "dim";
      if (S.follow) followNewest();
      break;
    case "t": cycleWindow(); break;
    case "g": S.userMoved = false; autoView(); break;
    case "c": if (S.selected) select(S.selected, { center: true }); break;
    case "s":
      document.body.classList.toggle("crt");
      try { localStorage.setItem("sagtop.crt", document.body.classList.contains("crt") ? "1" : "0"); } catch (_) {}
      break;
    case "?": help.hidden = !help.hidden; break;
    default: return;
  }
  e.preventDefault();
});

cy.on("tap", "node", (e) => {
  select(e.target.id());
  togglePanel(true);
});
cy.on("tap", (e) => {
  if (e.target === cy) select(null);
});
for (const ev of ["wheel", "mousedown", "touchstart"]) $("cy").addEventListener(ev, () => (S.userMoved = true), { passive: true });
$("help").addEventListener("click", () => ($("help").hidden = true));

// ------------------------------------------------------------------ timers
const relabel = (onlyRunning) =>
  cy.batch(() => {
    for (const id of S.visible) {
      const n = S.nodes.get(id);
      if (n && (!onlyRunning || n.status === "running")) cy.getElementById(id).data("label", label(n));
    }
  });
setInterval(() => {
  S.blink = !S.blink; // blinking cursor on running nodes only
  relabel(true);
}, 530);
setInterval(() => relabel(false), 5000); // relative ages everywhere

setInterval(() => {
  $("clock").textContent = new Date().toTimeString().slice(0, 8);
  const cut = performance.now() - 5000;
  while (S.evTimes.length && S.evTimes[0] < cut) S.evTimes.shift();
  $("rate").textContent = `${(S.evTimes.length / 5).toFixed(1)} ev/s`;
  if (!$("panel").hidden && S.selected) schedulePanel();
}, 1000);

// -------------------------------------------------------------------- boot
async function boot() {
  try { if (localStorage.getItem("sagtop.crt") === "0") document.body.classList.remove("crt"); } catch (_) {}
  const log = $("bootlog");
  const say = (s) => (log.textContent += s + "\n");
  say("$ sagtop");
  await document.fonts.load('12px "JBM"').catch(() => {});
  try {
    const snap = await loadSnapshot();
    say(`  watching ${S.dirs.map(tilde).join("  ")}`);
    say(`  scanned in ${snap.tick_ms} ms · ${snap.nodes.length} nodes · window ${snap.window}`);
    say("  attaching live stream…");
    connect();
    await new Promise((r) => setTimeout(r, 350));
    $("boot").hidden = true;
    autoView();
  } catch (err) {
    say(`  error: ${err}`);
    say("  retrying in 2s");
    setTimeout(() => location.reload(), 2000);
  }
}
boot();
