import assert from 'node:assert/strict'
import { test } from 'node:test'
import { explorerInit, explorerItems, explorerReduce, applyExplorerData,
  explorerRows, detailMatches, spanDetails, explorerSearchStatus } from '../hermes/tui-widgets/agenttrace.mjs'

const key = (s, ch = '', key = {}) => explorerReduce(s, { ch, key })
const search = (s, query) => {
  s = key(s, '/')
  s = { ...s, searchDraft: '' }
  s = key(s, query)
  return key(s, '', { return: true })
}
const sessions = [
  { session_id: 'alpha', adapter_id: 'hermes', cwd: '/projects/Alpha' },
  { session_id: 'beta', adapter_id: 'codex', cwd: '/projects/中文' },
  { session_id: 'gamma', adapter_id: 'hermes' }
]
const ready = () => applyExplorerData(explorerInit(), { sessions })

test('search editor captures command letters, cancels safely, deletes whole graphemes, bounds paste', () => {
  let s = key(ready(), '/')
  s = key(s, 'qrb')
  assert.equal(s.searchDraft, 'qrb')
  assert.equal(s.page, 'sessions')
  s = key(s, '', { escape: true })
  assert.equal(s.searching, false)
  assert.equal(s.query, '')
  assert.equal(key(s, 'q'), null)
  s = key(ready(), '/')
  s = key(s, '👨‍👩‍👧‍👦')
  s = key(s, '', { backspace: true })
  assert.equal(s.searchDraft, '')
  s = key(s, '中'.repeat(1000))
  assert.equal(Array.from(s.searchDraft).length, 256)
})

test('loaded-record search matches full IDs, adapter, CWD and Unicode, never regex', () => {
  assert.deepEqual(explorerItems(search(ready(), 'ALPHA')).map(s => s.session_id), ['alpha'])
  assert.deepEqual(explorerItems(search(ready(), '中文')).map(s => s.session_id), ['beta'])
  assert.equal(explorerItems(search(ready(), 'hermes')).length, 2)
  const s = search(ready(), '.*')
  assert.equal(explorerItems(s).length, 0)
  assert.match(explorerRows(s, 40, 8)[0], /No matches in loaded/)
  assert.match(explorerSearchStatus(s), /0 loaded matches/)
  assert.equal(explorerItems(search(s, '')).length, 3)
})

test('tree search includes folded descendants without destroying folds', () => {
  const s = { ...ready(), page: 'spans', traceId: 't', collapsed: ['root'], data: { spans: [
    { span_id: 'root', kind: 'task', name: 'turn' },
    { span_id: 'child', parent_span_id: 'root', kind: 'tool', name: 'needle', status: 'error' }
  ] } }
  assert.equal(explorerItems(s).length, 1)
  const filtered = search(s, 'needle')
  assert.deepEqual(explorerItems(filtered).map(s => s.span_id), ['child'])
  assert.deepEqual(filtered.collapsed, ['root'])
  assert.equal(explorerItems(search(filtered, '')).length, 1)
})

test('refresh follows stable identity across insertions, warns when the record disappears', () => {
  const s = { ...ready(), selected: 1 }
  const refreshed = applyExplorerData(s, { sessions: [{ session_id: 'new' }, ...sessions] })
  assert.equal(explorerItems(refreshed)[refreshed.selected].session_id, 'beta')
  const removed = applyExplorerData(refreshed, { sessions: [sessions[0]] })
  assert.equal(removed.selected, 0)
  assert.match(removed.notice, /previous record missing/)
})

test('back restores filter and selected identity even when parent data reorders', () => {
  let s = { ...search(ready(), 'hermes'), selected: 1 }
  s = key(s, '', { return: true })
  assert.equal(s.sessionId, 'gamma')
  s = applyExplorerData(s, { session_id: 'gamma', turns: [{ trace_id: 'one' }] })
  s = key(s, 'b')
  assert.equal(s.page, 'sessions')
  assert.equal(s.query, 'hermes')
  s = applyExplorerData(s, { sessions: [sessions[2], sessions[0], sessions[1]] })
  assert.equal(explorerItems(s)[s.selected].session_id, 'gamma')
  s = key(s, 'j')
  s = applyExplorerData(s, { sessions })
  assert.equal(explorerItems(s)[s.selected].session_id, 'alpha')
})

test('returning from lazy call detail reloads metadata and restores exact selection and filter', () => {
  let s = { ...ready(), page: 'spans', query: 'tool', data: { spans: [
    { span_id: 'a', kind: 'tool', name: 'first' }, { span_id: 'b', kind: 'tool', name: 'second' }
  ] }, selected: 1 }
  const data = s.data
  s = key(s, '', { return: true })
  assert.equal(s.page, 'detail')
  assert.equal(s.query, '')
  assert.equal(s.spanId, 'b')
  s = key(s, 'b')
  assert.equal(s.loading, true)
  s = applyExplorerData(s, data)
  assert.equal(s.selected, 1)
  assert.equal(s.query, 'tool')
  assert.equal(s.loading, false)
  assert.equal(s.data, data)
  assert.equal(s.anchor, undefined)
})

const detail = (output, width = 10) => ({ ...ready(), page: 'detail', spanId: 'call',
  data: { spans: [{ span_id: 'call', output }] }, detailWidth: width, detailHeight: 3 })

test('detail find matches literal regex characters and across display wrapping', () => {
  const text = 'prefix literal.*[abc] suffix'
  const s = search(detail(text, 8), 'literal.*[abc]')
  const matches = detailMatches(s)
  assert.equal(matches.length, 1)
  const lines = spanDetails(s.data.spans[0], 8)
  assert.equal(lines[matches[0]], 'prefix l')
  assert.equal(s.scroll, matches[0])
  const chinese = search(detail('前缀中文🙂后缀', 4), '中文🙂')
  assert.equal(detailMatches(chinese).length, 1)
})

test('detail next/previous wraps and no-match is explicit', () => {
  let s = search(detail('needle\nblank\nneedle\nblank\nneedle'), 'needle')
  const hits = detailMatches(s)
  assert.equal(hits.length, 3)
  assert.equal(s.matchRow, hits[0])
  s = key(s, 'n'); assert.equal(s.matchRow, hits[1])
  s = key(s, 'n'); assert.equal(s.matchRow, hits[2])
  s = key(s, 'n'); assert.equal(s.matchRow, hits[0])
  s = key(s, 'N'); assert.equal(s.matchRow, hits[2])
  s = search(s, 'missing')
  assert.match(s.notice, /no matching lines/)
})

test('batched /query keystrokes open the editor instead of losing the search', () => {
  let s = key(ready(), '/beta')
  assert.equal(s.searching, true)
  assert.equal(s.searchDraft, 'beta')
  s = key(s, '', { return: true })
  assert.deepEqual(explorerItems(s).map(item => item.session_id), ['beta'])
})


test('detail query punctuation is escaped, Ctrl+u clears the editor', () => {
  const punctuation = '[](){}.*+?^$|\\'
  let s = search(detail('literal ' + punctuation + ' tail', 8), punctuation)
  assert.equal(detailMatches(s).length, 1)
  s = key(s, '/')
  s = key(s, 'u', { ctrl: true })
  assert.equal(s.searchDraft, '')
  s = key(s, '', { return: true })
  assert.equal(s.query, '')
  assert.equal(s.notice, '')
})


test('detail refresh follows displayed span identity; back resolves parent bookmark against new order', () => {
  const a = { span_id: 'a', kind: 'tool', name: 'a' }
  const b = { span_id: 'b', kind: 'tool', name: 'b' }
  const initial = { ...ready(), page: 'spans', selected: 1, data: { spans: [a, b] } }
  let s = key(initial, '', { return: true })
  s = applyExplorerData(s, { spans: [b, a] })
  assert.equal(s.spanId, 'b')
  assert.equal(s.notice, '')
  s = applyExplorerData(key(s, 'b'), { spans: [b, a] })
  assert.equal(explorerItems(s)[s.selected].span_id, 'b')
  s = key(initial, '', { return: true })
  s = applyExplorerData(s, { spans: [b] })
  assert.equal(s.notice, '')
  s = applyExplorerData(key(s, 'b'), { spans: [b, a] })
  assert.equal(explorerItems(s)[s.selected].span_id, 'b')
  s = key(initial, '', { return: true })
  s = applyExplorerData(s, { spans: [a] })
  assert.match(s.notice, /displayed span missing/)
  assert.equal(s.spanId, 'b')
  s = applyExplorerData(key(s, 'b'), { spans: [a] })
  assert.match(s.notice, /previous record missing/)
})

test('literal queries preserve leading, trailing and whitespace-only text', () => {
  const s = { ...ready(), data: { sessions: [
    { session_id: ' y ' }, { session_id: 'yellow' }, { session_id: 'x y z' }
  ] } }
  const filtered = search(s, ' y ')
  assert.equal(filtered.query, ' y ')
  assert.deepEqual(explorerItems(filtered).map(item => item.session_id), [' y ', 'x y z'])
  assert.equal(search(s, ' ').query, ' ')
  const found = search(detail('prefix y suffix\nyellow'), ' y ')
  assert.equal(detailMatches(found).length, 1)
})

test('Unicode match locations stay exact at grapheme wrap boundaries', () => {
  for (const prefix of ['中文', '𠀀𠀀', '🇺🇸🇺🇸', '👨‍👩‍👧‍👦👨‍👩‍👧‍👦', 'éééé']) {
    const s = search(detail(prefix + 'needle', 4), 'needle')
    const lines = spanDetails(s.data.spans[0], 4)
    assert.equal(detailMatches(s).length, 1)
    assert.equal(lines[detailMatches(s)[0]], 'need')
  }
})
