# Examples

Runnable starting points. Each is self-contained and needs no network.

## 1. Give any OTel-instrumented app a trace view

Your app already exports OTLP? Dump it to a file and import:

```bash
# whatever writes OTLP JSON (collector file exporter, `otlp-json` sink, ...)
agenttrace otlp import ./otel-dump.json --out ~/.agent-trace
agenttrace index
agenttrace serve          # the app's spans now appear with the rest
```

`gen_ai.request.model`, `gen_ai.usage.*` and `gen_ai.operation.name` are read
from the OTel side; everything OTel cannot express is simply absent rather
than invented.

## 2. Trace a LangGraph agent

`examples/langgraph_agent.py` — a complete, dependency-free simulation of the
callback sequence LangGraph emits (swap the fake callbacks for a real
`graph.invoke(..., config={"callbacks": [handler]})`):

```bash
python3 examples/langgraph_agent.py
agenttrace index
agenttrace serve
```

## 3. Turn a bad run into a regression test

The workflow the platform exists for:

```bash
agenttrace serve                       # find the failing trace in the UI, note its id
agenttrace index

python3 - <<'PY'
from pathlib import Path
from core.storage.sqlite_store import TraceStore
from evaluation import datasets as ds, experiments as ex
store = TraceStore(Path.home() / ".agenttrace" / "agenttrace.db")
d = ds.create_dataset(store, "coding-regressions")
ds.from_trace(store, d["dataset_id"], "<trace-id>", {"tests_pass": True})
e = ex.create_experiment(store, "baseline", d["dataset_id"],
                         ["det:tests_pass", "det:claimed_vs_actual",
                          "traj:retry_loops", "traj:error_recovery"],
                         {"model": "gpt-4o"})
print(ex.run_experiment(store, e["experiment_id"]))
PY
```

`det:claimed_vs_actual` is the one to watch: it fires when the agent's final
message claims success while the last test run says failure.

## 4. Export traces to Grafana / Tempo / Jaeger

```bash
agenttrace otlp export --limit 50 > traces.otlp.json
# POST traces.otlp.json to your OTLP endpoint
```

We emit `gen_ai.*` attributes, so the receiving backend renders model,
tokens and operation name without knowing agent-trace exists.

## 5. Score every adapter in your install

```bash
agenttrace adapter list
for a in hermes codex pi otlp langgraph; do
  agenttrace adapter test "$a" || echo "FAILED: $a"
done
```

Each fixture is a real event stream produced by that adapter's own code path
(see `test/fixtures/adapters/`), so this checks the shipped behaviour, not a
hand-written ideal.
