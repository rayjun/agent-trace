import assert from 'node:assert/strict'
import { explorerInit, explorerReduce, loadExplorer, explorerItems, explorerRows, spanDetails } from '../hermes/tui-widgets/agenttrace.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:8741'
const sessionId = process.argv[3] ?? ''
let state = explorerInit(`${sessionId} ${base}`.trim())
if (!state) throw new Error('invalid arguments')
const load = async () => {
  state = { ...state, data: await loadExplorer(state), loading: false }
  console.log(`${state.page}: ${explorerItems(state).length} indexed records`)
}
const enter = () => { state = explorerReduce(state, { key: { return: true } }) }
await load()
if (!sessionId) {
  assert.ok(state.data.sessions.length, 'no indexed sessions')
  const hermes = state.data.sessions.findIndex(s => s.adapter_id === 'hermes')
  state.selected = Math.max(0, hermes)
  enter(); await load()
}
assert.equal(state.data.session_id, state.sessionId)
assert.ok(state.data.turns.length, 'no turns in selected session')
state.selected = state.data.turns.length - 1
enter(); await load()
assert.equal(state.data.session_id, state.sessionId)
assert.ok(state.data.spans.length, 'no spans in selected trace')
const selected = explorerItems(state).findIndex(s => ['llm', 'tool'].includes(s.kind))
state.selected = Math.max(0, selected)
enter()
const span = state.data.spans.find(s => s.span_id === state.spanId)
const lines = spanDetails(span, 64)
assert.ok(lines.includes('[input]') && lines.includes('[output]') && lines.includes('[error]'))
assert.ok(explorerRows(state, 64, 10).length > 0)
console.log(JSON.stringify({ session: state.sessionId, trace: state.traceId, kind: span.kind,
  status: span.status, detailLines: lines.length, inputCaptured: span.input != null,
  outputCaptured: span.output != null, errorCaptured: span.error != null }))
console.log('LIVE EXPLORER PASS (payload content intentionally not printed)')
