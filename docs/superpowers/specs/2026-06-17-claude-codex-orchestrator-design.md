# Claude–Codex collaboration: design notes

Date: 2026-06-17
Status: implemented and verified.

> Direction change (same day): during testing the user wanted to keep working in the Codex
> interface they like, so the main path moved from "headless script driver" to
> **Codex as home + Claude as an MCP planning advisor**. See [README.md](../../../README.md)
> and `claude_planner_mcp.py` for the final design. The headless `orchestrate.py` orchestrator
> described below still works and is kept as an optional batch mode.

## Goal

Cost arbitrage: Claude is expensive, Codex is cheap. Let Claude do only the small amount of
high-level planning, and push the bulky coding work to Codex. The key metric is to spend as few
Claude tokens as possible.

## Final architecture (main path)

You work in the Codex interface. Codex does all execution. When a task needs high-level planning
or an architecture decision, Codex calls a tool from the `claude-planner` MCP server, which runs
`claude -p` outside Codex's sandbox (so it has network) and returns the result.

- `claude_planner_mcp.py` — stdio MCP server exposing `plan` and `consult` tools. Standard library only.
- `prompts/claude.md` — a `/claude` slash command for Codex (installed into `~/.codex/prompts/`).
- `AGENTS.md` — tells Codex when to delegate high-level planning to the advisor.
- `install.py` — detects binaries, registers the MCP server, sets timeouts, installs the command.

Why MCP instead of Codex running `claude` from a shell command: Codex runs shell commands in a
sandbox that usually has no network, so `claude` could not reach Anthropic. An MCP server runs as
a separate process outside that sandbox and has normal network access.

## Key gotchas (solved)

- Claude's `--json-schema` result is in the `structured_output` field, not `result`.
- Codex's `--output-schema` requires every JSON-schema object to set `additionalProperties: false`,
  otherwise the API returns invalid_json_schema (400).
- Subprocesses must use `stdin=DEVNULL`, otherwise codex/claude hang at
  "Reading additional input from stdin...".
- In headless `codex exec`, MCP tool calls are auto-cancelled because there is no one to approve
  them ("user cancelled MCP tool call"). In the interactive Codex app you just approve once.

## Optional: headless orchestrator (`orchestrate.py`)

A fully headless orchestrator (no interface). It asks Claude for a plan, has Codex execute each
subtask, verifies with the plan's check commands, and commits each passing subtask on a dedicated
branch. Watch progress via `mission.md`. It never auto-merges into main.

```bash
python3 orchestrate.py --goal "add JWT auth to the API" --repo /path/to/repo
```

Loop: plan once (Claude) → for each subtask: Codex executes → run check command → on pass commit,
on repeated failure escalate to Claude for a retry/abort decision (capped by `--max-escalations`).

## Out of scope (YAGNI)

- No parallel subtasks (sequential avoids conflicts).
- No auto-merge into main.
- No web UI; progress via `mission.md` and terminal logs.
- Locked to Claude + Codex for now (not pluggable across models).
