// The dashboard's router, tested where it has actually broken.
//
// Two bugs shipped that no server test and no API test could see, because both
// were in the ROUTE -> RENDERER mapping:
//   * a session is identified by a file path, so its route holds percent-escapes
//     and the page escaped them a second time (%2F -> %252F): every session page
//     404'd;
//   * the route was taken from the whole hash, so `#/traces?agent=pi` was a route
//     name with no renderer and the Traces filter silently showed the Overview.
//
// This drives the real router with a DOM shim and asserts which API path each
// hash asks for, which is exactly the mapping that was wrong.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const src = readFileSync(new URL("../server/static/app.js", import.meta.url), "utf8");

function load(hash, { request, defer = setTimeout, cancel = clearTimeout, queryAll = () => [] } = {}) {
  const els = {};
  const mk = (id) => (els[id] ||= {
    id, innerHTML: "", dataset: {}, style: {}, value: "", textContent: "",
    classList: { add() {}, remove() {}, toggle() {} },
    listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelectorAll: selector => queryAll(selector, id), querySelector: () => null,
    contains(node) { return this.querySelectorAll(".tabs button").includes(node); },
    closest: () => null,
    focus() { document.activeElement = this; },
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
  });
  const calls = [];
  const document = {
    getElementById: (id) => mk(id),
    querySelectorAll: selector => queryAll(selector),
    querySelector: () => null,
  };
  let html = "";
  Object.defineProperty(mk("app"), "innerHTML", {
    get: () => html,
    set(value) {
      for (const [, id] of html.matchAll(/id="([^"]+)"/g)) {
        if (document.activeElement === els[id]) document.activeElement = null;
        delete els[id];
      }
      html = value;
    },
  });
  const window = { __AGENTTRACE_TOKEN__: "t", addEventListener() {} };
  const location = { search: "?token=t", hash };
  let stream;
  class EventSource { constructor() { stream = this; } }
  // Minimal valid payloads per endpoint: a renderer that throws on an EMPTY
  // dataset would fail here too, which is the fresh-install case.
  const shapes = [
    [/\/api\/review/, { reviews: [], total: 0 }],
    [/\/api\/overview/, { traces: 0, spans: 0, errors: 0, sessions: 0,
                            tokens: {}, task_duration_ms: {}, per_agent: [] }],
    [/\/api\/traces\?/, { traces: [], total: 0 }],
    [/\/api\/traces\//, { trace_id: "abc123", spans: [], events: [],
                            tokens: {}, adapter_id: "pi" }],
    [/\/api\/sessions\?/, { sessions: [] }],
    [/\/api\/sessions\//, { session_id: "s", turns: [], turn_count: 0,
                              tokens: {}, models: {} }],
    [/\/api\/adapters/, { adapters: [], ingest_files: [], coverage_warnings: [] }],
    [/\/api\/datasets\//, { dataset_id: "d", name: "d", version: 1, examples: [] }],
    [/\/api\/datasets/, { datasets: [] }],
    [/\/api\/experiments\//, { experiment_id: "e", name: "e", runs: [],
                                 evaluator_ids: {} }],
    [/\/api\/experiments/, { experiments: [] }],
    [/\/api\/evaluators/, { evaluators: [] }],
  ];
  const fetch = async (url, options) => {
    const asked = String(url);
    calls.push(asked);
    const payload = await request?.(asked, options);
    const hit = shapes.find(([re]) => re.test(asked));
    return { ok: true, status: 200,
             json: async () => payload ?? (hit ? hit[1] : {}), text: async () => "" };
  };
  const body = src.replace(/\n  router\(\);\n  connectSse\(\);/, "\n")
    + "\nreturn { router, connectSse };";
  const { router, connectSse } = new Function(
    "document", "window", "location", "EventSource", "fetch", "setTimeout", "clearTimeout", body
  )(document, window, location, EventSource, fetch, defer, cancel);
  connectSse();
  return { router, calls, els, location, stream, document };
}

async function routeFor(hash) {
  const { router, calls, els } = load(hash);
  await router();
  await flush();
  return { calls, html: els.app.innerHTML };
}

const SESSION = "/Users/ray/.pi/agent/sessions/--slug--/2026-10-17T02-46-10-412Z_abc.jsonl";

// 1. overview
{
  const { calls } = await routeFor("#/overview");
  assert.ok(calls.some((u) => u.includes("/api/overview")), `overview: ${calls}`);
}

// 2. the Traces page WITH a query — the bug that showed the Overview instead
{
  const { calls, html } = await routeFor("#/traces?agent=pi&status=ok");
  assert.ok(calls.some((u) => u.includes("/api/traces?")),
    `a filtered traces URL must reach the traces API, asked for: ${calls}`);
  assert.ok(!calls.some((u) => u.includes("/api/overview")),
    "and must not fall through to the overview API");
  assert.ok(/id="f-agent"/.test(html), "the filter controls should be rendered");
}

// 3. a session id with slashes: escaped ONCE, never twice
{
  const { calls } = await routeFor(`#/session/${encodeURIComponent(SESSION)}`);
  const asked = calls.find((u) => u.includes("/api/sessions/"));
  assert.ok(asked, `session: ${calls}`);
  assert.ok(!asked.includes("%252F"),
    `the id must not be double-escaped: ${asked.slice(0, 90)}`);
  const encoded = asked.slice(asked.indexOf("/api/sessions/") + "/api/sessions/".length)
    .split("?")[0];
  assert.equal(decodeURIComponent(encoded), SESSION,
    "one decode must give the session id back");
}

// 4. trace / dataset / experiment routes
for (const [hash, api] of [
  ["#/trace/abc123", "/api/traces/abc123"],
  ["#/dataset/def456", "/api/datasets/def456"],
  ["#/experiment/ghi789", "/api/experiments/ghi789"],
]) {
  const { calls } = await routeFor(hash);
  assert.ok(calls.some((u) => u.includes(api)),
    `${hash} should ask for ${api}, asked for: ${calls}`);
}

// 5. an unknown route lands on the overview rather than rendering nothing
{
  const { calls } = await routeFor("#/nonsense");
  assert.ok(calls.some((u) => u.includes("/api/overview")),
    `unknown route should fall back to the overview: ${calls}`);
}

function flush() { return new Promise(resolve => setImmediate(resolve)); }

function liveDashboard(request, hash = "#/overview", queryAll) {
  const timers = new Map();
  let nextId = 0;
  const ui = load(hash, {
    request, queryAll,
    defer(fn, ms) {
      assert.equal(ms, 500);
      timers.set(++nextId, fn);
      return nextId;
    },
    cancel(id) { timers.delete(id); },
  });
  return {
    ...ui, timers,
    async tick() {
      const entry = timers.entries().next().value;
      assert.ok(entry, "expected a scheduled refresh");
      timers.delete(entry[0]);
      entry[1]();
      await flush();
    },
  };
}

test("opening and reconnecting the stream each catch up without a change frame", async () => {
  const ui = liveDashboard();
  await ui.router();
  await flush();
  assert.equal(ui.calls.length, 2);
  ui.stream.onopen();
  await ui.tick();
  assert.equal(ui.calls.length, 4);
  ui.stream.onerror();
  assert.equal(ui.els.live.className, "live off");
  ui.stream.onopen();
  await ui.tick();
  assert.equal(ui.calls.length, 6);
  assert.equal(ui.els.live.className, "live on");
});

test("a burst cannot postpone refresh and slow reads get just one follow-up", async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const ui = liveDashboard(() => blocked);
  ui.router();
  assert.equal(ui.calls.length, 2);
  try {
    ui.stream.onmessage();
    const original = [...ui.timers.keys()];
    for (let i = 0; i < 20; i++) ui.stream.onmessage();
    assert.deepEqual([...ui.timers.keys()], original);
    await ui.tick();
    ui.stream.onmessage();
    await ui.tick();
    assert.equal(ui.calls.length, 2);
  } finally {
    release();
  }
  await flush();
  assert.equal(ui.calls.length, 4);
});

test("a slow response from the previous route cannot overwrite the current page", async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const ui = liveDashboard(url => url.includes("/api/overview") ? blocked : undefined);
  ui.router();
  ui.stream.onmessage();
  await ui.tick();
  ui.location.hash = "#/sessions";
  await ui.router();
  await flush();
  assert.match(ui.els.app.innerHTML, /<h1>sessions<\/h1>/);
  release();
  await flush();
  assert.match(ui.els.app.innerHTML, /<h1>sessions<\/h1>/);
  assert.equal(ui.calls.filter(url => url.includes("/api/overview")).length, 1);
});

test("returning to the same hash still rejects errors from the previous visit", async () => {
  let reject;
  const first = new Promise((_, fail) => { reject = fail; });
  let sessionsRequests = 0;
  const { router, location, els } = load("#/sessions", {
    request(url) {
      if (url.includes("/api/sessions?") && sessionsRequests++ === 0) return first;
    },
  });
  router();
  location.hash = "#/overview";
  await router();
  location.hash = "#/sessions";
  await router();
  await flush();
  assert.match(els.app.innerHTML, /<h1>sessions<\/h1>/);
  reject(new Error("obsolete request failed"));
  await flush();
  assert.match(els.app.innerHTML, /<h1>sessions<\/h1>/);
});

test("reconnect refreshes Work Review while preserving a focused filter and caret", async () => {
  const ui = liveDashboard(undefined, "#/review");
  await ui.router();
  const filter = ui.els["review-search"];
  filter.value = "draft search";
  filter.setSelectionRange(2, 4);
  filter.closest = () => ui.els["review-filters"];
  filter.focus();
  ui.stream.onopen();
  await ui.tick();
  const updated = ui.els["review-search"];
  assert.equal(ui.calls.length, 2);
  assert.notEqual(updated, filter);
  assert.equal(updated.value, "draft search");
  assert.equal(ui.document.activeElement, updated);
  assert.deepEqual([updated.selectionStart, updated.selectionEnd], [2, 4]);
});

test("reconnect preserves blurred dataset drafts without carrying them across visits", async () => {
  const ui = liveDashboard(undefined, "#/datasets");
  await ui.router();
  ui.els["ds-name"].value = "new dataset";
  ui.els["ds-desc"].value = "unsaved description";
  ui.stream.onopen();
  await ui.tick();
  assert.equal(ui.els["ds-name"].value, "new dataset");
  assert.equal(ui.els["ds-desc"].value, "unsaved description");
  ui.location.hash = "#/sessions";
  await ui.router();
  ui.location.hash = "#/datasets";
  await ui.router();
  assert.equal(ui.els["ds-name"].value, "");
});

test("a failed batch does not release its slot while a sibling read is pending", async () => {
  let fail, release;
  const rejected = new Promise((_, reject) => { fail = reject; });
  const blocked = new Promise(resolve => { release = resolve; });
  let firstOverview = true, firstTraces = true;
  const ui = liveDashboard(url => {
    if (firstOverview && url.includes("/api/overview")) { firstOverview = false; return rejected; }
    if (firstTraces && url.includes("/api/traces?")) { firstTraces = false; return blocked; }
  });
  ui.router();
  ui.stream.onmessage();
  await ui.tick();
  fail(new Error("overview failed"));
  await flush();
  assert.equal(ui.calls.length, 2);
  release();
  await flush();
  assert.equal(ui.calls.length, 4);
  assert.match(ui.els.app.innerHTML, /recent traces/);
});

for (const [hash, control, event] of [
  ["#/dataset/d", "from-trace", "submit"],
  ["#/experiment/e", "run-exp", "click"],
  ["#/experiment/e", "attach-run", "click"],
  ["#/datasets", "new-ds", "submit"],
  ["#/experiments", "new-exp", "submit"],
]) {
  test(`${control} completing after navigation leaves the new page alone`, async () => {
    let release;
    const blocked = new Promise(resolve => { release = resolve; });
    const ui = liveDashboard((_, options) => options?.method === "POST" ? blocked : undefined, hash);
    await ui.router();
    const submitted = ui.els[control].listeners[event]({ preventDefault() {} });
    ui.location.hash = "#/sessions";
    await ui.router();
    const callCount = ui.calls.length;
    release();
    await submitted;
    await flush();
    assert.equal(ui.calls.length, callCount);
    assert.equal(ui.location.hash, "#/sessions");
    assert.match(ui.els.app.innerHTML, /<h1>sessions<\/h1>/);
  });
}

function traceFixture(hash, beforeRead) {
  const controls = (key, values) => values.map(value => {
    const node = {
      dataset: { [key]: value }, style: {}, selected: false,
      classList: { toggle(_, enabled) { node.selected = enabled; } },
      addEventListener(_, fn) { this.click = fn; },
    };
    return node;
  });
  const rows = controls("span", ["first", "second/2"]);
  let tabs, panes;
  function replaceTabs() {
    tabs = controls("tab", ["input", "output"]);
    panes = controls("pane", ["input", "output"]);
  }
  replaceTabs();
  const makeSpan = (span_id, output) => ({
    span_id, name: span_id, kind: "llm", tokens: {}, output, start: "2026-11-27T00:00:00Z",
  });
  const snapshot = { trace_id: "shared", tokens: {}, events: [],
    spans: [makeSpan("first", "first output"), makeSpan("second/2", "second output")] };
  const ui = liveDashboard(async url => {
    if (!url.includes("/api/traces/")) return;
    await beforeRead?.();
    return structuredClone(snapshot);
  }, hash,
    (selector, id) => selector === ".wf-row" ? rows
      : id === "span-detail" && selector === ".tabs button" ? tabs
      : id === "span-detail" && selector === ".tab-pane" ? panes : []);
  return { ...ui, rows, get tabs() { return tabs; }, get panes() { return panes; },
    snapshot, makeSpan, replaceTabs };
}

test("span clicks read from the current snapshot and a pending refresh keeps the newest span and tab", async () => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  let reads = 0;
  const ui = traceFixture("#/trace/shared?agent=codex&session=%2Fsessions%2Fa",
    () => ++reads === 2 ? blocked : undefined);
  await ui.router();
  ui.stream.onmessage();
  await ui.tick();
  assert.equal(reads, 2);
  const detail = ui.els["span-detail"];
  const detachedInput = ui.tabs[0];
  let detailHtml = detail.innerHTML;
  Object.defineProperty(detail, "innerHTML", {
    get: () => detailHtml,
    set(value) { detailHtml = value; ui.replaceTabs(); },
  });
  ui.rows[1].click();
  ui.tabs[1].click();
  assert.match(detail.innerHTML, /<h2>.*second\/2/);
  assert.equal(ui.rows[1].selected, true);
  assert.equal(ui.panes[1].style.display, "block");
  assert.equal(detail.contains(detachedInput), false);
  detachedInput.click();
  assert.equal(ui.calls.length, 2, "only the scheduled refresh reads the API");
  ui.snapshot.spans[1].output = "updated second output";
  release();
  await flush();
  assert.match(ui.els.app.innerHTML, /wf-row selected" data-span="second\/2"/);
  assert.match(ui.els.app.innerHTML, /data-pane="output" style="display:block"\s*>updated second output/);
  for (const call of ui.calls) {
    const params = new URLSearchParams(call.split("?")[1]);
    assert.equal(params.get("agent"), "codex");
    assert.equal(params.get("session"), "/sessions/a");
  }
  ui.location.hash = "#/trace/shared?agent=pi&session=%2Fsessions%2Fb";
  await ui.router();
  assert.match(ui.els.app.innerHTML, /wf-row selected" data-span="first"/);
  assert.match(ui.els.app.innerHTML, /data-pane="input" style="display:block"/);
});

test("a deep-linked span omitted from the waterfall still opens its detail", async () => {
  const ui = traceFixture("#/trace/shared?span=second%2F2");
  ui.snapshot.spans[0].parent_span_id = "second/2";
  ui.snapshot.spans[1].parent_span_id = "first";
  await ui.router();
  assert.doesNotMatch(ui.els.app.innerHTML, /wf-row |not in this trace snapshot/);
  assert.match(ui.els.app.innerHTML, /<h2>.*second\/2/);
});

test("a missing deep-linked span remains selected if it later appears, without falling back to another span", async () => {
  const ui = traceFixture("#/trace/shared?span=later%2F3");
  await ui.router();
  assert.match(ui.els.app.innerHTML, /span later\/3 is not in this trace snapshot/);
  assert.doesNotMatch(ui.els.app.innerHTML, /wf-row selected/);
  ui.snapshot.spans.push(ui.makeSpan("later/3", "late output"));
  ui.stream.onmessage();
  await ui.tick();
  assert.match(ui.els.app.innerHTML, /wf-row selected" data-span="later\/3"/);
  assert.match(ui.els.app.innerHTML, /late output/);
});

console.log("PASS — dashboard routing: query strings, escaped session ids, "
  + "trace/dataset/experiment routes, unknown-route fallback");
