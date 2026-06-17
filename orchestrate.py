#!/usr/bin/env python3
"""Claude-Codex 编排器。

用法：
  python3 orchestrate.py --goal "给 API 加 JWT 认证" [--repo .] [--model opus]

脚本会让 Claude 生成有序子任务，让 Codex 逐项执行，运行每个子任务的检查命令，
通过后提交到专属分支，并持续写入 mission.json / mission.md 方便人工观察。
不会自动合并到 main。
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "check_command": {"type": "string"},
                },
                "required": ["description", "check_command"],
            },
        }
    },
    "required": ["subtasks"],
}

STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["DONE", "BLOCKED"]},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
    # OpenAI 结构化输出要求每个对象显式声明 additionalProperties:false，否则 API 返回
    # invalid_json_schema (400)，导致 codex 秒级失败。codex 的 --output-schema 走这条路径。
    "additionalProperties": False,
}

REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["retry", "abort"]},
        "instructions": {"type": "string"},
    },
    "required": ["action", "instructions"],
}

DRY_RUN = False
CODEX_TIMEOUT = 900  # 单个 Codex 子任务最长执行秒数，超时按 BLOCKED 处理


def _repo_path(repo):
    return pathlib.Path(repo).expanduser().resolve()


def _print_command(label, command):
    print(f"[dry-run] {label}: {_format_command(command)}")


def _format_command(command):
    parts = []
    for part in command:
        text = str(part)
        if not text:
            parts.append("''")
        elif re.search(r"\s|['\"$`]", text):
            parts.append("'" + text.replace("'", "'\"'\"'") + "'")
        else:
            parts.append(text)
    return " ".join(parts)


def _run_required(command, cwd=None):
    # stdin 必须为 DEVNULL：codex/claude 等工具在检测到打开的 stdin 时会等待输入而挂起。
    proc = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if proc.returncode != 0:
        output = (proc.stdout or "") + (proc.stderr or "")
        print(f"命令执行失败：{_format_command(command)}", file=sys.stderr)
        if output.strip():
            print(output[-4000:], file=sys.stderr)
        sys.exit(1)
    return proc


def _extract_json_object(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("空输出")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("无法从输出中解析 JSON")


def _structured_result(stdout):
    envelope = _extract_json_object(stdout)
    if isinstance(envelope, dict):
        # 使用 --json-schema 时，Claude 把校验过的结构化结果放在 structured_output，
        # 而 result 字段为空字符串。优先读 structured_output，兼容旧的 result 路径。
        structured = envelope.get("structured_output")
        if isinstance(structured, (dict, list)):
            return structured
        result = envelope.get("result")
    else:
        result = envelope
    if isinstance(result, str):
        return _extract_json_object(result)
    return result


def _tmp_file(name):
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    tmp_dir = pathlib.Path(os.environ.get("TMPDIR") or "/tmp")
    return tmp_dir / f"orchestrator-{os.getpid()}-{stamp}-{name}"


def preflight(repo):
    repo_path = _repo_path(repo)
    errors = []

    if shutil.which("claude") is None:
        errors.append("未找到 claude，请先确认 Claude Code CLI 已安装并在 PATH 中。")
    if shutil.which("codex") is None:
        errors.append("未找到 codex，请先安装 Codex CLI 并确认在 PATH 中。")

    proc = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        errors.append(f"目标目录不是 git 仓库：{repo_path}")

    if errors:
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)


def claude_plan(goal, repo, model, max_usd):
    repo_path = _repo_path(repo)
    prompt = (
        "请检视当前 git 仓库，围绕下面目标生成一个有序实施计划。\n"
        f"目标：{goal}\n\n"
        "要求：把目标拆成若干串行子任务。每个子任务必须包含：\n"
        "1. description：清楚描述要做什么。\n"
        "2. check_command：一条可在 shell 直接运行的测试、lint 或 build 命令，"
        "用于客观验收这个子任务。\n\n"
        "关于 check_command 的硬性要求（非常重要）：\n"
        "- 它必须用退出码表达成败：成功时退出码为 0，失败时退出码必须非 0。\n"
        "- 禁止使用 `|| echo`、`|| true` 等会把失败退出码吞掉、导致命令总是返回 0 的写法。\n"
        "- 例如检查文件存在应写 `test -f path`（而不是 `test -f path && echo ok || echo no`）。\n\n"
        "只输出符合 schema 的结构化 JSON。"
    )
    command = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(PLAN_SCHEMA, ensure_ascii=False),
        "--permission-mode",
        "plan",
        "--model",
        str(model),
        "--max-budget-usd",
        str(max_usd),
    ]

    if DRY_RUN:
        _print_command("将调用 Claude 生成计划", command)
        return {"subtasks": []}

    proc = _run_required(command, cwd=str(repo_path))
    data = _structured_result(proc.stdout)
    subtasks = data.get("subtasks") if isinstance(data, dict) else None
    if not isinstance(subtasks, list):
        print("错误：Claude 计划输出缺少 subtasks 列表。", file=sys.stderr)
        sys.exit(1)

    normalized = []
    for index, item in enumerate(subtasks, start=1):
        if not isinstance(item, dict):
            print(f"错误：第 {index} 个子任务不是对象。", file=sys.stderr)
            sys.exit(1)
        description = item.get("description")
        check_command = item.get("check_command")
        if not isinstance(description, str) or not isinstance(check_command, str):
            print(f"错误：第 {index} 个子任务缺少 description 或 check_command。", file=sys.stderr)
            sys.exit(1)
        normalized.append(
            {
                "id": index,
                "description": description,
                "check_command": check_command,
                "status": "pending",
                "attempts": 0,
                "commit": None,
            }
        )
    return {"subtasks": normalized}


def codex_exec(subtask, repo):
    repo_path = _repo_path(repo)
    schema_file = _tmp_file("status-schema.json")
    last_msg_file = _tmp_file("last-message.json")
    schema_file.write_text(json.dumps(STATUS_SCHEMA, ensure_ascii=False), encoding="utf-8")

    description = subtask.get("description", "")
    extra = subtask.get("repair_instructions")
    if extra:
        description = f"{description}\n\n补充修复指令：\n{extra}"

    prompt = (
        "请在当前仓库中执行下面这个子任务。\n\n"
        f"子任务：{description}\n\n"
        f"验收命令：{subtask.get('check_command', '')}\n\n"
        "完成后必须按提供的 output schema 汇报 status 和 summary。"
        "如果已经完成并准备让调度器运行验收命令，status 用 DONE；"
        "如果无法继续，status 用 BLOCKED，并在 summary 说明原因。"
    )
    command = [
        "codex",
        "exec",
        "-s",
        "workspace-write",
        "-C",
        str(repo_path),
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_file),
        "-o",
        str(last_msg_file),
        prompt,
    ]

    if DRY_RUN:
        _print_command("将调用 Codex 执行子任务", command)
        return {"status": "DONE", "summary": "dry-run：未真实调用 Codex"}

    try:
        # stdin=DEVNULL 关键：否则 codex exec 会停在 "Reading additional input from stdin..." 挂起。
        # timeout 兜底：gpt 高推理可能很慢，超时则视为 BLOCKED 交给上层升级。
        subprocess.run(
            command,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=CODEX_TIMEOUT,
        )
        raw = last_msg_file.read_text(encoding="utf-8")
        data = _extract_json_object(raw)
        status = data.get("status")
        summary = data.get("summary")
        if status not in ("DONE", "BLOCKED") or not isinstance(summary, str):
            raise ValueError("Codex 输出不符合 schema")
        return {"status": status, "summary": summary}
    except subprocess.TimeoutExpired:
        return {"status": "BLOCKED", "summary": f"Codex 执行超过 {CODEX_TIMEOUT} 秒超时"}
    except Exception:
        return {"status": "BLOCKED", "summary": "无法解析 Codex 输出"}


def run_check(check_command, repo):
    repo_path = _repo_path(repo)
    if DRY_RUN:
        print(f"[dry-run] 将运行检查命令：{check_command}")
        return True, "dry-run：未真实运行检查命令"
    proc = subprocess.run(
        check_command,
        shell=True,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output[-4000:]


def git_commit(repo, message):
    repo_path = _repo_path(repo)
    add_command = ["git", "-C", str(repo_path), "add", "-A"]
    commit_command = ["git", "-C", str(repo_path), "commit", "-m", message]
    hash_command = ["git", "-C", str(repo_path), "rev-parse", "--short", "HEAD"]

    if DRY_RUN:
        _print_command("将暂存变更", add_command)
        _print_command("将创建提交", commit_command)
        _print_command("将读取短 commit hash", hash_command)
        return "dryrun"

    _run_required(add_command)
    # 若没有任何待提交改动，git commit 会以非零退出导致整个编排器崩溃。
    # 这里先检查工作区状态：无改动则跳过提交，返回 None。
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        return None
    _run_required(commit_command)
    proc = _run_required(hash_command)
    return proc.stdout.strip()


def create_branch(repo, goal):
    repo_path = _repo_path(repo)
    raw_slug = goal[:30]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw_slug).strip("-").lower()
    if not slug:
        slug = "goal"
    today = datetime.datetime.now().strftime("%Y%m%d")
    branch = f"orchestrator/{slug}-{today}"
    command = ["git", "-C", str(repo_path), "checkout", "-b", branch]

    if DRY_RUN:
        _print_command("将创建并切换分支", command)
        return branch

    _run_required(command)
    return branch


def claude_repair(subtask, last_error, repo, model):
    repo_path = _repo_path(repo)
    prompt = (
        "一个 Codex 子任务连续失败，请判断是否应该重试或中止。\n\n"
        f"子任务：{subtask.get('description', '')}\n"
        f"验收命令：{subtask.get('check_command', '')}\n\n"
        "最近一次错误输出尾部：\n"
        f"{last_error[-4000:]}\n\n"
        "如果可以通过给 Codex 更明确的补充说明解决，action 返回 retry，"
        "instructions 写给 Codex 的追加指令；如果不应继续烧预算，action 返回 abort。"
        "只输出符合 schema 的结构化 JSON。"
    )
    command = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(REPAIR_SCHEMA, ensure_ascii=False),
        "--permission-mode",
        "plan",
        "--model",
        str(model),
    ]

    if DRY_RUN:
        _print_command("将调用 Claude 生成修复决策", command)
        return {"action": "abort", "instructions": "dry-run：未真实调用 Claude"}

    proc = _run_required(command, cwd=str(repo_path))
    data = _structured_result(proc.stdout)
    if not isinstance(data, dict):
        return {"action": "abort", "instructions": "Claude 修复输出不是对象"}
    action = data.get("action")
    instructions = data.get("instructions")
    if action not in ("retry", "abort") or not isinstance(instructions, str):
        return {"action": "abort", "instructions": "Claude 修复输出不符合 schema"}
    return {"action": action, "instructions": instructions}


def write_state(repo, state):
    repo_path = _repo_path(repo)
    path = repo_path / "mission.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_markdown(repo, state):
    repo_path = _repo_path(repo)
    path = repo_path / "mission.md"
    emoji = {
        "pending": "⬜",
        "running": "⏳",
        "done": "✅",
        "failed": "❌",
        "escalated": "⚠️",
    }
    lines = [
        "# Mission",
        "",
        f"- Goal: {state.get('goal', '')}",
        f"- Branch: {state.get('branch', '')}",
        "",
        "## Subtasks",
        "",
    ]
    for subtask in state.get("subtasks", []):
        status = subtask.get("status", "pending")
        marker = emoji.get(status, "⬜")
        commit = subtask.get("commit") or "-"
        lines.append(
            f"- {marker} #{subtask.get('id')} {status} "
            f"(attempts: {subtask.get('attempts', 0)}, commit: {commit}) "
            f"{subtask.get('description', '')}"
        )

    if state.get("log"):
        lines.extend(["", "## Log", ""])
        for item in state["log"][-20:]:
            lines.append(f"- {item}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _refresh(repo, state):
    if DRY_RUN:
        print("[dry-run] 将刷新 mission.json 和 mission.md")
        return
    write_state(repo, state)
    render_markdown(repo, state)


def _log(state, message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.setdefault("log", []).append(f"{timestamp} {message}")


def _run_escalated_retry(subtask, repo, state):
    subtask["attempts"] += 1
    res = codex_exec(subtask, repo)
    ok, tail = run_check(subtask["check_command"], repo)
    _log(
        state,
        f"子任务 {subtask['id']} 升级后重试：Codex={res['status']}，检查={'通过' if ok else '失败'}。",
    )
    if res["status"] == "DONE" and ok:
        subtask["commit"] = git_commit(
            repo,
            f"[orchestrator] 子任务{subtask['id']}: {subtask['description']}",
        )
        subtask["status"] = "done"
        return True, tail
    subtask["status"] = "failed"
    return False, tail


def main(argv=None):
    global DRY_RUN

    parser = argparse.ArgumentParser(description="Claude-Codex 编排器")
    parser.add_argument("--goal", required=True, help="要完成的目标")
    parser.add_argument("--repo", default=".", help="目标 git 仓库路径，默认当前目录")
    parser.add_argument("--model", default="opus", help="Claude 模型，默认 opus")
    parser.add_argument("--max-plan-usd", type=float, default=1.0, help="Claude 规划预算上限")
    parser.add_argument("--max-escalations", type=int, default=2, help="最多升级给 Claude 的次数")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不真实调用 agent 或写 git")
    args = parser.parse_args(argv)
    DRY_RUN = args.dry_run

    if args.dry_run:
        print("dry-run 模式：只展示将执行的关键步骤，不调用 claude/codex，也不执行 git 写操作。")

    preflight(args.repo)
    plan = claude_plan(args.goal, args.repo, args.model, args.max_plan_usd)
    branch = create_branch(args.repo, args.goal)
    subtasks = plan.get("subtasks", [])
    state = {"goal": args.goal, "branch": branch, "subtasks": subtasks, "log": []}
    _log(state, "任务启动。")
    _refresh(args.repo, state)

    escalations = 0
    stop_all = False

    for subtask in subtasks:
        subtask["status"] = "running"
        _log(state, f"开始子任务 {subtask['id']}。")
        _refresh(args.repo, state)

        success = False
        last_tail = ""
        for _ in range(2):
            subtask["attempts"] += 1
            res = codex_exec(subtask, args.repo)
            ok, last_tail = run_check(subtask["check_command"], args.repo)
            _log(
                state,
                f"子任务 {subtask['id']} 尝试 {subtask['attempts']}："
                f"Codex={res['status']}，检查={'通过' if ok else '失败'}。",
            )
            _refresh(args.repo, state)
            if res["status"] == "DONE" and ok:
                subtask["commit"] = git_commit(
                    args.repo,
                    f"[orchestrator] 子任务{subtask['id']}: {subtask['description']}",
                )
                subtask["status"] = "done"
                _log(state, f"子任务 {subtask['id']} 完成，commit={subtask['commit']}。")
                success = True
                break

        if not success:
            escalations += 1
            if escalations > args.max_escalations:
                subtask["status"] = "failed"
                _log(state, "已达升级上限，交还用户。")
                print("已达升级上限，交还用户。")
                stop_all = True
            else:
                decision = claude_repair(subtask, last_tail, args.repo, args.model)
                _log(state, f"Claude 修复决策：{decision['action']}。")
                if decision["action"] == "abort":
                    subtask["status"] = "failed"
                    _log(state, "Claude 建议中止，交还用户。")
                    stop_all = True
                else:
                    subtask["status"] = "escalated"
                    subtask["repair_instructions"] = decision["instructions"]
                    _refresh(args.repo, state)
                    retry_ok, last_tail = _run_escalated_retry(subtask, args.repo, state)
                    if not retry_ok:
                        _log(state, f"子任务 {subtask['id']} 升级后仍失败，交还用户。")
                        stop_all = True

        _refresh(args.repo, state)
        if stop_all:
            break

    done_count = sum(1 for item in subtasks if item.get("status") == "done")
    failed_count = sum(1 for item in subtasks if item.get("status") == "failed")
    print("最终总结：")
    print(f"- 完成：{done_count}")
    print(f"- 失败：{failed_count}")
    print(f"- 分支：{branch}")
    print("请 review 分支内容后自行合并。")


if __name__ == "__main__":
    main()
