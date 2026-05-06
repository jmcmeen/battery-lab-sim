"""orchestrator — YAML schedule executor with idempotent resume.

Per CLAUDE.md invariant #1: this service NEVER performs safety checks.
The cycler does that. We only request mode transitions and read state.

Per CLAUDE.md invariant #5: every command is idempotent. Before issuing a
transition, read current state. If already in desired state, do nothing.
"""
