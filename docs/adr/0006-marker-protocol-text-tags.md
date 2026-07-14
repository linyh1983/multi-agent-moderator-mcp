# ADR-0006: Marker Protocol — Text Tags（marker 协议：纯文本 tag）

## Status

Accepted — 2026-07-13（主计划 locked decision + R3/R5/R7 决议整合）。

## Context

Agent 通过 stdout 输出**结构化文本段**与系统交互。这些段称为 **marker**。REQUIREMENT 第 15 行："标记是 Agent 输出中的固定格式文本块，由 MCP Server 解析和路由。设计考量是使用纯文本标记而不是 API 调用，因为 Agent 运行在标准终端中，标记可以通过简单的文本解析提取，不依赖任何特殊通信机制。"

候选：

| 选项 | 优 | 劣 |
|---|---|---|
| **纯文本 marker**（本 ADR） | 简单、人类可读、Agent 端零依赖 | 解析容错复杂；嵌套 / 畸形要处理 |
| **JSON-RPC over stdout** | 结构化严格 | Agent 必须实现 JSON-RPC；污染 stdout |
| **自定义二进制协议** | 紧凑 | 不可读；调试难 |
| **sidecar HTTP 调用** | 解耦 | 增加远端依赖（每个 Agent 都要 HTTP server） |

REQUIREMENT 明确选纯文本："通过简单的文本解析提取，不依赖任何特殊通信机制"。

## Decision

**Marker 协议 = Agent stdout 中的纯文本结构化段，格式 `【<opener>】<content>【/<closer>】`。**

### 四种 marker

| Marker | opener | closer | 方向 | 触发动作 |
|---|---|---|---|---|
| 进度汇报 | `【进度汇报】` | `【/进度汇报】` | Agent → 服务器 | 写 `state.progress[agent]`，FIFO 50 |
| Agent 间消息 | `【TO:<agent_name>】` | `【/TO:<agent_name>】` | Agent → Agent | 写 `state.chat` + 写回目的 Agent |
| 执行申请 | `【申请执行】` | `【/申请执行】` | Agent → 服务器 | 创建 ApprovalAction + ack |
| 求助人类 | `【求助人类】` | `【/求助人类】` | Agent → moderator | 写 `state.help_requests` + 高亮 |

`【…】` 全角方括号——视觉上与 marker 协议的"全角"语义对齐。

### 解析流程（worker 每 poll cycle）

1. **读增量**：从 `log_offset` 起读 stdout 新字节。
2. **append 到 `partial_buffer`**：Agent 端的 marker 可能跨多次 poll。
3. **正则匹配 4 种 opener**：第一个匹配的优先；其余 `partial_buffer` 内暂存。
4. **找对应 closer**：匹配到完整 `<opener>…<closer>` 后提取 content，dispatch。
5. **dispatch**：
   - 进度汇报：写 `state.progress[agent]`（FIFO 50，详见 ADR-0014）。
   - TO:：路由白名单校验（ADR-0005）+ 状态校验（ADR-0010）+ 写 `state.chat`。
   - 申请执行：创建 ApprovalAction + ack `<moderator-info status=queued">`（ADR-0007）。
   - 求助人类：写 `state.help_requests`。
6. **dedup**：`sha256(opener+content+closer)` 写入 `seen_marker_hashes`（FIFO 5000）；重复丢弃。

### 边界条件（完整在 ADR-0010）

- **嵌套 opener**：body 内出现 `【…】` opener → 警告 + 忽略内层 + 保留外层。
- **未闭合 / 畸形**：留 `partial_buffer`，超 64 KiB 仍不闭合 → 丢弃 + `MarkerParseWarning`。
- **孤立 closer**：警告 + 忽略。
- **64 KiB 上限**：每 marker 内容 ≤ 64 KiB；`partial_buffer` 总长 ≤ 64 KiB；超出截断 + 警告（详见 ADR-0013）。
- **`TO:moderator` 拒绝**：解析时硬失败 + `<moderator-info kind="parse-warning">`。

### 路由约束

- **`additional_agents` 白名单**：发送方 sender 在自己的白名单里列了 target ⇒ 路由合法（ADR-0005）。
- **目标状态校验**：target 不在 `running` / `blocked` / `starting` / `stuck` → 路由拒绝 + 标记 `(undelivered)` 写 `state.chat` + writeback 提示发送方（ADR-0010）。
- **不排队补送**：peer 聊天不保证顺序，target 上线后不补历史消息。

### dedup 全局 FIFO 5000

- `seen_marker_hashes` 是全局列表（跨所有 Agent），FIFO 5000。
- 详细理由与 marker 速率见 ADR-0013。

### 不做的事

- **v1 不做配置驱动 marker**：加新 marker = 改代码（ADR-0013）。
- **v1 不做 marker schema 校验**：靠正则 + 容错 + `MarkerParseWarning`。
- **v1 不做 marker 加密 / 签名**：明文（ADR-0012）。

## Consequences

### 正面

- **Agent 端零依赖**：写 stdout 即可，不需要 HTTP / RPC / 库。
- **人类可读**：用户 `tmux attach` 直接看到 marker。
- **纯文本容错**：嵌套 / 畸形可恢复（部分）。
- **契合 REQUIREMENT**："不依赖任何特殊通信机制"。

### 负面 / 后续工作

- **解析器要严格**：嵌套 / 畸形 / 截断都有边界 case；ADR-0010 文档化处理。
- **marker 速率压力**：狂发 marker 时依赖 dedup + worker 调度限速（ADR-0013）。
- **扩展新 marker 慢**：每次改代码（v1 决定；ADR-0013 §"marker 扩展路径"）。

### 交叉引用

- Glossary: [Marker](../glossary.md#marker--标记)、[Writeback tag](../glossary.md#writeback-tag--写回标签)（与 marker 方向相反）
- ADR-0007: 写回协议（ack / approve / reject / cue 的语法）
- ADR-0008: Agent 生命周期（worker poll cycle）
- ADR-0009: 审批语义（`【申请执行】` 的下游）
- ADR-0010: marker 边界条件（嵌套 / 畸形 / 大小 / 路由）
- ADR-0011: 多 moderator（marker 不跨进程）
- ADR-0012: state 文件不加密（marker 内容存 state 明文）
- ADR-0013: marker 速率（dedup + worker 限速 + marker 扩展）
- ADR-0014: state schema（progress / chat / help_requests / actions 形状）

## Alternatives Considered

- **JSON-RPC over stdout**：Agent 必须实现 RPC；污染 stdout 流。**不采用**。
- **二进制协议**：不可读、调试难。**不采用**。
- **sidecar HTTP**：远端依赖重。**不采用**。
- **半角 `[…]` 替代全角 `【…】`**：与 markdown / 代码块容易混淆。**不采用**，REQUIREMENT 已用全角。