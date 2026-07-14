# ADR-0009: Approval Queue Semantics（审批队列语义）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 4 轮。

## Context

审批队列是整个 Moderator MCP Server **最核心** 的不变量："讨论自由，执行受控"。REQUIREMENT 写明"讨论可以自由，但改代码跑命令必须审批"——任何对这条边界的模糊都会破坏整套信任模型。

第 1 轮已定下基础语义（`blocked` 状态、`ApprovalAction` 状态机）。本 ADR 解决 6 个边界问题：block 怎么解除、批处理语义、反提案、自动取消、note vs cue、reject 隐私。

## Decision

### 4.1 单等不变量（Single-wait Invariant）

> **A blocked agent waits for exactly one approve/reject decision on exactly one action_id. Nothing else resumes it.**

**精确含义：**

- 一个 Agent 处于 `blocked` 状态 ⇒ 它**唯一**等待的是某个特定 `action_id`（记为 `waiting_for`）。
- 解除条件只有两种：
  - `approve(action_id == waiting_for)` ⇒ Agent 进入 `running`，可继续推进。
  - `reject(action_id == waiting_for)` ⇒ Agent 进入 `running`（按 reject reason 调整方案）。
- **不解除**的操作：
  - `cue(target=blocked_agent)` —— 写进 Agent 的 stdin 但不解除 block。Agent 收到 cue 时仍然不能继续推进，等它的 action_id 决定。
  - `approve(action_id != waiting_for)` —— 这是别的 Agent 的 action，与本 Agent 无关。
  - `reject(action_id != waiting_for)` —— 同上。
  - 任何 `TO:` peer 消息。
- **设计理由**：避免"moderator 随口 cue 一下，无意中唤醒了一个不该走动的 Agent"。

**例外（writeback ack）：** 服务器发 `<moderator-info action-id=X status=queued>` 这种 ack tag **不**算解除 block，只是确认"你的申请我收到了"。Agent 必须等 approve/reject。

### 4.2 批量 approve / reject：全有全无

**`approve(action_ids: list[str], note?: str)` 行为：**

1. 校验阶段（**先全校验，再 apply**）：
   - 每个 `action_id` 必须存在。
   - 每个 `action_id` 必须处于 `pending` 状态。
   - 如果任一 id 校验失败 → 整个调用返回错误，**不改任何状态**。错误信息列出所有 bad ids。
2. Apply 阶段：
   - 一次性把全部 actions 状态改为 `approved`，写 `decided_at` + `decided_by`。
   - 写入 `state.actions` 的永久日志（每条 action 都记录）。
3. Writeback：
   - 对每个被 approve 的 action，发 `<moderator-approve action-id=X note="...">` 给对应 Agent。
   - 解除每个对应 Agent 的 block（如果它正 blocked 在这个 action_id 上）。

**`reject(action_ids, reason)`** 同理，全有全无。

**理由**：避免"批准了前 3 条但第 4 条失败"的中间状态——moderator 必须明确知道结果。

### 4.3 Reject 只拒绝，反提案走 cue

- `reject(action_id=X, reason="缺少 email 字段校验")` **只做一件事**：把 X 置 `rejected`，writeback 告诉 Agent。
- **不会**自动创建一条新 pending action。
- 如果 moderator 想给反提案，**单独**调 `cue(target=agent, message="请用 Y 方案重新提交")`。
- **理由**：避免隐式 action 创建，让 Agent 的 action 序列与它的请求一一对应（moderator 容易追溯）。

### 4.4 Agent stopped → 自动取消其 pending actions

- `stop_session(agent_name)` 调用后：
  1. 该 Agent 的所有 `pending` actions 状态置 `cancelled`。
  2. 每个 cancelled action 写入 `cancelled_at`、`cancelled_reason="agent_stopped"`。
  3. 已 `approved` / `rejected` 的 action 不动（保留历史）。
- **不删除** cancelled actions —— 仍是 `state.actions` 永久日志的一部分。
- **理由**：保持 `state.actions` 是"完整审计日志"，moderator 事后能复盘"那个 Agent 发了什么、为什么被停"。

### 4.5 Approve(note=...) 与 Approve() + Cue(...) 终态相同

**API 形状：**

```
approve(action_ids=["a-3", "a-7"], note="LGTM，保持 200 行内")
```

**实现：**

- `note` 直接嵌入 `<moderator-approve action-id="a-3" note="LGTM...">` writeback tag。
- Agent 收到一条 approve tag，里面既有 action_id 也有 note。
- 不需要再发 `cue`。

**vs 拆成 approve + cue：**

- 拆成两条会让 Agent 收到 `<moderator-approve ...>` + `<moderator-cue>...</moderator-cue>`，Agent 要把两者关联起来（哪个 cue 对应哪个 approve）。
- 合并成一条更原子、更易理解。

**文档化**：README 把 `approve(note=...)` 标为推荐路径。

### 4.6 Reject 隐私分级

**三级可见性：**

| 接收方 | 看到的内容 |
|---|---|
| **发起方 Agent**（writeback） | 完整 `reason` |
| **Peer Agents**（群聊历史 / `state.chat`） | reason 存在但带 `private_to_originator` 标签；`check` 工具在 peer 视角下渲染为 `[private: see agent X]`，不展开 |
| **Moderator**（`state.actions` 永久日志 + `check` 全部视角） | 完整 `reason`，无遮蔽 |

**为什么不让 peer 看到：**

- peer Agent 看到别人的 reject reason 可能产生不必要的信息噪音。
- 一个 Agent 的失误（"写错了 password 字段"）被广播给其他 Agent，可能让其他 Agent 误以为整个项目有该问题。
- moderator 看完整信息足够——他是决定者。

**实现：**

- `state.actions[X].reject_reason` 字段始终存完整 reason（moderator 用）。
- 写进 `state.chat` 的对应条目带 `private_to_originator=<originating_agent>` 标记。
- `check` 工具根据调用者身份（v1 只有 moderator，所以 moderator 总能看到）决定渲染级别。

## Consequences

### 正面

- **行为契约清晰**：`blocked` 的解除条件唯一，Agent 实现简单（"等我的 action_id 的 approve/reject"）。
- **批量审批可预测**：moderator 一眼能预测整批结果，不会出现"批了一半"的灰色地带。
- **审计完整**：`state.actions` 是永久日志，`stopped` / `cancelled` / `approved` / `rejected` 全部留痕。
- **Reject 不隐式创建新 action**：避免 Agent 困惑"我刚收到 reject，怎么又有一条新申请"。
- **Note 与 Approve 原子合并**：Agent 处理路径短。
- **Reject 隐私保护**：peer Agent 不被无关信息干扰。

### 负面 / 后续工作

- **cue 不解除 block**：moderator 想"紧急叫醒"一个 blocked Agent 的话，必须先 approve/reject。可以文档化为"先批准再说"。
- **批量粒度**：moderator 想"批准 a-3 但延迟 a-7" 做不到——必须分两次调用。可接受，moderator 本来就该按 action 粒度决策。
- **Cancelled action 仍占 state.actions 空间**：需要 FIFO 上限（v1 暂定 10000 条）。
- **Note 字数限制**（v1 未定）：超长 note 可能撑爆 writeback。建议上限 4 KiB。
- **Reject 隐私靠 `state.chat` 的标签实现**：peer Agent 实现如果忽略这个标签，会读到完整 reason。靠"role prompt 教 Agent 尊重 `private_to_originator`"。

### 交叉引用

- Glossary: [ApprovalAction](../glossary.md#approvalaction--执行申请)、[Approval queue](../glossary.md#approval-queue--审批队列)、[Blocked agent](../glossary.md#blocked-agent--被阻塞-agent)、[Single-wait invariant](../glossary.md#single-wait-invariant单等不变量)、[All-or-nothing batch](../glossary.md#all-or-nothing-batch全有全无批处理)
- ADR-0007: 写回协议（approve / reject / cue tag 的具体语法）
- ADR-0008: Agent 生命周期（`stopped` 状态转换）
- ADR-0014: state schema 对账（`cancelled_at`、`private_to_originator` 字段）

## Alternatives Considered

- **任何 approve / reject 都能解除 block**：Agent 不知道该等哪个决定。**不采用**。
- **批量部分 apply**：moderator 难预测结果。**不采用**。
- **Reject 自动创建反提案 action**：action 历史与 Agent 请求不对应。**不采用**。
- **Stopped 时直接删除 pending**：审计不完整。**不采用**。
- **Approve + note 与 approve + cue 走不同路径**：Agent 处理复杂。**不采用**。
- **Reject 原因对所有 Agent 可见**：peer Agent 受无关信息干扰。**不采用**。