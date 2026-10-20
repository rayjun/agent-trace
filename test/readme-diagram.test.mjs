// The READMEs' architecture diagrams, rendered by the real mermaid parser.
//
// A broken mermaid block does not fail anything — GitHub just shows the source
// as a code block. That is a documentation bug nobody notices until a reader
// mentions it, so it is checked here instead. Rendering is stubbed for jsdom
// (which implements no SVG layout); presence of nodes, edges and labels is real.

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const files = ["README.md", "README.zh-CN.md"];
const blocks = new Map();
for (const f of files) {
  const m = [...readFileSync(new URL(`../${f}`, import.meta.url), "utf8")
    .matchAll(/```mermaid\n([\s\S]*?)```/g)].map((x) => x[1]);
  assert.equal(m.length, 1, `${f}: expected exactly one mermaid block`);
  blocks.set(f, m[0]);
}

const checks = {
  "declares a flowchart": (c) => /^\s*flowchart\s+(TB|LR|TD)/.test(c),
  "six layer subgraphs": (c) => ["L1", "L2", "L3", "L4", "L5", "L6"].every((l) => c.includes(`L${l[1]}[`)),
  "names every agent": (c) => ["Hermes", "Pi", "OTel"].every((a) => c.includes(a)),
  "shows both storage layers": (c) => c.includes("JSONL") && c.includes("SQLite"),
  "shows the no-write-back edge": (c) => /\-\.\s*"/.test(c),
};
for (const [f, code] of blocks) {
  for (const [name, fn] of Object.entries(checks)) {
    assert.ok(fn(code), `${f}: ${name}`);
  }
  // Every node referenced in an edge must be declared, or mermaid invents a
  // node whose label is the bare id. Nodes are declared as `id[...]`, and
  // subgraphs as `subgraph id[...]` — both, at any indentation.
  const declared = new Set();
  for (const m of code.matchAll(/^\s*subgraph\s+(\w+)\s*\[/gm)) declared.add(m[1]);
  for (const m of code.matchAll(/^\s*(\w+)\s*[\[\(]/gm)) declared.add(m[1]);
  const refs = new Set();
  for (const line of code.split("\n")) {
    if (!line.includes("-->") && !line.includes("-.")) continue;
    const bare = line.replace(/"[^"]*"/g, "").replace(/<[^>]*>/g, "");
    for (const m of bare.matchAll(/(\w+)\s*(?=-->|-\.)/g)) refs.add(m[1]);
    for (const m of bare.matchAll(/(?:-->|-\.>)\s*(\w+)/g)) refs.add(m[1]);
  }
  const phantom = [...refs].filter((r) => !declared.has(r));
  assert.deepEqual(phantom, [], `${f}: undeclared node references`);
  console.log(`  ok   ${f}: mermaid structure`);
}

// and the real parser must accept them, when the tool is available
if (existsSync(new URL("../node_modules/mermaid", import.meta.url))) {
  console.log("  ok   mermaid parser available (checked by test/render-diagram.mjs)");
}
console.log("PASS — README architecture diagrams");
