import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { test } from 'node:test'
import register, { snapshot, startPolling, cardRows, fmtDur } from '../hermes/tui-widgets/agenttrace.mjs'

const sleep = ms => new Promise(r => setTimeout(r, ms))
const totals = { traces: 5, spans: 9, errors: 2, unclosed: 1, tokens: { input: 1234, output: 45 } }
const turns = [
  { adapter_id: 'hermes', status: 'incomplete', unclosed: 1, duration_ms: 9000 },
  { adapter_id: 'hermes', status: 'ok', unclosed: 0, duration_ms: null },
  { adapter_id: 'hermes', status: 'error', unclosed: 2, duration_ms: 99999 },
  { adapter_id: 'hermes', status: 'error', unclosed: 0, duration_ms: 2000 },
  { adapter_id: 'hermes', status: 'ok', unclosed: 0, duration_ms: 0 },
  { adapter_id: 'hermes', status: 'ok', unclosed: 0, duration_ms: 1000 }
]

async function fixture(fn) {
  let token = 'fixture-one'
  let status = 200
  let malformed = false
  let roots = 0
  const server = createServer((req, res) => {
    if (req.url === '/') {
      roots++
      res.end(`window.__AGENTTRACE_TOKEN__ = "${token}"`)
      return
    }
    if (req.headers.authorization !== `Bearer ${token}`) {
      res.writeHead(401); res.end('{"error":"unauthorized"}'); return
    }
    res.writeHead(status, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(malformed ? {} : req.url.startsWith('/api/overview') ? totals : req.url === '/api/health' ? { building: false, last_scan: { problems: 0 } } : { traces: turns }))
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  try {
    await fn(`http://127.0.0.1:${server.address().port}`, {
      rotate: () => { token = 'fixture-two' },
      fail: value => { status = value },
      malformed: () => { malformed = true },
      roots: () => roots
    })
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)) }
}

test('API: missing/running durations do not become zero latency; preserve newest order', async () => {
  await fixture(async base => {
    const snap = await snapshot(base)
    assert.deepEqual(snap.durations, [1000, 0, 2000])
    assert.equal(snap.p50, 1000)
    assert.equal(snap.sampleSize, 3)
    assert.equal(snap.last.status, 'incomplete')
    assert.equal(snap.totals.errors, 2)
  })
})

test('API: rebootstrap token after rotation without reopening widget', async () => {
  await fixture(async (base, control) => {
    await snapshot(base)
    control.rotate()
    await snapshot(base)
    assert.equal(control.roots(), 2)
  })
})

test('API: HTTP failures and bad schemas are errors, never a successful zero card', async () => {
  await fixture(async (base, control) => {
    control.fail(503)
    await assert.rejects(snapshot(base), /HTTP 503/)
    control.fail(200); control.malformed()
    await assert.rejects(snapshot(base), /invalid server (health )?response/)
  })
})

test('API: caller cancellation reaches fetch', async () => {
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(snapshot('http://127.0.0.1:1', { signal: controller.signal }), { name: 'AbortError' })
})

test('polling: single flight, stale data retained, recover, cleanup stops requests', async () => {
  let inflight = 0, max = 0, calls = 0
  const phases = []
  const snap = { updatedAt: Date.now() }
  const cleanup = startPolling('fixture', phase => phases.push(phase), {
    delay: 5,
    load: async () => {
      calls++; inflight++; max = Math.max(max, inflight)
      await sleep(20); inflight--
      if (calls === 2) throw new Error('fixture offline')
      return snap
    }
  })
  try {
    for (let i = 0; i < 100 && phases.length < 3; i++) await sleep(5)
    assert.equal(max, 1)
    assert.deepEqual(phases.slice(0, 3).map(p => p.kind), ['ready', 'error', 'ready'])
    assert.equal(phases[1].snap, snap)
  } finally { cleanup() }
  const before = calls
  await sleep(40)
  assert.equal(calls, before)
})

test('polling: cleanup aborts pending request; late result cannot set state', async () => {
  let signal, resolve
  const phases = []
  const cleanup = startPolling('fixture', p => phases.push(p), {
    load: (_base, options) => { signal = options.signal; return new Promise(r => { resolve = r }) }
  })
  cleanup()
  assert.equal(signal.aborted, true)
  resolve({ updatedAt: 0 })
  await sleep(10)
  assert.equal(phases.length, 0)
})

test('layout: four rows at every width and phase; controls never reach terminal', async () => {
  await fixture(async base => {
    const snap = await snapshot(base)
    snap.last.adapter_id = '\u001b[2J中文long-adapter'
    for (const width of [1, 10, 18, 22, 30, 40]) {
      for (const phase of [
        { kind: 'ready', snap },
        { kind: 'error', message: 'HTTP 503', snap },
        { kind: 'error', message: 'offline', hint: 'start server' }
      ]) {
        const rows = cardRows(phase, width, (_values, w) => '▁'.repeat(w))
        assert.equal(rows.length, 4)
        assert.ok(rows.every(row => row.length === width))
        assert.ok(rows.every(row => !/[\u0000-\u001f]/.test(row)))
      }
    }
    const rows = cardRows({ kind: 'ready', snap }, 40, (_v, w) => '▁'.repeat(w))
    assert.match(rows[0], /all agents/)
    assert.match(rows[1], /p50\/3/)
    assert.match(rows[3], /incomplete/)
  })
})

test('registration: ambient dock, invalid URL refused, responsive inner width', () => {
  let app
  register({ defineWidgetApp: cfg => { app = cfg }, h: (type, props, ...children) => ({ type, props, children }), Dialog: 'Dialog' })
  assert.equal(app.mode, 'ambient')
  for (const url of ['file:///etc/passwd', 'http://user:password@localhost', 'invalid']) assert.equal(app.init(url), null)
  for (const cols of [24, 32, 40, 80, 110]) {
    const tree = app.render({ cols, state: app.init(''), t: {} })
    assert.ok(tree.props.width <= cols)
    assert.equal(tree.children[0].props.width, tree.props.width - 6)
  }
})

test('duration formatting: zero valid, null unknown, minute carry correct', () => {
  assert.equal(fmtDur(0), '0ms')
  assert.equal(fmtDur(null), '-')
  assert.equal(fmtDur(119999), '2m00s')
})


test('real HTTP: closing polling cancels a pending fetch without state updates', async () => {
  let began, closed
  const started = new Promise(r => { began = r })
  const disconnected = new Promise(r => { closed = r })
  const server = createServer((req, res) => {
    began()
    res.on('close', closed)
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  const phases = []
  const stop = startPolling(`http://127.0.0.1:${server.address().port}`, p => phases.push(p))
  try {
    await started
    stop()
    await Promise.race([disconnected, sleep(1000).then(() => { throw new Error('HTTP socket not closed') })])
    assert.equal(phases.length, 0)
  } finally {
    stop(); server.closeAllConnections(); await new Promise(r => server.close(r))
  }
})

test('real HTTP: persistent 401 retries once and cancels pending siblings', async () => {
  let roots = 0, pending = 0, closed = 0
  const server = createServer((req, res) => {
    if (req.url === '/') {
      roots++; res.end('window.__AGENTTRACE_TOKEN__ = "fixture-rejected"'); return
    }
    if (req.url === '/api/health') { res.end(JSON.stringify({ building: false })); return }
    if (req.url.startsWith('/api/traces')) {
      pending++; res.on('close', () => { closed++ }); return
    }
    res.writeHead(401); res.end('{}')
  })
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  try {
    await assert.rejects(snapshot(`http://127.0.0.1:${server.address().port}`), /HTTP 401/)
    for (let i = 0; i < 100 && closed < pending; i++) await sleep(5)
    assert.equal(roots, 2)
    assert.ok(pending > 0)
    assert.equal(closed, pending)
  } finally { server.closeAllConnections(); await new Promise(r => server.close(r)) }
})
