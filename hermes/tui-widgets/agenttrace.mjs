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

export async function requestGroup(base, paths, { signal } = {}) {
  base = normalizeBase(base)
  const deadline = AbortSignal.timeout(8000)
  const combined = signal ? AbortSignal.any([signal, deadline]) : deadline
  for (let attempt = 0; attempt < 2; attempt++) {
    const controller = new AbortController()
    const attemptSignal = AbortSignal.any([combined, controller.signal])
    try {
      const token = await dashboardToken(base, attemptSignal)
      return await Promise.all(paths.map(async path => {
        const response = await checkedFetch(`${base}${path}`, {
          signal: attemptSignal, headers: { Authorization: `Bearer ${token}` }
        })
        return response.json()
      }))
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        if (tokenCache.base === base) tokenCache = { base: '', token: '' }
        if (attempt === 0) continue
      }
      throw error
    } finally { controller.abort() }
  }
}

export async function requestJSON(base, path, options) {
  return (await requestGroup(base, [path], options))[0]
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

const agentQuery = adapterId => adapterId ? `?${new URLSearchParams({ agent: adapterId })}` : ''
const adapterMatches = (data, adapterId) => !adapterId || data?.adapter_id === adapterId

export async function snapshot(base = DEFAULT_BASE, { signal, sessionId = '', adapterId = '' } = {}) {
  base = normalizeBase(base)
  const health = await requestJSON(base, '/api/health', { signal })
  if (typeof health?.building !== 'boolean') throw new Error('invalid server health response')
  if (health.building) return { updatedAt: Date.now(), sessionId, adapterId, health,
    totals: { traces: null, spans: null, errors: null, unclosed: null, inTok: null, outTok: null },
    durations: [], p50: null, last: null, sampleSize: 0 }
  const paths = [sessionId ? `/api/sessions/${encodeURIComponent(sessionId)}/metrics${agentQuery(adapterId)}` : '/api/overview']
  if (!sessionId) paths.push(`/api/traces?limit=${SLOTS}`)
  const [response, globalRecent] = await requestGroup(base, paths, { signal })
  const recent = sessionId ? { traces: response.recent_traces } : globalRecent
  if (!response || !finite(response.traces) || !finite(response.spans) ||
      !finite(response.errors) || !finite(response.unclosed) ||
      !(response.tokens?.input === null || finite(response.tokens?.input)) || !(response.tokens?.output === null || finite(response.tokens?.output)) ||
      !Array.isArray(recent?.traces) || recent.traces.some(t => !t || typeof t !== 'object') ||
      typeof health?.building !== 'boolean' || (sessionId && (response.session_id !== sessionId || !adapterMatches(response, adapterId)))) {
    throw new Error('invalid server response')
  }
  const turns = recent.traces.slice(0, SLOTS).reverse()
  const durations = turns.filter(t => ['ok', 'error'].includes(t.status) && t.unclosed === 0 && finite(t.duration_ms)).map(t => t.duration_ms)
  return {
    updatedAt: Date.now(), sessionId, adapterId: adapterId || response.adapter_id || '', health,
    totals: { traces: response.traces, spans: response.spans, errors: response.errors,
      unclosed: response.unclosed, inTok: response.tokens.input, outTok: response.tokens.output },
    durations, p50: median(durations), last: turns.at(-1) ?? null, sampleSize: durations.length
  }
}

const scopeListeners = new Set()
let pinnedScope = { base: DEFAULT_BASE, sessionId: '', adapterId: '' }
export function pinSession(base, sessionId = '', adapterId = '') {
  pinnedScope = { base: normalizeBase(base), sessionId, adapterId: sessionId ? adapterId : '' }
  for (const listener of scopeListeners) listener(pinnedScope)
}
export const currentScope = () => ({ ...pinnedScope })

function median(values) {
  const clean = values.filter(v => Number.isFinite(v) && v >= 0).sort((a, b) => a - b)

  if (!clean.length) return null

  const mid = Math.floor(clean.length / 2)

  return clean.length % 2 ? clean[mid] : Math.round((clean[mid - 1] + clean[mid]) / 2)
}

const clamp = (text, width) => text.length > width ? `${text.slice(0, Math.max(0, width - 1))}~` : text

function fmtTok(n) {
  if (!finite(n)) return '-'
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
  const title = stale ? `STALE ${Math.max(0, Math.floor((now - snap.updatedAt) / 1000))}s`
    : snap.health?.building ? 'indexing' : snap.sessionId ? `session ${snap.sessionId.slice(0, 12)}` : 'all agents'
  const sample = `p50/${snap.sampleSize} ${fmtDur(snap.p50)}`
  const chartWidth = Math.max(0, Math.min(22, width - sample.length - 1))
  const chart = chartWidth ? sparkline(snap.durations, chartWidth) : ''
  const tokens = width >= 32
    ? `in ${fmtTok(totals.inTok)} out ${fmtTok(totals.outTok)} spanErr ${fmtTok(totals.errors)}`
    : `i ${fmtTok(totals.inTok)} o ${fmtTok(totals.outTok)}`
  const problems = snap.health?.last_scan?.problems
  const problemCount = Array.isArray(problems) ? problems.length : finite(problems) ? problems : 0
  const status = !last ? 'no turns yet' : `${last.adapter_id ?? '?'} ${last.status ?? 'unknown'} ${fmtDur(last.duration_ms)}`
  return [pair(title, `${fmtTok(totals.traces)} turns`, width), pair(chart, sample, width),
    fit(tokens, width), fit(stale ? phase.message : problemCount
      ? `ingest: ${problemCount} problems` : snap.health?.building ? 'indexing trace directories' : `new ${status}`, width)]
}

export default function register(sdk) {
  let dockApp, explorerApp
  if (sdk.Overlay && sdk.updateWidget) explorerApp = registerExplorer(sdk, (base, sessionId, adapterId) => {
    pinSession(base, sessionId, adapterId)
    if (sdk.openWidget) sdk.openWidget(dockApp, { base })
  })
  const { Box, Dialog, React, ShimmerRows, Text, defineWidgetApp, h, sparkline } = sdk
  function Body({ base, t, width }) {
    const [phase, setPhase] = React.useState({ kind: 'loading' })
    const [scope, setScope] = React.useState(currentScope)
    React.useEffect(() => { scopeListeners.add(setScope); return () => scopeListeners.delete(setScope) }, [])
    const sessionId = scope.base === base ? scope.sessionId : ''
    const adapterId = scope.base === base ? scope.adapterId : ''
    React.useEffect(() => {
      setPhase({ kind: 'loading' })
      return startPolling(base, setPhase, { load: (address, options) => snapshot(address, { ...options, sessionId, adapterId }) })
    }, [base, sessionId, adapterId])
    if (phase.kind === 'loading') {
      return h(Box, { width, height: 4, flexDirection: 'column' },
        h(ShimmerRows, { color: t.color.muted, highlight: t.color.label,
          rows: Array.from({ length: 4 }, () => [Math.max(1, width - 2)]) }))
    }
    return h(Box, { width, height: 4, flexDirection: 'column', onClick: () => {
      if (explorerApp && sdk.openWidget) sdk.openWidget(explorerApp, { ...explorerInit(base),
        page: sessionId ? 'turns' : 'sessions', sessionId, adapterId })
    } },
      ...cardRows(phase, width, sparkline).map((row, i) => h(Text, {
        key: i, wrap: 'truncate', color: phase.kind === 'error' ? t.color.error :
          (i === 0 ? t.color.label : i === 1 ? t.color.primary : t.color.muted)
      }, row)))
  }
  dockApp = defineWidgetApp({
    id: 'agenttrace', help: 'agent-trace: all-agent totals and recent-turn latency',
    mode: 'ambient', zone: 'dock-bottom', width: 46,
    usage: `usage: /agenttrace [global|session ID] [server-url] [--agent ADAPTER] default ${DEFAULT_BASE}`,
    init(arg) {
      const args = arg.trim().split(/\s+/).filter(Boolean)
      try {
        if (args[0] === 'global') {
          if (args.length > 2) return null
          const base = normalizeBase(args[1] || pinnedScope.base)
          pinSession(base, ''); return { base }
        }
        if (args[0] === 'session') {
          const direct = explorerInit(args.slice(1).join(' '))
          if (!direct?.sessionId) return null
          const { base, sessionId, adapterId } = direct
          pinSession(base, sessionId, adapterId); return { base }
        }
        if (args.length > 1) return null
        const base = normalizeBase(args[0] || pinnedScope.base)
        if (args.length && base !== pinnedScope.base) pinSession(base, '')
        return { base }
      } catch { return null }
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
  let adapterId = ''
  const option = args.indexOf('--agent')
  if (option >= 0) {
    if (option !== args.length - 2 || !args[option + 1] || args[option + 1] === '--agent') return null
    adapterId = args[option + 1]
    args.splice(option, 2)
  }
  if (args.length > 2) return null
  const sessionId = args[0]?.startsWith('http') ? '' : args[0] ?? ''
  if (adapterId && !sessionId) return null
  const address = args[0]?.startsWith('http') ? args[0] : args[1] ?? DEFAULT_BASE
  try {
    return { base: normalizeBase(address), page: sessionId ? 'turns' : 'sessions', sessionId, adapterId,
      traceId: '', selected: 0, scroll: 0, collapsed: [], errorsOnly: false,
      loading: true, error: '', data: null, revision: 0, query: '', searching: false,
      searchDraft: '', history: [], notice: '', live: false, offset: 0, pageSize: 50,
      filters: { search: '', agent: '', model: '', since: '' }, searchTarget: 'local' }
  } catch { return null }
}

export function explorerResource(state) {
  const session = encodeURIComponent(state.sessionId)
  if (state.page === 'sessions') {
    const args = new URLSearchParams({ limit: String(state.pageSize ?? 50), offset: String(state.offset ?? 0) })
    for (const [name, value] of Object.entries(state.filters ?? {})) if (value) args.set(name, value)
    return `/api/sessions?${args}`
  }
  if (state.page === 'turns') return `/api/sessions/${session}${agentQuery(state.adapterId)}`
  if (state.page === 'metrics') return `/api/sessions/${session}/metrics${agentQuery(state.adapterId)}`
  const trace = encodeURIComponent(state.traceId)
  const args = new URLSearchParams()
  if (state.page !== 'detail') args.set('summary', '1')
  if (state.adapterId) args.set('agent', state.adapterId)
  if (state.sessionId) args.set('session', state.sessionId)
  const query = args.size ? `?${args}` : ''
  if (state.page === 'detail') return `/api/traces/${trace}/spans/${encodeURIComponent(state.spanId)}${query}`
  return `/api/traces/${trace}${query}`
}

export async function loadExplorer(state, options) {
  const data = await requestJSON(state.base, explorerResource(state), options)
  if (state.page !== 'sessions' && !adapterMatches(data, state.adapterId)) throw new Error('adapter identity mismatch')
  if (state.page === 'detail') {
    if (!data || data.span_id !== state.spanId || data.trace_id !== state.traceId || data.session_id !== state.sessionId) {
      throw new Error('span identity mismatch')
    }
    return { trace_id: data.trace_id, session_id: data.session_id, adapter_id: data.adapter_id, spans: [data] }
  }
  if (state.page === 'metrics') {
    if (!data || data.session_id !== state.sessionId || !finite(data.traces) || !Array.isArray(data.slowest)) {
      throw new Error('invalid metrics response')
    }
    return data
  }
  const name = state.page === 'sessions' ? 'sessions' : state.page === 'turns' ? 'turns' : 'spans'
  if (!data || !Array.isArray(data[name]) || data[name].some(item => !item || typeof item !== 'object')) {
    throw new Error('invalid explorer response')
  }
  if (state.page === 'turns' && data.session_id !== state.sessionId) throw new Error('session identity mismatch')
  if (name === 'spans' && (data.trace_id !== state.traceId || data.session_id !== state.sessionId)) throw new Error('trace identity mismatch')
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
  if (!state.data || state.page === 'metrics') return []
  let items
  if (state.page === 'sessions') items = state.data.sessions
  else if (state.page === 'turns') items = state.data.turns
  else {
    items = spanTree(state.data.spans, state.query && state.page !== 'detail' ? [] : state.collapsed)
    if (state.errorsOnly) items = items.filter(s => ['error', 'cancelled', 'interrupted'].includes(s.status))
  }
  const query = state.page === 'detail' ? '' : (state.query ?? '').toLocaleLowerCase()
  return query ? items.filter(item => [item.session_id, item.trace_id, item.span_id, item.adapter_id,
    item.name, item.model, item.status, item.kind, item.cwd].some(value =>
    value != null && terminalSafe(value).toLocaleLowerCase().includes(query))) : items
}

const recordKey = item => item?.span_id ?? item?.trace_id ?? (item?.session_id == null ? undefined : JSON.stringify([item.adapter_id ?? '', item.session_id]))
const bookmark = state => ({ page: state.page, sessionId: state.sessionId, adapterId: state.adapterId, traceId: state.traceId,
  selected: state.selected, anchor: recordKey(explorerItems(state)[state.selected]), scroll: state.scroll,
  spanId: state.spanId, query: state.query ?? '', collapsed: state.collapsed,
  errorsOnly: state.errorsOnly, offset: state.offset, filters: state.filters, updatedAt: state.updatedAt })

const navigateExplorer = (state, page, fields = {}) => ({ ...state, page, selected: 0, scroll: 0,
  query: '', searchDraft: '', searching: false, notice: '', anchor: undefined, matchRow: null, showExport: false, showHelp: false,
  collapsed: [], errorsOnly: false, data: null, error: '', loading: true, updatedAt: null,
  history: [...(state.history ?? []), bookmark(state)], ...fields })

export function applyExplorerData(state, data) {
  if (state.page === 'metrics') return { ...state, data, loading: false, error: '', notice: '', updatedAt: Date.now() }
  if (state.page === 'detail') return { ...state, data, loading: false, error: '', updatedAt: Date.now(), anchor: undefined,
    notice: data.spans.some(span => span.span_id === state.spanId) ? '' : 'displayed span missing; refresh or go back' }
  const anchor = state.anchor ?? recordKey(explorerItems(state)[state.selected])
  const items = explorerItems({ ...state, data })
  const found = anchor == null ? -1 : items.findIndex(item => recordKey(item) === anchor)
  return { ...state, data, loading: false, error: '', updatedAt: Date.now(), anchor: undefined,
    selected: found >= 0 ? found : anchor != null ? 0 : Math.max(0, Math.min(state.selected, items.length - 1)),
    notice: anchor != null && found < 0 ? 'previous record missing; selection reset' : '' }
}

const detailLines = (state, width = state.detailWidth ?? 40) =>
  spanDetails(state.data?.spans.find(s => s.span_id === state.spanId), width)

export function detailMatches(state, width = state.detailWidth ?? 40) {
  const query = state.query ?? ''
  if (!query || !state.data) return []
  const span = state.data.spans.find(s => s.span_id === state.spanId)
  if (!span) return []
  if (!matchesCache.has(span)) matchesCache.set(span, new Map())
  const cache = matchesCache.get(span)
  const cacheKey = `${width}\u0000${query}`
  if (cache.has(cacheKey)) return cache.get(cacheKey)
  const remember = hits => {
    if (cache.size >= 16) cache.delete(cache.keys().next().value)
    cache.set(cacheKey, hits); return hits
  }
  const logical = spanLogicalLines(span)
  const escaped = Array.from(query).map(char => '\\^$.*+?()[]{}|'.includes(char) ? '\\' + char : char).join('')
  const expression = new RegExp(escaped, 'giu')
  const found = new Set()
  let base = 0
  for (const line of logical) {
    const rows = wrapExplorer(line, width)
    let row = 0, end = rows[0]?.length ?? 0
    for (const match of line.matchAll(expression)) {
      while (row < rows.length - 1 && match.index >= end) end += rows[++row].length
      found.add(base + row)
      if (found.size >= 1000) return remember([...found])
    }
    base += rows.length
  }
  return remember([...found])
}

function moveDetailMatch(state, backwards = false, first = false) {
  const hits = detailMatches(state)
  if (!hits.length) return { ...state, notice: 'no matching lines in captured detail' }
  const position = state.matchRow ?? state.scroll
  const target = first ? hits[0] : backwards
    ? [...hits].reverse().find(row => row < position) ?? hits.at(-1)
    : hits.find(row => row > position) ?? hits[0]
  const maximum = Math.max(0, detailLines(state).length - (state.detailHeight ?? 1))
  return { ...state, scroll: Math.min(target, maximum), matchRow: target, notice: '' }
}

// Date-only means UTC midnight. Datetimes must specify an offset: never let
// Date.parse guess the host timezone or silently roll an impossible day forward.
export function normalizeSince(value) {
  if (!value) return ''
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:\d{2}))?$/.exec(value)
  if (!match) throw new Error('invalid since date/time')
  const [, year, month, day, hour, minute, second, , zone] = match
  const calendar = new Date(0)
  calendar.setUTCFullYear(Number(year), Number(month) - 1, Number(day))
  if (Number(year) < 1 || calendar.getUTCFullYear() !== Number(year) || calendar.getUTCMonth() !== Number(month) - 1 || calendar.getUTCDate() !== Number(day) ||
      (hour != null && (Number(hour) > 23 || Number(minute) > 59 || Number(second ?? 0) > 59)) ||
      (zone && zone !== 'Z' && (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4)) > 59))) throw new Error('invalid since date/time')
  const instant = Date.parse(zone ? value : `${value}T00:00:00Z`)
  if (!Number.isFinite(instant)) throw new Error('invalid since date/time')
  const utc = new Date(instant).toISOString()
  return match[7]?.length > 3 ? utc.replace(/\.\d{3}Z$/, `.${match[7].padEnd(6, '0')}Z`) : utc
}

export function explorerReduce(state, { ch = '', key = {} }) {
  if (state.searching) {
    if (key.escape) return { ...state, searching: false, searchDraft: '' }
    if (key.return) {
      if (state.searchTarget && state.searchTarget !== 'local') {
        let value = state.searchDraft
        if (state.searchTarget === 'since' && value) {
          try { value = normalizeSince(value) }
          catch { return { ...state, notice: 'Invalid since date/time: use YYYY-MM-DD or a valid datetime with Z/offset' } }
        }
        return { ...state, searching: false, filters: { ...state.filters, [state.searchTarget]: value },
          offset: 0, selected: 0, query: '', data: null, anchor: undefined, loading: true,
          revision: state.revision + 1, notice: '' }
      }
      const next = { ...state, searching: false, query: state.searchDraft, selected: 0, scroll: 0, matchRow: null, notice: '' }
      return next.page === 'detail' && next.query ? moveDetailMatch(next, false, true) : next
    }
    if (key.backspace || key.delete) {
      const parts = [...graphemes.segment(state.searchDraft)]
      return { ...state, searchDraft: parts.slice(0, -1).map(part => part.segment).join('') }
    }
    if (key.ctrl && ch.toLowerCase() === 'u') return { ...state, searchDraft: '' }
    if (key.ctrl || key.meta || key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) return state
    const text = terminalSafe(ch).replace(/[\n\t]/g, ' ')
    return text ? { ...state, searchDraft: Array.from(state.searchDraft + text).slice(0, 256).join('') } : state
  }
  if (ch.startsWith('/') && !key.ctrl && !key.meta) return { ...state, searching: true, searchTarget: 'local',
    searchDraft: ch.length === 1 ? state.query ?? '' : Array.from(terminalSafe(ch.slice(1)).replace(/[\n\t]/g, ' ')).slice(0, 256).join('') }
  if (ch === 'q') return null
  if (ch === '?') return { ...state, showHelp: !state.showHelp }
  if (state.showHelp) return key.escape || ch === 'b' ? { ...state, showHelp: false } : state
  if (ch === 'l') return { ...state, live: !state.live }
  if (state.page === 'sessions' && ['f', 'a', 'M', 't'].includes(ch[0]) && !key.ctrl && !key.meta) {
    const target = { f: 'search', a: 'agent', M: 'model', t: 'since' }[ch[0]]
    return { ...state, searching: true, searchTarget: target,
      searchDraft: ch.length > 1 ? Array.from(terminalSafe(ch.slice(1)).replace(/[\n\t]/g, ' ')).slice(0, 256).join('') : state.filters?.[target] ?? '', notice: '' }
  }
  if (state.showExport && (key.escape || ch === 'b')) return { ...state, showExport: false, scroll: 0 }
  if (key.escape || ch === 'b' || key.backspace) {
    const history = state.history ?? []
    if (history.length) {
      const previous = history.at(-1)
      const same = explorerResource(previous) === explorerResource(state)
      const restored = { ...state, ...previous, history: history.slice(0, -1), searching: false,
        notice: '', error: '', anchor: previous.anchor,
        data: same ? state.data : null, loading: same ? state.loading : true }
      return same && state.data ? applyExplorerData(restored, state.data) : restored
    }
    if (state.page === 'sessions') return null
    return { ...navigateExplorer(state, state.page === 'detail' ? 'spans' : state.page === 'spans' ? 'turns' : 'sessions'), history: [] }
  }
  if (ch === 'r') return { ...state, revision: state.revision + 1, loading: true, error: '' }
  if ((state.loading || state.error) && !state.data) return state
  if (state.page === 'sessions' && (key.leftArrow || key.rightArrow)) {
    const offset = Math.max(0, (state.offset ?? 0) + (key.leftArrow ? -1 : 1) * (state.pageSize ?? 50))
    if (offset === (state.offset ?? 0) || (key.rightArrow && !state.data?.has_more)) return state
    return { ...state, offset, data: null, selected: 0, anchor: undefined, loading: true, revision: state.revision + 1, notice: '' }
  }
  if (ch === 'v' && state.page === 'detail') return { ...state, showExport: !state.showExport, scroll: 0 }
  if (ch === 'm' && state.page !== 'metrics') {
    const item = state.page === 'sessions' ? explorerItems(state)[state.selected] : null
    const sessionId = item ? item.session_id : state.sessionId
    const adapterId = state.page === 'sessions' ? item?.adapter_id ?? '' : state.adapterId
    return sessionId ? navigateExplorer(state, 'metrics', { sessionId, adapterId }) : state
  }
  if (state.page === 'metrics' || state.showExport) {
    const delta = key.pageDown ? 10 : key.pageUp ? -10 : key.downArrow || ch === 'j' ? 1 : key.upArrow || ch === 'k' ? -1 : 0
    const lines = state.showExport ? exportRows(state, state.detailWidth ?? 40) : metricsRows(state.data, state.detailWidth ?? 40)
    return delta ? { ...state, scroll: Math.max(0, Math.min(Math.max(0, lines.length - (state.detailHeight ?? 1)), state.scroll + delta)) } : state
  }
  const items = explorerItems(state)
  if (state.page === 'detail') {
    const delta = key.pageDown ? 10 : key.pageUp ? -10 : key.downArrow || ch === 'j' ? 1 : key.upArrow || ch === 'k' ? -1 : 0
    if (ch === 'n' || ch === 'N') return moveDetailMatch(state, ch === 'N')
    const lines = detailLines(state)
    const maxScroll = Math.max(0, lines.length - (state.detailHeight ?? 1))
    const section = { i: 'input', o: 'output', x: 'error', g: 'span' }[ch]
    if (section) return { ...state, matchRow: null, scroll: Math.min(maxScroll,
      detailSections.get(lines)?.[section] ?? 0) }
    return delta ? { ...state, matchRow: null, scroll: Math.max(0, Math.min(maxScroll, state.scroll + delta)) } : state
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
    if (state.page === 'sessions') return navigateExplorer(state, 'turns', { sessionId: item.session_id, adapterId: item.adapter_id ?? '' })
    if (state.page === 'turns') return navigateExplorer(state, 'spans', { traceId: item.trace_id })
    return navigateExplorer(state, 'detail', { spanId: item.span_id })
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

const logicalCache = new WeakMap()
const rowsCache = new WeakMap()
const matchesCache = new WeakMap()
const logicalSections = new WeakMap()
const detailSections = new WeakMap()

function spanLogicalLines(span) {
  if (span && logicalCache.has(span)) return logicalCache.get(span)
  if (!span) return ['span not found in snapshot; press b then r']
  const output = []
  const sections = new Map()
  for (const [label, value] of [
    ['span', span.span_id], ['kind / name', `${span.kind} / ${span.name}`],
    ['status', span.status], ['duration', `${fmtDur(span.duration_ms)}${span.duration_suspect ? ' (suspect: resume gap)' : ''}`],
    ['model', span.model], ['tokens', span.tokens], ['input', span.input], ['output', span.output],
    ['error', span.error], ['privacy', span.privacy]
  ]) {
    sections.set(output.length, label)
    output.push(`[${label}]`)
    const text = value == null ? '(not captured)' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
    output.push(...terminalSafe(text.slice(0, 100000)).replace(/\t/g, '  ').split('\n'))
    if (text.length > 100000) output.push('(section truncated at 100000 characters)')
  }
  logicalCache.set(span, output)
  logicalSections.set(output, sections)
  return output
}

export function spanDetails(span, width) {
  if (!span) return spanLogicalLines(span).flatMap(line => wrapExplorer(line, width))
  if (!rowsCache.has(span)) rowsCache.set(span, new Map())
  const cache = rowsCache.get(span)
  if (cache.has(width)) return cache.get(width)
  const logical = spanLogicalLines(span), rows = [], sections = {}
  for (const [index, line] of logical.entries()) {
    const section = logicalSections.get(logical)?.get(index)
    if (section) sections[section] = rows.length
    rows.push(...wrapExplorer(line, width))
  }
  detailSections.set(rows, sections)
  if (cache.size >= 4) cache.delete(cache.keys().next().value)
  cache.set(width, rows)
  return rows
}

export function metricsRows(data, width) {
  if (!data) return []
  const lines = [
    `SESSION ${data.session_id}`,
    `turns ${data.traces} | spans ${data.spans} | error spans ${data.errors} | unclosed ${data.unclosed}`,
    `tokens in ${data.tokens?.input ?? '?'} | out ${data.tokens?.output ?? '?'} | cache-read ${data.tokens?.cache_read ?? '?'}`,
    'LATENCY (completed measured calls; not TTFT)'
  ]
  for (const [label, latency] of [['LLM', data.llm], ['Tools', data.tools]]) {
    lines.push(`${label} n=${latency?.n ?? 0} p50 ${fmtDur(latency?.p50_ms)} p95 ${fmtDur(latency?.p95_ms)} max ${fmtDur(latency?.max_ms)}`)
  }
  lines.push('p95 needs at least 5 measured calls; unknown values are -', 'SLOWEST CALLS')
  for (const item of data.slowest ?? []) lines.push(`${item.kind} ${fmtDur(item.duration_ms)} ${item.status} ${item.name}`)
  if (!data.slowest?.length) lines.push('No completed measured calls in this session.')
  lines.push('Cost and throughput are not inferred from token totals.')
  return lines.flatMap(line => wrapExplorer(line, width))
}

function exportRows(state, width) {
  const address = new URL(state.base)
  if (address.hostname !== '127.0.0.1' || address.protocol !== 'http:') return wrapExplorer('Export helper supports local loopback HTTP only.', width)
  const quote = value => "'" + String(value).replaceAll("'", "'\"'\"'") + "'"
  const context = (state.adapterId ? ` --agent ${quote(state.adapterId)}` : '') + (state.sessionId ? ` --session ${quote(state.sessionId)}` : '')
  const command = `python3 scripts/export-span.py --trace ${quote(state.traceId)} --span ${quote(state.spanId)}${context} --output span.json --port ${address.port || 80}`
  return ['FULL STORED CAPTURE EXPORT', 'Run from the agent-trace repo in another terminal:', command,
    'Uses the existing local token. Writes mode 0600; refuses overwrite.',
    'No additional payload redaction is disabled. Press v to return.'].flatMap(line => wrapExplorer(line, width))
}

export function explorerRows(state, width, height) {
  if (state.showHelp) return ['KEYS', '/ local find; f server find (sessions)', 'a agent; M model; t since date (sessions)', 'Left/Right previous/next session page', 'Enter open; b/Esc back; q close', 'l live/pause; p pin dock; G global dock', 'm metrics for selected session; r refresh', 'Space fold; e errors; i/o/x detail sections', 'n/N next/previous match; v export help', 'Ctrl+u clears find; ? closes help'].flatMap(line => wrapExplorer(line, width)).slice(0, height)
  if (state.loading && !state.data) return ['loading snapshot...']
  if (state.error && !state.data) return wrapExplorer(`ERROR: ${state.error}\npress r to retry, b to go back`, width)
  if (state.page === 'metrics' || state.showExport) {
    const lines = state.showExport ? exportRows(state, width) : metricsRows(state.data, width)
    const start = Math.min(state.scroll, Math.max(0, lines.length - height))
    return lines.slice(start, start + height)
  }
  if (state.page === 'detail') {
    const lines = spanDetails(state.data.spans.find(s => s.span_id === state.spanId), width)
    const start = Math.min(state.scroll, Math.max(0, lines.length - height))
    return lines.slice(start, start + height)
  }
  const items = explorerItems(state)
  if (!items.length) return [state.query ? 'No matches in loaded records.' : state.errorsOnly ? 'No error spans in this trace.' : 'No indexed records in this scope.']
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

export function explorerSearchStatus(state, width = 100) {
  if (state.searching) return `${state.notice ? state.notice + ' | ' : ''}${state.searchTarget === 'local' ? 'find' : state.searchTarget}> ${wrapExplorer(state.searchDraft + '_', Math.max(2, width - 8)).at(-1)}`
  if (state.error) return `STALE: ${state.error} | r retry`
  if (state.notice) return state.notice
  if (!state.query) {
    const filters = Object.entries(state.filters ?? {}).filter(([, value]) => value).map(([name, value]) => `${name}=${value}`).join(' ')
    const stamp = state.updatedAt ? new Date(state.updatedAt).toISOString().slice(11, 19) + 'Z' : 'not loaded'
    return `${state.live ? 'LIVE' : 'PAUSED'} ${stamp}${filters ? ' | ' + filters : ''}`
  }
  if (state.page !== 'detail') return `/${state.query} | ${explorerItems(state).length} loaded matches | / edit`
  const count = detailMatches(state).length
  return `/${state.query} | ${count === 1000 ? '>=1000' : count} matching rows | n/N next/previous`
}

function registerExplorer(sdk, pinDock) {
  const { Box, Dialog, Overlay, React, Text, ShimmerRows, h, defineWidgetApp, updateWidget } = sdk
  let app
  function ExplorerBody({ state, width, height, t }) {
    const resource = explorerResource(state)
    React.useEffect(() => {
      updateWidget(app, current => ({ ...current, detailWidth: width, detailHeight: height, scroll: 0 }))
    }, [width, height])
    React.useEffect(() => {
      if (state.searching || (!state.live && state.data && !state.loading)) return
      const controller = new AbortController()
      let active = true
      let timer
      const pull = async () => {
        try {
          const data = await loadExplorer(state, { signal: controller.signal })
          if (active) updateWidget(app, current => {
            if (current.base !== state.base || current.adapterId !== state.adapterId || current.sessionId !== state.sessionId || explorerResource(current) !== resource || current.revision !== state.revision || current.searching) return current
            return applyExplorerData(current, data)
          })
        } catch (error) {
          if (active) updateWidget(app, current => current.base === state.base && current.adapterId === state.adapterId && current.sessionId === state.sessionId && explorerResource(current) === resource && current.revision === state.revision
            ? { ...current, loading: false, error: describe(error).message } : current)
        } finally {
          if (active && state.live) timer = setTimeout(pull, POLL_MS)
        }
      }
      pull()
      return () => { active = false; clearTimeout(timer); controller.abort() }
    }, [state.base, state.adapterId, state.sessionId, resource, state.revision, state.live, state.searching])
    if (state.loading && !state.data && !state.showHelp) return h(Box, { width, height }, h(ShimmerRows, {
      color: t.color.muted, highlight: t.color.label, rows: Array.from({ length: Math.min(4, height) }, () => [Math.max(1, width - 2)])
    }))
    return h(Box, { width, height, flexDirection: 'column' },
      ...explorerRows(state, width, height).map((row, i) => h(Text, {
        key: i, wrap: 'truncate', color: state.error ? t.color.error : row.startsWith('>') ? t.color.primary : t.color.text
      }, row)))
  }
  app = defineWidgetApp({
    id: 'tracex', help: 'browse sessions, turn trees, and LLM/tool inputs, outputs, errors', mode: 'modal',
    usage: 'usage: /tracex [session-id] [http://127.0.0.1:8741] [--agent ADAPTER]',
    init: explorerInit,
    reduce(state, input) {
      if (!state.searching && !state.showHelp && (input.ch === 'p' || input.ch === 'G')) {
        const sessionId = input.ch === 'G' ? '' : state.page === 'sessions' ? explorerItems(state)[state.selected]?.session_id : state.sessionId
        const adapterId = input.ch === 'G' ? '' : state.page === 'sessions' ? explorerItems(state)[state.selected]?.adapter_id ?? '' : state.adapterId
        if (sessionId == null) return state
        pinDock(state.base, sessionId, adapterId)
        return { ...state, notice: sessionId ? 'dock pinned to selected session' : 'dock switched to global' }
      }
      return explorerReduce(state, input)
    },
    render({ cols, rows, state, t }) {
      const width = Math.max(12, Math.min(104, cols - 4))
      const height = Math.max(2, Math.min(24, rows - 12))
      const count = state.data ? explorerItems(state).length : 0
      const scope = state.page === 'sessions'
        ? `sessions ${state.offset ?? 0}-${(state.offset ?? 0) + (state.data?.sessions?.length ?? 0)} / ${state.data?.total ?? '?'} | Left/Right pages; f server find`
        : `session ${state.sessionId}`
      return h(Overlay, {}, h(Dialog, { width },
        h(Text, { color: t.color.label }, wrapExplorer(`agent-trace / ${state.page} ${state.live ? '[LIVE]' : '[PAUSED]'} ${state.errorsOnly ? '[errors]' : ''}`, width - 6)[0]),
        h(Text, { color: t.color.muted }, wrapExplorer(scope, width - 6)[0]),
        h(ExplorerBody, { state, width: width - 6, height, t }),
        h(Text, { color: t.color.muted }, wrapExplorer(state.searching ? 'Enter apply | Esc cancel' : width < 46 ? 'j/k Enter b q ?' : `${state.page === 'detail' ? 'j/k scroll' : `j/k select ${count ? state.selected + 1 : 0}/${count}`} | Enter | b back | q close | l live | p pin | m metrics | ? help`, width - 6)[0]),
        h(Text, { color: t.color.muted }, wrapExplorer(explorerSearchStatus(state, width - 6) || (state.page === 'detail' ? 'i input | o output | x error | / find | r refresh' : width < 46 ? 'r refresh e errors' : 'r refresh | Space fold | e errors | snapshot (not live)'), width - 6)[0])
      ))
    }
  })
  return app
}
