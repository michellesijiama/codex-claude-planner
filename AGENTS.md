# Codex working agreement (collaborating with the Claude planning advisor)

You are the **executor**. You are good at — and should focus on — reading and writing code,
running commands, debugging, and landing changes step by step.

A high-level planning advisor, **Claude**, is available through the `claude-planner` MCP server,
with two tools:

- `plan(goal, project_dir)` — break a large or unclear goal into ordered, executable steps (with check commands).
- `consult(question, project_dir)` — get a decisive recommendation on architecture, tech choice, or trade-offs.

## When to call the advisor

**Proactively** call `plan` or `consult` at these moments instead of reasoning at length yourself:

1. You receive a **large or fuzzy** task (multiple files, multiple steps, unclear requirements) → call `plan` first, then execute the returned steps.
2. You are stuck on a **high-level decision** (tech choice, architecture trade-off, directory layout, whether to add a dependency) → call `consult`.
3. A step **keeps failing** and you suspect the approach is wrong rather than an implementation detail → call `consult` describing the situation for a fresh angle.

Pass the project's **absolute path** as `project_dir` so the advisor can inspect the project.

## When NOT to call it

- The task is small and clear (edit one function, fix an obvious bug, add a line of config) → just do it; don't waste a round trip.
- Pure execution details (syntax, how to call a specific API) → solve it yourself or read the code.

## Division of labor

- **High-level work (decomposition, architecture, direction) goes to Claude; low-level work (implementation, debugging, running) is yours.**
- This way the stronger model only steps in at key decisions, keeping cost down and reducing drift.
- The advisor's plan is a **recommendation**: tweak it if you hit an obvious problem during execution, but run `consult` again before any directional change.
