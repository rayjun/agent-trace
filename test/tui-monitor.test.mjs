import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { test } from 'node:test'
import register, { explorerInit, explorerResource, explorerReduce, loadExplorer, snapshot,
  applyExplorerData, currentScope, pinSession, metricsRows, spanDetails, detailMatches, cardRows } from '../hermes/tui-widgets/agenttrace.mjs'
const key = (s, ch = '', key = {}) => explorerReduce(s, { ch, key })
const wait = ms => new Promise(r => setTimeout(r, ms))
const metrics = { session_id: 's', traces: 2, spans: 4, errors: 1, unclosed: 0,
  tokens: { input: 30, output: 4, cache_read: 20 },
  llm: { n: 1, p50_ms: 100, p95_ms: null, max_ms: 100 },
  tools: { n: 0, p50_ms: null, p95_ms: null, max_ms: null }, slowest: [],
  recent_traces: [{ trace_id: 't', status: 'ok', unclosed: 0, duration_ms: 100 }] }

async function fixture(fn, options = {}) {
  const seen = []
  const server = createServer((req, res) => {
    seen.push(req.url)
    if (req.url === '/') { res.end('window.__AGENTTRACE_TOKEN__ = "fixture-optimize"'); return }
    assert.equal(req.headers.authorization, 'Bearer fixture-optimize')
    const url = new URL(req.url, 'http://127.0.0.1')
    let body
    if (url.pathname === '/api/health') body = options.health ?? { building: false, last_scan: { problems: [] } }
    else if (url.pathname === '/api/sessions/s/metrics') body = options.metrics ?? metrics
    else if (url.pathname === '/api/sessions') body = { sessions: [{ session_id: 's', adapter_id: 'hermes' }], total: 120, offset: Number(url.searchParams.get('offset')), has_more: true }
    else if (url.pathname === '/api/sessions/s') body = { session_id: 's', turns: [{ trace_id: 't' }] }
    else if (url.pathname === '/api/traces/t' && url.searchParams.get('summary') === '1') body = { trace_id: 't', session_id: 's', spans: [{ span_id: 'call', kind: 'llm', name: 'model', status: 'ok' }] }
    else if (url.pathname === '/api/traces/t/spans/call') body = { span_id: 'call', trace_id: 't', session_id: 's', input: 'captured', output: 'done' }
    else { res.writeHead(404); res.end('{}'); return }
    res.setHeader('Content-Type', 'application/json')
    if (options.delaySessions && url.pathname === '/api/sessions') setTimeout(() => res.end(JSON.stringify(body)), options.delaySessions)
    else res.end(JSON.stringify(body))
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  try { await fn(`http://127.0.0.1:${server.address().port}`, seen) }
  finally { server.closeAllConnections(); await new Promise(r => server.close(r)) }
}

test('server search, agent/model/time filters and pagination use separate exact resources', () => {
  let s = explorerInit()
  s = { ...s, loading: false, data: { sessions: [], total: 120, has_more: true } }
  s = key(s, '', { rightArrow: true })
  assert.equal(s.offset, 50)
  assert.match(explorerResource(s), /offset=50/)
  s = key(s, 'f'); s = key(s, 'older/session'); s = key(s, '', { return: true })
  assert.equal(s.offset, 0)
  assert.match(explorerResource(s), /search=older%2Fsession/)
  for (const [ch, value, expected] of [['a', 'hermes', 'agent=hermes'], ['M', 'model+v1', 'model=model%2Bv1'], ['t', '2026-11-01', 'since=2026-11-01']]) {
    s = key(s, ch); s = key(s, value); s = key(s, '', { return: true })
    assert.ok(explorerResource(s).includes(expected))
  }
})

test('metadata tree and individual detail use lazy payload endpoints, metrics retain nulls', async () => {
  await fixture(async (base, seen) => {
    let s = { ...explorerInit(`s ${base}`), page: 'spans', traceId: 't' }
    s = applyExplorerData(s, await loadExplorer(s))
    assert.equal(s.data.spans[0].input, undefined)
    s = key(s, '', { return: true })
    assert.equal(s.loading, true)
    s = applyExplorerData(s, await loadExplorer(s))
    assert.equal(s.data.spans[0].input, 'captured')
    assert.ok(seen.includes('/api/traces/t?summary=1'))
    assert.ok(seen.includes('/api/traces/t/spans/call'))
    const m = await loadExplorer({ ...s, page: 'metrics' })
    assert.equal(m.llm.p95_ms, null)
    assert.ok(metricsRows(m, 80).some(row => row.includes('p95 -')))
  })
})

test('session-scoped dock requests exact metrics, not global totals', async () => {
  await fixture(async (base, seen) => {
    const snap = await snapshot(base, { sessionId: 's' })
    assert.equal(snap.sessionId, 's')
    assert.equal(snap.totals.traces, 2)
    assert.equal(snap.health.building, false)
    assert.ok(!seen.includes('/api/overview'))
    assert.ok(!seen.some(path => path.startsWith('/api/traces?')))
  })
})

test('large detail wrapping and find results are cached by immutable span identity', () => {
  const span = { span_id: 'call', input: 'needle 中文 '.repeat(1000) }
  assert.equal(spanDetails(span, 40), spanDetails(span, 40))
  const s = { page: 'detail', spanId: 'call', query: 'needle', data: { spans: [span] }, detailWidth: 40 }
  assert.equal(detailMatches(s), detailMatches(s))
  assert.notEqual(spanDetails(span, 40), spanDetails(span, 50))
  assert.notEqual(spanDetails(span, 40), spanDetails({ ...span }, 40))
})

test('SDK pin action opens ambient dock without replacing modal; explicit global unpins', () => {
  const apps = new Map(), opened = []
  register({ Overlay: 'Overlay', updateWidget: () => {}, openWidget: (app, state) => opened.push([app, state]),
    defineWidgetApp: app => { apps.set(app.id, app); return app } })
  const explorer = apps.get('tracex')
  const s = { ...explorerInit('s'), loading: false, data: { turns: [] } }
  explorer.reduce(s, { ch: 'p', key: {} })
  assert.equal(currentScope().sessionId, 's')
  assert.equal(opened[0][0].id, 'agenttrace')
  assert.equal(opened[0][0].mode, 'ambient')
  apps.get('agenttrace').init('global')
  assert.equal(currentScope().sessionId, '')
})

test('live mode polls without overlap and pausing cancels it without another fetch', async () => {
  await fixture(async (base, seen) => {
    const apps = new Map(), slots = []
    let cursor = 0, pending = [], current = { ...explorerInit(`s ${base}`), live: true }
    const sdk = {
      Box: 'Box', Text: 'Text', Dialog: 'Dialog', Overlay: 'Overlay', ShimmerRows: 'ShimmerRows',
      React: { useEffect(fn, deps) {
        const i = cursor++, previous = slots[i]
        if (!previous || deps.some((value, j) => value !== previous.deps[j])) pending.push(() => {
          previous?.cleanup?.(); slots[i] = { deps, cleanup: fn() }
        })
      } },
      h: (type, props, ...children) => ({ type, props, children }),
      defineWidgetApp: app => { apps.set(app.id, app); return app }, updateWidget: (_app, fn) => { current = fn(current) }
    }
    register(sdk)
    const app = apps.get('tracex')
    const render = () => {
      cursor = 0; pending = []
      const dialog = app.render({ cols: 80, rows: 30, state: current, t: { color: {} } }).children[0]
      const body = dialog.children.find(c => typeof c.type === 'function')
      body.type(body.props); pending.forEach(fn => fn())
    }
    try {
      render()
      for (let i = 0; i < 100 && current.loading; i++) await wait(5)
      assert.equal(current.loading, false)
      const before = seen.filter(path => path === '/api/sessions/s').length
      await wait(2600)
      assert.ok(seen.filter(path => path === '/api/sessions/s').length > before)
      current = key(current, 'l'); render()
      const paused = seen.length
      await wait(100)
      assert.equal(seen.length, paused)
      assert.equal(current.live, false)
    } finally { slots.forEach(slot => slot.cleanup?.()); pinSession('http://127.0.0.1:8741', '') }
  })
})

test('health problem arrays are counts, not empty or serialized diagnostics', async () => {
  const spark = () => ''
  await fixture(async base => {
    const snap = await snapshot(base, { sessionId: 's' })
    assert.ok(cardRows({ kind: 'ready', snap }, 80, spark)[3].startsWith('new '))
    snap.health.last_scan.problems = [{ detail: 'sensitive text' }, { detail: 'other' }]
    const row = cardRows({ kind: 'ready', snap }, 80, spark)[3]
    assert.match(row, /ingest: 2 problems/)
    assert.ok(!row.includes('sensitive'))
  })
})

test('indexing health returns unknown totals without waiting for data queries', async () => {
  await fixture(async (base, seen) => {
    const snap = await snapshot(base, { sessionId: 's' })
    assert.equal(snap.health.building, true)
    assert.equal(snap.totals.traces, null)
    assert.ok(!seen.includes('/api/sessions/s/metrics'))
    const rows = cardRows({ kind: 'ready', snap }, 80, () => '')
    assert.match(rows[0], /indexing/)
    assert.match(rows[2], /in - out -/)
  }, { health: { building: true, last_scan: { problems: [] } } })
})

test('session token components remain unknown rather than rejected or zero-filled', async () => {
  await fixture(async base => {
    const snap = await snapshot(base, { sessionId: 's' })
    assert.equal(snap.totals.inTok, null)
    assert.match(cardRows({ kind: 'ready', snap }, 80, () => '')[2], /in - out -/)
  }, { metrics: { ...metrics, tokens: { input: null, output: null, cache_read: null } } })
})

test('server filter editor accepts batched command plus query from real terminals', () => {
  let s = key(explorerInit(), 'folder/session')
  assert.equal(s.searchTarget, 'search')
  assert.equal(s.searchDraft, 'older/session')
  s = key(s, '', { return: true })
  assert.equal(s.filters.search, 'older/session')
  assert.equal(s.loading, true)
  s = key(s, 'Mmodel-v2')
  assert.equal(s.searchTarget, 'model')
  assert.equal(s.searchDraft, 'model-v2')
})

for (const manual of [false, true]) for (const submit of [false, true]) {
  test(`paused ${manual ? 'manual refresh' : 'initial load'} resumes after search ${submit ? 'submission' : 'cancellation'} overlaps delayed response`, async () => {
    await fixture(async (base, seen) => {
      const apps = new Map(), slots = []
      let cursor = 0, pending = []
      const previous = { sessions: [{ session_id: 's', adapter_id: 'hermes' }], total: 1, has_more: false }
      let current = { ...explorerInit(), base, data: manual ? previous : null, loading: true }
      register({
        Box: 'Box', Text: 'Text', Dialog: 'Dialog', Overlay: 'Overlay', ShimmerRows: 'ShimmerRows',
        React: { useEffect(fn, deps) {
          const i = cursor++, old = slots[i]
          if (!old || deps.some((value, j) => value !== old.deps[j])) pending.push(() => {
            old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }
          })
        } },
        h: (type, props, ...children) => ({ type, props, children }),
        defineWidgetApp: app => { apps.set(app.id, app); return app },
        updateWidget: (_app, fn) => { current = fn(current) }
      })
      const app = apps.get('tracex')
      const render = () => {
        cursor = 0; pending = []
        const dialog = app.render({ cols: 80, rows: 30, state: current, t: { color: {} } }).children[0]
        const body = dialog.children.find(c => typeof c.type === 'function')
        body.type(body.props); pending.forEach(fn => fn())
      }
      try {
        render()
        for (let i = 0; i < 100 && !seen.some(path => path.startsWith('/api/sessions?')); i++) await wait(5)
        assert.ok(seen.some(path => path.startsWith('/api/sessions?')))
        current = key(current, '/s'); render()
        await wait(100)
        assert.equal(current.searching, true)
        assert.equal(current.data, manual ? previous : null)
        current = key(current, '', submit ? { return: true } : { escape: true }); render()
        for (let i = 0; i < 100 && current.loading; i++) await wait(5)
        assert.equal(current.loading, false)
        assert.equal(current.searching, false)
        assert.equal(current.data.sessions[0].session_id, 's')
        assert.ok(seen.filter(path => path.startsWith('/api/sessions?')).length >= 2)
      } finally { slots.forEach(slot => slot.cleanup?.()) }
    }, { delaySessions: 70 })
  })
}
