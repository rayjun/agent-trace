"""agent-trace platform core — the code every higher layer shares.

Layout:
    core/schema/     canonical v2 event model + validation (stdlib only)
    core/convert/    v1 flat-record -> v2 canonical event conversion

Design constraint: this package and everything under it imports the standard
library ONLY. Adapters run inside host processes we do not control (the
deployed Hermes plugin is a byte copy of a single file; the Codex hook is a
fresh 50 ms process), so the shared layers can never afford a third-party
dependency. Pydantic/FastAPI belong to server/, which runs in our own
process — see docs/decisions.md.
"""
