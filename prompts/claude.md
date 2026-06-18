---
description: Ask Claude (the high-level planner) to plan a task or answer an architecture question
argument-hint: a goal to plan, or a question to ask
---

The user invoked `/claude` followed by some text. Treat that text as a request for **Claude**,
the high-level planning advisor exposed through the `claude-planner` MCP server.

Pick the right tool:

- If the text is a task to break down, plan, design, or implement → call the `claude-planner`
  **`plan`** tool with `goal` set to the user's text.
- If the text is a question, a decision, a "should I…", or an architecture / tech-choice
  trade-off → call the `claude-planner` **`consult`** tool with `question` set to the user's text.

Always pass `project_dir` = the absolute path of the current working project, so Claude can
inspect the codebase.

After the tool returns:

- Show the user what Claude returned.
- If you called `plan`, go ahead and execute the plan step by step (run each step's check command
  to verify), **unless** the user explicitly said "just plan" / "don't execute".

If `/claude` was typed with no text after it, ask the user what they want Claude to plan or decide
before calling any tool.

Note: the `plan` / `consult` call takes roughly 40–70 seconds (Claude is reading the project and
thinking) and the first call in a session may ask for approval — that is expected.
