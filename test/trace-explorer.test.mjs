import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { test } from 'node:test'
import register, { explorerInit, explorerResource, explorerReduce, explorerItems,
  loadExplorer, spanTree, spanDetails, explorerRows, terminalSafe, wrapExplorer } from '../hermes/tui-widgets/agenttrace.mjs'

const key = (state, ch = '', key = {}) => explorerReduce(state, { ch, key })
const spans = [
  { span_id: 'tool', parent_span_id: 'root', kind: 'tool', name: 'terminal', status: 'error', duration_ms: 50,
    input: { command: 'test' }, output: '中文结果🙂', error: { message: 'fixture failure' } },
  { span_id: 'root', kind: 'task', name: 'turn', status: 'error', duration_ms: 100 },
  { span_id: 'llm', parent_span_id: 'root', kind: 'llm', name: 'model', status: 'ok', duration_ms: 30, tokens: { input: 12, output: 4 } },
  { span_id: 'orphan', parent_span_id: 'missing', kind: 'tool', name: 'orphan', status: 'incomplete' }
]

test('explicit session identity, URL normalization, encoded identifiers', () => {
  const state = explorerInit('profile/session%id http://127.0.0.1:8741/')
  assert.equal(state.sessionId, 'profile/session%id')
  assert.equal(explorerResource(state), '/api/sessions/profile%2Fsession%25id')
  assert.equal(explorerInit('').page, 'sessions')
  assert.equal(explorerInit('http://localhost:8741').sessionId, '')
  assert.equal(explorerInit('id file:///etc/passwd'), null)
  assert.equal(explorerInit('a b c'), null)
})

test('tree builds parent-first without losing orphans; folds subtree; cycles cannot hang', () => {
  assert.deepEqual(spanTree(spans).map(s => [s.span_id, s.depth]), [['root', 0], ['tool', 1], ['llm', 1], ['orphan', 0]])
  assert.deepEqual(spanTree(spans, ['root']).map(s => s.span_id), ['root', 'orphan'])
  const cycle = [{ span_id: 'a', parent_span_id: 'b' }, { span_id: 'b', parent_span_id: 'a' }]
  assert.equal(spanTree(cycle).length, 2)
  assert.equal(spanTree(cycle, ['a']).length, 1)
})

test('navigation: sessions -> exact turns -> tree -> details -> back -> close', () => {
  let state = { ...explorerInit(), loading: false, data: { sessions: [{ session_id: 'selected-session' }] } }
  state = key(state, '', { return: true })
  assert.equal(state.page, 'turns'); assert.equal(state.sessionId, 'selected-session')
  state = { ...state, loading: false, data: { turns: [{ trace_id: 'trace/a' }] } }
  state = key(state, '', { return: true })
  assert.equal(state.page, 'spans'); assert.equal(state.traceId, 'trace/a')
  assert.equal(explorerResource(state), '/api/traces/trace%2Fa?summary=1')
  state = { ...state, loading: false, data: { spans } }
  state = key(state, ' ')
  assert.deepEqual(explorerItems(state).map(s => s.span_id), ['root', 'orphan'])
  state = key(state, ' ')
  state = key(state, 'j')
  state = key(state, '', { return: true })
  assert.equal(state.page, 'detail'); assert.equal(state.spanId, 'tool')
  assert.ok(spanDetails(spans[0], 40).some(s => s.includes('fixture failure')))
  state = key(state, 'b'); assert.equal(state.page, 'spans')
  state = key(state, 'e'); assert.ok(explorerItems(state).every(s => s.status === 'error'))
  state = key(state, 'b'); assert.equal(state.page, 'turns')
  state = key(state, 'b'); assert.equal(state.page, 'sessions')
  assert.equal(key(state, '', { escape: true }), null)
  assert.equal(key({ ...state, loading: true }, 'q'), null)
})

test('detail scrolling is bounded by actual content, not an arbitrary sentinel', () => {
  let state = { ...explorerInit('s'), page: 'detail', loading: false, spanId: 'tool', data: { spans },
    detailWidth: 24, detailHeight: 8 }
  for (let i = 0; i < 100; i++) state = key(state, '', { pageDown: true })
  const length = spanDetails(spans[0], 24).length
  assert.equal(state.scroll, Math.max(0, length - 8))
  assert.equal(explorerRows(state, 24, 8).length, 8)
  for (let i = 0; i < 100; i++) state = key(state, 'k')
  assert.equal(state.scroll, 0)
  state = key(state, 'o')
  assert.ok(state.scroll > 0)
  assert.ok(explorerRows(state, 24, 8).some(row => row === '[output]'))
  state = key(state, 'g'); assert.equal(state.scroll, 0)
})

test('Unicode content preserved, terminal controls stripped; rows fit cell width', () => {
  assert.equal(terminalSafe('\x1b[31m中文\x1b[0m\x07\u202eevil'), '中文evil')
  assert.deepEqual(wrapExplorer('中文🙂abc', 4), ['中文', '🙂ab', 'c'])
  const lines = spanDetails(spans[0], 20)
  assert.ok(lines.some(row => row.includes('中文结果🙂')))
  assert.ok(spanDetails({ span_id: 'x' }, 20).includes('(not captured)'))
})

test('HTTP drilldown: exact session/trace scope and schema/identity checks', async () => {
  let wrong = false
  const routes = []
  const server = createServer((req, res) => {
    routes.push(req.url)
    if (req.url === '/') { res.end('window.__AGENTTRACE_TOKEN__ = "fixture-explorer"'); return }
    assert.equal(req.headers.authorization, 'Bearer fixture-explorer')
    res.setHeader('Content-Type', 'application/json')
    if (req.url.startsWith('/api/sessions?')) res.end(JSON.stringify({ sessions: [{ session_id: 's/a' }] }))
    else if (req.url === '/api/sessions/s%2Fa') res.end(JSON.stringify({ session_id: wrong ? 'other' : 's/a', turns: [{ trace_id: 't' }] }))
    else if (req.url === '/api/traces/t?summary=1') res.end(JSON.stringify({ trace_id: 't', session_id: 's/a', spans }))
    else { res.writeHead(404); res.end('{}') }
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const state = explorerInit(`s/a ${base}`)
    const session = await loadExplorer(state)
    assert.equal(session.turns[0].trace_id, 't')
    assert.equal((await loadExplorer({ ...state, page: 'spans', traceId: 't' })).spans.length, 4)
    wrong = true
    await assert.rejects(loadExplorer(state), /identity mismatch/)
    await assert.rejects(loadExplorer({ ...state, sessionId: 'missing' }), /HTTP 404/)
    assert.ok(routes.includes('/api/sessions/s%2Fa'))
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)) }
})

test('SDK registration: both apps and modal dimensions', () => {
  const apps = new Map(), effects = []
  const sdk = {
    Box: 'Box', Text: 'Text', Dialog: 'Dialog', Overlay: 'Overlay', ShimmerRows: 'ShimmerRows',
    React: { useEffect: fn => effects.push(fn) },
    h: (type, props, ...children) => ({ type, props, children }),
    defineWidgetApp: app => { apps.set(app.id, app); return app }, updateWidget: () => {}
  }
  register(sdk)
  assert.equal(apps.get('agenttrace').mode, 'ambient')
  const app = apps.get('tracex')
  assert.equal(app.mode, 'modal')
  for (const cols of [24, 32, 80, 110]) {
    const tree = app.render({ cols, rows: 30, state: explorerInit(), t: { color: {} } })
    const dialog = tree.children[0]
    assert.ok(dialog.props.width <= cols)
    const body = dialog.children.find(child => typeof child.type === 'function')
    assert.ok(body.props.width <= cols - 6)
    assert.ok(body.props.height <= 30 - 12)
  }
})


test('SDK async delivery: changing session aborts old fetch; old reply cannot replace scope', async () => {
  let oldStarted, oldClosed
  const started = new Promise(r => { oldStarted = r })
  const disconnected = new Promise(r => { oldClosed = r })
  const server = createServer((req, res) => {
    if (req.url === '/') { res.end('window.__AGENTTRACE_TOKEN__ = "fixture-ui"'); return }
    if (req.url.startsWith('/api/sessions?')) {
      oldStarted(); res.on('close', oldClosed); return
    }
    res.end(JSON.stringify({ session_id: 'selected', turns: [] }))
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  const base = `http://127.0.0.1:${server.address().port}`
  const apps = new Map()
  let effects = [], current = explorerInit(base)
  const sdk = {
    Box: 'Box', Text: 'Text', Dialog: 'Dialog', Overlay: 'Overlay', ShimmerRows: 'ShimmerRows',
    React: { useEffect: fn => effects.push(fn) },
    h: (type, props, ...children) => ({ type, props, children }),
    defineWidgetApp: app => { apps.set(app.id, app); return app },
    updateWidget: (_app, fn) => { current = fn(current) }
  }
  register(sdk)
  const app = apps.get('tracex')
  const mount = () => {
    effects = []
    const tree = app.render({ cols: 80, rows: 30, state: current, t: { color: {} } })
    const body = tree.children[0].children.find(c => typeof c.type === 'function')
    body.type(body.props)
    return effects.map(fn => fn()).filter(fn => typeof fn === 'function')
  }
  let cleanups = mount()
  try {
    await started
    cleanups.forEach(fn => fn())
    current = explorerInit(`selected ${base}`)
    cleanups = mount()
    await disconnected
    for (let i = 0; i < 100 && current.loading; i++) await new Promise(r => setTimeout(r, 5))
    assert.equal(current.loading, false)
    assert.equal(current.sessionId, 'selected')
    assert.equal(current.data.session_id, 'selected')
    assert.equal(current.error, '')
  } finally {
    cleanups.forEach(fn => fn())
    server.closeAllConnections(); await new Promise(r => server.close(r))
  }
})


test('wide Unicode cannot disappear behind truncation: flags, keycaps, supplementary CJK', () => {
  for (const grapheme of ['🇺🇸', '1️⃣', '𠀀', '👨‍👩‍👧‍👦']) {
    assert.deepEqual(wrapExplorer(grapheme.repeat(3), 4), [grapheme.repeat(2), grapheme])
    const payload = grapheme.repeat(12)
    const lines = spanDetails({ span_id: 'x', output: payload }, 6)
    const wrapped = wrapExplorer(payload, 6)
    assert.equal(wrapped.length, 4)
    assert.equal(wrapped.join(''), payload)
    assert.ok(wrapped.every(row => lines.includes(row)))
  }
  assert.deepEqual(wrapExplorer('e\u0301'.repeat(5), 4), ['e\u0301'.repeat(4), 'e\u0301'])
})
