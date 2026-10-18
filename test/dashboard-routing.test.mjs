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

const src = readFileSync(new URL("../server/static/app.js", import.meta.url), "utf8");

function load(hash) {
  const els = {};
  const mk = (id) => (els[id] ||= {
    id, innerHTML: "", dataset: {}, style: {}, value: "", textContent: "",
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
    closest: () => null, focus() {},
  });
  const calls = [];
  globalThis.document = {
    getElementById: (id) => mk(id),
    querySelectorAll: () => [],
    querySelector: () => null,
  };
  globalThis.window = { __AGENTTRACE_TOKEN__: "t", addEventListener() {} };
  globalThis.location = { search: "?token=t", hash };
  globalThis.EventSource = class { close() {} };
  // Minimal valid payloads per endpoint: a renderer that throws on an EMPTY
  // dataset would fail here too, which is the fresh-install case.
  const shapes = [
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
  globalThis.fetch = async (url) => {
    const asked = String(url);
    calls.push(asked);
    const hit = shapes.find(([re]) => re.test(asked));
    return { ok: true, status: 200,
             json: async () => (hit ? hit[1] : {}), text: async () => "" };
  };
  const body = src
    .replace("const TOKEN = new URLSearchParams(location.search).get(\"token\")\n  || window.__AGENTTRACE_TOKEN__ || \"\";", "const TOKEN = \"t\";")
    .replace(/^router\(\);\s*$/m, "")
    .replace(/^connectSse\(\);\s*$/m, "")
    + "\nreturn { router };";
  const { router } = new Function(body)();
  return { router, calls, els };
}

async function routeFor(hash) {
  const { router, calls, els } = load(hash);
  router();
  await new Promise((r) => setTimeout(r, 60));
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

console.log("PASS — dashboard routing: query strings, escaped session ids, "
  + "trace/dataset/experiment routes, unknown-route fallback");
