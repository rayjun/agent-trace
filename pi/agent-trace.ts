/**
 * agent-trace — local LLM input/output tracer for Pi (pi-coding-agent).
 *
 * Writes one JSONL record per observed event to ~/.pi/traces/pi-YYYYMMDD.jsonl
 * in the shared agent-trace format (schema/trace.schema.json), so `agenttrace`
 * reads Hermes, Codex and Pi traces with one CLI.
 *
 * Pi is the only one of the three with a *live* request hook, so this adapter is
 * the most complete: it sees the exact wire payload before the provider call
 * and the exact response after.
 *
 * Events used (see packages/coding-agent/src/core/extensions/types.ts):
 *   before_provider_request -> the outbound payload (full messages + tools)
 *   after_provider_response -> HTTP status, before the stream is consumed
 *   message_end            -> finalized assistant message incl. tool calls
 *   tool_execution_end     -> tool result
 *   turn_end / agent_end   -> turn boundaries
 *
 * Privacy: content is written in cleartext to a local file. Nothing leaves the
 * machine. Set AGENTTRACE_CAPTURE=metadata to drop message bodies.
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// --------------------------------------------------------------------------
// the Pi surface this adapter reads
// --------------------------------------------------------------------------
//
// Pi's ExtensionAPI ships no type package and this extension is loaded as
// live source rather than compiled against pi's own types, so every handler
// parameter used to be `any`. Under `strict` that still type-checked a typo:
// `event.toolNam` compiled fine and quietly emitted a record with
// `name: undefined`, which the panel then rendered as `⚙ undefined`.
//
// These interfaces pin down ONLY what this file actually reads. Anything the
// adapter does not touch stays out of them — inventing fields would make the
// types a claim about Pi rather than a description of our own usage.

interface SessionContext {
	cwd?: string;
	mode?: string;
	model?: string | { id?: string; provider?: string; api?: string };
	/** Present only in runner-launched contexts; see pickSessionId(). */
	sessionManager?: {
		getSessionFile?: () => string | undefined;
		getSessionId?: () => string | undefined;
	};
	ui: { notify: (message: string, level?: string) => void };
}

/** The subset of each lifecycle event this adapter consumes. */
interface ExtensionEvent {
	/** session_start */
	reason?: string;
	/** before_provider_request — the exact outbound wire payload. */
	payload?: any;
	/** after_provider_response */
	status?: number;
	/** message_end */
	message?: any;
	/** tool_execution_start / tool_execution_end */
	toolName?: string;
	toolCallId?: string;
	args?: unknown;
	isError?: boolean;
	durationMs?: number;
}

interface ExtensionHost {
	on(event: string, handler: (event: ExtensionEvent, ctx: SessionContext) => void): void;
	registerCommand(
		name: string,
		opts: {
			description: string;
			handler: (args: string, ctx: SessionContext) => Promise<void> | void;
		},
	): void;
}

const AGENT = "pi";
const SCHEMA_V = 1;
const MAX_CHARS = Number(process.env.AGENTTRACE_MAX_CHARS ?? 200_000);

function traceDir(): string {
	const base = process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
	const dir = process.env.AGENTTRACE_PI_DIR || join(base, "traces");
	mkdirSync(dir, { recursive: true });
	return dir;
}

function captureMode(): string {
	return (process.env.AGENTTRACE_CAPTURE || "full").trim().toLowerCase();
}

function truncate(v: unknown): unknown {
	if (typeof v === "string" && v.length > MAX_CHARS) {
		return v.slice(0, MAX_CHARS) + `\n...[truncated ${v.length - MAX_CHARS} chars]`;
	}
	return v;
}

function emit(rec: Record<string, unknown>): void {
	const out: Record<string, unknown> = {
		v: SCHEMA_V,
		ts: rec.ts ?? new Date().toISOString(),
		agent: AGENT,
		...rec,
	};
	for (const k of Object.keys(out)) {
		if (out[k] === undefined) delete out[k];
	}
	let line: string;
	try {
		line = JSON.stringify(out);
	} catch {
		return;
	}
	if (process.env.AGENTTRACE_REDACT !== "0") {
		line = line.replace(/(sk-[A-Za-z0-9_-]{16,})/g, "sk-***");
	}
	const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
	try {
		appendFileSync(join(traceDir(), `pi-${day}.jsonl`), line + "\n", "utf8");
	} catch {
		/* never break the agent over tracing */
	}
}

/** Flatten Pi's content-block union (text / thinking / image / toolCall) to text. */
function contentToText(content: unknown): string {
	if (content == null) return "";
	if (typeof content === "string") return content;
	if (Array.isArray(content)) {
		return content
			.map((part: any) => {
				if (typeof part === "string") return part;
				if (part && typeof part === "object") {
					if (typeof part.text === "string") return part.text;
					if (typeof part.thinking === "string") return part.thinking;
					if (typeof part.content === "string") return part.content;
				}
				return "";
			})
			.filter(Boolean)
			.join("\n");
	}
	return String(content);
}

function shapeMessages(messages: unknown): any[] {
	if (!Array.isArray(messages)) return [];
	return messages.map((m: any) => {
		const row: Record<string, unknown> = { role: m?.role };
		row.content = truncate(contentToText(m?.content));
		if (Array.isArray(m?.toolCalls) && m.toolCalls.length) {
			row.tool_calls = m.toolCalls.map((tc: any) => ({
				id: tc?.toolCallId ?? tc?.id,
				name: tc?.name ?? tc?.function?.name,
				arguments: truncate(tc?.arguments ?? tc?.function?.arguments),
			}));
		}
		if (m?.toolCallId) row.tool_call_id = m.toolCallId;
		if (m?.model) row.model = m.model;
		if (m?.usage) row.usage = m.usage;
		return row;
	});
}

/** Pull usage off a Pi message (pi-ai normalizes to input/output/cache tokens). */
function shapeUsage(u: unknown): Record<string, number> | null {
	if (!u || typeof u !== "object") return null;
	const src = u as Record<string, any>;
	const pick = (...keys: string[]): number | undefined => {
		for (const k of keys) {
			const v = src[k];
			if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
		}
		return undefined;
	};
	const out: Record<string, number> = {};
	const put = (k: string, v: number | undefined) => {
		if (v !== undefined) out[k] = v;
	};
	const input = pick("input", "inputTokens", "input_tokens", "promptTokens");
	const cacheRead = pick("cacheRead", "cacheReadTokens", "cache_read_input_tokens");
	const cacheWrite = pick("cacheWrite", "cacheWriteTokens", "cache_creation_input_tokens");

	// pi-ai's `input` EXCLUDES the cache — every provider normalisation
	// subtracts it (`input = promptTokens - cacheRead - cacheWrite` in
	// openai-completions.js) and pi-ai's own total is
	// `input + output + cacheRead + cacheWrite`. The shared trace schema's
	// `input_tokens` means the opposite: hermes writes OpenAI's
	// `prompt_tokens` through untouched, cache included.
	//
	// Mapping pi-ai's exclusive `input` straight onto `input_tokens` is what
	// made `cache_read / input` exceed 100% — 7.7% of records on this machine
	// read >100%, and the aggregate hit rate came out at 2703%. Report the
	// TOTAL input so the field means the same thing whichever adapter wrote it
	// (this also makes `tok in+out` equal pi-ai's `totalTokens`).
	put("input_tokens", input === undefined ? undefined : input + (cacheRead ?? 0) + (cacheWrite ?? 0));
	put("output_tokens", pick("output", "outputTokens", "output_tokens", "completionTokens"));
	put("cache_read_tokens", cacheRead);
	put("cache_write_tokens", cacheWrite);
	put("reasoning_tokens", pick("reasoningTokens", "reasoning_tokens"));
	return Object.keys(out).length ? out : null;
}

/**
 * Session identity for every record. Pi does NOT set PI_SESSION_FILE in its
 * own process (live-verified: env unset at session_start) — it only injects
 * it into the bash tool's child env — so the reliable source is the runner
 * context: createContext() exposes `sessionManager` (runner.js), whose
 * getSessionFile()/getSessionId() return the session this run is writing.
 * env stays first for tests and for any future Pi that exports it.
 */
function pickSessionId(ctx: SessionContext): string | undefined {
	if (process.env.PI_SESSION_FILE) return process.env.PI_SESSION_FILE;
	const sm = ctx?.sessionManager;
	try {
		if (typeof sm?.getSessionFile === "function") {
			const f = sm.getSessionFile();
			if (f) return String(f);
		}
	} catch {
		/* fall through */
	}
	try {
		if (typeof sm?.getSessionId === "function") {
			const i = sm.getSessionId();
			if (i) return String(i);
		}
	} catch {
		/* fall through */
	}
	return undefined;
}

export default function agentTrace(pi: ExtensionHost) {
	let sessionFile: string | undefined = process.env.PI_SESSION_FILE || undefined;
	let model: string | undefined;
	let provider: string | undefined;
	let apiMode: string | undefined;

	pi.on("session_start", (event, ctx) => {
		sessionFile = pickSessionId(ctx);
		emit({
			event: "session_start",
			session_id: sessionFile,
			cwd: ctx?.cwd,
			mode: ctx?.mode,
			note: `reason=${event?.reason ?? "startup"}`,
		});
	});

	pi.on("turn_start", (_event, ctx) => {
		const m = ctx?.model;
		if (m) {
			model = typeof m === "string" ? m : m.id;
			if (typeof m !== "string") {
				provider = m.provider ?? provider;
				apiMode = m.api ?? apiMode;
			}
		}
	});

	// The real request. `before_provider_request` carries the exact outbound
	// payload for the active api (openai-completions | anthropic-messages | ...).
	pi.on("before_provider_request", (event, ctx) => {
		const payload: any = event?.payload;
		const meta = captureMode() === "metadata";
		// Anthropic keeps the system prompt in `system`; OpenAI keeps it as a
		// message with role=system; Responses uses `instructions`.
		const messages = Array.isArray(payload?.messages)
			? payload.messages
			: Array.isArray(payload?.input)
				? payload.input
				: Array.isArray(payload)
					? payload
					: [];
		const systemPrompt =
			typeof payload?.system === "string"
				? payload.system
				: typeof payload?.instructions === "string"
					? payload.instructions
					: null;
		emit({
			event: "llm_request",
			session_id: sessionFile,
			model,
			provider,
			api_mode: apiMode ?? payload?.api,
			cwd: ctx?.cwd,
			mode: ctx?.mode,
			request: {
				messages: meta ? [] : shapeMessages(messages),
				system_prompt: meta ? null : truncate(systemPrompt),
				instructions: typeof payload?.instructions === "string" && !meta ? truncate(payload.instructions) : null,
				tools: meta ? null : truncate(payload?.tools ?? payload?.functions),
				tool_count: Array.isArray(payload?.tools ?? payload?.functions)
					? (payload.tools ?? payload.functions).length
					: null,
				message_count: messages.length || null,
			},
		});
	});

	pi.on("after_provider_response", (event) => {
		emit({
			event: "note",
			session_id: sessionFile,
			model,
			provider,
			note: `provider_response status=${event?.status ?? "?"}`,
		});
	});

	// The real response. `message_end` fires once the assistant message is final,
	// after all streamed deltas are assembled.
	pi.on("message_end", (event, ctx) => {
		const msg: any = event?.message;
		if (!msg) return;
		const role = msg.role;
		if (role === "user") {
			emit({
				event: "user_prompt",
				session_id: sessionFile,
				model,
				cwd: ctx?.cwd,
				request: { messages: shapeMessages([msg]) },
			});
			return;
		}
		if (role !== "assistant") return;

		const text = contentToText(msg.content);
		const toolCalls = Array.isArray(msg.toolCalls) ? msg.toolCalls : [];
		emit({
			event: "llm_response",
			session_id: sessionFile,
			model,
			provider,
			api_mode: apiMode,
			duration_ms: typeof msg.durationMs === "number" ? Math.trunc(msg.durationMs) : undefined,
			response: {
				content: captureMode() === "metadata" ? null : truncate(text),
				reasoning: captureMode() === "metadata" ? null : undefined,
				tool_calls: toolCalls.map((tc: any) => ({
					id: tc?.toolCallId ?? tc?.id,
					name: tc?.name ?? tc?.function?.name,
					arguments: truncate(tc?.arguments ?? tc?.function?.arguments),
				})),
				finish_reason: msg.stopReason ?? msg.finishReason ?? null,
				usage: shapeUsage(msg.usage),
			},
		});
	});

	// Execution, not just the decision: llm_response carries WHAT the model
	// chose (the ↳ decide line); this marks the ⚙ call itself so the panel
	// can pair choice → execution → result. Payload per Pi 0.87.1:
	// { type, toolCallId, toolName, args }.
	pi.on("tool_execution_start", (event, ctx) => {
		emit({
			event: "tool_call",
			session_id: sessionFile,
			model,
			cwd: ctx?.cwd,
			tool: {
				name: event?.toolName,
				call_id: event?.toolCallId,
				args: truncate(event?.args),
			},
		});
	});

	pi.on("tool_execution_end", (event, ctx) => {
		emit({
			event: "tool_result",
			session_id: sessionFile,
			model,
			cwd: ctx?.cwd,
			tool: {
				name: event?.toolName,
				call_id: event?.toolCallId,
				status: event?.isError ? "error" : "ok",
			},
			duration_ms: typeof event?.durationMs === "number" ? Math.trunc(event.durationMs) : undefined,
		});
	});

	pi.on("agent_end", (_event, ctx) => {
		emit({
			event: "session_end",
			session_id: sessionFile,
			model,
			cwd: ctx?.cwd,
			note: "agent_end",
		});
	});

	// `/agenttrace path` — show where records are being written.
	pi.registerCommand("agenttrace", {
		description: "Show the agent-trace output directory for this Pi session",
		handler: async (_args: string, ctx) => {
			ctx.ui.notify(`agent-trace: ${traceDir()}${sessionFile ? `  session=${sessionFile}` : ""}`, "info");
		},
	});
}
