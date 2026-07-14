# ADR-0005: Additional-Agents Routing Allowlist（additional_agents 路由白名单）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 6 轮 6.2 + 6.5。

## Context

Agent 之间通过 `【TO:<agent_name>】` 互相发消息。但任意两个 Agent 互相发消息会让群聊失控——`agent_A` 不该能 ping 它从未谋面的 `agent_B`。需要一个白名单机制告诉系统"哪些 Agent 之间可以互发"。

同时，`start_session` 时如果 moderator 误打了一个已存在的 `agent_name`，会发生什么？覆盖？拒绝？

## Decision

### 6.2 `additional_agents` 单向

**白名单是单向的**——`agent_B` 在自己的 `additional_agents` 列表里包含 `agent_A` ⇒ `agent_A` 发 `【TO:agent_B】` 时路由合法；`agent_B` 发 `【TO:agent_A】` 时路由非法（除非 A 也列了 B）。

- **理由**：让"被动接受者"控制谁能联系它。`agent_B` 决定"我愿意听谁说话"，不需要每个 sender 都反向配置。
- **不影响 `【求助人类】` 和 `【进度汇报】`**：这两种 marker 的去向是服务器而非 peer，白名单不约束。
- **不影响 `【申请执行】`**：执行申请的去向是 moderator，无白名单约束。

**实现：**

```python
# 发送方是 agent_A，发 TO:agent_B
if agent_B not in state.agents[agent_A].additional_agents:
    writeback(agent_A, "<moderator-info kind='parse-warning' detail='unknown peer X'>")
    return  # 不进入 state.chat
```

**路由约束补充（ADR-0005 §"Routing enforcement"，贴合第 7 轮 7.6）：**

- 发送方 sender 在自己的 `additional_agents` 中列了 target ⇒ 路由合法。
- 否则 → 解析时硬失败 + 写 `<moderator-info kind="parse-warning">` 给 sender。
- 不丢弃到 `state.chat`（不能让 sender 误以为消息已送达）。
- 不在 sender / target 任一方离线时补送（与 ADR-0010 路由拒绝一致）。

### 6.5 同名 Agent 全局拒绝

**`agent_name` 是全局唯一主键。** 不论 host 是否相同，`start_session(name="X")` 发现 `state.agents` 已含 X ⇒ 拒绝。

**行为：**

- `start_session(name="X")` 返回错误 `AgentAlreadyExists(name="X", host=<旧 host>, state=<旧 state>)`。
- moderator 必须先 `stop_session(name="X")`（即使是 stopped / offline / error 状态），再 `start_session(name="X")`。
- `stopped` 状态的 AgentRecord **保留**（见 ADR-0008），所以 `start_session` 用同名仍会被拒绝——moderator 想"复用"旧名时必须明确 `stop_session + delete_record`（v2 候选 API）。

**为什么跨主机同名也拒绝：**

- `agent_name` 是 moderator 与 Agent 交互的唯一标识（所有 MCP 工具的入参）。两个同名 Agent 让 moderator 不知道命令发给了谁。
- `state.agents` 以 `agent_name` 作主键（dict 语义），同名会被后者覆盖——比拒绝更危险（覆盖可能让 moderator 误以为旧 Agent 已死）。
- 命名空间冲突应该由 moderator 自觉避免（如 `frontend_dev_host_a` / `frontend_dev_host_b`），不靠系统宽容。

**为什么 `stopped` 状态同名也拒绝：**

- `stopped` 是 postmortem 保留——记录里可能有重要信息（`error`、`last_output_at` 等）。
- moderator 想"重启"一个旧 Agent 时，必须用 `delete_record`（v2 候选）或显式接受"旧记录被新进程接管"（v1 拒绝）。
- 强制 moderator 手动清理，避免误覆盖。

### 不做的事

- **不做自动重命名**（如冲突时附加后缀 `-1`）：掩盖冲突，对 moderator 是反直觉。
- **不做"同主机同名 = 不同身份"**：违反全局唯一性。
- **不做 `start_session --force`**（强制接管）：v2 候选，v1 强制走 stop + start。

## Consequences

### 正面

- **路由白名单单向**：配置负担低（接收方一次声明即生效）。
- **同名拒绝**：避免 moderator 误把命令发错对象。
- **`stopped` 保留 + 同名拒绝**：postmortem 不丢，moderator 也不误覆盖。

### 负面 / 后续工作

- **跨主机场景 moderator 命名负担大**：`frontend_dev_a` / `frontend_dev_b` 容易出错。建议 README 命名规范。
- **复用旧名要手动清理**：v2 加 `delete_record(agent_name)` API。
- **白名单单向需要主动同步**：如果 A 想发给 B，但 B 没列 A，A 没法"申请加入 B 的白名单"——需要 moderator 调 `stop_session + start_session` 重新配 B。
- **没有"全员可发"模式**：v1 没有 `additional_agents: ["*"]` 之类的通配；moderator 必须显式列每个。

### 交叉引用

- Glossary: [Agent](../glossary.md#agent)、[Marker](../glossary.md#marker--标记)（`TO:` 路由）
- ADR-0007: writeback 协议（`undelivered` / `parse-warning` ack）
- ADR-0008: Agent 生命周期（`stopped` 记录保留）
- ADR-0010: marker 边界条件（路由拒绝行为）

## Alternatives Considered

- **`additional_agents` 双向**：双方都必须列对方才允许路由。**不采用**，配置负担 ×2。
- **同名跨主机视为不同身份**：违反全局唯一。**不采用**。
- **同名自动覆盖 / 加后缀**：掩盖错误。**不采用**。
- **`stopped` 状态可复用同名**：postmortem 信息丢失风险。**不采用**。
- **`start_session --force` 接管**：v2 候选，v1 不强制。