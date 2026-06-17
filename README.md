# codex-claude-planner

Let **Codex do the work** and **Claude do the high-level planning** — while spending as few
of the expensive Claude tokens as possible.

You stay in the Codex interface you already like. Codex handles all execution. When a task
needs high-level planning or an architecture decision, Codex calls a small MCP tool that
consults Claude. Because Claude is only invoked occasionally, the cost stays low.

```
You ──▶ Codex (your interface) ──── executes / edits code ────▶ your project
              │
              │ when planning is needed, calls the MCP tool: plan / consult
              ▼
      claude-planner  (a standalone MCP process, outside Codex's sandbox, has network)
              │ runs `claude -p` internally
              ▼
          Claude returns a plan  (rarely called → cheap)
```

## Why this design

- **Cost arbitrage.** The strong/expensive model (Claude) is only used for the small amount
  of high-level thinking. The cheap model (Codex) does the bulky execution.
- **Why MCP and not just `claude` from a Codex shell command?** Codex runs shell commands in a
  sandbox that usually has no network, so `claude` couldn't reach Anthropic. An MCP server runs
  as a separate process *outside* that sandbox, so it has normal network access.

## Prerequisites

- [Codex CLI / Codex.app](https://github.com/openai/codex) — installed, on your `PATH`, logged in.
- [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`) — installed, on your `PATH`, logged in.
- Python 3.8+ (standard library only — no pip installs).

## Install

```bash
git clone https://github.com/michellesijiama/codex-claude-planner.git
cd codex-claude-planner
python3 install.py
```

That registers a `claude-planner` MCP server in your Codex config (`~/.codex/config.toml`),
pointing at this repo's `claude_planner_mcp.py`, and sets sane timeouts. Then **restart Codex**.

Options:

```bash
python3 install.py --model opus    # stronger (slower, pricier) planning; default is sonnet
python3 install.py --max-usd 2.0   # raise the per-plan budget cap (default 1.0)
```

Uninstall: `codex mcp remove claude-planner`

## Use

1. Restart Codex (app or CLI session) so it loads the new server.
2. Work in Codex normally.
3. For a large or fuzzy task, let Codex call the planner — e.g. tell it:
   > Use the claude-planner `plan` tool with goal "<your goal>" and project_dir "<absolute path>", then follow the plan.
4. **The first tool call shows an approval prompt — approve it** (choose "always allow" to skip it next time).
5. After ~40–70 seconds Claude returns a plan and Codex executes it.

### The two tools

| Tool | What it does |
|------|--------------|
| `plan(goal, project_dir)` | Breaks a goal into ordered, executable steps, each with a check command (passes = exit 0). |
| `consult(question, project_dir)` | Gives a decisive answer on architecture / tech-choice / trade-off questions. |

Pass the project's **absolute path** as `project_dir` so Claude can inspect it.

### Optional: drop in `AGENTS.md`

Copy this repo's [`AGENTS.md`](AGENTS.md) into a project root (or merge it into your global
`~/.codex/AGENTS.md`) to nudge Codex to *proactively* delegate high-level planning to the
planner and keep its own work low-level.

## Configuration

Set per-server env in `~/.codex/config.toml` under `[mcp_servers.claude-planner.env]`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `CLAUDE_PLANNER_MODEL` | `sonnet` | Claude model used for planning. |
| `CLAUDE_PLANNER_MAX_USD` | `1.0` | Budget cap per planning call. |
| `CLAUDE_BIN` | auto-detected | Path to the `claude` binary. |
| `CLAUDE_PLANNER_LOG` | (unset) | If set to a file path, writes a debug log. |

## Troubleshooting

- **"user cancelled MCP tool call" in headless `codex exec`.** Expected: headless mode has no one
  to approve the tool call. Use the interactive Codex app and approve, or pass
  `--dangerously-bypass-approvals-and-sandbox` for headless testing.
- **Plan call seems slow (~1 min).** Normal — Claude is reading your project and thinking. The
  installer sets `tool_timeout_sec = 300` to allow for it.
- **`claude` not found when the server runs.** Set `CLAUDE_BIN` to the absolute path of `claude`
  in the server's env block.

## Optional autopilot: `orchestrate.py`

`orchestrate.py` is a separate, fully-headless orchestrator (no interface). It asks Claude for a
plan, has Codex execute each subtask, verifies with the plan's check commands, and commits each
passing subtask on a dedicated branch. Watch progress via `mission.md`.

```bash
python3 orchestrate.py --goal "add JWT auth to the API" --repo /path/to/repo
```

It's not the main path (that's the interactive MCP flow above) but is handy for hands-off batches.

## License

MIT — see [LICENSE](LICENSE).
