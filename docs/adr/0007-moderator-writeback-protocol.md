# ADR-0007: Moderator Writeback Protocol（moderator 写回协议）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 6 轮 6.1。

## Context

服务器往 Agent stdin 写两类内容：

1. **业务写回**：`<moderator-approve>` / `<moderator-reject>` / `<moderator-cue>` —— 改变 Agent 状态或引导 Agent 行为。
2. **Ack / 状态通知**：`<moderator-info>` —— 服务器**仅**确认收到了 Agent 的请求，不要求 Agent 采取行动。

REQUIREMENT 没明确 ack 是否每次必发；这是设计决策。Agent 看到 ack 才知道"我的 `【申请执行】` 已经在 moderator 视野里"，但 ack 太频繁又会污染 Agent 的上下文窗口。

Marker 用全角方括号 `【…】`，writeback 用尖括号 `<moderator-…>`，两者视觉上明确区分。

## Decision

### 写回 tag 语法

**所有写回 tag 都以 `<moderator-<kind> …>` 开头**：

```xml
<moderator-approve action-id="a-3" note="LGTM，保持 200 行内">
<moderator-reject action-id="a-7" reason="缺少 email 字段校验">
<moderator-cue target="<agent_name>|all">…</moderator-cue>
<moderator-info action-id="a-3" status="queued">
<moderator-info kind="undelivered" target="X">peer X 不可达</moderator-info>
<moderator-info kind="truncated" marker="申请执行">your last marker was truncated at 64 KiB</moderator-info>
<moderator-info kind="parse-warning" detail="嵌套 opener 已忽略">…</moderator-info>
```

### 业务写回（前 3 种）

完整语义见 ADR-0009。这里只列语法骨架：

| Tag | 必填字段 | 选填字段 | 触发场景 |
|---|---|---|---|
| `<moderator-approve>` | `action-id` | `note` | moderator 批准 |
| `<moderator-reject>` | `action-id` | `reason` | moderator 驳回 |
| `<moderator-cue>` | `target` | — | moderator 主动发声 |

`<moderator-cue>` 允许 `target="all"` 给所有 Agent 群发。

### Ack / 状态通知（`<moderator-info>`）

**6.1 决议：每次收到 `【申请执行】` 都发 `<moderator-info action-id="a-X" status="queued">` ack。**

#### Ack 类型清单

| `kind` 或属性 | 触发场景 | 解 block? |
|---|---|---|
| `status="queued"` | 收到 `【申请执行】`，已落 `state.actions` | **否**（确认而非批准） |
| `kind="undelivered" target="X"` | `【TO:X】` 路由失败（X 不在白名单或状态不可达） | 否 |
| `kind="truncated" marker="..."` | marker 内容超 64 KiB 被截断 | 否 |
| `kind="parse-warning" detail="..."` | marker 嵌套 / 畸形 / 不在白名单 | 否 |

**为什么 ack 不解 block（贴合 ADR-0009 单等不变量）：**

- ack 仅是服务器确认"收到了"，不是决定。
- Agent 收到 ack 仍需等待对应 `action-id` 的 `<moderator-approve>` / `<moderator-reject>`。
- 避免 Agent 误以为"moderator 已处理"，提早返回业务推进。

**为什么不只 ack 一次：**

- Agent 可能在重发（网络抖动、心跳）。
- 每次 ack 是显式的"在线信号"——moderator 知道 Agent 在监听 stdin。
- ack 信息密度低（~50 字节），不会污染 Agent 上下文。

#### ack 内容格式

```
<moderator-info action-id="a-3" status="queued">
```

- 单行。
- 不带 `note`（note 留给 `<moderator-approve>` 的语义合并）。
- v1 不发"delivered to peer" ack（peer 是否真的收到由 peer 自己报告 `【进度汇报】` 来确认）。

### 传输方式

- **短单行**：直接 `tmux send-keys`，回车提交。
- **多行 / 长内容**：tmux `load-buffer` + `paste-buffer`（避免 send-keys 转义问题）。
- **大小上限**：单 tag 内容 ≤ 16 KiB（与 marker 上限 64 KiB 不同，writeback 通常更短）。

### 解析协议

- 写回是**纯文本**，Agent 自己解析。
- Agent 实现约定：收到 `<moderator-…>` tag 时，提取属性 + 内容，按 `kind` 分发。
- 解析错误由 Agent 自己负责（不是 moderator 责任）。

## Consequences

### 正面

- **Agent 永远知道"我在不在 moderator 视野里"**：ack 频繁给确认。
- **明确区分"确认"和"批准"**：避免 Agent 误推进。
- **写回协议完整**：4 类业务 + 4 类状态通知，覆盖 v1 所有需要。
- **ack 信息密度低**：不污染 Agent 上下文窗口。

### 负面 / 后续工作

- **每次 `【申请执行】` 都 ack**：网络流量 +1 个 round-trip。可接受（writeback 路径已经存在）。
- **`<moderator-info>` 多属性不统一**（`action-id` 是属性，`kind` 也是属性，`target` 也是属性）：v1 可读；如果种类爆炸 v2 评估改为 `<moderator-info type="..." payload="...">`。
- **`<moderator-cue target="all">` 群发会被所有 Agent 看到隐私**（如 moderator 想给单个 Agent 私聊 → 用 `target="<agent_name>"`）。
- **Agent 必须自己解析 tag**：不强制 schema，靠 role_prompt 教会 Agent 解析规则。

### 交叉引用

- Glossary: [Writeback tag](../glossary.md#writeback-tag--写回标签)、[Single-wait invariant](../glossary.md#single-wait-invariant单等不变量)
- ADR-0006: marker 协议（marker 的写回目标）
- ADR-0009: 审批语义（approve/reject/cue 的语义）
- ADR-0010: marker 边界条件（`undelivered` / `truncated` / `parse-warning` 三种 ack 的来源）
- ADR-0011: 多 moderator（ack 也走文件锁）
- ADR-0013: marker 速率（写回速率与 marker 速率对称，10/s）

## Alternatives Considered

- **只在第一次 ack**：Agent 不知道后续重发是否到。**不采用**。
- **ack 解 block**：违反单等不变量。**不采用**。
- **不发 ack**：Agent 不知道 moderator 是否在线。**不采用**。
- **JSON-RPC 替代纯文本 tag**：需 Agent 实现 JSON 解析，复杂。**不采用**。
- **schema 强制**（Pydantic / JSON Schema）：Agent 端解析复杂。**不采用**，v1 靠 role_prompt 教学。