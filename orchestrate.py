#!/usr/bin/env python3
"""Claude-Codex orchestrator.

Usage:
  python3 orchestrate.py --goal "add JWT auth to the API" [--repo .] [--model opus]

The script asks Claude to produce ordered subtasks, has Codex execute each one, runs each
subtask's check command, commits passing subtasks to a dedicated branch, and continuously
writes mission.json / mission.md for you to watch. It never auto-merges into main.
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
    # OpenAI structured output requires every object to declare additionalProperties:false,
    # otherwise the API returns invalid_json_schema (400) and codex fails within seconds.
    # codex's --output-schema goes through that path.
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
CODEX_TIMEOUT = 900  # max seconds for a single Codex subtask; on timeout treat as BLOCKED


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
    # stdin must be DEVNULL: tools like codex/claude wait for input and hang if stdin is open.
    proc = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if proc.returncode != 0:
        output = (proc.stdout or "") + (proc.stderr or "")
        print(f"Command failed: {_format_command(command)}", file=sys.stderr)
        if output.strip():
            print(output[-4000:], file=sys.stderr)
        sys.exit(1)
    return proc


def _extract_json_object(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty output")
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
    raise ValueError("could not parse JSON from output")


def _structured_result(stdout):
    envelope = _extract_json_object(stdout)
    if isinstance(envelope, dict):
        # With --json-schema, Claude puts the validated structured result in structured_output,
        # while the result field is an empty string. Prefer structured_output; fall back to result.
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
        errors.append("claude not found. Make sure the Claude Code CLI is installed and on PATH.")
    if shutil.which("codex") is None:
        errors.append("codex not found. Install the Codex CLI and make sure it is on PATH.")

    proc = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        errors.append(f"target directory is not a git repo: {repo_path}")

    if errors:
        for error in errors:
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


def claude_plan(goal, repo, model, max_usd):
    repo_path = _repo_path(repo)
    prompt = (
        "Inspect the current git repo and produce an ordered implementation plan for the goal below.\n"
        f"Goal: {goal}\n\n"
        "Requirements: break the goal into sequential subtasks. Each subtask must include:\n"
        "1. description: a clear description of what to do.\n"
        "2. check_command: a single shell command (test, lint, or build) that objectively "
        "verifies the subtask.\n\n"
        "Hard requirements for check_command (very important):\n"
        "- It must signal success via its exit code: 0 on success, non-zero on failure.\n"
        "- Never use `|| echo`, `|| true`, or anything that swallows the exit code and makes the "
        "command always return 0.\n"
        "- For example, to check a file exists write `test -f path` (not `test -f path && echo ok || echo no`).\n\n"
        "Output only structured JSON matching the schema."
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
        _print_command("would call Claude to generate the plan", command)
        return {"subtasks": []}

    proc = _run_required(command, cwd=str(repo_path))
    data = _structured_result(proc.stdout)
    subtasks = data.get("subtasks") if isinstance(data, dict) else None
    if not isinstance(subtasks, list):
        print("Error: Claude plan output is missing the subtasks list.", file=sys.stderr)
        sys.exit(1)

    normalized = []
    for index, item in enumerate(subtasks, start=1):
        if not isinstance(item, dict):
            print(f"Error: subtask #{index} is not an object.", file=sys.stderr)
            sys.exit(1)
        description = item.get("description")
        check_command = item.get("check_command")
        if not isinstance(description, str) or not isinstance(check_command, str):
            print(f"Error: subtask #{index} is missing description or check_command.", file=sys.stderr)
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
        description = f"{description}\n\nAdditional fix instructions:\n{extra}"

    prompt = (
        "Execute the following subtask in the current repo.\n\n"
        f"Subtask: {description}\n\n"
        f"Check command: {subtask.get('check_command', '')}\n\n"
        "When finished, you must report status and summary using the provided output schema. "
        "If you have completed it and are ready for the orchestrator to run the check command, "
        "use status DONE; if you cannot continue, use status BLOCKED and explain why in summary."
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
        _print_command("would call Codex to execute the subtask", command)
        return {"status": "DONE", "summary": "dry-run: Codex not actually called"}

    try:
        # stdin=DEVNULL is critical: otherwise codex exec hangs at
        # "Reading additional input from stdin...".
        # timeout is a safety net: high-reasoning models can be slow; on timeout treat as
        # BLOCKED and let the upper layer escalate.
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
            raise ValueError("Codex output does not match the schema")
        return {"status": status, "summary": summary}
    except subprocess.TimeoutExpired:
        return {"status": "BLOCKED", "summary": f"Codex timed out after {CODEX_TIMEOUT}s"}
    except Exception:
        return {"status": "BLOCKED", "summary": "could not parse Codex output"}


def run_check(check_command, repo):
    repo_path = _repo_path(repo)
    if DRY_RUN:
        print(f"[dry-run] would run check command: {check_command}")
        return True, "dry-run: check command not actually run"
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
        _print_command("would stage changes", add_command)
        _print_command("would create commit", commit_command)
        _print_command("would read short commit hash", hash_command)
        return "dryrun"

    _run_required(add_command)
    # If there is nothing to commit, `git commit` exits non-zero and would crash the whole
    # orchestrator. Check the working tree first: if there are no changes, skip and return None.
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
        _print_command("would create and switch branch", command)
        return branch

    _run_required(command)
    return branch


def claude_repair(subtask, last_error, repo, model):
    repo_path = _repo_path(repo)
    prompt = (
        "A Codex subtask has failed repeatedly. Decide whether to retry or abort.\n\n"
        f"Subtask: {subtask.get('description', '')}\n"
        f"Check command: {subtask.get('check_command', '')}\n\n"
        "Tail of the most recent error output:\n"
        f"{last_error[-4000:]}\n\n"
        "If clearer additional instructions for Codex could fix it, return action retry and put "
        "the extra instructions for Codex in instructions; if it is not worth burning more budget, "
        "return action abort. Output only structured JSON matching the schema."
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
        _print_command("would call Claude for a repair decision", command)
        return {"action": "abort", "instructions": "dry-run: Claude not actually called"}

    proc = _run_required(command, cwd=str(repo_path))
    data = _structured_result(proc.stdout)
    if not isinstance(data, dict):
        return {"action": "abort", "instructions": "Claude repair output is not an object"}
    action = data.get("action")
    instructions = data.get("instructions")
    if action not in ("retry", "abort") or not isinstance(instructions, str):
        return {"action": "abort", "instructions": "Claude repair output does not match the schema"}
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
        print("[dry-run] would refresh mission.json and mission.md")
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
        f"Subtask {subtask['id']} retry after escalation: Codex={res['status']}, "
        f"check={'pass' if ok else 'fail'}.",
    )
    if res["status"] == "DONE" and ok:
        subtask["commit"] = git_commit(
            repo,
            f"[orchestrator] subtask {subtask['id']}: {subtask['description']}",
        )
        subtask["status"] = "done"
        return True, tail
    subtask["status"] = "failed"
    return False, tail


def main(argv=None):
    global DRY_RUN

    parser = argparse.ArgumentParser(description="Claude-Codex orchestrator")
    parser.add_argument("--goal", required=True, help="the goal to accomplish")
    parser.add_argument("--repo", default=".", help="target git repo path, default current dir")
    parser.add_argument("--model", default="opus", help="Claude model, default opus")
    parser.add_argument("--max-plan-usd", type=float, default=1.0, help="Claude planning budget cap")
    parser.add_argument("--max-escalations", type=int, default=2, help="max times to escalate to Claude")
    parser.add_argument("--dry-run", action="store_true", help="only print the commands, do not call agents or write git")
    args = parser.parse_args(argv)
    DRY_RUN = args.dry_run

    if args.dry_run:
        print("dry-run mode: showing the key steps only; not calling claude/codex and not writing git.")

    preflight(args.repo)
    plan = claude_plan(args.goal, args.repo, args.model, args.max_plan_usd)
    branch = create_branch(args.repo, args.goal)
    subtasks = plan.get("subtasks", [])
    state = {"goal": args.goal, "branch": branch, "subtasks": subtasks, "log": []}
    _log(state, "Task started.")
    _refresh(args.repo, state)

    escalations = 0
    stop_all = False

    for subtask in subtasks:
        subtask["status"] = "running"
        _log(state, f"Starting subtask {subtask['id']}.")
        _refresh(args.repo, state)

        success = False
        last_tail = ""
        for _ in range(2):
            subtask["attempts"] += 1
            res = codex_exec(subtask, args.repo)
            ok, last_tail = run_check(subtask["check_command"], args.repo)
            _log(
                state,
                f"Subtask {subtask['id']} attempt {subtask['attempts']}: "
                f"Codex={res['status']}, check={'pass' if ok else 'fail'}.",
            )
            _refresh(args.repo, state)
            if res["status"] == "DONE" and ok:
                subtask["commit"] = git_commit(
                    args.repo,
                    f"[orchestrator] subtask {subtask['id']}: {subtask['description']}",
                )
                subtask["status"] = "done"
                _log(state, f"Subtask {subtask['id']} done, commit={subtask['commit']}.")
                success = True
                break

        if not success:
            escalations += 1
            if escalations > args.max_escalations:
                subtask["status"] = "failed"
                _log(state, "Escalation limit reached, handing back to the user.")
                print("Escalation limit reached, handing back to the user.")
                stop_all = True
            else:
                decision = claude_repair(subtask, last_tail, args.repo, args.model)
                _log(state, f"Claude repair decision: {decision['action']}.")
                if decision["action"] == "abort":
                    subtask["status"] = "failed"
                    _log(state, "Claude advised aborting, handing back to the user.")
                    stop_all = True
                else:
                    subtask["status"] = "escalated"
                    subtask["repair_instructions"] = decision["instructions"]
                    _refresh(args.repo, state)
                    retry_ok, last_tail = _run_escalated_retry(subtask, args.repo, state)
                    if not retry_ok:
                        _log(state, f"Subtask {subtask['id']} still failed after escalation, handing back to the user.")
                        stop_all = True

        _refresh(args.repo, state)
        if stop_all:
            break

    done_count = sum(1 for item in subtasks if item.get("status") == "done")
    failed_count = sum(1 for item in subtasks if item.get("status") == "failed")
    print("Final summary:")
    print(f"- done: {done_count}")
    print(f"- failed: {failed_count}")
    print(f"- branch: {branch}")
    print("Review the branch and merge it yourself.")


if __name__ == "__main__":
    main()
