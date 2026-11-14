import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createServer } from 'node:http'
import register, { explorerInit, explorerResource, explorerReduce, applyExplorerData, loadExplorer, explorerRows } from '../hermes/tui-widgets/agenttrace.mjs'

const enter = s => explorerReduce(s, { key: { return: true } })

test('SDK drilldown carries selected adapter and session; HTTP foreign payloads rejected without fallback', async () => {
  const requests = []
  let foreign = false, ambiguous = false
  const server = createServer((req, res) => {
    requests.push(req.url)
    if (req.url === '/') return res.end('window.__AGENTTRACE_TOKEN__ = "synthetic"')
    assert.equal(req.headers.authorization, 'Bearer synthetic')
    const url = new URL(req.url, 'http://localhost')
    assert.equal(url.searchParams.get('agent'), 'b/%2F+')
    if (url.pathname.startsWith('/api/traces/')) assert.equal(url.searchParams.get('session'), 'same/%2F+')
    if (ambiguous) { res.writeHead(409); return res.end('{}') }
    const data = { adapter_id: foreign ? 'a' : 'b/%2F+', session_id: 'same/%2F+', trace_id: 't/%2F',
      turns: [{ trace_id: 't/%2F' }], spans: [{ span_id: 'p/%2F' }], span_id: 'p/%2F', input: 'synthetic-private' }
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data))
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  try {
    const apps = new Map()
    register({ Overlay: 'Overlay', updateWidget: () => {}, defineWidgetApp: app => { apps.set(app.id, app); return app }, openWidget: () => {} })
    const app = apps.get('tracex')
    let state = explorerInit(`http://127.0.0.1:${server.address().port}`)
    state = app.reduce(applyExplorerData(state, { sessions: [{ adapter_id: 'b/%2F+', session_id: 'same/%2F+' }] }), { key: { return: true } })
    state = enter(applyExplorerData(state, await loadExplorer(state)))
    const summary = state
    assert.equal(explorerResource(state), '/api/traces/t%2F%252F?summary=1&agent=b%2F%252F%2B&session=same%2F%252F%2B')
    state = enter(applyExplorerData(state, await loadExplorer(state)))
    assert.equal(explorerResource(state), '/api/traces/t%2F%252F/spans/p%2F%252F?agent=b%2F%252F%2B&session=same%2F%252F%2B')
    const detail = await loadExplorer(state)
    assert.equal(detail.adapter_id, 'b/%2F+')
    assert.equal(detail.spans[0].input, 'synthetic-private')
    foreign = true
    await assert.rejects(loadExplorer(summary), /adapter identity mismatch/)
    await assert.rejects(loadExplorer(state), /adapter identity mismatch/)
    ambiguous = true
    const start = requests.length
    await assert.rejects(loadExplorer(state), /HTTP 409/)
    assert.equal(requests.length, start + 1) // cached token; exact resource only, no global fallback
    assert.equal(requests.at(-1), explorerResource(state))
    const lines = explorerRows({ ...state, showExport: true, loading: false, data: detail }, 1000, 100)
    const command = lines.find(line => line.includes('scripts/export-span.py'))
    assert.ok(command.includes("--agent 'b/%2F+'"))
    assert.ok(command.includes("--session 'same/%2F+'"))
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
})
