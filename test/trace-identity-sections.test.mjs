import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createServer } from 'node:http'
import register, { explorerInit, explorerReduce, explorerResource, applyExplorerData,
  explorerItems, spanDetails, wrapExplorer, loadExplorer, snapshot, pinSession, currentScope,
  explorerSearchStatus, normalizeSince } from '../hermes/tui-widgets/agenttrace.mjs'

const press = (s, ch = '', key = {}) => explorerReduce(s, { ch, key })
const enter = s => press(s, '', { return: true })
const sessions = [ { adapter_id: 'a', session_id: 'duplicate' }, { adapter_id: 'b', session_id: 'duplicate' } ]
const picker = () => applyExplorerData(explorerInit(), { sessions })

function host() {
  const apps = new Map(), opened = [], effects = [], hooks = []
  let cursor = 0
  const sdk = { Box: 'Box', Text: 'Text', Dialog: 'Dialog', Overlay: 'Overlay', ShimmerRows: 'ShimmerRows',
    sparkline: () => '', h: (type, props, ...children) => ({ type, props, children }),
    React: { useState: initial => { const i = cursor++; if (!(i in hooks)) hooks[i] = typeof initial === 'function' ? initial() : initial; return [hooks[i], v => { hooks[i] = v }] }, useEffect: fn => effects.push(fn) },
    defineWidgetApp: app => { apps.set(app.id, app); return app }, updateWidget: () => {},
    openWidget: (app, state) => opened.push({ app, state }) }
  register(sdk)
  return { apps, opened, effects, hooks, render: (body) => { cursor = 0; return body.type(body.props) } }
}

test('composite selection survives duplicate-id refresh, navigation and Back', () => {
  let state = press(picker(), 'j')
  state = applyExplorerData(state, { sessions: [...sessions].reverse() })
  assert.equal(explorerItems(state)[state.selected].adapter_id, 'b')
  state = enter(state)
  assert.equal(state.adapterId, 'b')
  assert.equal(explorerResource(state), '/api/sessions/duplicate?agent=b')
  state = press(press(state, 'l'), 'r')
  assert.equal(state.live, true)
  assert.equal(explorerResource(state), '/api/sessions/duplicate?agent=b')
  state = applyExplorerData(state, { turns: [{ trace_id: 't' }] })
  state = enter(state)
  state = applyExplorerData(state, { spans: [{ span_id: 's' }] })
  state = enter(state)
  assert.equal(state.adapterId, 'b')
  for (let n = 0; n < 3; n++) state = press(state, 'b')
  state = applyExplorerData(state, { sessions })
  assert.equal(explorerItems(state)[state.selected].adapter_id, 'b')
  assert.equal(press(state, 'm').adapterId, 'b')
  const missing = applyExplorerData(state, { sessions: [sessions[0]] })
  assert.match(missing.notice, /previous record missing/)
})

test('direct commands preserve literal identifiers and explicit namespace; legacy commands remain', () => {
  const { apps } = host()
  for (const command of ['s/% http://localhost:8741 --agent A/+', 's/% --agent A/+']) {
    const state = explorerInit(command)
    assert.equal(state.sessionId, 's/%'); assert.equal(state.adapterId, 'A/+')
    assert.equal(explorerResource(state), '/api/sessions/s%2F%25?agent=A%2F%2B')
    assert.ok(apps.get('agenttrace').init('session ' + command))
    assert.equal(currentScope().adapterId, 'A/+')
  }
  assert.equal(explorerInit('s').adapterId, '')
  assert.ok(apps.get('agenttrace').init('session s'))
  assert.equal(currentScope().adapterId, '')
  for (const bad of ['s --agent', '--agent b', 's --agent b --agent c', 's u extra', 'http://localhost --agent b']) assert.equal(explorerInit(bad), null)
})

test('SDK picker pin, metrics, ambient polling and click preserve adapter namespace', async () => {
  const requests = []
  const server = createServer((req, res) => {
    requests.push(req.url)
    if (req.url === '/') return res.end('window.__AGENTTRACE_TOKEN__ = "sdk-identity"')
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(req.url === '/api/health' ? { building: false } : {
      session_id: 'duplicate', adapter_id: 'b', traces: 1, spans: 1, errors: 0, unclosed: 0,
      tokens: { input: 1, output: 2 }, recent_traces: [] }))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const harness = host(), { apps } = harness
  const state = { ...press(picker(), 'j'), base: `http://127.0.0.1:${server.address().port}` }
  apps.get('tracex').reduce(state, { ch: 'p' })
  assert.deepEqual(currentScope(), { base: state.base, sessionId: 'duplicate', adapterId: 'b' })
  assert.equal(explorerResource(press(state, 'm')), '/api/sessions/duplicate/metrics?agent=b')
  const dock = apps.get('agenttrace')
  const dialog = dock.render({ cols: 100, state: { base: state.base }, t: { color: {} } })
  const body = dialog.children[0]
  harness.render(body)
  const cleanups = harness.effects.splice(0).map(effect => effect())
  try {
    const deadline = Date.now() + 2000
    while (harness.hooks[0].kind === 'loading' && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 5))
    assert.equal(harness.hooks[0].kind, 'ready')
    assert.ok(requests.includes('/api/sessions/duplicate/metrics?agent=b'))
    const ready = harness.render(body)
    ready.props.onClick()
    const clicked = harness.opened.at(-1)
    assert.equal(clicked.app.id, 'tracex')
    assert.equal(clicked.state.adapterId, 'b')
    assert.equal(clicked.state.sessionId, 'duplicate')
    assert.equal(explorerResource(clicked.state), '/api/sessions/duplicate?agent=b')
    apps.get('tracex').reduce(state, { ch: 'G' })
    assert.equal(currentScope().adapterId, '')
  } finally {
    cleanups.forEach(cleanup => cleanup?.())
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve))
  }
})

test('since editor canonicalizes accepted instants and visibly rejects invalid input without altering other filters', () => {
  for (const invalid of ['2026-02-30', '2026-02-29', '2026-13-01', '2026-00-01', '2026-01-00',
    '2026-01-01T12:00:00', '2026-01-01T24:00:00Z', '2026-01-01T12:00:60Z',
    '2026-01-01T12:00:00+08:99', '0000-01-01']) assert.throws(() => normalizeSince(invalid))
  assert.equal(normalizeSince('2024-02-29'), '2024-02-29T00:00:00.000Z')
  assert.equal(normalizeSince('2026-01-01T23:30:00-03:00'), '2026-01-02T02:30:00.000Z')
  assert.equal(normalizeSince('2026-11-12T08:00:00.000501+08:00'), '2026-11-12T00:00:00.000501Z')
  assert.equal(normalizeSince('2026-11-12T23:59:59.9999-01:00'), '2026-11-13T00:59:59.999900Z')
  const precise = enter(press(picker(), 't2026-11-12T08:00:00.000501+08:00'))
  assert.equal(new URL(explorerResource(precise), 'http://localhost').searchParams.get('since'), '2026-11-12T00:00:00.000501Z')
  let state = enter(press(picker(), 't2026-01-02T03:04:05+08:00'))
  assert.equal(state.filters.since, '2026-01-01T19:04:05.000Z')
  state = enter(press(state, 't2026-02-03'))
  assert.equal(state.filters.since, '2026-02-03T00:00:00.000Z')
  const invalid = enter(press(state, 'tnot a date'))
  assert.equal(invalid.searching, true)
  assert.match(explorerSearchStatus(invalid), /Invalid since date\/time/)
  assert.equal(invalid.filters.since, state.filters.since)
  const literal = enter(press(state, 'f  literal /%  '))
  assert.equal(literal.filters.search, '  literal /%  ')
})

test('detail jumps use structural positions despite hostile headers, narrow wraps and cache eviction', () => {
  const span = { span_id: '[input]\n[output]\n[error]\n[metadata]', name: '[input]\n[output]',
    input: '[output]\n[error]\n[metadata]\n[span]\n中文🙂'.repeat(3),
    output: 'REAL OUTPUT\n[input]\n[error]\n[metadata]', error: 'REAL ERROR', privacy: { fixture: true } }
  for (const width of [40, 4, 7, 20, 80, 40, 4]) {
    const lines = spanDetails(span, width)
    assert.ok(Array.isArray(lines))
    assert.equal(spanDetails(span, width), lines)
    const state = { ...explorerInit('s'), page: 'detail', spanId: span.span_id, data: { spans: [span] }, loading: false, detailWidth: width, detailHeight: 1 }
    const output = press(state, 'o'), error = press(state, 'x'), input = press(state, 'i')
    assert.deepEqual(lines.slice(output.scroll, output.scroll + wrapExplorer('[output]', width).length), wrapExplorer('[output]', width))
    assert.equal(lines[output.scroll + wrapExplorer('[output]', width).length], wrapExplorer('REAL OUTPUT', width)[0])
    assert.equal(lines[error.scroll + wrapExplorer('[error]', width).length], wrapExplorer('REAL ERROR', width)[0])
    assert.ok(input.scroll < output.scroll && output.scroll < error.scroll)
    assert.equal(press(state, 'g').scroll, 0)
  }
})

test('real HTTP session detail, metrics and snapshot namespace; reject mismatches and 409 without fallback', async () => {
  let wrong = false
  let reversed = false
  const requests = []
  const server = createServer((req, res) => {
    requests.push(req.url)
    if (req.url === '/') return res.end('window.__AGENTTRACE_TOKEN__ = "identity"')
    assert.equal(req.headers.authorization, 'Bearer identity')
    res.setHeader('Content-Type', 'application/json')
    if (req.url === '/api/health') return res.end(JSON.stringify({ building: false }))
    const url = new URL(req.url, 'http://localhost')
    if (url.pathname === '/api/sessions') return res.end(JSON.stringify({ sessions: reversed ? [...sessions].reverse() : sessions }))
    if (!url.searchParams.has('agent')) { res.writeHead(409); return res.end('{}') }
    const data = { session_id: 'duplicate', adapter_id: wrong ? 'a' : url.searchParams.get('agent'), turns: [], traces: 1, spans: 2, errors: 0, unclosed: 0,
      tokens: { input: 1, output: 2 }, recent_traces: [], slowest: [] }
    res.end(JSON.stringify(data))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    let picked = explorerInit(base)
    picked = press(applyExplorerData(picked, await loadExplorer(picked)), 'j')
    reversed = true
    picked = applyExplorerData(picked, await loadExplorer(press(picked, 'r')))
    assert.equal(explorerItems(picked)[picked.selected].adapter_id, 'b')
    picked = enter(picked)
    assert.equal((await loadExplorer(picked)).adapter_id, 'b')
    const state = explorerInit(`duplicate ${base} --agent b`)
    assert.equal((await loadExplorer(state)).adapter_id, 'b')
    assert.equal((await loadExplorer({ ...state, page: 'metrics' })).adapter_id, 'b')
    const result = await snapshot(base, { sessionId: 'duplicate', adapterId: 'b' })
    assert.equal(result.adapterId, 'b'); assert.equal(result.totals.spans, 2)
    wrong = true
    await assert.rejects(loadExplorer(state), /adapter identity mismatch/)
    await assert.rejects(loadExplorer({ ...state, page: 'metrics' }), /adapter identity mismatch/)
    await assert.rejects(snapshot(base, { sessionId: 'duplicate', adapterId: 'b' }), /invalid server response/)
    const start = requests.length
    await assert.rejects(loadExplorer({ ...state, adapterId: '' }), /HTTP 409/)
    await assert.rejects(snapshot(base, { sessionId: 'duplicate' }), /HTTP 409/)
    assert.ok(requests.slice(start).every(path => path === '/api/health' || path.startsWith('/api/sessions/duplicate')))
    assert.ok(requests.includes('/api/sessions/duplicate?agent=b'))
    assert.ok(requests.includes('/api/sessions/duplicate/metrics?agent=b'))
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
})
