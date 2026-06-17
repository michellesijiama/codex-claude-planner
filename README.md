# Claude ↔ Codex 协作工具

让 **Codex 当家干活、Claude 当高层规划顾问**,同时尽量少花贵的 Claude token。

核心思路:你在自己喜欢的 **Codex 界面**里工作,Codex 干所有执行的活;遇到需要高层
规划/架构决策时,Codex 通过一个 MCP 工具去"请教"Claude。Claude 很少被叫,所以省钱。

```
你 ──→ Codex 界面 ──执行/改代码──→ 项目
            │
            │ 需要规划时,调用 MCP 工具 plan / consult
            ▼
   claude-planner(独立 MCP 进程,沙箱外、有网络)
            │ 内部调 claude -p
            ▼
        Claude 出计划(很少被叫 → 省钱)
```

## 组成

| 文件 | 作用 |
|------|------|
| `claude_planner_mcp.py` | MCP 服务:把 Claude 暴露成 `plan` / `consult` 两个工具给 Codex。纯标准库,零依赖。 |
| `AGENTS.md` | 告诉 Codex 何时该委派给 Claude、何时自己干(放进项目根目录即生效)。 |
| `orchestrate.py` | **可选**的全自动无头编排器(无界面,看 `mission.md` 进度)。用于放手批处理,不是主路径。 |

## 已完成的安装

`claude-planner` 已注册进全局 Codex 配置 `~/.codex/config.toml`:

```toml
[mcp_servers.claude-planner]
command = "/usr/bin/python3"
args = ["/Users/sijiama/Desktop/Projects/自动化插件/claude_planner_mcp.py"]
startup_timeout_sec = 30
tool_timeout_sec = 300        # Claude 规划约需 40-70 秒,留足超时

[mcp_servers.claude-planner.env]
CLAUDE_BIN = "/opt/homebrew/bin/claude"
CLAUDE_PLANNER_MAX_USD = "1.0"   # 单次规划预算上限
CLAUDE_PLANNER_MODEL = "sonnet"  # 规划模型;想更强可改 opus(更慢更贵)
```

改前的配置已备份在 `~/.codex/config.toml.bak-before-claude-planner`。

## 怎么用

1. **重启 Codex.app**(让它加载新的 MCP 服务)。
2. 正常在 Codex 里干活。
3. 当任务较大/较模糊时,让 Codex 调 `plan`(或它自己会调);卡在高层决策时调 `consult`。
   - 例:在 Codex 里说"用 claude-planner 的 plan 工具把这个需求拆成步骤再做"。
4. **第一次调用会弹批准框** —— 点同意(可勾"始终允许"以后免点)。
5. 等约 1 分钟,Claude 的计划会返回给 Codex,Codex 按计划执行。

## 两个工具

- `plan(goal, project_dir)` —— 把目标拆成有序、可执行步骤(含退出码验收命令)。
- `consult(question, project_dir)` —— 就架构/选型/取舍给出果断建议。

传参时把当前项目的**绝对路径**作为 `project_dir`,Claude 会据此检视项目。

## 可调项

- 想要更强规划:把 `CLAUDE_PLANNER_MODEL` 改成 `opus`(更慢、更贵)。
- 想看 MCP 调试日志:在 env 里加 `CLAUDE_PLANNER_LOG = "/tmp/claude-planner-mcp.log"`。
- 卸载:`codex mcp remove claude-planner`。

## 已知特性

- 每次 `plan` 约 40-70 秒(Claude 读项目 + 思考),属正常。
- 无头 `codex exec` 下 MCP 调用会因无人批准而取消;交互式 app 里点同意即可。这是 Codex 的
  批准机制,不是 bug。
