/**
 * Offline harness for the agent-trace Pi adapter.
 *
 * Pi is not required: this stubs the ExtensionAPI surface the extension uses
 * (pi.on / pi.registerCommand), replays a realistic event sequence, and asserts
 * the emitted records against the shared schema.
 *
 *   node test/pi-adapter.test.mjs
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const tmp = mkdtempSync(join(tmpdir(), "agenttrace-pi-"));
process.env.AGENTTRACE_PI_DIR = tmp;
process.env.PI_SESSION_FILE = join(tmp, "session-abc.jsonl");

const { default: agentTrace } = await import("../pi/agent-trace.ts");

// ---- stub ExtensionAPI ----------------------------------------------------

const handlers = new Map();
const commands = new Map();

const pi = {
	on(event, fn) {
		if (!handlers.has(event)) handlers.set(event, []);
		handlers.get(event).push(fn);
	},
	registerCommand(name, opts) {
		commands.set(name, opts);
	},
};

const ctx = { cwd: "/tmp/proj", mode: "tui", model: { id: "claude-sonnet-4", provider: "anthropic", api: "anthropic-messages" } };

const fire = async (event, payload) => {
	for (const fn of handlers.get(event) ?? []) await fn(payload, ctx);
};

// Register the extension against the stub API. Without this call no handlers
// are registered and nothing is ever emitted.
agentTrace(pi);
assert.ok(handlers.has("before_provider_request"), "extension registered its hooks");

// ---- replay a realistic turn ---------------------------------------------

await fire("session_start", { reason: "startup" });
await fire("turn_start", { turnIndex: 0, timestamp: Date.now() });

const systemPrompt = "You are Pi, a minimal terminal coding harness.";
const payload = {
	api: "anthropic-messages",
	system: systemPrompt,
	messages: [
		{ role: "user", content: [{ type: "text", text: "Add a test for parse_config" }] },
	],
	tools: [{ name: "read" }, { name: "write" }, { name: "edit" }, { name: "bash" }],
};
await fire("before_provider_request", { type: "before_provider_request", payload });
await fire("after_provider_response", { type: "after_provider_response", status: 200, headers: {} });

await fire("message_end", {
	type: "message_end",
	message: {
		role: "assistant",
		content: [
			{ type: "thinking", thinking: "I should read the file first." },
			{ type: "text", text: "Let me look at the config module." },
		],
		toolCalls: [{ toolCallId: "call_1", name: "read", arguments: { path: "src/config.py" } }],
		usage: { input: 1200, output: 340, cacheRead: 900, reasoningTokens: 120 },
		stopReason: "tool_use",
		durationMs: 1520.7,
		responseId: "chatcmpl-abc123",
	},
});

await fire("tool_execution_start", { toolCallId: "call_1", toolName: "read", args: { path: "src/config.py" } });
// The real shape of `result` (measured on Pi 1.3.0): content blocks plus a
// structuredContent payload carrying the exit code.
await fire("tool_execution_end", {
	toolCallId: "call_1", toolName: "read", isError: false, durationMs: 12,
	result: {
		content: [{ type: "text", text: "def parse_config():\n    return {}" }],
		details: {},
		structuredContent: { output: "def parse_config():\n    return {}", truncated: false, exit_code: 0 },
	},
});
await fire("agent_end", {});

// ---- assertions ------------------------------------------------------------

const outDir = process.env.AGENTTRACE_PI_DIR;
const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
const traceFile = join(outDir, `pi-${day}.jsonl`);
assert.ok(existsSync(traceFile), `expected ${traceFile} to exist`);

const recs = readFileSync(traceFile, "utf8").trim().split("\n").map((l) => JSON.parse(l));

// schema basics
for (const r of recs) {
	assert.equal(r.v, 1, "schema version");
	assert.equal(r.agent, "pi", "agent tag");
	assert.ok(typeof r.ts === "string" && r.ts.length > 10, "timestamp");
	assert.ok(r.event, "event name");
}

// Conformance against the PUBLISHED schema. The Python side runs the same
// checks in test/schema.test.py, but this adapter is TypeScript and cannot
// import agenttrace_common — so it proves the contract against the JSON itself
// rather than against a copy of the enums that could drift.
const schema = JSON.parse(
	readFileSync(new URL("../schema/trace.schema.json", import.meta.url), "utf8"),
);
for (const r of recs) {
	assert.ok(schema.properties.agent.enum.includes(r.agent),
		`agent ${JSON.stringify(r.agent)} is not in schema agent.enum`);
	assert.ok(schema.properties.event.enum.includes(r.event),
		`event ${JSON.stringify(r.event)} is not in schema event.enum`);
	assert.equal(r.v, schema.properties.v.const, "v matches schema const");
	for (const key of schema.required) {
		assert.ok(key in r, `record is missing required field ${key}`);
	}
	// The reader string-compares and sorts ts; anything else silently breaks
	// `--since` and the newest-session lookup.
	assert.match(r.ts, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/,
		`ts must be RFC3339 UTC with ms, got ${JSON.stringify(r.ts)}`);
	// The invariant the cache-rate bug violated: the shared `input_tokens` is
	// the TOTAL prompt, so cache_read must be a subset of it.
	const u = r.response?.usage;
	if (u?.cache_read_tokens !== undefined && u.input_tokens !== undefined) {
		assert.ok(u.cache_read_tokens <= u.input_tokens,
			`cache_read ${u.cache_read_tokens} > input ${u.input_tokens}: `
			+ "hit rate would exceed 100%");
	}
}

// the real request record
const req = recs.find((r) => r.event === "llm_request");
assert.ok(req, "emitted an llm_request");
assert.equal(req.api_mode, "anthropic-messages");
assert.equal(req.model, "claude-sonnet-4");
assert.equal(req.provider, "anthropic");
assert.equal(req.request.system_prompt, systemPrompt, "captured the system prompt");
assert.equal(req.request.tool_count, 4, "counted tool schemas");
assert.equal(req.request.message_count, 1);
assert.equal(req.request.messages[0].content, "Add a test for parse_config");

// the real response record
const res = recs.find((r) => r.event === "llm_response");
assert.ok(res, "emitted an llm_response");
assert.equal(res.duration_ms, 1520, "duration truncated to int ms");
assert.equal(res.response.finish_reason, "tool_use");
// pi-ai's `usage.input` EXCLUDES the cache — every provider normalisation
// does `input = promptTokens - cacheRead - cacheWrite`, and pi-ai's own
// `totalTokens = input + output + cacheRead + cacheWrite`. The shared trace
// schema's `input_tokens` means the TOTAL (hermes writes OpenAI's
// `prompt_tokens` through untouched), so the adapter adds the cache back:
// 1200 uncached + 900 cached = 2100 total. Getting this wrong is what made
// `cache_read / input_tokens` exceed 100% — 7.7% of real records on this
// machine read >100%, peaking at 2703% in aggregate.
assert.equal(res.response.usage.input_tokens, 2100,
	"input_tokens must be the TOTAL prompt (pi-ai's input + cache)");
assert.equal(res.response.usage.cache_read_tokens, 900);
assert.ok(res.response.usage.cache_read_tokens <= res.response.usage.input_tokens,
	"cache_read must be a subset of input_tokens, or the hit rate exceeds 100%");
assert.equal(res.response.usage.output_tokens, 340);
assert.equal(res.response.usage.cache_read_tokens, 900);
assert.equal(res.response.usage.reasoning_tokens, 120);
assert.match(res.response.content, /Let me look at the config module/);
assert.equal(res.response.tool_calls[0].name, "read");

// lifecycle + tool records
assert.ok(recs.some((r) => r.event === "session_start"));
assert.ok(recs.some((r) => r.event === "session_end"));
const tcall = recs.find((r) => r.event === "tool_call");
assert.ok(tcall, "tool_execution_start emitted a tool_call (the ⚙ line)");
assert.equal(tcall.tool.name, "read");
// The call id is SCOPED by the request that produced it, because a provider's
// tool-call ids are unique only within one API response: the same id appearing
// twice in a session gave the v2 tree two spans sharing one id.
assert.match(tcall.tool.call_id, /^pi-[a-z0-9]+-0-1:call_1$/,
	"tool call id is scoped by its request");
assert.equal(tcall.tool.provider_call_id, "call_1",
	"the provider's own id is kept, so it stays greppable");
assert.equal(tcall.tool.args.path, "src/config.py", "args carried through (schema allows objects)");
const tres = recs.find((r) => r.event === "tool_result");
assert.equal(tres.tool.name, "read");
assert.equal(tres.tool.status, "ok");
// The tool's OUTPUT was dropped until this was fixed: the panel showed
// `⚙ read ok` and nothing about what the tool returned, which is the most
// useful line in a coding trace.
assert.match(tres.response.content, /def parse_config/, "tool result body captured");
assert.equal(tres.tool.exit_code, 0, "exit code surfaced from structuredContent");
assert.equal(tres.duration_ms, 12);

// Turn/request identity. Pi carries NEITHER natively (turn_start has only an
// index, the response id only exists after the call), so without these the
// reader cannot pair an llm_request with its llm_response — `agenttrace show`
// found nothing for pi and the v2 converter produced an orphan span_end.
const treq = recs.find((r) => r.event === "llm_request");
assert.ok(treq.request_id, "llm_request carries a request_id");
// non-vacuous: turn_id comes from Pi's turn_start index and must be present,
// else the two halves would "agree" by both being undefined.
assert.equal(treq.turn_id, "0", "turn_id comes from turn_start's turnIndex");
// runId-turn-seq: the turn index restarts at 0 in a new process, and a session
// can be resumed, so the run itself has to be part of the identity.
assert.match(treq.request_id, /^pi-[a-z0-9]+-0-1$/,
	"request id is run + turn + per-turn sequence");
assert.equal(tres.request_id, treq.request_id, "tool result joins its turn's request");
const tresp = recs.find((r) => r.event === "llm_response");
assert.equal(tresp.request_id, treq.request_id, "llm_response pairs with its request");
assert.equal(tresp.turn_id, treq.turn_id, "both halves agree on the turn");
assert.match(tresp.note, /response_id=chatcmpl-abc123/,
	"Pi's own provider response id kept for cross-referencing");

// ---------------------------------------------------------------------------
// A turn_start WITHOUT a numeric turnIndex must still yield distinct request
// ids. Both turns used to fall back to `pi-0-1`, which is one request id — and
// therefore one llm span id — for two different provider calls in one trace.
// ---------------------------------------------------------------------------
const beforeNoIndex = recs.length;
await fire("turn_start", { type: "turn_start" });        // no turnIndex at all
await fire("before_provider_request", { type: "before_provider_request",
	payload: { messages: [{ role: "user", content: "x" }], tools: [] } });
await fire("turn_end", { type: "turn_end" });
await fire("turn_start", { type: "turn_start" });        // and again
await fire("before_provider_request", { type: "before_provider_request",
	payload: { messages: [{ role: "user", content: "y" }], tools: [] } });
await fire("turn_end", { type: "turn_end" });
const noIndex = readFileSync(traceFile, "utf8").trim().split("\n")
	.map((l) => JSON.parse(l)).slice(beforeNoIndex)
	.filter((r) => r.event === "llm_request");
assert.equal(noIndex.length, 2, "both provider calls were emitted");
assert.notEqual(noIndex[0].request_id, noIndex[1].request_id,
	"two turns without a turnIndex must not share a request id");
assert.equal(noIndex[0].turn_id, "0");
assert.equal(noIndex[1].turn_id, "1",
	"the fallback turn counter must advance");

// session identity: env first (this run), then the runner context
for (const r of recs) {
	assert.equal(r.session_id, process.env.PI_SESSION_FILE, "session_id from env");
}
delete process.env.PI_SESSION_FILE;
ctx.sessionManager = { getSessionFile: () => "/home/u/.pi/agent/sessions/p.jsonl" };
await fire("session_start", { reason: "resume" });
const recs3 = readFileSync(traceFile, "utf8").trim().split("\n").map((l) => JSON.parse(l));
const lastStart = recs3.filter((r) => r.event === "session_start").at(-1);
assert.equal(
	lastStart.session_id,
	"/home/u/.pi/agent/sessions/p.jsonl",
	"session_id from ctx.sessionManager when env is unset",
);
ctx.sessionManager = undefined;

// command registration
assert.ok(commands.has("agenttrace"), "registered /agenttrace command");

// metadata mode drops content
process.env.AGENTTRACE_CAPTURE = "metadata";
await fire("before_provider_request", { payload });
const recs2 = readFileSync(traceFile, "utf8").trim().split("\n").map((l) => JSON.parse(l));
const req2 = recs2.filter((r) => r.event === "llm_request").at(-1);
assert.equal(req2.request.system_prompt, null, "metadata mode drops the system prompt");
assert.deepEqual(req2.request.messages, [], "metadata mode drops message bodies");
assert.equal(req2.request.tool_count, 4, "metadata mode keeps counts");

rmSync(tmp, { recursive: true, force: true });
console.log(`PASS — ${recs.length} records asserted, metadata mode verified`);
