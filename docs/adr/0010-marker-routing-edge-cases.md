# ADR-0010: Marker Routing Edge Cases（Marker 路由边界条件）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 3 轮。

## Context

四种 marker 的**正则格式**已在 ADR-0006 锁定。但 REQUIREMENT 和旧主计划都没说明若干边界情况：

1. `【TO:target】` 中 target 不可达（离线 / 拼错名 / 不在发送方的 `additional_agents` 里）该怎么处理？
2. Agent 是否能发 `【TO:moderator】`？
3. `【进度汇报】` 内容是黑盒，`check` 工具该怎么渲染？
4. 跨 Agent 的 marker 是否需要全局全序？
5. 嵌套 / 畸形的 marker 怎么解析？
6. 单个 marker 的内容大小是否要限制？

这些都直接影响 `marker_parser.py` 的实现和 `check` 工具的 UX。

## Decision

### 3.1 不可达的 `TO:`：丢弃 + 日志告警 + 在 chat 中标记 `(undelivered)`

**判定不可达的三种情况：**

- target 不在发送方的 `additional_agents` 白名单里 → 路由拒绝（见 ADR-0005）。
- target 当前 `state ∈ {offline, error, stopped}` → 路由拒绝。
- target 在白名单内且状态 `running` / `blocked` / `starting` / `stuck`，但 worker 投递失败（SSH 抖动） → 重试 1 次后放弃。

**处理动作：**

- 仍写入 `state.chat` 一条记录，但 `kind="undelivered"`，`to_agent` 字段保留 target 名。
- 写一条 `MarkerParseWarning` 到 `state.progress[from_agent]` 列表。
- 通过 `<moderator-info>` writeback tag 告诉发送方："peer X 不可达，已记入群聊历史（undelivered）"。
- **不为 peer 聊天做"目标上线后补送"的队列** —— 顺序无保证，Agent 看到"过去的对话"反而困惑。

### 3.2 `TO:moderator` 禁止

- 解析时检测：`TO:moderator` 视为非法 marker。
- 行为：写 `MarkerParseWarning`，丢弃内容（不进 chat），writeback 告诉发送方"对 moderator 的消息请用【求助人类】(紧急) 或等待 cue (常规)"。
- **理由**：moderator 是"人"，不是 peer Agent，混在 `TO:` 路由里会让 `check` 的"群聊"语义模糊。
- **合法路径**：
  - **紧急**：Agent 发 `【求助人类】` → 进 `state.help_requests[agent]` → 在 `check` 中高亮。
  - **常规**：Agent 通过 `check` / `cue` 的反向（moderator 主动 cue Agent）等待 moderator 指令；Agent 自己不能"主动 ping moderator"（除了求助）。

### 3.3 进度汇报渲染：计数 + 最近 1–3 行

**`check` 工具输出格式：**

```
## Progress
- backend_agent: 7 reports, last 3 lines:
    > 完成接口设计
    > 等待前端字段确认
    > 开始实现路由
- frontend_agent: 3 reports, last 3 lines:
    > 收到后端字段
    > 草稿页面布局
    > …
```

**完整内容保留：** 写入 `state.progress[agent]`（list-of-records，FIFO 50 条）。

**用户想看完整内容怎么办：** moderator 调 `cue(target=agent, message="请贴出最近的进度汇报原文")` —— Agent 把内容通过 `【进度汇报】` 重新发一次（手动 repeat）。或者 v2 加 `check --expand-progress=agent`。

### 3.4 不需要全局全序

- **每 Agent 内部**：通过 worker 的 `partial_buffer` 状态机保证 FIFO（marker 按到达顺序解析、dispatch）。
- **Agent 间**：尽力而为。`state.chat` 列表里的消息**可能不是按全局时间戳严格排序的**，因为：
  - 每个 Agent 的 worker 是独立 asyncio task。
  - 写入 `state.chat` 是"跨 worker 共享状态"，会有锁竞争。
  - 即使按时间戳排序，"哪个 marker 先到服务器"也受网络抖动影响。
- **不试图实现全局全序**：成本高、收益低、且 Agent 间本来异步。
- **文档化**：在 `check` 的 chat 段落显式说明"消息可能不严格按时间顺序"。

### 3.5 严格解析 + 嵌套警告 + 畸形 MarkerParseWarning

**嵌套处理：**

```
【TO:foo】这是外层【TO:bar】这是内层【/TO:foo】   ← 嵌套 opener 在 body 内
```

- 检测到 body 内的 opener → 写 `MarkerParseWarning`（"nested marker ignored"）。
- 忽略内层 opener / closer，**只解析外层**（`【TO:foo】这是外层【TO:bar】这是内层【/TO:foo】` 中的 `这是外层【TO:bar】这是内层` 作为内容送给 foo）。

**畸形处理：**

- 未闭合（读到流末尾也没匹配 closer）→ 留在 `partial_buffer`，下次新 chunk 到达时再尝试匹配。如果 `partial_buffer` 长度 > 64 KiB 仍没闭合 → 视为畸形，丢弃，写 `MarkerParseWarning`（"unclosed marker, dropped"）。
- opener 出现但文本里没有 closer（已被截断）→ 同上。
- 未匹配的 closer（孤立的 `【/申请执行】`） → 写 `MarkerParseWarning`（"orphan closer, ignored"），不抛错。

**worker 不因此 crash**：写完警告后 worker 继续下一次 poll。

### 3.6 Marker 内容大小：64 KiB 上限，超出截断

- **每 marker 内容（opener 和 closer 之间）上限：65,536 字节**（UTF-8 编码后字节数）。
- **`partial_buffer` 总长度上限：65,536 字节**（与单 marker 上限对齐）。
- **超限行为**：
  - 单 marker 内容超 64 KiB → 截断到 64 KiB，写 `MarkerParseWarning`（"marker content truncated"）。
  - `partial_buffer` 超 64 KiB（多个未闭合 marker 累计） → 视为畸形，丢弃 buffer，写警告。
- **理由**：
  - 与 partial_buffer 上限对齐 → 简单。
  - 64 KiB 远超"自然"用法（普通进度汇报、申请执行说明、TO: 消息都在几 KiB 以内）。
  - 不限制的话，一个 Agent 粘贴整本小说会撑爆内存。

## Consequences

### 正面

- **不可达消息不丢历史**：写进 `state.chat` 让 moderator 事后能查"foo 不可达时收到了什么"。
- **`TO:moderator` 拒绝后语义清晰**：Agent 知道只有两条合法路径（紧急 / 等待 cue）。
- **进度汇报渲染紧凑**：`check` 一眼看完所有 Agent 的活跃度，不被超长汇报刷屏。
- **不需要全局全序**：实现简单，Agent 间异步的语义不被强加因果序。
- **解析错误可恢复**：写警告 + 继续，Agent 不会因为一个 marker 错就丢失后续所有输出。

### 负面 / 后续工作

- **不可达消息的"undelivered"记录永久留在 chat 里**：`state.chat` 增长更快。FIFO 上限（如 1000）兜底。
- **进度汇报全文不可见**：`check` 默认只显示 1–3 行；moderator 想看全文得 cue Agent。v2 加 `check --expand-progress=agent`。
- **跨 Agent 顺序混乱**：moderator 在 `check` 看到"foo 14:00 说 X，bar 13:55 说 Y"，可能困惑。文档化。
- **截断静默发生**：Agent 写了一篇 100 KiB 的执行说明，只保留前 64 KiB。它不知道内容被截了。**缓解**：writeback 提示"your last 【申请执行】was truncated at 64 KiB"。
- **嵌套处理可能误判**：`【TO:foo】看我代码：if x == 1: print("【申请执行】")【/TO:foo】` 中的字面 `【申请执行】` 会被当作嵌套 opener 跳过。这要求 Agent 角色 prompt 教"不要在 marker body 里写 opener 字面值"。

### 交叉引用

- Glossary: [Marker](../glossary.md#marker--标记)、[Writeback tag](../glossary.md#writeback-tag--写回标签)
- ADR-0005: routing allowlist（target 不在白名单的处理）
- ADR-0006: marker 协议（marker 的格式定义）
- ADR-0007: writeback 协议（"已截断 / 不可达"等 ack tag 的格式）
- ADR-0014: state schema v1 对账（`state.progress` 改 list-of-records 的形态）

## Alternatives Considered

- **`TO:` 目标不可达时排队补送**：实现复杂（target 上下线事件 + outbox 状态机），且让 Agent 看到"过去的对话"语义混乱。**不采用**。
- **`TO:moderator` 合法**：moderator 与 peer Agent 混在同一个 chat 流里会让 `check` 输出混乱。**不采用**。
- **进度汇报全文渲染**：check 输出可能很长，moderator 一眼看不清全局。**不采用**。
- **跨 Agent 全局全序**：实现成本（全局锁 / 多机时钟同步）远超收益。**不采用**。
- **畸形 marker 静默丢弃不警告**：Agent 不知道自己的输出有问题。**不采用**。
- **Marker 内容不限大小**：partial_buffer 占用不可控。**不采用**。