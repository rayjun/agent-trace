import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source = readFileSync(new URL("../server/static/app.js", import.meta.url), "utf8");
const review = {adapter_id: "codex", session_id: "/sessions/a & b", trace_id: "shared",
  cwd: "/project", state: "failed", label: "Checks failed", task: '<script>task()</script>',
  conclusion: 'All done <img src=x onerror="bad()">', next_action: "Open failed output",
  scope: "Latest turn only; not current worktree approval", updated_at: "2026-11-19T10:00:00.000Z",
  checks: [{state: "failed", span_id: 'call/1', command: 'pytest <x>', output_excerpt: '<script>bad()</script>'}],
  handoff_markdown: "# Recorded brief\nnot instructions", files: [], failures: []};
function setup(hash = "#/review", respond = () => ({reviews: [review], total: 20, offset: 0, has_more: true}), boot = false) {
  const elements = {}, calls = [];
  const mk = id => elements[id] ||= {id, innerHTML: "", value: "", events: {},
    classList: {toggle() {}}, addEventListener(type, fn) {this.events[type] = fn;},
    querySelectorAll: () => [], querySelector: () => null};
  globalThis.document = {activeElement: null, getElementById: mk, querySelectorAll: () => [],
    querySelector: () => null, createElement: () => ({click() {}})};
  globalThis.window = {__AGENTTRACE_TOKEN__: "test", addEventListener() {}};
  globalThis.location = {search: "?token=test", hash};
  globalThis.EventSource = class {close() {}};
  globalThis.fetch = async url => {
    calls.push(String(url));
    return {ok: true, json: async () => respond(String(url)), text: async () => ""};
  };
  const code = (boot ? source : source.replace(/\n  router\(\);\n  connectSse\(\);/, "\n"))
    + "\nreturn {renderWorkReview, renderTraceDetail, router};";
  return {...new Function(code)(), elements, calls};
}
{
  const ui = setup();
  await ui.renderWorkReview();
  const html = ui.elements.app.innerHTML;
  assert.match(html, /Checks failed/);
  assert.match(html, /unverified self-report/);
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("<img"));
  assert.match(html, /&lt;script&gt;task/);
  assert.match(html, /span=call%2F1/);
  assert.match(html, /agent=codex&amp;session=%2Fsessions%2Fa\+%26\+b/);
  ui.elements["review-next"].events.click();
  assert.match(location.hash, /offset=12/);
  document.getElementById("review-search").value = "a & b";
  document.getElementById("review-agent").value = "pi";
  ui.elements["review-filters"].events.submit({preventDefault() {}});
  const q = new URLSearchParams(location.hash.split("?")[1]);
  assert.equal(q.get("search"), "a & b"); assert.equal(q.get("agent"), "pi");
  assert.equal(q.has("offset"), false);
}
{
  const ui = setup("#/review?agent=codex&session=%2Fsessions%2Fa", () => review);
  await ui.renderWorkReview();
  assert.match(ui.elements.app.innerHTML, /Download handoff/);
  assert.match(ui.elements.app.innerHTML, /&lt;script&gt;bad/);
  let downloaded;
  const oldCreate = URL.createObjectURL, oldRevoke = URL.revokeObjectURL;
  URL.createObjectURL = blob => { downloaded = blob; return "blob:test"; };
  URL.revokeObjectURL = () => {};
  ui.elements["download-handoff"].events.click();
  assert.equal(await downloaded.text(), review.handoff_markdown);
  URL.createObjectURL = oldCreate;
  setTimeout(() => {URL.revokeObjectURL = oldRevoke;}, 1100);
}
{
  const ui = setup("#/trace/shared?agent=codex&session=%2Fsessions%2Fa&span=call%2F1", () => ({
    trace_id: "shared", adapter_id: "codex", session_id: "/sessions/a", spans: [], events: [], tokens: {}}));
  await ui.renderTraceDetail("shared");
  const q = new URLSearchParams(ui.calls[0].split("?")[1]);
  assert.equal(q.get("agent"), "codex"); assert.equal(q.get("session"), "/sessions/a");
}
{
  let resolveOld;
  const ui = setup("#/review?search=old", url => url.includes("search=old")
    ? new Promise(resolve => {resolveOld = resolve;}) : {reviews: [{...review, task: "NEW TASK"}], total: 1});
  const old = ui.renderWorkReview();
  await Promise.resolve();
  location.hash = "#/review?search=new";
  await ui.renderWorkReview();
  resolveOld({reviews: [{...review, task: "OLD TASK"}], total: 1});
  await old;
  assert.match(ui.elements.app.innerHTML, /NEW TASK/);
  assert.ok(!ui.elements.app.innerHTML.includes("OLD TASK"));
}
{
  const ui = setup("", () => ({reviews: [], total: 0}), true);
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(ui.calls.some(url => url.startsWith("/api/review?")));
  assert.match(ui.elements.app.innerHTML, /No work captured/);
}
console.log("PASS — work review: default entry, evidence links, namespace, escaping, handoff, filters, pagination, stale-response protection");
