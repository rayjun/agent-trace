/**
 * agent-trace — live trace metrics in the Hermes TUI dock.
 *
 * Ships with agent-trace (`hermes/tui-widgets/agenttrace.mjs`); `install.sh
 * hermes <profile>` copies it into `<HERMES_HOME>/tui-widgets/`. The TUI
 * hot-loads it: `/agenttrace` docks the card, `/agenttrace` again closes it.
 *
 * Data comes from the local agent-trace server (`agenttrace serve --daemon`,
 * loopback 127.0.0.1:8741). The server gates /api/* behind a bearer token it
 * writes to ~/.agenttrace/server.token; a widget cannot read files, so this
 * page bootstraps the token from the dashboard HTML (GET / needs no token and
 * embeds it for the browser bundle — the same bytes any local process can
 * already read). Nothing here weakens the server's gate: no credentials are
 * stored, the token is re-read from the page on each cold start.
 *
 * Failure is a phase, never a crash: with the server down the card says so and
 * tells you the command that fixes it.
 */

const DEFAULT_BASE = 'http://127.0.0.1:8741'
const POLL_MS = 2500
const SLOTS = 24
export function normalizeBase(value) {
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('expected an HTTP(S) server URL without credentials')
  }
  return url.href.replace(/\/+$/, '')
}

async function checkedFetch(url, options) {
  const response = await fetch(url, { ...options, redirect: 'error' })
  if (!response.ok) {
    const error = new Error(`server HTTP ${response.status}`)
    error.status = response.status
    throw error
  }
  return response
}

let tokenCache = { base: '', token: '' }

async function dashboardToken(base, signal) {
  if (tokenCache.base === base && tokenCache.token) return tokenCache.token
  const res = await checkedFetch(`${base}/`, { signal })
  const match = (await res.text()).match(/AGENTTRACE_TOKEN__\s*=\s*"([^"]+)"/)
  if (!match) throw new Error('dashboard token missing')
  tokenCache = { base, token: match[1] }
  return match[1]
}

export async function requestJSON(base, path, { signal } = {}) {
  base = normalizeBase(base)
  const deadline = AbortSignal.timeout(8000)
  const combined = signal ? AbortSignal.any([signal, deadline]) : deadline
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const token = await dashboardToken(base, combined)
      const response = await checkedFetch(`${base}${path}`, {
        signal: combined, headers: { Authorization: `Bearer ${token}` }
      })
      return await response.json()
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        if (tokenCache.base === base) tokenCache = { base: '', token: '' }
        if (attempt === 0) continue
      }
      throw error
    }
  }
}

function describe(error) {
  const message = error instanceof Error ? error.message : String(error)
  if (error?.name === 'TimeoutError' || error?.name === 'AbortError') {
    return { message: 'server timed out', hint: 'retrying; agenttrace doctor' }
  }
  if (/fetch failed|ECONNREFUSED|network/i.test(message)) {
    return { message: 'server unreachable', hint: 'agenttrace serve --daemon' }
  }
  return { message, hint: 'retrying; agenttrace doctor' }
}

const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0

export async function snapshot(base = DEFAULT_BASE, { signal } = {}) {
  base = normalizeBase(base)
  const deadline = AbortSignal.timeout(8000)
  const requestSignal = signal ? AbortSignal.any([signal, deadline]) : deadline
  let overview, recent
  for (let attempt = 0; attempt < 2; attempt++) {
    const attemptController = new AbortController()
    const attemptSignal = AbortSignal.any([requestSignal, attemptController.signal])
    try {
      const token = await dashboardToken(base, attemptSignal)
      const headers = { Authorization: `Bearer ${token}` }
      ;[overview, recent] = await Promise.all([
        checkedFetch(`${base}/api/overview`, { headers, signal: attemptSignal }).then(r => r.json()),
        checkedFetch(`${base}/api/traces?limit=${SLOTS}`, { headers, signal: attemptSignal }).then(r => r.json())
      ])
      break
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        if (tokenCache.base === base) tokenCache = { base: '', token: '' }
        if (attempt === 0) continue
      }
      throw error
    } finally {
      attemptController.abort()
    }
  }
  if (!overview || !finite(overview.traces) || !finite(overview.spans) ||
      !finite(overview.errors) || !finite(overview.unclosed) ||
      !finite(overview.tokens?.input) || !finite(overview.tokens?.output) ||
      !Array.isArray(recent?.traces) || recent.traces.some(t => !t || typeof t !== 'object')) {
    throw new Error('invalid server response')
  }
  const turns = recent.traces.slice(0, SLOTS).reverse()
  const durations = turns.filter(t => ['ok', 'error'].includes(t.status) && t.unclosed === 0 && finite(t.duration_ms)).map(t => t.duration_ms)
  return {
    updatedAt: Date.now(),
    totals: {
      traces: overview.traces, spans: overview.spans, errors: overview.errors,
      unclosed: overview.unclosed, inTok: overview.tokens.input, outTok: overview.tokens.output
    },
    durations, p50: median(durations), last: turns.at(-1) ?? null, sampleSize: durations.length
  }
}

function median(values) {
  const clean = values.filter(v => Number.isFinite(v) && v >= 0).sort((a, b) => a - b)

  if (!clean.length) return null

  const mid = Math.floor(clean.length / 2)

  return clean.length % 2 ? clean[mid] : Math.round((clean[mid - 1] + clean[mid]) / 2)
}

const clamp = (text, width) => text.length > width ? `${text.slice(0, Math.max(0, width - 1))}~` : text

function fmtTok(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`

  return String(n)
}

export function fmtDur(ms) {
  if (!finite(ms)) return '-'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`
}

export function startPolling(base, onPhase, { delay = POLL_MS, load = snapshot } = {}) {
  let active = true
  let timer
  let previous = null
  const controller = new AbortController()
  const pull = async () => {
    try {
      const snap = await load(base, { signal: controller.signal })
      if (active) {
        previous = snap
        onPhase({ kind: 'ready', snap })
      }
    } catch (error) {
      if (active) onPhase({ kind: 'error', snap: previous, ...describe(error) })
    } finally {
      if (active) timer = setTimeout(pull, delay)
    }
  }
  pull()
  return () => {
    active = false
    clearTimeout(timer)
    controller.abort()
  }
}

const fit = (text, width) => clamp(String(text).replace(/[^\x20-\x7e\u2581-\u2588]/g, '?'), width).padEnd(width)
const pair = (left, right, width) => left.length + right.length + 1 > width
  ? fit(`${left} ${right}`, width) : left + ' '.repeat(width - left.length - right.length) + right

export function cardRows(phase, width, sparkline, now = Date.now()) {
  if (!phase.snap) return [phase.message, phase.hint, 'scope: all indexed agents', 'retrying automatically'].map(s => fit(s ?? '', width))
  const { snap } = phase
  const { totals, last } = snap
  const stale = phase.kind === 'error'
  const title = stale ? `STALE ${Math.max(0, Math.floor((now - snap.updatedAt) / 1000))}s` : 'all agents'
  const sample = `p50/${snap.sampleSize} ${fmtDur(snap.p50)}`
  const chartWidth = Math.max(0, Math.min(22, width - sample.length - 1))
  const chart = chartWidth ? sparkline(snap.durations, chartWidth) : ''
  const tokens = width >= 32
    ? `in ${fmtTok(totals.inTok)} out ${fmtTok(totals.outTok)} spanErr ${fmtTok(totals.errors)}`
    : `i ${fmtTok(totals.inTok)} o ${fmtTok(totals.outTok)}`
  const status = !last ? 'no turns yet' : `${last.adapter_id ?? '?'} ${last.status ?? 'unknown'} ${fmtDur(last.duration_ms)}`
  return [pair(title, `${fmtTok(totals.traces)} turns`, width), pair(chart, sample, width),
    fit(tokens, width), fit(stale ? phase.message : `new ${status}`, width)]
}

export default function register(sdk) {
  if (sdk.Overlay && sdk.updateWidget) registerExplorer(sdk)
  const { Box, Dialog, React, ShimmerRows, Text, defineWidgetApp, h, sparkline } = sdk
  function Body({ base, t, width }) {
    const [phase, setPhase] = React.useState({ kind: 'loading' })
    React.useEffect(() => startPolling(base, setPhase), [base])
    if (phase.kind === 'loading') {
      return h(Box, { width, height: 4, flexDirection: 'column' },
        h(ShimmerRows, { color: t.color.muted, highlight: t.color.label,
          rows: Array.from({ length: 4 }, () => [Math.max(1, width - 2)]) }))
    }
    return h(Box, { width, height: 4, flexDirection: 'column' },
      ...cardRows(phase, width, sparkline).map((row, i) => h(Text, {
        key: i, wrap: 'truncate', color: phase.kind === 'error' ? t.color.error :
          (i === 0 ? t.color.label : i === 1 ? t.color.primary : t.color.muted)
      }, row)))
  }
  defineWidgetApp({
    id: 'agenttrace', help: 'agent-trace: all-agent totals and recent-turn latency',
    mode: 'ambient', zone: 'dock-bottom', width: 46,
    usage: `usage: /agenttrace [server-url] default ${DEFAULT_BASE}`,
    init(arg) {
      try { return { base: normalizeBase(arg.trim() || DEFAULT_BASE) } }
      catch { return null }
    },
    reduce(state) { return state },
    render({ cols, state, t }) {
      const width = Math.max(7, Math.min(46, cols - 4))
      return h(Dialog, { width }, h(Body, { base: state.base, t, width: width - 6 }))
    }
  })
}


export function explorerInit(arg = '') {
  const args = arg.trim().split(/\s+/).filter(Boolean)
  if (args.length > 2) return null
  const sessionId = args[0]?.startsWith('http') ? '' : args[0] ?? ''
  const address = args[0]?.startsWith('http') ? args[0] : args[1] ?? DEFAULT_BASE
  try {
    return { base: normalizeBase(address), page: sessionId ? 'turns' : 'sessions', sessionId,
      traceId: '', selected: 0, scroll: 0, collapsed: [], errorsOnly: false,
      loading: true, error: '', data: null, revision: 0 }
  } catch { return null }
}

export function explorerResource(state) {
  if (state.page === 'sessions') return '/api/sessions?limit=200'
  if (state.page === 'turns') return `/api/sessions/${encodeURIComponent(state.sessionId)}`
  return `/api/traces/${encodeURIComponent(state.traceId)}`
}

export async function loadExplorer(state, options) {
  const data = await requestJSON(state.base, explorerResource(state), options)
  const key = state.page === 'sessions' ? 'sessions' : state.page === 'turns' ? 'turns' : 'spans'
  if (!data || !Array.isArray(data[key]) || data[key].some(item => !item || typeof item !== 'object')) {
    throw new Error('invalid explorer response')
  }
  if (state.page === 'turns' && data.session_id !== state.sessionId) throw new Error('session identity mismatch')
  if (key === 'spans' && data.trace_id !== state.traceId) throw new Error('trace identity mismatch')
  return data
}

export function spanTree(spans, collapsed = []) {
  const byId = new Map(spans.map(span => [span.span_id, span]))
  const children = new Map()
  for (const span of spans) {
    const parent = byId.has(span.parent_span_id) && span.parent_span_id !== span.span_id ? span.parent_span_id : null
    if (!children.has(parent)) children.set(parent, [])
    children.get(parent).push(span)
  }
  const result = [], visited = new Set(), hidden = new Set(collapsed)
  const walk = roots => {
    const stack = roots.map(span => ({ span, depth: 0 })).reverse()
    while (stack.length) {
      const { span, depth } = stack.pop()
      if (visited.has(span.span_id)) continue
      visited.add(span.span_id)
      const kids = children.get(span.span_id) ?? []
      result.push({ ...span, depth, hasChildren: kids.length > 0 })
      if (hidden.has(span.span_id)) {
        const pending = [...kids]
        while (pending.length) {
          const kid = pending.pop()
          if (visited.has(kid.span_id)) continue
          visited.add(kid.span_id)
          pending.push(...(children.get(kid.span_id) ?? []))
        }
      } else {
        stack.push(...kids.map(child => ({ span: child, depth: depth + 1 })).reverse())
      }
    }
  }
  walk(children.get(null) ?? [])
  for (const span of spans) if (!visited.has(span.span_id)) walk([span])
  return result
}

export function explorerItems(state) {
  if (!state.data) return []
  if (state.page === 'sessions') return state.data.sessions
  if (state.page === 'turns') return state.data.turns
  const tree = spanTree(state.data.spans, state.collapsed)
  return state.errorsOnly ? tree.filter(s => ['error', 'cancelled', 'interrupted'].includes(s.status)) : tree
}

const navigateExplorer = (state, page, fields = {}) => ({ ...state, page, selected: 0, scroll: 0,
  collapsed: [], errorsOnly: false, data: null, error: '', loading: true, ...fields })

export function explorerReduce(state, { ch = '', key = {} }) {
  if (ch === 'q') return null
  if (key.escape || ch === 'b' || key.backspace) {
    if (state.page === 'sessions') return null
    if (state.page === 'detail') return { ...state, page: 'spans', scroll: 0 }
    return navigateExplorer(state, state.page === 'spans' ? 'turns' : 'sessions')
  }
  if (ch === 'r') return { ...state, revision: state.revision + 1, loading: true, error: '' }
  if (state.loading || state.error) return state
  const items = explorerItems(state)
  if (state.page === 'detail') {
    const delta = key.pageDown ? 10 : key.pageUp ? -10 : key.downArrow || ch === 'j' ? 1 : key.upArrow || ch === 'k' ? -1 : 0
    const lines = spanDetails(state.data.spans.find(s => s.span_id === state.spanId), state.detailWidth ?? 40)
    const maxScroll = Math.max(0, lines.length - (state.detailHeight ?? 1))
    const section = { i: '[input]', o: '[output]', x: '[error]', g: '[span]' }[ch]
    if (section) return { ...state, scroll: Math.min(maxScroll, Math.max(0, lines.indexOf(section))) }
    return delta ? { ...state, scroll: Math.max(0, Math.min(maxScroll, state.scroll + delta)) } : state
  }
  if (ch === 'e' && state.page === 'spans') return { ...state, errorsOnly: !state.errorsOnly, collapsed: [], selected: 0 }
  const delta = key.pageDown ? 10 : key.pageUp ? -10 : key.downArrow || ch === 'j' ? 1 : key.upArrow || ch === 'k' ? -1 : 0
  if (delta) return { ...state, selected: Math.max(0, Math.min(items.length - 1, state.selected + delta)) }
  const item = items[state.selected]
  if (!item) return state
  if (ch === ' ' && state.page === 'spans' && item.hasChildren) {
    const collapsed = state.collapsed.includes(item.span_id)
      ? state.collapsed.filter(id => id !== item.span_id) : [...state.collapsed, item.span_id]
    return { ...state, collapsed, selected: Math.min(state.selected, explorerItems({ ...state, collapsed }).length - 1) }
  }
  if (key.return) {
    if (state.page === 'sessions') return navigateExplorer(state, 'turns', { sessionId: item.session_id })
    if (state.page === 'turns') return navigateExplorer(state, 'spans', { traceId: item.trace_id })
    return { ...state, page: 'detail', spanId: item.span_id, scroll: 0 }
  }
  return state
}

export function terminalSafe(value) {
  return String(value).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, '')
}

const graphemes = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
const wideRanges = [
  [0x1100, 0x115f], [0x2329, 0x232a], [0x2e80, 0x3247], [0x3250, 0x4dbf],
  [0x4e00, 0xa4c6], [0xa960, 0xa97c], [0xac00, 0xd7a3], [0xf900, 0xfaff],
  [0xfe10, 0xfe19], [0xfe30, 0xfe6b], [0xff01, 0xff60], [0xffe0, 0xffe6],
  [0x1b000, 0x1b001], [0x1f200, 0x1f251], [0x20000, 0x3fffd]
]
const cells = text => {
  if (/[\p{Extended_Pictographic}\p{Regional_Indicator}\u20e3]/u.test(text)) return 2
  let width = 0
  for (const char of text) {
    if (/[\p{Mark}\p{Format}]/u.test(char)) continue
    const cp = char.codePointAt(0)
    width += cp !== 0x303f && wideRanges.some(([lo, hi]) => cp >= lo && cp <= hi) ? 2 : 1
  }
  return width
}

export function wrapExplorer(value, width) {
  width = Math.max(2, width)
  const result = []
  for (const line of terminalSafe(value).replace(/\t/g, '  ').split('\n')) {
    let row = '', used = 0
    for (const { segment } of graphemes.segment(line)) {
      const size = cells(segment)
      if (used + size > width) { result.push(row); row = ''; used = 0 }
      row += segment; used += size
    }
    result.push(row)
  }
  return result
}

export function spanDetails(span, width) {
  if (!span) return ['span not found in snapshot; press b then r']
  const output = []
  for (const [label, value] of [
    ['span', span.span_id], ['kind / name', `${span.kind} / ${span.name}`],
    ['status', span.status], ['duration', `${fmtDur(span.duration_ms)}${span.duration_suspect ? ' (suspect: resume gap)' : ''}`],
    ['model', span.model], ['tokens', span.tokens], ['input', span.input], ['output', span.output],
    ['error', span.error], ['privacy', span.privacy]
  ]) {
    output.push(...wrapExplorer(`[${label}]`, width))
    const text = value == null ? '(not captured)' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
    output.push(...wrapExplorer(text.slice(0, 100000), width))
    if (text.length > 100000) output.push('(section truncated at 100000 characters)')
  }
  return output
}

export function explorerRows(state, width, height) {
  if (state.loading) return ['loading snapshot...']
  if (state.error) return wrapExplorer(`ERROR: ${state.error}\npress r to retry, b to go back`, width)
  if (state.page === 'detail') {
    const lines = spanDetails(state.data.spans.find(s => s.span_id === state.spanId), width)
    const start = Math.min(state.scroll, Math.max(0, lines.length - height))
    return lines.slice(start, start + height)
  }
  const items = explorerItems(state)
  if (!items.length) return [state.errorsOnly ? 'No error spans in this trace.' : 'No indexed records in this scope.']
  const start = Math.max(0, state.selected - height + 1)
  return items.slice(start, start + height).map((item, i) => {
    const marker = start + i === state.selected ? '> ' : '  '
    let line
    if (state.page === 'sessions') line = `${item.adapter_id ?? '?'} ${item.session_id} (${item.trace_count} turns)`
    else if (state.page === 'turns') line = `${item.status} ${fmtDur(item.duration_ms)} ${item.model ?? ''} ${item.trace_id}`
    else line = `${'  '.repeat(Math.min(item.depth, 8))}${item.hasChildren ? state.collapsed.includes(item.span_id) ? '+ ' : '- ' : '  '}${item.kind} ${item.status} ${fmtDur(item.duration_ms)} ${item.name}`
    return wrapExplorer(marker + line, width)[0]
  })
}

function registerExplorer(sdk) {
  const { Box, Dialog, Overlay, React, Text, ShimmerRows, h, defineWidgetApp, updateWidget } = sdk
  let app
  function ExplorerBody({ state, width, height, t }) {
    const resource = explorerResource(state)
    React.useEffect(() => {
      updateWidget(app, current => ({ ...current, detailWidth: width, detailHeight: height, scroll: 0 }))
    }, [width, height])
    React.useEffect(() => {
      const controller = new AbortController()
      let active = true
      loadExplorer(state, { signal: controller.signal }).then(data => {
        if (!active) return
        updateWidget(app, current => {
          if (explorerResource(current) !== resource || current.revision !== state.revision) return current
          return { ...current, data, loading: false, error: '',
            selected: Math.max(0, Math.min(current.selected, explorerItems({ ...current, data }).length - 1)) }
        })
      }).catch(error => {
        if (active) updateWidget(app, current => explorerResource(current) === resource && current.revision === state.revision
          ? { ...current, loading: false, error: describe(error).message } : current)
      })
      return () => { active = false; controller.abort() }
    }, [state.base, resource, state.revision])
    if (state.loading) return h(Box, { width, height }, h(ShimmerRows, {
      color: t.color.muted, highlight: t.color.label, rows: Array.from({ length: Math.min(4, height) }, () => [Math.max(1, width - 2)])
    }))
    return h(Box, { width, height, flexDirection: 'column' },
      ...explorerRows(state, width, height).map((row, i) => h(Text, {
        key: i, wrap: 'truncate', color: state.error ? t.color.error : row.startsWith('>') ? t.color.primary : t.color.text
      }, row)))
  }
  app = defineWidgetApp({
    id: 'tracex', help: 'browse sessions, turn trees, and LLM/tool inputs, outputs, errors', mode: 'modal',
    usage: 'usage: /tracex [session-id] [http://127.0.0.1:8741]',
    init: explorerInit, reduce: explorerReduce,
    render({ cols, rows, state, t }) {
      const width = Math.max(12, Math.min(104, cols - 4))
      const height = Math.max(2, Math.min(24, rows - 12))
      const count = state.data ? explorerItems(state).length : 0
      const scope = state.page === 'sessions' ? 'sessions (up to 200 loaded)' : `session ${state.sessionId}`
      return h(Overlay, {}, h(Dialog, { width },
        h(Text, { color: t.color.label }, wrapExplorer(`agent-trace / ${state.page} ${state.errorsOnly ? '[errors]' : ''}`, width - 6)[0]),
        h(Text, { color: t.color.muted }, wrapExplorer(scope, width - 6)[0]),
        h(ExplorerBody, { state, width: width - 6, height, t }),
        h(Text, { color: t.color.muted }, wrapExplorer(width < 46 ? 'j/k Enter b q' : `${state.page === 'detail' ? 'j/k scroll' : `j/k select ${count ? state.selected + 1 : 0}/${count}`} | Enter open | b back | q close`, width - 6)[0]),
        h(Text, { color: t.color.muted }, wrapExplorer(state.page === 'detail' ? 'i input | o output | x error | r refresh' : width < 46 ? 'r refresh e errors' : 'r refresh | Space fold | e errors | snapshot (not live)', width - 6)[0])
      ))
    }
  })
}
