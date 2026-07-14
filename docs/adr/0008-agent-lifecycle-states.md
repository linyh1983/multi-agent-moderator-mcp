# ADR-0008: Agent Lifecycle States（Agent 生命周期状态）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 2 轮。

## Context

主计划的 state schema 列出 7 个状态：`starting|running|paused|offline|stuck|stopped|error`。REQUIREMENT 未明确枚举状态，但提及：

- "启动" → `starting`
- "Agent 发出后必须暂停等待" → 某种行为契约状态（第 1 轮已定为 `blocked`，见 [Glossary: Blocked agent](../glossary.md#blocked-agent--被阻塞-agent)）
- "卡住、离线或输出无法解析的内容" → 异常处理（`stuck` / `offline` / 解析警告）
- "停止并清理" → `stopped`

`paused` 在 REQUIREMENT 中从未出现，且与"等审批的 `blocked`"语义容易混淆。`stuck` 的精确语义（基于 stdout 字节还是基于 marker 字节）需要钉死，否则 `find /` 这类长时间静默执行的 Agent 会被误报 stuck。

## Decision

### 2.1 状态集合：删除 `paused`，新增 `blocked`

**最终 `AgentState` 枚举（7 个值）：**

```
starting  → running      (start_session 成功后自动转)
running   → blocked      (Agent 发 【申请执行】 后转)
running   → stuck        (last_output_at 超过 stuck_threshold_minutes 且 tmux 存活)
running   → offline      (tmux 会话在远端消失)
running   → stopped      (moderator 调 stop_session 后)
blocked   → running      (针对该 action_id 的 approve / reject 到达后)
stuck     → running      (poll cycle 重新读到 stdout 后)
offline   → running      (tmux 会话重新出现在远端 + 读到 stdout 后)
stuck     → stopped      (moderator 决定收尾)
offline   → stopped      (moderator 决定收尾)
any       → error        (start_session 内部抛异常)
error     → stopped      (moderator 调 stop_session 后)
stopped   → (terminal; record 保留，见 2.6)
```

**删除 `paused` 的理由：** 没有命名得出会落到它的迁移。如果将来需要"moderator 手动暂停某个 Agent"的语义，再加回来——YAGNI。

**`blocked` 与 `running` 区分的理由：** 这是 REQUIREMENT 写明的行为契约（"必须暂停等待"），不是礼貌。把 `blocked` 提升为独立状态，让 `check` 能高亮显示、让 worker 知道不要在 `blocked` 状态下对其他 marker 做特殊处理。

### 2.2 Stuck 的语义：基于 stdout 字节，不用 marker 字节

- **判定公式**：`now - last_output_at > stuck_threshold_minutes` 且 `is_alive()`（tmux 会话存在）。
- **`last_output_at` 的更新**：worker 的 `read_new()` 只要读到任何字节就更新，**不论是不是 marker**。
- **含义**：Agent 跑 `find /` 10 分钟没输出 = stuck。这与 REQUIREMENT "Agent 卡住、……系统需要检测这些异常并在 check 中醒目提示" 一致。
- **不引入独立 marker-stuck 阈值**：marker 速率作为另一个指标可在 `check` 里展示，但不作为状态转换的触发器。

### 2.3 stop_session 的 grace period：默认 30 秒，可配置

- **API 形状**：`stop_session(agent_name: str, force: bool = False, grace_seconds: int | None = None)`
- **默认**：`grace_seconds = 30`。
- **优先级**：调用参数 `grace_seconds` 覆盖默认值；`force=True` 跳过 grace 直接 kill tmux。
- **grace 期内**：服务器向 Agent 发 `<moderator-cue>please wrap up; you have N seconds</moderator-cue>`（writeback tag），然后等。
- **grace 期满**：发 `tmux kill-session`（catch `tmux has-session` 失败，不抛错）。
- **`stopped` 时间戳**：写 `decided_at` 到 AgentRecord。

### 2.4 Offline 转换：1 次失败 poll + 1 次重试

- **判定流程**：
  1. worker 调 `is_alive()` 返回 `False`（tmux 不存在）。
  2. 重试 1 次（间隔 = 当前 poll interval，默认 2 秒）。
  3. 仍 `False` → 状态置 `offline`，记录 `offline_at` 时间戳。
- **不立即转 `offline`**：容忍 SSH 瞬时抖动或远端 `tmux has-session` 的小延迟。
- **`offline` 与 `error` 的区别**：
  - `offline` = 启动成功后，远程会话消失（瞬态，可恢复）。
  - `error` = 启动失败（持久，直到 moderator 干预）。
- **恢复路径**：`tmux has-session` 重新为 true **且** worker 能读到 stdout → `running`。

### 2.5 Error 永不自动恢复

- **不自动重试**。
- **moderator 干预路径**：`stop_session(agent_name)` → 状态 `stopped` → 再 `start_session(...)` 创建新 AgentRecord。
- **理由**：贴合 REQUIREMENT "系统不应该代替 Agent 做决定或自动修复 Agent 的问题"。

### 2.6 Stopped 记录保留

- **`stop_session` 不删除 AgentRecord**。
- **保留字段**：`name`, `host`, `started_at`, `stopped_at`, 最终状态（`stopped` / `error`），`error`（如果有），`role_prompt_path`, `additional_agents`。
- **保留用途**：事后复盘（"那个 Agent 究竟跑了什么？为什么被停？"）。
- **可选清理**：未来可加 `--prune-stopped-after` CLI 命令（v1 不实现）。

## Consequences

### 正面

- **状态集合小而清晰**：7 个状态，每个都有命名的迁移路径。
- **`blocked` 显式**：用户能在 `check` 看到"该 Agent 在等你审批"，避免"为什么它停了？"的困惑。
- **`stuck` 语义明确**：不再有"沉默执行算不算 stuck"的争论。
- **grace period 可配置**：moderator 在收尾时不必干等 30 秒。

### 负面 / 后续工作

- **`offline` 与 `stuck` 在 `check` 中需要视觉区分**（高亮样式不同），UI 不在 v1 范围内，靠文字标识。
- **grace period 期间 Agent 可能继续发 marker**：这些 marker 仍会进入 parser / state，但 Agent 已经被通知要收尾；不一致窗口期最多 30 秒。可接受。
- **`error → stopped` 后如何重新 `start_session`**：现有 AgentRecord 不能原地恢复（`error` 是终态），moderator 必须用不同的 `agent_name` 或先 `stop_session` 再 `start_session`。文档化。
- **offline 状态恢复的延迟**：最长 2 × poll_interval（默认 4 秒）才从 offline 转回 running，poll interval 是 worker 的全局配置。

### 交叉引用

- Glossary: [Agent](../glossary.md#agent)、[AgentRecord](../glossary.md)（待补）、[Blocked agent](../glossary.md#blocked-agent--被阻塞-agent)、Stuck / Offline / Error / Stopped agent（待第 2 轮补）
- ADR-0009: 审批队列语义（`blocked` 解除条件详见此 ADR）
- ADR-0014: state schema v1 对账（`stopped_at` 字段的添加待此 ADR 确认）

## Alternatives Considered

- **保留 `paused`**：需要命名至少一个会落到它的迁移（moderator 手动暂停）。v1 没用例，删除。
- **`stuck` 基于 marker 字节**：会导致 `find /` 这类合理静默被误报。不采用。
- **grace period 固定 30 秒**：增加 moderator 操作负担（每个 Agent 都需要不同宽限时无法定制）。不采用。
- **error 自动重试**：违背 "不代替 Agent 做决定"。不采用。
- **stopped 记录 30 天 TTL**：v1 数据量小，postmortem 用例需要长期保留。不采用。