/* agent-trace dashboard — zero-build SPA (decision D9).
   Everything rendered from the JSON API; all interpolated data escaped —
   traces carry untrusted agent output, innerHTML injection is a real risk. */
"use strict";

// Prefer the token in the page URL (that is how the user got here, and it
// survives a stale or broken injection), then the one the server injected.
// Relying on the injection alone meant a single bad `replace` at serve time
// turned the whole dashboard into `401 missing or invalid token`.
const TOKEN = new URLSearchParams(location.search).get("token")
  || window.__AGENTTRACE_TOKEN__ || "";
const app = document.getElementById("app");

function missingTokenNotice() {
  return `<div class="panel"><h1>no access token</h1>
    <p>This page was opened without one, so every API call would be refused.</p>
    <p class="dim">Open the URL printed by <code>agenttrace serve</code> — it ends in
    <code>?token=…</code> — or run <code>agenttrace serve --status</code> and add the
    token from the URL it reports to this page's address.</p></div>`;
}

async function api(path) {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`/api/${path}${sep}token=${encodeURIComponent(TOKEN)}`);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

async function post(path, body) {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`/api/${path}${sep}token=${encodeURIComponent(TOKEN)}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtTs(ts) {
  if (!ts) return "—";
  const m = ts.match(/T(\d{2}:\d{2}:\d{2})\.(\d{3})Z$/);
  return m ? `${m[1]}.${m[2]}` : ts.slice(0, 19);
}
function fmtDay(ts) { return ts ? ts.slice(0, 10) : "—"; }
function fmtDur(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m${String(Math.round(ms / 1000) % 60).padStart(2, "0")}s`;
}
function fmtTok(n) {
  if (n == null) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}
function badge(status) {
  // The CSS class is the status itself; the visible text is the status too.
  // Deriving one from the other with a regex produced
  // class="badge no_signal">error< — the fix for no_signal, negated by the
  // renderer.
  return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
}
function kindDot(kind) { return `<span class="kind-dot kind-${esc(kind)}"></span>`; }

/* ------------------------------------------------------------------ */
/* router                                                              */
/* ------------------------------------------------------------------ */

const routes = {
  overview: renderOverview,
  traces: renderTraces,
  trace: renderTraceDetail,
  sessions: renderSessions,
  session: renderSessionDetail,
  adapters: renderAdapters,
  datasets: renderDatasets,
  dataset: renderDatasetDetail,
  experiments: renderExperiments,
  experiment: renderExperimentDetail,
};

let currentRender = null; // re-invoked on SSE change events

function router() {
  const hash = location.hash || "#/overview";
  // The route is the part BEFORE the query. Splitting the whole hash on "/" made
  // `#/traces?agent=pi` a route name nobody has a renderer for, so the filter
  // silently fell through to the Overview page. A renderer that needs the query
  // reads `location.hash` itself.
  const parts = hash.slice(2).split("?")[0].split("/");
  const name = parts[0] || "overview";
  // DECODE the segment. A session is identified by a file PATH, so the route
  // holds percent-escapes; passing them through meant the API call escaped them
  // a second time (%2F -> %252F) and every session page 404'd. Trace ids are
  // hex and never showed it, which is why only this route was broken.
  const arg = decodeArg(parts[1]);
  document.querySelectorAll("#topbar nav a").forEach(a => {
    a.classList.toggle("active", a.dataset.route === name ||
      (a.dataset.route === "traces" && name === "trace"));
  });
  const fn = routes[name] || renderOverview;
  currentRender = () => fn(arg).catch(e => {
    // Say WHICH route failed and what it was given: a bare "error" gave no clue
    // that the id arriving here was still escaped.
    app.innerHTML = `<div class="panel"><h1>error</h1>
      <pre class="body">${esc(e.message)}</pre>
      <p class="dim">route ${esc(name)} — ${esc(String(arg))}</p></div>`;
  });
  currentRender();
}

function decodeArg(raw) {
  if (!raw) return raw;
  try {
    return decodeURIComponent(raw);
  } catch (e) {
    return raw;      // a malformed escape is not worth turning into a blank page
  }
}

window.addEventListener("hashchange", router);

/* ------------------------------------------------------------------ */
/* overview                                                            */
/* ------------------------------------------------------------------ */

async function renderOverview() {
  const [ov, traces] = await Promise.all([
    api("overview"), api("traces?limit=8")]);
  app.innerHTML = `
    <div class="cards">
      <div class="card"><div class="num">${ov.traces}</div><div class="lbl">traces</div></div>
      <div class="card"><div class="num">${ov.spans}</div><div class="lbl">spans</div></div>
      <div class="card ${ov.errors ? "err" : ""}"><div class="num">${ov.errors}</div><div class="lbl">failed spans</div></div>
      <div class="card"><div class="num">${ov.sessions}</div><div class="lbl">sessions</div></div>
      <div class="card"><div class="num">${fmtTok(ov.tokens.input)}</div><div class="lbl">input tokens</div></div>
      <div class="card"><div class="num">${fmtTok(ov.tokens.output)}</div><div class="lbl">output tokens</div></div>
      <div class="card"><div class="num">${fmtTok(ov.tokens.cache_read)}</div><div class="lbl">cache read</div></div>
      <div class="card"><div class="num">${fmtDur(ov.task_duration_ms.p95)}</div><div class="lbl">task p95</div></div>
    </div>
    <div class="panel">
      <h2>per adapter</h2>
      <table><thead><tr><th>adapter</th><th>traces</th><th>spans</th><th>errors</th><th>input tok</th><th>output tok</th></tr></thead>
      <tbody>${ov.per_agent.map(a => `
        <tr><td class="mono">${esc(a.adapter_id)}</td><td>${a.traces}</td><td>${a.spans}</td>
        <td>${a.errors || 0}</td><td>${fmtTok(a.in_tok)}</td><td>${fmtTok(a.out_tok)}</td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="panel">
      <h2>recent traces</h2>
      ${traceTable(traces.traces)}
    </div>`;
  bindTraceRows();
}

/* ------------------------------------------------------------------ */
/* traces                                                              */
/* ------------------------------------------------------------------ */

function traceTable(traces) {
  if (!traces.length) return `<p class="dim">no traces</p>`;
  return `<table><thead><tr>
    <th>start</th><th>adapter</th><th>name</th><th>status</th>
    <th>spans</th><th>tokens in/out</th><th>duration</th></tr></thead>
    <tbody>${traces.map(t => `
    <tr class="clickable" data-trace="${esc(t.trace_id)}">
      <td class="mono dim">${esc(fmtDay(t.start))} ${esc(fmtTs(t.start))}</td>
      <td class="mono">${esc(t.adapter_id)}</td>
      <td>${esc(t.name)}</td>
      <td>${badge(t.status)}</td>
      <td>${t.span_count}${t.unclosed ? ` <span class="dim">(${t.unclosed} open)</span>` : ""}</td>
      <td class="mono">${fmtTok(t.tokens.input)} / ${fmtTok(t.tokens.output)}</td>
      <td class="mono">${fmtDur(t.duration_ms)}</td>
    </tr>`).join("")}</tbody></table>`;
}

function bindTraceRows() {
  document.querySelectorAll("tr[data-trace]").forEach(row => {
    row.addEventListener("click", () => {
      location.hash = `#/trace/${row.dataset.trace}`;
    });
  });
}

async function renderTraces() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const agent = params.get("agent") || "";
  const status = params.get("status") || "";
  const q = [`limit=100`];
  if (agent) q.push(`agent=${encodeURIComponent(agent)}`);
  if (status) q.push(`status=${encodeURIComponent(status)}`);
  const data = await api(`traces?${q.join("&")}`);
  const adapters = [...new Set(data.traces.map(t => t.adapter_id))].sort();
  app.innerHTML = `
    <div class="panel">
      <div class="filters">
        <select id="f-agent">
          <option value="">all adapters</option>
          ${adapters.map(a => `<option ${a === agent ? "selected" : ""}>${esc(a)}</option>`).join("")}
        </select>
        <select id="f-status">
          ${["", "ok", "error", "incomplete"].map(s =>
            `<option value="${s}" ${s === status ? "selected" : ""}>${s || "all states"}</option>`).join("")}
        </select>
        <span class="dim">${data.total} trace(s)</span>
      </div>
      ${traceTable(data.traces)}
    </div>`;
  bindTraceRows();
  const refilter = () => {
    const a = document.getElementById("f-agent").value;
    const s = document.getElementById("f-status").value;
    location.hash = `#/traces?agent=${encodeURIComponent(a)}&status=${encodeURIComponent(s)}`;
  };
  document.getElementById("f-agent").addEventListener("change", refilter);
  document.getElementById("f-status").addEventListener("change", refilter);
}

/* ------------------------------------------------------------------ */
/* trace detail: execution tree + waterfall + span detail              */
/* ------------------------------------------------------------------ */

function buildRows(spans) {
  /* Flatten the tree depth-first for rendering; tolerate forests (orphan
     parents render as roots — better than dropping the span). */
  const byId = new Map(spans.map(s => [s.span_id, s]));
  const children = new Map();
  const roots = [];
  for (const s of spans) {
    const p = s.parent_span_id;
    if (p && byId.has(p) && p !== s.span_id) {
      if (!children.has(p)) children.set(p, []);
      children.get(p).push(s);
    } else {
      roots.push(s);
    }
  }
  const rows = [];
  const walk = (s, depth) => {
    rows.push({ span: s, depth });
    (children.get(s.span_id) || [])
      .sort((a, b) => (a.start || "").localeCompare(b.start || ""))
      .forEach(c => walk(c, depth + 1));
  };
  roots.sort((a, b) => (a.start || "").localeCompare(b.start || ""))
    .forEach(r => walk(r, 0));
  return rows;
}

async function renderTraceDetail(traceId, selectedSpan) {
  const t = await api(`traces/${encodeURIComponent(traceId)}`);
  const rows = buildRows(t.spans);
  const t0 = t.start, t1 = t.end;
  const total = Math.max(1, (Date.parse(t1) || 0) - (Date.parse(t0) || 0));
  const leftPct = s => Math.max(0, ((Date.parse(s.start || s.end) - Date.parse(t0)) / total) * 100);
  const widthPct = s => {
    const a = Date.parse(s.start || s.end), b = Date.parse(s.end || s.start);
    return Math.max(0.5, ((b - a) / total) * 100);
  };
  const sel = selectedSpan || (rows[0] && rows[0].span.span_id);
  const selSpan = t.spans.find(s => s.span_id === sel);

  app.innerHTML = `
    <div class="panel">
      <h1>${kindDot("task")} ${esc(t.trace_id.slice(0, 24))} ${badge(t.status)}</h1>
      <dl class="kv">
        <dt>adapter</dt><dd class="mono">${esc(t.adapter_id)}</dd>
        <dt>session</dt><dd class="mono">${esc(t.session_id || "—")}</dd>
        <dt>start</dt><dd class="mono">${esc(t.start)}</dd>
        <dt>duration</dt><dd class="mono">${fmtDur(t.duration_ms)}</dd>
        <dt>tokens in/out/cache</dt><dd class="mono">${fmtTok(t.tokens.input)} / ${fmtTok(t.tokens.output)} / ${fmtTok(t.tokens.cache_read)}</dd>
        <dt>spans</dt><dd>${t.spans.length}</dd>
      </dl>
      ${workspaceBlock(t.workspace)}
    </div>
    <div class="panel">
      <h2>waterfall</h2>
      ${rows.map(({ span, depth }) => `
        <div class="wf-row ${span.span_id === sel ? "selected" : ""}" data-span="${esc(span.span_id)}">
          <div class="wf-name" style="padding-left:${depth * 16}px">
            ${kindDot(span.kind)}<span>${esc(span.name)}</span>
            ${span.synthetic ? '<span class="note">synthetic</span>' : ""}
            ${span.status === "error" ? '<span class="badge error">err</span>' : ""}
            ${!span.ended ? '<span class="badge incomplete">open</span>' : ""}
            ${span.duration_suspect ? '<span class="badge incomplete" title="another turn of this session started inside this span: its duration is a gap (interruption or resume), not work">duration is a gap</span>' : ""}
          </div>
          <div class="wf-track">
            <div class="wf-bar kind-${esc(span.kind)} ${span.status === "error" ? "error" : ""} ${!span.ended ? "incomplete" : ""}"
                 style="left:${leftPct(span).toFixed(2)}%;width:${widthPct(span).toFixed(2)}%"></div>
          </div>
          <div class="wf-dur">${span.duration_suspect ? "~" : ""}${fmtDur(span.duration_ms)}</div>
        </div>`).join("")}
    </div>
    ${t.events.length ? `<div class="panel"><h2>events</h2>
      ${t.events.map(e => `<div class="ev-row"><span class="ts mono">${esc(fmtTs(e.ts))}</span>
        <span class="mono">${esc(e.name)}</span>
        <span class="dim">${esc(summarizeAttrs(e.attributes))}</span></div>`).join("")}
    </div>` : ""}
    <div class="panel" id="span-detail">${selSpan ? spanDetail(selSpan) : ""}</div>`;

  document.querySelectorAll(".wf-row").forEach(row => {
    row.addEventListener("click", () => {
      renderTraceDetail(traceId, row.dataset.span).catch(console.error);
    });
  });
  bindTabs();
}

function workspaceBlock(ws) {
  // A trace without the code it worked on answers "the tests failed" but not
  // "on which branch, at which commit, after changing what". `at` matters: a
  // snapshot read at index time is the directory NOW, not what the agent saw.
  if (!ws || !ws.git) return "";
  const g = ws.git;
  const bits = [];
  if (g.branch) bits.push(`<span class="mono">${esc(g.branch)}</span>`);
  if (g.detached) bits.push('<span class="badge unknown">detached</span>');
  if (g.head) bits.push(`<span class="mono dim">@${esc(g.head)}</span>`);
  if (g.dirty) bits.push(`<span class="badge incomplete">${g.changed_count || 0} changed</span>`);
  else bits.push('<span class="badge ok">clean</span>');
  if (g.at === "index") bits.push('<span class="note">read at index time, not at capture</span>');
  const files = (g.changed_files || []).length
    ? `<div class="note mono">${g.changed_files.map(esc).join("<br>")}</div>` : "";
  const diff = g.diff
    ? `<pre class="body" style="margin-top:8px">${esc(g.diff)}</pre>`
      + (g.diff_truncated ? '<div class="note">(diff truncated)</div>' : "")
    : (g.diff_withheld
        ? '<div class="note">diff not captured — set <code>AGENTTRACE_GIT=full</code> to record it</div>'
        : "");
  return `<h2>workspace</h2>
    <dl class="kv">
      <dt>repository</dt><dd class="mono dim">${esc(g.root || "—")}</dd>
      <dt>state</dt><dd>${bits.join(" ")}</dd>
      ${g.subject ? `<dt>commit</dt><dd>${esc(g.subject)}</dd>` : ""}
    </dl>${files}${diff}`;
}

function summarizeAttrs(attrs) {
  if (!attrs) return "";
  const parts = [];
  if (attrs.text) parts.push(String(attrs.text).slice(0, 80));
  if (attrs.note) parts.push(String(attrs.note).slice(0, 80));
  return parts.join(" · ");
}

function spanDetail(s) {
  return `
    <h2>${kindDot(s.kind)} ${esc(s.name)} ${badge(s.status)}</h2>
    <dl class="kv">
      <dt>span_id</dt><dd class="mono">${esc(s.span_id)}</dd>
      <dt>kind</dt><dd>${esc(s.kind)}</dd>
      <dt>start → end</dt><dd class="mono">${esc(fmtTs(s.start))} → ${esc(fmtTs(s.end))} (${fmtDur(s.duration_ms)})</dd>
      ${s.model ? `<dt>model</dt><dd class="mono">${esc(s.model)}</dd>` : ""}
      <dt>tokens in/out/cache</dt><dd class="mono">${fmtTok(s.tokens.input)} / ${fmtTok(s.tokens.output)} / ${fmtTok(s.tokens.cache_read)}</dd>
    </dl>
    <div class="tabs" style="margin-top:10px">
      ${["input", "output", "error", "attributes", "raw"].map((tab, i) =>
        `<button data-tab="${tab}" class="${i === 0 ? "active" : ""}">${tab}</button>`).join("")}
    </div>
    ${["input", "output", "error", "attributes", "raw"].map((tab, i) => `
      <pre class="body tab-pane" data-pane="${tab}" style="display:${i === 0 ? "block" : "none"}"
>${esc(pretty(tab === "raw" ? s : s[tab]))}</pre>`).join("")}`;
}

function pretty(v) {
  if (v == null) return "(not captured — the adapter did not provide this field)";
  return typeof v === "string" ? v : JSON.stringify(v, null, 2);
}

function bindTabs() {
  document.querySelectorAll(".tabs button").forEach(btn => {
    btn.addEventListener("click", () => {
      const host = btn.closest(".panel");
      host.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      host.querySelectorAll(".tab-pane").forEach(p => {
        p.style.display = p.dataset.pane === btn.dataset.tab ? "block" : "none";
      });
    });
  });
}

/* ------------------------------------------------------------------ */
/* sessions / adapters                                                 */
/* ------------------------------------------------------------------ */

async function renderSessions() {
  const data = await api("sessions?limit=100");
  app.innerHTML = `<div class="panel"><h1>sessions</h1>
    <p class="dim">A session is a conversation; click one to see every turn with
    its own duration. (A single trace is ONE turn, which is why a trace page shows
    one round.)</p>
    <table><thead><tr><th>session</th><th>adapter</th><th>last activity</th>
    <th>traces</th><th>tokens in/out</th><th>model</th><th>cwd</th><th>state</th></tr></thead>
    <tbody>${data.sessions.map(s => `
      <tr class="clickable" data-session="${esc(s.session_id)}"><td class="mono">${esc((s.session_id || "").slice(0, 28))}</td>
      <td class="mono">${esc(s.adapter_id)}</td>
      <td class="mono dim">${esc(fmtDay(s.last_ts))} ${esc(fmtTs(s.last_ts))}</td>
      <td>${s.trace_count}</td>
      <td class="mono">${fmtTok(s.tokens.input)} / ${fmtTok(s.tokens.output)}</td>
      <td class="mono dim">${esc(s.model || "—")}</td>
      <td class="mono dim" title="${esc(s.cwd || "")}">${esc((s.cwd || "—").slice(-40))}</td>
      <td>${s.ended ? '<span class="badge ok">ended</span>' : '<span class="badge incomplete">open</span>'}</td>
      </tr>`).join("")}</tbody></table></div>`;
  document.querySelectorAll("tr[data-session]").forEach(row => {
    row.addEventListener("click", () => {
      location.hash = `#/session/${encodeURIComponent(row.dataset.session)}`;
    });
  });
}

/* ------------------------------------------------------------------ */
/* session detail: per-turn timing                                     */
/* ------------------------------------------------------------------ */

async function renderSessionDetail(id) {
  const d = await api(`sessions/${encodeURIComponent(id)}`);
  if (d.error) { app.innerHTML = `<div class="panel"><h1>error</h1><pre class="body">${esc(d.error)}</pre></div>`; return; }
  const max = Math.max(1, ...d.turns.map(t => t.duration_ms || 0));
  const total = Math.max(1, d.work_ms || 1);
  const wall = Math.max(1, d.wall_ms || 1);

  // Two different numbers, deliberately shown apart: what the agent spent, and
  // how long the conversation was open. A reader who conflates them will think
  // the agent was working the whole time the session was open.
  app.innerHTML = `
    <div class="panel">
      <h1>conversation <span class="mono dim">${esc(d.adapter_id)}</span></h1>
      <div class="cards">
        <div class="card"><div class="num">${d.turn_count}</div><div class="lbl">turns</div></div>
        <div class="card"><div class="num">${fmtDur(d.work_ms)}</div><div class="lbl">agent work (trusted turns)</div></div>
        <div class="card"><div class="num">${fmtDur(d.turn_p50_ms)}</div><div class="lbl">turn p50</div></div>
        <div class="card"><div class="num">${fmtDur(d.turn_p95_ms)}</div><div class="lbl">turn p95</div></div>
        <div class="card"><div class="num">${fmtDur(d.wall_ms)}</div><div class="lbl">session open for</div></div>
        <div class="card"><div class="num">${fmtDur(d.outside_ms)}</div><div class="lbl">outside turns</div></div>
        <div class="card ${d.errors ? "err" : ""}"><div class="num">${d.errors}</div><div class="lbl">failed spans</div></div>
        <div class="card"><div class="num">${fmtTok(d.tokens.input)}</div><div class="lbl">input tokens</div></div>
      </div>
      <dl class="kv">
        <dt>session</dt><dd class="mono dim">${esc(d.session_id)}</dd>
        <dt>window</dt><dd class="mono">${esc(d.start)} → ${esc(d.end)}</dd>
        ${Object.keys(d.models || {}).length ? `<dt>models</dt><dd class="mono">${Object.entries(d.models).map(([m, v]) => `${esc(m)}×${v.turns}`).join(", ")}</dd>` : ""}
      </dl>
    </div>

    <div class="panel">
      <h2>each turn, in order</h2>
      <p class="dim">Bar width is the turn's own duration (the longest turn is
      full width); the number is its share of agent time.</p>
      ${d.turns.map((t, i) => `
        <div class="turn-row clickable" data-trace="${esc(t.trace_id)}">
          <span class="turn-idx mono dim">${i + 1}</span>
          <span class="turn-bar-wrap">
            <span class="turn-bar ${t.errors ? "error" : ""} ${t.status === "incomplete" ? "incomplete" : ""}"
                  style="width:${Math.max(2, (100 * (t.duration_ms || 0)) / max).toFixed(2)}%"></span>
          </span>
          <span class="turn-dur mono">${fmtDur(t.duration_ms)}</span>
          <span class="turn-share mono dim">${(((t.duration_ms || 0) / total) * 100).toFixed(1)}%</span>
          <span class="turn-meta dim">${esc((t.start || "").slice(11, 19))} · ${t.spans} spans ·
            ${t.kinds.join("/") || "no spans"}
            ${t.interrupted ? '· <span class="badge incomplete">interrupted (wall time is a gap, not work)</span>' : ""}</span>
        </div>`).join("")}
    </div>

    <div class="panel">
      <h2>across the session's wall clock</h2>
      <p class="dim">Turns positioned where they actually happened; the gaps are
      time the agent was not working.</p>
      <div class="wall-strip">
        ${d.turns.map((t, i) => {
          const s0 = Date.parse(d.start), e0 = Date.parse(d.end);
          const a = Date.parse(t.start || t.end), b = Date.parse(t.end || t.start);
          const left = Math.max(0, ((a - s0) / wall) * 100);
          const width = Math.max(0.15, ((b - a) / wall) * 100);
          return `<span class="wall-block ${t.errors ? "error" : ""}"
                        style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%"
                        title="turn ${i + 1}: ${esc(t.start)} (${fmtDur(t.duration_ms)})"></span>`;
        }).join("")}
      </div>
      <div class="note mono">${esc(d.start)} → ${esc(d.end)}</div>
    </div>

    <div class="panel">
      <h2>turns</h2>
      <table><thead><tr><th>#</th><th>start</th><th>duration</th><th>% agent time</th>
        <th>status</th><th>spans</th><th>tokens in/out</th><th>model</th></tr></thead>
      <tbody>${d.turns.map((t, i) => `
        <tr class="clickable" data-trace="${esc(t.trace_id)}">
          <td class="mono dim">${i + 1}</td>
          <td class="mono dim">${esc(t.start)}</td>
          <td class="mono">${fmtDur(t.duration_ms)}</td>
          <td class="mono dim">${(((t.duration_ms || 0) / total) * 100).toFixed(1)}%</td>
          <td>${badge(t.status)}</td>
          <td>${t.spans}</td>
          <td class="mono">${fmtTok(t.tokens.input)} / ${fmtTok(t.tokens.output)}</td>
          <td class="mono dim">${esc(t.model || "—")}</td>
        </tr>`).join("")}</tbody></table>
    </div>`;
  document.querySelectorAll("tr[data-trace], .turn-row[data-trace]").forEach(row => {
    row.addEventListener("click", () => { location.hash = `#/trace/${row.dataset.trace}`; });
  });
}

async function renderAdapters() {
  const data = await api("adapters");
  // Coverage first: an adapter can be installed, verified and enabled while
  // capturing NOTHING, because a live hook is loaded at process start and an
  // agent already running when it was installed writes nothing until it
  // restarts. That is what an empty dashboard looks like from the inside.
  const building = data.coverage_ready === false
    ? `<div class="panel"><p class="dim">${esc(data.coverage_note || "index still building")}</p></div>`
    : "";
  const warn = (data.coverage_warnings || []).length
    ? `<div class="panel alert">
         <h1>⚠ adapters are not capturing everything</h1>
         ${data.coverage_warnings.map(w => `<p>${esc(w)}</p>`).join("")}
       </div>` : "";
  app.innerHTML = building + warn + data.adapters.map(a => `
    <div class="panel">
      <h1 class="mono">${esc(a.adapter_id)} <span class="badge kind">${esc(a.kind)}</span>
        <span class="badge ${a.compatibility ? esc(a.compatibility.status === "verified" ? "ok" : "unknown") : "unknown"}">${esc(a.compatibility ? a.compatibility.status : "unverified")}</span>
        <span class="badge kind">${esc(a.source)}</span></h1>
      <dl class="kv">
        <dt>integration</dt><dd>${esc(a.integration || "—")}</dd>
        <dt>agent versions</dt><dd>${esc(a.agent_versions || "—")}</dd>
        ${a.module ? `<dt>module</dt><dd class="mono">${esc(a.module)}</dd>` : ""}
        ${a.compatibility ? `<dt>verified by</dt><dd class="mono dim">${esc(a.compatibility.target)}<br>${esc(a.compatibility.verified_by)}</dd>` : ""}
        ${a.observed ? `<dt>observed</dt><dd class="mono">${a.observed.spans} spans, last ${esc(a.observed.last_activity || "—")}${a.observed.unclosed ? `, ${a.observed.unclosed} open` : ""}</dd>` : "<dt>observed</dt><dd class='dim'>no data yet</dd>"}
        ${coverageRow(a)}
      </dl>
      <table class="cap-table" style="margin-top:8px"><tbody>
        ${Object.entries(a.capabilities).map(([cap, info]) => `
          <tr><td>${esc(cap)}</td><td>${badge(info.level)}</td>
          <td class="note">${esc(info.note || "")}</td></tr>`).join("")}
      </tbody></table>
    </div>`).join("") +
    `<div class="panel"><h2>ingest files</h2>
     <table><thead><tr><th>file</th><th>bytes</th><th>corrupt lines</th><th>last scan</th></tr></thead>
     <tbody>${data.ingest_files.map(f => `
       <tr><td class="mono dim">${esc(f.path)}</td><td class="mono">${fmtTok(f.indexed_bytes)}</td>
       <td class="mono ${f.corrupt_lines ? "" : "dim"}">${f.corrupt_lines}${f.last_error ? ` (${esc(f.last_error.slice(0, 40))})` : ""}</td>
       <td class="mono dim">${esc(fmtTs(f.updated_at))}</td></tr>`).join("")}</tbody></table></div>`;
}

function coverageRow(a) {
  const c = a.coverage;
  if (!c) return "";
  const recent = c.untraced_recent_count || 0;
  if (c.error) {
    return `<dt>coverage</dt><dd class="dim">unknown — ${esc(c.error)}</dd>`;
  }
  if (!c.sessions_known) {
    return `<dt>coverage</dt><dd class="dim">no session artefacts found at
      <span class="mono">${esc(c.source || "")}</span></dd>`;
  }
  const state = recent
    ? `<span class="badge error">${recent} recent gap(s)</span>`
    : `<span class="badge ok">complete</span>`;
  const detail = [];
  if (c.untraced_historical) detail.push(`${c.untraced_historical} older than the install`);
  if (c.untraced_unknown_age) detail.push(`${c.untraced_unknown_age} of unknown age (no install marker)`);
  if (c.sessions_traced_only) detail.push(`${c.sessions_traced_only} traced with no artefact left`);
  if (recent && c.backfill_hint) detail.push(`recover: <code>${esc(c.backfill_hint)}</code>`);
  return `<dt>coverage</dt><dd>${state}
      <span class="mono">${c.sessions_covered}/${c.sessions_known} sessions traced</span>
      ${c.installed_at ? `<span class="dim">(adapter since ${esc(c.installed_at.slice(0, 16))})</span>` : ""}
      ${detail.length ? `<br><span class="dim">${detail.join(" · ")}</span>` : ""}
      ${recent ? `<br><span class="mono dim">oldest gap: ${esc(c.untraced_recent[0].started)} ${esc(c.untraced_recent[0].session.slice(0, 46))}</span>` : ""}
    </dd>`;
}

/* ------------------------------------------------------------------ */
/* datasets / experiments (M3)                                         */
/* ------------------------------------------------------------------ */

async function renderDatasets() {
  const data = await api("datasets");
  app.innerHTML = `<div class="panel"><h1>datasets</h1>
    <form id="new-ds" class="filters">
      <input id="ds-name" placeholder="name" required>
      <input id="ds-desc" placeholder="description" size="40">
      <button>create</button>
    </form>
    <table><thead><tr><th>name</th><th>version</th><th>examples</th><th>created</th></tr></thead>
    <tbody>${data.datasets.map(d => `
      <tr class="clickable" data-ds="${esc(d.dataset_id)}">
        <td>${esc(d.name)}</td><td>v${d.version}</td><td>${d.examples}</td>
        <td class="mono dim">${esc(fmtDay(d.created_at))}</td></tr>`).join("")}
    </tbody></table></div>`;
  document.getElementById("new-ds").addEventListener("submit", async e => {
    e.preventDefault();
    const d = await post("datasets", {
      name: document.getElementById("ds-name").value,
      description: document.getElementById("ds-desc").value});
    location.hash = `#/dataset/${d.dataset_id}`;
  });
  document.querySelectorAll("tr[data-ds]").forEach(row => {
    row.addEventListener("click", () => { location.hash = `#/dataset/${row.dataset.ds}`; });
  });
}

async function renderDatasetDetail(id) {
  const d = await api(`datasets/${encodeURIComponent(id)}`);
  app.innerHTML = `
    <div class="panel">
      <h1>${esc(d.name)} <span class="badge kind">v${d.version}</span></h1>
      <p class="dim">${esc(d.description || "")}</p>
      <h2>add example from a recorded trace</h2>
      <form id="from-trace" class="filters">
        <input id="ft-trace" placeholder="trace_id" size="36" required>
        <input id="ft-expected" placeholder='expected JSON, e.g. {"tests_pass": true}' size="44">
        <button>import</button>
      </form>
    </div>
    <div class="panel"><h2>examples (${d.examples.length})</h2>
      <table><thead><tr><th>id</th><th>task</th><th>expected</th><th>source trace</th></tr></thead>
      <tbody>${d.examples.map(e => `
        <tr><td class="mono dim">${esc(e.example_id)}</td>
        <td>${esc(String((e.inputs || {}).task || JSON.stringify(e.inputs)).slice(0, 90))}</td>
        <td class="mono dim">${esc(e.expected ? JSON.stringify(e.expected).slice(0, 60) : "—")}</td>
        <td class="mono dim">${e.metadata && e.metadata.source_trace_id
          ? `<a href="#/trace/${esc(e.metadata.source_trace_id)}">${esc(e.metadata.source_trace_id.slice(0, 20))}…</a>` : "—"}</td>
        </tr>`).join("")}</tbody></table></div>`;
  document.getElementById("from-trace").addEventListener("submit", async e => {
    e.preventDefault();
    let expected = null;
    const raw = document.getElementById("ft-expected").value.trim();
    if (raw) { try { expected = JSON.parse(raw); } catch { alert("expected is not valid JSON"); return; } }
    try {
      await post(`datasets/${encodeURIComponent(id)}/from_trace`,
        {trace_id: document.getElementById("ft-trace").value.trim(), expected});
      renderDatasetDetail(id);
    } catch (err) { alert(String(err)); }
  });
}

async function renderExperiments() {
  const [exps, datasets, evaluators] = await Promise.all([
    api("experiments"), api("datasets"), api("evaluators")]);
  app.innerHTML = `<div class="panel"><h1>experiments</h1>
    <form id="new-exp" class="filters">
      <input id="ex-name" placeholder="name" required>
      <select id="ex-ds">${datasets.datasets.map(d =>
        `<option value="${esc(d.dataset_id)}">${esc(d.name)}</option>`).join("")}</select>
      <input id="ex-model" placeholder="model (optional, for cost)">
      <button>create</button>
    </form>
    <p class="note">evaluators: ${evaluators.evaluators.map(e =>
      `<code title="${esc(e.description)}">${esc(e.evaluator_id)}</code>`).join(" ")}</p>
    <table><thead><tr><th>name</th><th>status</th><th>runs</th><th>dataset</th><th>created</th></tr></thead>
    <tbody>${exps.experiments.map(e => `
      <tr class="clickable" data-exp="${esc(e.experiment_id)}">
        <td>${esc(e.name)}</td><td>${badge(e.status === "done" ? "ok" : "unknown").replace("ok", e.status).replace("unknown", e.status)}</td>
        <td>${e.runs}</td><td class="mono dim">${esc(e.dataset_id.slice(0, 12))}</td>
        <td class="mono dim">${esc(fmtDay(e.created_at))}</td></tr>`).join("")}
    </tbody></table></div>`;
  document.getElementById("new-exp").addEventListener("submit", async e => {
    e.preventDefault();
    const model = document.getElementById("ex-model").value.trim();
    const exp = await post("experiments", {
      name: document.getElementById("ex-name").value,
      dataset_id: document.getElementById("ex-ds").value,
      evaluator_ids: evaluators.evaluators
        .filter(v => v.kind !== "llm_judge")   // judge is opt-in: it costs a model call
        .map(v => v.evaluator_id),
      agent_config: model ? {model} : {}});
    location.hash = `#/experiment/${exp.experiment_id}`;
  });
  document.querySelectorAll("tr[data-exp]").forEach(row => {
    row.addEventListener("click", () => { location.hash = `#/experiment/${row.dataset.exp}`; });
  });
}

async function renderExperimentDetail(id) {
  const e = await api(`experiments/${encodeURIComponent(id)}`);
  const runs = e.runs || [];
  const evalIds = Object.keys(e.evaluator_ids || {});
  app.innerHTML = `
    <div class="panel">
      <h1>${esc(e.name)} <span class="badge kind">${esc(e.status)}</span></h1>
      <dl class="kv">
        <dt>dataset</dt><dd class="mono">${esc(e.dataset_id)} v${e.dataset_version}</dd>
        <dt>agent config</dt><dd class="mono">${esc(JSON.stringify(e.agent_config))}</dd>
        <dt>evaluators</dt><dd class="mono">${evalIds.map(x =>
          `${esc(x)}@${esc(e.evaluator_ids[x])}`).join(", ")}</dd>
      </dl>
      <div class="filters" style="margin-top:10px">
        <button id="run-exp">run (score attached traces)</button>
        <input id="at-example" placeholder="example_id" size="18">
        <input id="at-trace" placeholder="trace_id" size="34">
        <button id="attach-run">attach</button>
      </div>
    </div>
    <div class="panel"><h2>runs (${runs.length})</h2>
      <table><thead><tr><th>example</th><th>trace</th><th>status</th>
        ${evalIds.map(x => `<th>${esc(x)}</th>`).join("")}
        <th>tokens in/out</th><th>cost</th></tr></thead>
      <tbody>${runs.map(r => `
        <tr><td class="mono dim">${esc(r.example_id.slice(0, 10))}</td>
        <td class="mono">${r.trace_id
          ? `<a href="#/trace/${esc(r.trace_id)}">${esc(r.trace_id.slice(0, 14))}…</a>` : "—"}</td>
        <td>${badge(r.status)}</td>
        ${evalIds.map(x => {
          const s = (r.scores || {})[x];
          if (!s) return `<td class="dim">—</td>`;
          return `<td title="${esc((s.evidence || []).join("\n"))}">${s.passed ? "✅" : "❌"} ${s.score}${s.confidence < 0.5 ? " <span class='dim'>?</span>" : ""}</td>`;
        }).join("")}
        <td class="mono">${fmtTok(r.tokens_in)} / ${fmtTok(r.tokens_out)}</td>
        <td class="mono">${r.cost_usd == null ? "<span class='dim'>unknown</span>" : "$" + r.cost_usd.toFixed(4)}</td>
        </tr>`).join("")}</tbody></table></div>
    <div class="panel"><h2>evidence</h2>
      ${runs.filter(r => r.scores).map(r =>
        Object.values(r.scores).filter(s => (s.evidence || []).length).map(s =>
          `<div class="ev-row"><span class="mono dim">${esc(s.evaluator_id)}</span>
           <span>${esc(s.evidence[0])}${s.confidence < 1 ? ` <span class="dim">(confidence ${s.confidence})</span>` : ""}</span></div>`
        ).join("")).join("") || '<p class="dim">run the experiment to see evidence</p>'}
    </div>`;
  document.getElementById("run-exp").addEventListener("click", async () => {
    await post(`experiments/${encodeURIComponent(id)}/run`, {});
    renderExperimentDetail(id);
  });
  document.getElementById("attach-run").addEventListener("click", async () => {
    await post(`experiments/${encodeURIComponent(id)}/attach`, {
      example_id: document.getElementById("at-example").value.trim(),
      trace_id: document.getElementById("at-trace").value.trim()});
    renderExperimentDetail(id);
  });
}

/* ------------------------------------------------------------------ */
/* live updates via SSE                                                */
/* ------------------------------------------------------------------ */

function connectSse() {
  const dot = document.getElementById("live");
  const src = new EventSource(`/api/stream?token=${encodeURIComponent(TOKEN)}`);
  let timer = null;
  src.onopen = () => { dot.className = "live on"; dot.title = "live updates: connected"; };
  src.onerror = () => { dot.className = "live off"; dot.title = "live updates: reconnecting"; };
  src.onmessage = () => {
    clearTimeout(timer);
    timer = setTimeout(() => currentRender && currentRender(), 500);
  };
}

if (!TOKEN) {
  app.innerHTML = missingTokenNotice();
} else {
  router();
  connectSse();
}
