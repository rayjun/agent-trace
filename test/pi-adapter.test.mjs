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
	},
});

await fire("tool_execution_start", { toolCallId: "call_1", toolName: "read", args: { path: "src/config.py" } });
await fire("tool_execution_end", { toolCallId: "call_1", toolName: "read", isError: false, durationMs: 12 });
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
assert.equal(res.response.usage.input_tokens, 1200);
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
assert.equal(tcall.tool.call_id, "call_1");
assert.equal(tcall.tool.args.path, "src/config.py", "args carried through (schema allows objects)");
const tres = recs.find((r) => r.event === "tool_result");
assert.equal(tres.tool.name, "read");
assert.equal(tres.tool.status, "ok");

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
