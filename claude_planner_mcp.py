#!/usr/bin/env python3
"""claude-planner MCP server.

Exposes Claude as a "high-level planning advisor" to Codex. You work in the Codex
interface; when a task needs high-level planning or an architecture decision, Codex
calls a tool from this server. The server runs `claude -p` outside Codex's sandbox
(so it has normal network access) and returns the result to Codex.

Protocol: MCP (JSON-RPC 2.0 over stdio, newline-delimited). Standard library only, no
dependencies, so Codex can launch it without any pip install.

Two tools:
- plan(goal, project_dir)        Ask Claude to break a goal into ordered, executable steps.
- consult(question, project_dir) Ask Claude an architecture / approach question.

Environment variables:
- CLAUDE_PLANNER_MODEL    Claude model, default opus
- CLAUDE_PLANNER_MAX_USD  budget cap per planning call (USD), default 1.0
"""

import datetime
import json
import os
import shutil
import subprocess
import sys

DEBUG_LOG = os.environ.get("CLAUDE_PLANNER_LOG")


def _debug(message):
    if not DEBUG_LOG:
        return
    try:
        stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(DEBUG_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass

MODEL = os.environ.get("CLAUDE_PLANNER_MODEL", "opus")
MAX_USD = os.environ.get("CLAUDE_PLANNER_MAX_USD", "1.0")
# When Codex launches the MCP server the environment may be minimal, so `claude` may
# not be on PATH. Prefer CLAUDE_BIN, then probe PATH, then fall back to the literal "claude".
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
SERVER_NAME = "claude-planner"
SERVER_VERSION = "0.1.0"

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "check": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
        },
    },
    "required": ["summary", "steps"],
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _call_claude(prompt, schema, project_dir):
    """Run `claude -p` and return its structured_output (dict). stdin=DEVNULL avoids hangs."""
    cwd = project_dir if project_dir and os.path.isdir(project_dir) else os.getcwd()
    command = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
        "--permission-mode",
        "plan",
        "--model",
        MODEL,
        "--max-budget-usd",
        str(MAX_USD),
    ]
    _debug(f"claude call start model={MODEL} cwd={cwd}")
    proc = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    _debug(f"claude call done returncode={proc.returncode} stdout_len={len(proc.stdout)}")
    if not proc.stdout.strip():
        raise RuntimeError(f"claude produced no output. stderr: {proc.stderr[:600]}")
    envelope = json.loads(proc.stdout)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    # Fallback: the result field may contain a JSON string.
    result = envelope.get("result")
    if isinstance(result, str) and result.strip():
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"answer": result}
    raise RuntimeError(f"claude returned no structured result. raw: {proc.stdout[:600]}")


def tool_plan(args):
    goal = args.get("goal", "")
    project_dir = args.get("project_dir", "")
    prompt = (
        "You are a high-level planning advisor. Inspect the current project and produce "
        "an ordered, executable implementation plan for a low-level executor AI (Codex) "
        "to carry out step by step.\n\n"
        f"Goal: {goal}\n\n"
        "Requirements: break the goal into sequential steps. Each step has a title (one "
        "line), a detail (what to do and what to watch out for), and an optional check (a "
        "single shell command whose exit code signals success: 0 on success, non-zero on "
        "failure; never use `|| echo` or anything that swallows the exit code). Also give a "
        "summary of the overall approach. Output only structured JSON matching the schema."
    )
    data = _call_claude(prompt, PLAN_SCHEMA, project_dir)
    return _format_plan(data)


def tool_consult(args):
    question = args.get("question", "")
    project_dir = args.get("project_dir", "")
    prompt = (
        "You are a high-level architecture / approach advisor. Answer the question below "
        "concisely and decisively, recommending an option and the reasoning when relevant, "
        "so an executor AI can adopt it directly.\n\n"
        f"Question: {question}\n\n"
        "Output only structured JSON matching the schema; put your answer in the answer field."
    )
    data = _call_claude(prompt, ANSWER_SCHEMA, project_dir)
    return str(data.get("answer", "")).strip() or "(Claude gave no answer)"


def _format_plan(data):
    """Format the structured plan into markdown that is easy for Codex to read."""
    lines = []
    summary = data.get("summary")
    if summary:
        lines.append(f"Plan summary: {summary}")
        lines.append("")
    steps = data.get("steps") or []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        lines.append(f"{index}. {step.get('title', '')}")
        detail = step.get("detail")
        if detail:
            lines.append(f"   - What to do: {detail}")
        check = step.get("check")
        if check:
            lines.append(f"   - Check: `{check}`")
    if not steps:
        lines.append("(Claude gave no steps)")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "plan",
        "description": (
            "Ask the high-level advisor Claude to break a goal into ordered, executable "
            "steps (with check commands). Call this when a task is large or unclear and "
            "needs overall planning / decomposition. Pass the project's absolute path as "
            "project_dir."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "the goal to achieve"},
                "project_dir": {
                    "type": "string",
                    "description": "absolute path of the current project",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "consult",
        "description": (
            "Ask the high-level advisor Claude a question about architecture, tech choice, "
            "or trade-offs and get a decisive recommendation. Call this when you are stuck "
            "on a high-level decision and want a stronger model's judgment. Pass the "
            "project's absolute path as project_dir."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "the question to ask"},
                "project_dir": {
                    "type": "string",
                    "description": "absolute path of the current project",
                },
            },
            "required": ["question"],
        },
    },
]

TOOL_FUNCS = {"plan": tool_plan, "consult": tool_consult}


def _send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle(message):
    method = message.get("method")
    req_id = message.get("id")
    is_notification = req_id is None
    _debug(f"request method={method} id={req_id}")

    if method == "initialize":
        params = message.get("params") or {}
        protocol = params.get("protocolVersion", "2024-11-05")
        _result(
            req_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return

    if method in ("notifications/initialized", "initialized"):
        return  # notification, no response needed

    if method == "tools/list":
        _result(req_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        func = TOOL_FUNCS.get(name)
        if func is None:
            _error(req_id, -32602, f"unknown tool: {name}")
            return
        try:
            text = func(args)
            _result(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # return the error as the tool result so Codex sees the cause
            _result(
                req_id,
                {
                    "content": [{"type": "text", "text": f"Claude call failed: {exc}"}],
                    "isError": True,
                },
            )
        return

    if method == "ping":
        _result(req_id, {})
        return

    if not is_notification:
        _error(req_id, -32601, f"method not implemented: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(message)
        except Exception as exc:
            req_id = message.get("id") if isinstance(message, dict) else None
            if req_id is not None:
                _error(req_id, -32603, f"internal error: {exc}")


if __name__ == "__main__":
    main()
