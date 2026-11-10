/**
 * Headless harness for the agent-trace TUI widget.
 *
 * Widgets run inside the Hermes TUI bundle, so this file supplies what the
 * bundle would: a tiny React (useState/useEffect), `h`, and inert component
 * tags. It mounts the widget, runs its effect against the REAL local server,
 * re-renders once the data lands, and prints the card as text rows.
 *
 *   node scripts/widget-harness.mjs [base-url]
 */
const base = process.argv[2] ?? 'http://127.0.0.1:8741'
const { default: register, snapshot } = await import(
  new URL('../hermes/tui-widgets/agenttrace.mjs', import.meta.url).href
)

// -- minimal React ----------------------------------------------------------
const hooks = []
let cursor = 0
let app = null

const React = {
  useState(init) {
    const i = cursor++

    if (!(i in hooks)) hooks[i] = typeof init === 'function' ? init() : init

    return [hooks[i], value => { hooks[i] = typeof value === 'function' ? value(hooks[i]) : value }]
  },
  useEffect(fn) {
    const i = cursor++

    if (!hooks[i]) hooks[i] = fn
  }
}

const h = (type, props, ...children) => ({ type, props: props ?? {}, children: children.flat(Infinity) })

// Same contract as the bundle's chart helper: exactly `width` cells.
const sparkline = (series, width = series.length) => {
  const blocks = '▁▂▃▄▅▆▇█'
  const view = series.slice(-width)

  if (!view.length) return ' '.repeat(width)

  const min = Math.min(...view)
  const range = Math.max(...view) - min || 1

  return view.map(v => blocks[Math.min(7, Math.floor(((v - min) / range) * 8))]).join('').padStart(width)
}

const sdk = {
  Box: 'Box',
  Dialog: 'Dialog',
  React,
  ShimmerRows: 'ShimmerRows',
  Text: 'Text',
  defineWidgetApp: cfg => (app = cfg),
  h,
  sparkline
}

const t = { color: { completionBg: '', error: '', label: '', muted: '', ok: '', primary: '', text: '' } }

register(sdk)

if (!app) throw new Error('register() did not define a widget app')

const state = app.init(process.argv[3] ?? '')

if (!state) throw new Error('init() refused the launch')

const findBody = node => {
  if (!node || typeof node !== 'object') return null
  if (typeof node.type === 'function' && node.type.name === 'Body') return node
  for (const child of node.children ?? []) {
    const hit = findBody(child)

    if (hit) return hit
  }

  return null
}

const tree = app.render({ cols: 80, state, t })
const bodyEl = findBody(tree)

if (!bodyEl) throw new Error('render() produced no Body component')

const textOf = node => {
  if (node === null || node === undefined) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node !== 'object') return ''
  if (node.type === 'Box') return node.children.map(textOf).join(' ')

  return (node.children ?? []).map(textOf).join('')
}

const rowsOf = node => {
  if (!node || typeof node !== 'object') return []
  if (node.type === 'ShimmerRows') return [`<shimmer rows=${node.props?.rows?.length ?? 0}>`]
  if (node.type === 'Text') return [textOf(node)]
  if (node.type === 'Box') {
    if (node.props?.flexDirection === 'row') return [node.children.map(textOf).join('  ')]

    return node.children.flatMap(rowsOf)
  }

  return (node.children ?? []).flatMap(rowsOf)
}

const print = (label, node) => {
  console.log(`\n--- ${label} ---`)

  for (const row of rowsOf(node)) console.log('|' + row.padEnd(40).slice(0, 40) + '|')
}

// Phase 1: the loading card (what the dock shows on the first frame).
cursor = 0
print('loading', bodyEl.type(bodyEl.props))

// Phase 2: run the polling effect against the live server, then re-render.
const effect = hooks.find(fn => typeof fn === 'function')
const cleanup = effect()
await new Promise(resolve => setTimeout(resolve, 1500))
cursor = 0
print('ready', bodyEl.type(bodyEl.props))

cleanup()

const direct = await snapshot(base)
console.log('\nsnapshot() =', JSON.stringify({
  traces: direct.totals.traces,
  errors: direct.totals.errors,
  p50: direct.p50,
  slots: direct.durations.length,
  last: direct.last && `${direct.last.adapter_id} ${direct.last.duration_ms}ms`
}))
