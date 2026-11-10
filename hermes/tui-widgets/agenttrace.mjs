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
const INNER = 40

let tokenCache = { base: '', token: '' }

async function dashboardToken(base) {
  if (tokenCache.base === base && tokenCache.token) return tokenCache.token

  const res = await fetch(`${base}/`, { signal: AbortSignal.timeout(8000) })

  if (!res.ok) throw new Error(`dashboard HTTP ${res.status}`)

  const html = await res.text()
  const match = html.match(/AGENTTRACE_TOKEN__\s*=\s*"([^"]+)"/)

  if (!match) throw new Error('token not embedded in dashboard page')
  tokenCache = { base, token: match[1] }

  return match[1]
}

const sig = () => AbortSignal.timeout(8000)

/** Server-first diagnosis: a dead port and a busy indexer read differently. */
function describe(error) {
  const name = error?.name ?? ''
  const message = error instanceof Error ? error.message : String(error)

  if (name === 'TimeoutError' || name === 'AbortError' || /aborted due to time/i.test(message)) {
    return { hint: 'indexer busy — retrying', message: 'agent-trace is indexing' }
  }

  if (/fetch failed|ECONNREFUSED|network/i.test(message)) {
    return { hint: 'start it: agenttrace serve --daemon', message: 'agent-trace server down' }
  }

  return { hint: 'agenttrace doctor', message }
}

/** One poll: totals from /api/overview, recent turns from /api/traces. */
export async function snapshot(base = DEFAULT_BASE) {
  const token = await dashboardToken(base)
  const headers = { Authorization: `Bearer ${token}` }
  const [overview, recent] = await Promise.all([
    fetch(`${base}/api/overview`, { headers, signal: sig() }).then(r => r.json()),
    fetch(`${base}/api/traces?limit=${SLOTS}`, { headers, signal: sig() }).then(r => r.json())
  ])

  // The API answers newest-first; charts read left-to-right oldest-first.
  const turns = (recent.traces ?? [])
    .slice()
    .sort((a, b) => String(a.start ?? '').localeCompare(String(b.start ?? '')))

  return {
    updatedAt: Date.now(),
    totals: {
      traces: Number(overview.traces ?? 0),
      spans: Number(overview.spans ?? 0),
      errors: Number(overview.errors ?? 0),
      unclosed: Number(overview.unclosed ?? 0),
      inTok: Number(overview.tokens?.input ?? 0),
      outTok: Number(overview.tokens?.output ?? 0)
    },
    durations: turns.map(t => Number(t.duration_ms ?? 0)),
    p50: median(turns.map(t => Number(t.duration_ms ?? 0))),
    last: turns.length ? turns[turns.length - 1] : null,
    agents: (overview.per_agent ?? []).length
  }
}

function median(values) {
  const clean = values.filter(v => Number.isFinite(v) && v >= 0).sort((a, b) => a - b)

  if (!clean.length) return null

  const mid = Math.floor(clean.length / 2)

  return clean.length % 2 ? clean[mid] : Math.round((clean[mid - 1] + clean[mid]) / 2)
}

const cell = (value, width) => String(value).padStart(width)
const clamp = (text, width) => (text.length > width ? `${text.slice(0, Math.max(0, width - 1))}…` : text)

function fmtTok(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`

  return String(n)
}

function fmtDur(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`

  const min = Math.floor(ms / 60_000)

  return `${min}m${String(Math.round((ms % 60_000) / 1000)).padStart(2, '0')}s`
}

export default function register(sdk) {
  const { Box, Dialog, React, ShimmerRows, Text, defineWidgetApp, h, sparkline } = sdk

  // Skeleton mirrors the ready rows so the card never changes height.
  const LOADING_ROWS = [
    [16, 8],
    [24, 6],
    [18, 10],
    [14, 9]
  ]

  function Body({ base, t }) {
    const [phase, setPhase] = React.useState({ kind: 'loading' })

    React.useEffect(() => {
      let alive = true
      let timer = null

      const pull = async () => {
        try {
          const snap = await snapshot(base)

          if (alive) setPhase({ kind: 'ready', snap })
        } catch (error) {
          if (alive) setPhase({ kind: 'error', ...describe(error) })
        }
      }

      pull()
      timer = setInterval(pull, POLL_MS)

      return () => {
        alive = false
        clearInterval(timer)
      }
    }, [base])

    if (phase.kind === 'loading') {
      return h(ShimmerRows, { color: t.color.muted, highlight: t.color.label, rows: LOADING_ROWS })
    }

    if (phase.kind === 'error') {
      return h(
        Box,
        { flexDirection: 'column' },
        h(Text, { color: t.color.error }, clamp(phase.message, INNER)),
        h(Text, { color: t.color.muted }, clamp(phase.hint, INNER))
      )
    }

    const { snap } = phase
    const { totals } = snap
    const last = snap.last
    const dot = totals.errors > 0 || totals.unclosed > 0 ? t.color.error : t.color.ok

    return h(
      Box,
      { flexDirection: 'column' },
      h(
        Box,
        { flexDirection: 'row', justifyContent: 'space-between', width: INNER },
        h(Text, { bold: true, color: t.color.label }, 'agent-trace'),
        h(
          Text,
          { color: t.color.muted },
          h(Text, { color: dot }, '● '),
          `${cell(fmtTok(totals.traces), 5)} turns`
        )
      ),
      h(
        Box,
        { flexDirection: 'row', justifyContent: 'space-between', width: INNER },
        h(Text, { color: t.color.primary }, sparkline(snap.durations, 22)),
        h(Text, { color: t.color.muted }, `p50 ${cell(fmtDur(snap.p50), 5)}`)
      ),
      h(
        Text,
        { color: t.color.text },
        `in ${cell(fmtTok(totals.inTok), 6)}  out ${cell(fmtTok(totals.outTok), 5)}  err ${cell(totals.errors, 3)}`
      ),
      h(
        Text,
        { color: t.color.muted },
        last
          ? clamp(
              `last ${last.adapter_id ?? '?'} ${fmtDur(last.duration_ms)} · ${Number(last.span_count ?? 0)} spans${
                last.status && last.status !== 'ok' ? ' · failed' : ''
              }`,
              40
            )
          : 'no turns yet — send the agent a message'
      )
    )
  }

  defineWidgetApp({
    id: 'agenttrace',
    help: 'live agent-trace traffic: turns, tokens, errors, p50',
    mode: 'ambient',
    zone: 'dock-bottom',
    width: 46,
    usage: `usage: /agenttrace [base-url]   default ${DEFAULT_BASE}`,

    init(arg) {
      return { base: (arg.trim() || DEFAULT_BASE).replace(/\/+$/, '') }
    },

    reduce(state, { ch, key }) {
      return key.escape || ch === 'q' ? null : state
    },

    render({ cols, state, t }) {
      return h(Dialog, { width: Math.min(46, Math.max(24, cols - 4)) }, h(Body, { base: state.base, t }))
    }
  })
}
