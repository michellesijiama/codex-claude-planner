#!/usr/bin/env python3
"""Install claude-planner into Codex.

Automatically:
- locates this repo's claude_planner_mcp.py (absolute path)
- detects claude / python3
- registers a stdio MCP server named claude-planner via `codex mcp add`
- adds startup_timeout_sec / tool_timeout_sec for that server (Claude planning is slow)
- installs the /claude slash command into Codex's prompts directory

Usage:
  python3 install.py                # default opus planning model
  python3 install.py --model sonnet # use a cheaper, faster model
  python3 install.py --max-usd 2.0  # raise the per-plan budget cap

Uninstall: codex mcp remove claude-planner
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SERVER_NAME = "claude-planner"
STARTUP_TIMEOUT_SEC = 30
TOOL_TIMEOUT_SEC = 300


def fail(message):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def main():
    parser = argparse.ArgumentParser(description="Install claude-planner into Codex")
    parser.add_argument("--model", default="opus", help="Claude planning model (default opus)")
    parser.add_argument("--max-usd", default="1.0", help="budget cap per planning call (USD)")
    args = parser.parse_args()

    server_path = os.path.join(HERE, "claude_planner_mcp.py")
    if not os.path.isfile(server_path):
        fail(f"not found: {server_path}")

    codex = shutil.which("codex")
    if not codex:
        fail("codex CLI not found. Install Codex and make sure it is on your PATH.")

    claude = shutil.which("claude")
    if not claude:
        print("Warning: claude not found on PATH. The server will probe again at runtime; "
              "if that fails, make sure the Claude Code CLI is installed.")
        claude = "claude"

    python = sys.executable or shutil.which("python3") or "python3"

    # Remove any existing entry first so the installer is idempotent.
    subprocess.run([codex, "mcp", "remove", SERVER_NAME],
                   capture_output=True, text=True)

    add_cmd = [
        codex, "mcp", "add", SERVER_NAME,
        "--env", f"CLAUDE_BIN={claude}",
        "--env", f"CLAUDE_PLANNER_MODEL={args.model}",
        "--env", f"CLAUDE_PLANNER_MAX_USD={args.max_usd}",
        "--", python, server_path,
    ]
    proc = subprocess.run(add_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        fail(f"codex mcp add failed:\n{proc.stdout}\n{proc.stderr}")
    print(f"Registered MCP server '{SERVER_NAME}'.")

    _patch_timeouts()
    _install_slash_command()

    print()
    print("Done. Next steps:")
    print("  1. Restart Codex (app or CLI session) to load the new server.")
    print("  2. In Codex, call the plan / consult tools when you need planning;")
    print("     the first call shows an approval prompt - approve it (you can choose 'always allow').")
    print(f"  Planning model: {args.model} (use a cheaper one: python3 install.py --model sonnet)")
    print("  Or use the slash command in Codex: /claude <task to plan or question to ask>")


def _install_slash_command():
    """Copy the /claude slash command into Codex's prompts directory."""
    src = os.path.join(HERE, "prompts", "claude.md")
    if not os.path.isfile(src):
        return
    dest_dir = os.path.join(codex_home(), "prompts")
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copyfile(src, os.path.join(dest_dir, "claude.md"))
    print("Installed slash command /claude (type /claude <text> in Codex).")


def _patch_timeouts():
    """Add timeout keys under [mcp_servers.claude-planner] in config.toml (if missing)."""
    config_path = os.path.join(codex_home(), "config.toml")
    if not os.path.isfile(config_path):
        print("Note: config.toml not found, skipping timeout settings "
              "(codex mcp add should have created the config).")
        return

    with open(config_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    header = f"[mcp_servers.{SERVER_NAME}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return

    # Find the extent of this section (up to the next [ section, or end of file).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break

    section = "".join(lines[start:end])
    inserts = []
    if "startup_timeout_sec" not in section:
        inserts.append(f"startup_timeout_sec = {STARTUP_TIMEOUT_SEC}\n")
    if "tool_timeout_sec" not in section:
        inserts.append(f"tool_timeout_sec = {TOOL_TIMEOUT_SEC}\n")

    if not inserts:
        return

    # Insert right after the section header.
    new_lines = lines[:start + 1] + inserts + lines[start + 1:]
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.writelines(new_lines)
    print(f"Set timeouts: startup={STARTUP_TIMEOUT_SEC}s, tool={TOOL_TIMEOUT_SEC}s.")


if __name__ == "__main__":
    main()
