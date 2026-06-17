#!/usr/bin/env python3
"""claude-planner MCP 服务器。

把 Claude 作为"高层规划顾问"暴露给 Codex。Codex 在它自己的界面里干活，
遇到需要高层规划/架构决策时调用本服务的工具，本服务在沙箱外用正常网络
调用 `claude -p` 拿到结果再返回给 Codex。

协议：MCP（JSON-RPC 2.0，stdio，换行分隔）。只用 Python 标准库，零依赖，
这样 Codex 启动它时不会因为缺包失败。

暴露两个工具：
- plan(goal, project_dir)    让 Claude 把一个目标拆成有序、可执行的步骤。
- consult(question, project_dir)  让 Claude 回答一个架构/方案层面的问题。

可通过环境变量调节：
- CLAUDE_PLANNER_MODEL    Claude 模型，默认 opus
- CLAUDE_PLANNER_MAX_USD  单次规划调用预算上限（美元），默认 1.0
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
# Codex 启动 MCP 服务时环境可能很精简，claude 不一定在 PATH 上。
# 优先用 CLAUDE_BIN 环境变量，否则尝试在 PATH 上探测，最后退回字面量 "claude"。
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
    """调用 claude -p，返回 structured_output（dict）。stdin=DEVNULL 避免挂起。"""
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
    _debug(f"claude 调用开始 model={MODEL} cwd={cwd}")
    proc = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    _debug(f"claude 调用结束 returncode={proc.returncode} stdout_len={len(proc.stdout)}")
    if not proc.stdout.strip():
        raise RuntimeError(f"claude 无输出。stderr: {proc.stderr[:600]}")
    envelope = json.loads(proc.stdout)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    # 兜底：result 字段里可能是 JSON 字符串
    result = envelope.get("result")
    if isinstance(result, str) and result.strip():
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"answer": result}
    raise RuntimeError(f"claude 未返回结构化结果。原始：{proc.stdout[:600]}")


def tool_plan(args):
    goal = args.get("goal", "")
    project_dir = args.get("project_dir", "")
    prompt = (
        "你是高层规划顾问。请围绕下面的目标，检视当前项目，给出一个有序、"
        "可执行的实施计划，供一个低层执行型 AI（Codex）逐步落地。\n\n"
        f"目标：{goal}\n\n"
        "要求：把目标拆成若干串行步骤。每个步骤包含 title（一句话）、detail"
        "（要做什么、注意什么），可选 check（一条用退出码表达成败的验收命令，"
        "成功退出码为 0，失败非 0，禁止用 `|| echo` 之类吞掉退出码）。"
        "另给一个 summary 概述整体思路。只输出符合 schema 的结构化 JSON。"
    )
    data = _call_claude(prompt, PLAN_SCHEMA, project_dir)
    return _format_plan(data)


def tool_consult(args):
    question = args.get("question", "")
    project_dir = args.get("project_dir", "")
    prompt = (
        "你是高层架构/方案顾问。请简洁、果断地回答下面的问题，"
        "必要时给出推荐方案和理由，供执行型 AI 直接采用。\n\n"
        f"问题：{question}\n\n"
        "只输出符合 schema 的结构化 JSON，answer 字段写你的回答。"
    )
    data = _call_claude(prompt, ANSWER_SCHEMA, project_dir)
    return str(data.get("answer", "")).strip() or "（Claude 未给出回答）"


def _format_plan(data):
    """把结构化计划格式化成 Codex 易读的 markdown。"""
    lines = []
    summary = data.get("summary")
    if summary:
        lines.append(f"规划概述：{summary}")
        lines.append("")
    steps = data.get("steps") or []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        lines.append(f"{index}. {step.get('title', '')}")
        detail = step.get("detail")
        if detail:
            lines.append(f"   - 做什么：{detail}")
        check = step.get("check")
        if check:
            lines.append(f"   - 验收：`{check}`")
    if not steps:
        lines.append("（Claude 未给出步骤）")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "plan",
        "description": (
            "让高层规划顾问 Claude 把一个目标拆成有序、可执行的步骤（含验收命令）。"
            "当你需要对一个较大或不明确的任务做整体规划/拆解时调用。"
            "请把当前项目的绝对路径作为 project_dir 传入。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "要实现的目标"},
                "project_dir": {
                    "type": "string",
                    "description": "当前项目的绝对路径",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "consult",
        "description": (
            "就架构、技术选型或方案权衡向高层顾问 Claude 提一个问题，拿到果断的建议。"
            "当你卡在一个高层决策、需要更强模型判断时调用。"
            "请把当前项目的绝对路径作为 project_dir 传入。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要请教的问题"},
                "project_dir": {
                    "type": "string",
                    "description": "当前项目的绝对路径",
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
    _debug(f"收到请求 method={method} id={req_id}")

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
        return  # 通知，无需响应

    if method == "tools/list":
        _result(req_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        func = TOOL_FUNCS.get(name)
        if func is None:
            _error(req_id, -32602, f"未知工具：{name}")
            return
        try:
            text = func(args)
            _result(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # 把错误作为工具结果返回，方便 Codex 看到原因
            _result(
                req_id,
                {
                    "content": [{"type": "text", "text": f"调用 Claude 失败：{exc}"}],
                    "isError": True,
                },
            )
        return

    if method == "ping":
        _result(req_id, {})
        return

    if not is_notification:
        _error(req_id, -32601, f"未实现的方法：{method}")


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
                _error(req_id, -32603, f"内部错误：{exc}")


if __name__ == "__main__":
    main()
