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
 *   turn_start             -> turnIndex, the id every record in a turn carries
 *   before_provider_request -> the outbound payload (full messages + tools)
 *   after_provider_response -> HTTP status, before the stream is consumed
 *   message_end            -> finalized assistant message incl. tool calls
 *   tool_execution_end     -> tool result
 *   turn_end / agent_end   -> turn boundaries
 *
 * `turn_id` and `request_id` are load-bearing and were MISSING until the v2
 * pipeline exposed it. Pi's own events carry neither: `turn_start` has an
 * index, `before_provider_request` has only a payload, and the response id
 * (`msg.responseId`) only exists AFTER the call. Without a request_id the
 * reader could not pair a pi `llm_request` with its `llm_response` at all —
 * `agenttrace show <request_id>` silently found nothing for pi, and the v2
 * converter minted two unrelated spans (one unclosed start, one orphan end),
 * which the conformance kit rejected. The adapter therefore synthesises a
 * request id per provider call and threads the turn index through everything,
 * which also splits a session into per-turn traces instead of one blob.
 *
 * Event ORDER (measured with a probe extension, Pi 1.3.0) — the pairing below
 * depends on it:
 *   session_start, agent_start, turn_start(turnIndex=0), message_end(system),
 *   message_end(user), before_provider_request, after_provider_response,
 *   message_end(assistant), tool_execution_start/end, message_end(toolResult),
 *   turn_end, turn_start(1), ...
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
	/** tool_execution_end — the tool's return value (see toolResultText). */
	result?: unknown;
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

/**
 * The tool's output as displayable text.
 *
 * `tool_execution_end.result` is normally `{content:[{type:"text",text}], …}`
 * (measured on Pi 1.3.0), so `contentToText` covers the common case; the
 * fallbacks handle a tool that returns a bare string or `{output}` payload.
 * Until this existed the adapter stored a tool result with NO body at all —
 * the panel showed `⚙ bash ok` and nothing about what the command printed,
 * which is the single most useful thing in a coding trace.
 */
function toolResultText(result: unknown): string {
	if (result == null) return "";
	if (typeof result === "string") return result;
	const obj = result as any;
	if (Array.isArray(obj.content)) {
		const text = contentToText(obj.content);
		if (text) return text;
	}
	if (typeof obj.output === "string") return obj.output;
	if (typeof obj.text === "string") return obj.text;
	const text = contentToText(obj);
	return text === "[object Object]" ? "" : text;
}

/** A shell tool's exit code when the result carries one (bash sets it). */
function toolExitCode(result: unknown): number | undefined {
	const sc = (result as any)?.structuredContent;
	const code = sc?.exit_code ?? (result as any)?.exit_code;
	return typeof code === "number" ? Math.trunc(code) : undefined;
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

	// Turn/request identity. `turnIndex` is Pi's own and starts at 0 for the
	// first assistant turn; `requestSeq` counts provider calls so a retry (or a
	// context-overflow re-request) inside one turn gets a distinct id instead
	// of overwriting the previous one's pairing.
	let turnIndex: number | undefined;
	/**
	 * A turn counter that ALWAYS advances, used when Pi's `turn_start` event
	 * carries no numeric `turnIndex`. Without it two turns both fell back to
	 * `pi-0-1` — the same request id, and therefore the same llm span id, in
	 * one trace.
	 */
	let turnFallback = -1;
	let requestSeq = 0;
	/**
	 * Identifies THIS run of the process inside the session.
	 *
	 * `turnIndex` restarts at 0 in every new process, and a session can be
	 * resumed — two runs ten minutes apart both produced `pi-0-1` for the same
	 * session file, which is one span id for two different provider calls, and
	 * the conformance kit rejected the stream. Request ids therefore have to be
	 * unique within the SESSION, not within the run.
	 */
	const runId = Date.now().toString(36).slice(-5);
	/**
	 * request_id of the provider call this turn is currently on. Kept for the
	 * WHOLE turn, not cleared when the response arrives: the tool calls that
	 * response asked for execute after it, and a tool that names no request is
	 * harder to attribute than one that does. Reset at the next `turn_start`,
	 * so a retry inside a turn still overwrites it and the response pairs with
	 * the most recent attempt.
	 */
	let turnRequestId: string | undefined;
	const currentTurnId = () => (turnIndex === undefined ? undefined : String(turnIndex));
	/**
	 * A tool call's identity, scoped to the request that produced it.
	 *
	 * `toolCallId` alone is only unique within one API response: a resumed or
	 * retried turn can reuse an id, and the resulting pair of spans then shared
	 * one span id. The request id is already unique per provider call, so
	 * scoping by it is uniqueness by construction; the raw id stays readable
	 * after the separator.
	 */
	const scopedToolId = (raw?: string) =>
		raw ? `${turnRequestId ?? `turn${turnIndex ?? 0}`}:${raw}` : undefined;

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

	pi.on("turn_start", (event: any, ctx) => {
		if (typeof event?.turnIndex === "number") {
			turnIndex = event.turnIndex;
		} else {
			turnFallback += 1;
			turnIndex = turnFallback;
		}
		// `requestSeq` is NOT reset here. A turn can start more than once in a
		// run — Pi retries a turn after a context-overflow compaction — and
		// resetting the sequence gave the second attempt the same request id as
		// the first (`pi-joh84-0-1` twice), which is one span id for two
		// provider calls. Monotonic per run keeps every id unique; the turn
		// index stays in the id for readability.
		turnRequestId = undefined;
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
		requestSeq += 1;
		// Unique per session and stable enough to be grepped by eye; the
		// session is prepended because `agenttrace show` searches all files.
		const requestId = `pi-${runId}-${turnIndex ?? 0}-${requestSeq}`;
		turnRequestId = requestId;
		emit({
			event: "llm_request",
			session_id: sessionFile,
			request_id: requestId,
			turn_id: currentTurnId(),
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
		const status = event?.status;
		const requestId = turnRequestId;
		if (typeof status === "number" && status >= 400) {
			// A structured failure, not free text: this is the one HTTP-level
			// error the extension can actually observe, and it used to be a
			// `note` that no reader could filter on.
			emit({
				event: "llm_error",
				session_id: sessionFile,
				request_id: requestId,
				turn_id: currentTurnId(),
				model,
				provider,
				error: {
					type: status >= 500 ? "provider_error" : "provider_rejected",
					message: `provider responded status=${status}`,
					status_code: status,
					// 429 and 5xx are the retryable classes; 4xx otherwise is not.
					retryable: status === 429 || status >= 500,
				},
			});
			return;
		}
		emit({
			event: "note",
			session_id: sessionFile,
			request_id: requestId,
			turn_id: currentTurnId(),
			model,
			provider,
			note: `provider_response status=${status ?? "?"}`,
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
				// turn_start fires BEFORE the user message (measured), so the
				// prompt belongs to the turn that is about to run.
				turn_id: currentTurnId(),
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
			request_id: turnRequestId,
			turn_id: currentTurnId(),
			model,
			provider,
			api_mode: apiMode,
			duration_ms: typeof msg.durationMs === "number" ? Math.trunc(msg.durationMs) : undefined,
			// Pi's own provider response id is kept for cross-referencing an
			// upstream log; it is NOT the pairing key because it does not exist
			// at request time.
			note: typeof msg.responseId === "string" ? `response_id=${msg.responseId}` : undefined,
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
			request_id: turnRequestId,
			turn_id: currentTurnId(),
			model,
			cwd: ctx?.cwd,
			tool: {
				name: event?.toolName,
				// Scoped by the request that produced it. Provider tool-call ids
				// are unique within ONE API response, not within a session or a
				// turn — the same id was observed twice in one real turn, which
				// gave the v2 tree two spans with one id. The provider's raw id
				// is kept after the separator so it stays greppable.
				call_id: scopedToolId(event?.toolCallId),
				provider_call_id: event?.toolCallId,
				args: truncate(event?.args),
			},
		});
	});

	pi.on("tool_execution_end", (event, ctx) => {
		const meta = captureMode() === "metadata";
		const exitCode = toolExitCode(event?.result);
		emit({
			event: "tool_result",
			session_id: sessionFile,
			request_id: turnRequestId,
			turn_id: currentTurnId(),
			model,
			cwd: ctx?.cwd,
			tool: {
				name: event?.toolName,
				call_id: scopedToolId(event?.toolCallId),
				provider_call_id: event?.toolCallId,
				status: event?.isError ? "error" : "ok",
				exit_code: exitCode,
			},
			response: {
				content: meta ? null : truncate(toolResultText(event?.result)),
			},
			duration_ms: typeof event?.durationMs === "number" ? Math.trunc(event.durationMs) : undefined,
		});
	});

	// Turn boundary: clears the request id so a tool event that arrives after
	// turn_end (there should be none) cannot claim the previous turn's call.
	pi.on("turn_end", (event: any) => {
		turnRequestId = undefined;
		if (typeof event?.turnIndex === "number") turnIndex = event.turnIndex;
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
