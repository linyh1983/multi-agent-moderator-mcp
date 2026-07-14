# ADR-0017: No Auto-Restart on Offline（offline 不自动重启）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 6 轮 6.6。

## Context

Agent 进入 `offline` 状态（远端 tmux 会话消失）时，moderator 应该等多久？自动重启吗？自动重启多少次？

正面：自动重启让"网络抖动"场景下用户无需介入。

反面：

- REQUIREMENT 强调"系统不应该代替 Agent 做决定或自动修复 Agent 的问题"——重启属于"修复"。
- 自动重启可能让"Agent crash loop"无限循环（如果每次重启都崩回 offline）。
- moderator 可能需要看到 offline 状态来诊断问题。

## Decision

**offline 不自动重启。moderator 必须主动 `stop_session`（清理 offline 状态）后重新 `start_session`。**

### 行为

1. Agent 进入 `offline`（详见 ADR-0008）：
   - 1 次失败 poll + 1 次重试（间隔 = 当前 poll interval，默认 2 秒）后仍 `is_alive() == False` ⇒ 状态置 `offline`。
   - `state.agents[X].last_error` 字段记录最后一次 `is_alive()` 失败的 stderr / 错误码。
2. **不自动重启**：worker 不调 `start_session`，不发任何 tmux 命令。
3. **worker 持续观察**：offline 状态下，worker 仍按 poll interval 检查 `is_alive()`。
   - 如果 tmux 重新出现 + 读出新字节 ⇒ `offline` → `running`（恢复路径）。
   - 这种情况是"短暂中断后自愈"，moderator 可能根本不知道发生过。
4. **moderator 收到 `check` 时看到 offline**：可选择三种动作：
   - **等自愈**：`check` 提示"X 离线但可能在恢复中"。
   - **显式重启**：`stop_session(name=X)` → `start_session(name=X, ...)`。
   - **放弃**：`stop_session(name=X)`（不重启，记录保留为 stopped）。

### 不做的事

- **不做 retry budget**：v1 不限制"只能自动重启 N 次"——因为根本不做自动重启。
- **不做 offline 超时升级 error**：`offline` 永不自转 `error`；moderator 决定何时升级（用 stop + 重新 start；如果启动失败才进入 `error`）。
- **不做"网络抖动检测"**：简化。

### 何时升级

- 如果实际场景频繁出现"用户抱怨每次都要手动重启短暂中断的 Agent" → v2 加 `auto_restart: bool`（默认 `false`）+ `auto_restart_max_attempts: int`（默认 3）+ 指数退避。
- 触发条件：≥3 个独立报告，或主计划用户主动要求。

## Consequences

### 正面

- **贴合 REQUIREMENT**：不代替 Agent 做决定。
- **crash loop 不失控**：moderator 一定能看到 offline，避免 Agent 反复重启消耗资源。
- **诊断信息保留**：`state.agents[X].last_error` 让 moderator 知道"为什么 offline"。

### 负面 / 后续工作

- **短暂中断仍需手动重启**：网络抖动场景体验略差。
- **没有指数退避自动恢复**：v1 一律手动。
- **`offline → running` 自愈路径是隐式的**：moderator 看不到状态转换（除非调 `check` 看到状态变化）。

### 交叉引用

- Glossary: [Offline agent](../glossary.md#offline-agent)、[Error agent](../glossary.md#error-agent)
- ADR-0008: Agent 生命周期（`offline` 状态定义）
- ADR-0009: Agent stopped → 自动取消 pending actions（`stop_session` 清理）

## Alternatives Considered

- **自动重启**：违反 REQUIREMENT。**不采用**。
- **可配置自动重启（默认 false）**：v2 候选。**不采用**。
- **`offline` 超时升级 `error`**：模糊 offline / error 区别。**不采用**。
- **指数退避自动恢复**：复杂度 +1，需求未证实。**不采用**。