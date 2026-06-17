# Claude–Codex 编排器 设计文档

日期：2026-06-17
状态：已实现并验证。后续方向调整见下方说明。

> **方向调整（2026-06-17 同日）**：实测中用户希望保留自己喜欢的 **Codex 交互界面**，
> 因此主路径从"无头脚本驱动"改为 **Codex 当家 + Claude 作为 MCP 规划顾问**。
> 最终落地与使用见 [README.md](../../../README.md) 和 `claude_planner_mcp.py`。
> 本文下方描述的 `orchestrate.py` 全自动无头编排器仍然可用、已验证，作为**可选**的批处理模式保留。

## 1. 目标

用**成本套利**省钱：Claude 贵、Codex 便宜。让 Claude 只做少量高层规划/思考，把大量改代码的体力活交给 Codex。核心指标是**尽量少消耗 Claude 的 token**。

## 2. 总体架构

一个独立的 **Python 调度脚本**（"工头"），在目标 git 仓库里运行：

```
orchestrate.py --goal "给 API 加 JWT 认证"
```

三个角色：

| 角色 | 怎么调用 | 花费 | 职责 |
|------|----------|------|------|
| 调度器（脚本） | 本地进程 | **0 token** | 拥有循环、读写状态文件、运行检查命令、管理 git |
| Claude Code | `claude -p`（无头） | 贵，**很少叫** | 开局规划一次；卡住时修复 |
| Codex | `codex exec`（无头） | 便宜，**重度用** | 执行每个子任务、实际改代码 |

**省钱原理一句话**：Claude 只看到 `目标 + 精简计划 +（偶尔）一条报错`；Codex 一次只看到一个子任务；两边都看不到对方的完整对话记录。

## 3. 共享文件 = 脚本管理的状态板

由**脚本**拥有的状态文件，不是两个 agent 互相轮询的频道。

- `mission.json` —— 机器可读的真实状态。
- `mission.md` —— 人类可读镜像，用户可 `tail -f` 实时看进度。

`mission.json` 结构（草案）：

```json
{
  "goal": "给 API 加 JWT 认证",
  "branch": "orchestrator/jwt-auth-20260617",
  "subtasks": [
    {
      "id": 1,
      "description": "新增 token 签发/校验工具",
      "check_command": "pytest tests/auth -q",
      "status": "done",      // pending | running | done | failed | escalated
      "attempts": 1,
      "commit": "a1b2c3d"
    }
  ],
  "log": ["..."]
}
```

关键：**没有 agent 盯着这个文件**。脚本读它，每次只把所需的一小片喂给对应 agent。脚本读文件不花 token（它不是模型）。

## 4. 主循环

```
plan = claude_plan(goal, repo_context)   # 唤醒 Claude 一次
write_state(plan)
create_branch()

for subtask in plan.subtasks:
    for attempt in 1..2:
        codex_exec(subtask)              # Codex 改代码
        if run_check(subtask.check_command) 通过:
            git_commit(subtask)          # 一个子任务 = 一次 commit
            mark done; break
    else:                                # 两次都失败
        decision = claude_repair(subtask, last_error)   # 唤醒 Claude
        apply(decision)                  # 重试 / 改计划 / 终止
    update_state_files()                 # 刷新 mission.json + mission.md
```

- **规划**：`claude -p`，提示词要求 Claude 检视仓库并输出符合 schema 的 JSON 计划（含每步的 check_command）。脚本解析。
- **执行**：`codex exec "<子任务描述 + 验收命令>"`，无头、可写工作区。
- **验收**：脚本直接在 shell 跑该子任务的 check_command —— 客观真值，不花 Claude token。
- **升级**：连续失败或 Codex 报 BLOCKED → 调 `claude_repair`，返回"修复指令（再交给 Codex）/ 修订计划 / 中止"。

## 5. 通用性（适配任何项目）

脚本本身**不懂任何具体项目**。项目知识来自两处：

1. **Claude 规划时现场读当前仓库**，把合适的 check_command 当作数据写进计划。
2. **可选**的每项目配置 `.orchestrator.json`（根目录），声明默认值；没有就让 Claude 推断：

```json
{ "test_cmd": "pytest -q", "build_cmd": "npm run build", "codex_sandbox": "workspace-write" }
```

同一个脚本可在任意仓库运行，每次只作用于当前 cwd 和它自己的分支，互不干扰。

## 6. 分支与合并冲突

1. **自动开专属分支**：`orchestrator/<goal-slug>-<日期>`，绝不直接碰 main。
2. **子任务串行 → 内部零冲突**：一次只做一个，前后排队，不并行，所以子任务之间不会互相冲突。每个验收通过 = 一次 commit（可单独回滚）。
3. **唯一真正的冲突**发生在最后合回 main 时 —— 跟普通 feature 分支一样，**默认留给人** review/合并，脚本不自动合 main。
4. **可选 worktree 隔离**：用 `git worktree` 给脚本单开目录+分支，不打扰用户当前 checkout。

## 7. 错误处理 / 安全

- **前置检查**：启动时确认 `claude`、`codex` 已安装、当前在 git 仓库内；缺失则报错退出。（注意：当前机器 **codex 未安装**，第零步需 `npm i -g @openai/codex` 或等价方式安装。）
- **升级次数上限**：每个子任务的 Claude 修复次数封顶（如 2 次），超过则停下、把控制权交还用户，不无限烧钱。
- **Codex 沙箱**：以 `workspace-write` 一类受限模式运行，改动全在专属分支，可 diff、可回滚。
- **检查命令无效**：视为规划缺陷，升级给 Claude 修正计划。
- **中断可恢复**：状态在 `mission.json`，可在中断后从上次进度续跑（后续增强）。

## 8. 不做的事（YAGNI）

- 不做并行子任务（串行已够，且避免冲突）。
- 不自动合并到 main。
- 首版不做 Web UI；进度通过 `mission.md` + 终端日志查看。
- 首版不做多模型可插拔（先锁定 Claude + Codex）。

## 9. 技术选型

- 语言：Python 3.12（本机已装）。
- 依赖：尽量只用标准库（`subprocess`、`json`、`pathlib`、`argparse`）。
- 形态：单个可执行 CLI 脚本 + 一个可选项目配置文件。
