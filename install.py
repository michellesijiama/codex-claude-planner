#!/usr/bin/env python3
"""安装 claude-planner 到 Codex。

自动完成：
- 定位本仓库里的 claude_planner_mcp.py（绝对路径）
- 探测 claude / python3
- 用 `codex mcp add` 注册一个名为 claude-planner 的 stdio MCP 服务
- 给该服务补上 startup_timeout_sec / tool_timeout_sec（Claude 规划较慢）

用法：
  python3 install.py                # 默认 sonnet 规划模型
  python3 install.py --model opus   # 改用更强（更慢更贵）的模型
  python3 install.py --max-usd 2.0  # 调高单次规划预算上限

卸载：codex mcp remove claude-planner
"""

import argparse
import os
import shutil
import subprocess
import sys

SERVER_NAME = "claude-planner"
STARTUP_TIMEOUT_SEC = 30
TOOL_TIMEOUT_SEC = 300


def fail(message):
    print(f"错误：{message}", file=sys.stderr)
    sys.exit(1)


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def main():
    parser = argparse.ArgumentParser(description="安装 claude-planner 到 Codex")
    parser.add_argument("--model", default="sonnet", help="Claude 规划模型，默认 sonnet")
    parser.add_argument("--max-usd", default="1.0", help="单次规划预算上限（美元）")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(here, "claude_planner_mcp.py")
    if not os.path.isfile(server_path):
        fail(f"找不到 {server_path}")

    codex = shutil.which("codex")
    if not codex:
        fail("未找到 codex CLI，请先安装 Codex 并确认在 PATH 上。")

    claude = shutil.which("claude")
    if not claude:
        print("警告：未在 PATH 上找到 claude。服务运行时仍会再探测一次；"
              "若失败，请确保 Claude Code CLI 已安装。")
        claude = "claude"

    python = sys.executable or shutil.which("python3") or "python3"

    # 已存在则先移除，保证可重复运行（幂等）。
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
        fail(f"codex mcp add 失败：\n{proc.stdout}\n{proc.stderr}")
    print(f"已注册 MCP 服务 '{SERVER_NAME}'。")

    _patch_timeouts()

    print()
    print("✅ 安装完成。接下来：")
    print("  1. 重启 Codex（app 或 CLI 会话）以加载新服务。")
    print("  2. 在 Codex 里需要规划时调用 plan / consult 工具；")
    print("     第一次调用会弹批准框，点同意（可选『始终允许』）。")
    print(f"  规划模型：{args.model}（改用更强模型：python3 install.py --model opus）")


def _patch_timeouts():
    """在 config.toml 的 [mcp_servers.claude-planner] 段补超时键（若缺）。"""
    config_path = os.path.join(codex_home(), "config.toml")
    if not os.path.isfile(config_path):
        print("提示：未找到 config.toml，跳过超时设置（codex mcp add 应已创建配置）。")
        return

    with open(config_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    header = f"[mcp_servers.{SERVER_NAME}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return

    # 找到本段范围（到下一个 [ 开头的段，或文件结尾）。
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

    # 插在段头之后。
    new_lines = lines[:start + 1] + inserts + lines[start + 1:]
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.writelines(new_lines)
    print(f"已设置超时：startup={STARTUP_TIMEOUT_SEC}s, tool={TOOL_TIMEOUT_SEC}s。")


if __name__ == "__main__":
    main()
