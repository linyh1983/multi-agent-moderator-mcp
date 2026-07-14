# ADR-0014: State Schema v1 Reconciliation（state schema v1 对账）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 7 轮 7.1 / 7.2 / 7.3 / 7.4 / 7.7。7.5 / 7.6 / 7.8 见交叉引用。

## Context

第 7 轮探索阶段标记了 `.test-state/moderator_state.json` 实际形状与 REQUIREMENT / 主计划预期之间的 **10 项缺口**。本 ADR 解决 schema 形状相关的 5 项（7.1 / 7.2 / 7.3 / 7.4 / 7.7）。其余 3 项已被之前 ADR 覆盖（7.5 → ADR-0011 文件锁；7.6 → ADR-0005 路由硬失败；7.8 → ADR-0013 全局去重）。

## Decision

### v1 state schema 顶层结构

```json
{
  "schema_version": 1,
  "created_at": "2026-07-13T...",
  "updated_at": "2026-07-13T...",

  "agents": { "<agent_name>": AgentRecord, ... },
  "actions": { "<action_id>": ApprovalAction, ... },
  "chat": [ ChatMessage, ... ],
  "progress": { "<agent_name>": [ ProgressEntry, ... ], ... },
  "help_requests": [ HelpRequest, ... ],

  "seen_marker_hashes": [ "<sha256>", ... ],   // FIFO 5000
  "action_seq": 0
}
```

### 7.2 AgentRecord 的 `state` 字段：Pydantic enum 为真源

```python
# core/models.py
from enum import Enum

class AgentState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    BLOCKED = "blocked"
    STUCK = "stuck"
    OFFLINE = "offline"
    STOPPED = "stopped"
    ERROR = "error"
```

- **真源**：`core/models.py` 的 Pydantic enum。
- **镜像**：`docs/glossary.md` §4 的 7 值表格（人类阅读用）。
- **不写两份代码**：`glossary` 是文档，不被 import；Pydantic 才是运行时校验。
- **不强制 CI 校验**：v1 接受"人工同步"；如果漂移频繁 → v2 加 lint。

### 7.1 ApprovalAction：永久日志

```python
class ApprovalAction(BaseModel):
    id: str                         # "a-<seq>"
    seq: int                        # 取自 action_seq
    agent_name: str
    content: str                    # 申请内容（来自 marker body）
    status: Literal["pending", "approved", "rejected", "cancelled"]
    created_at: datetime

    decided_at: datetime | None = None
    decided_by: str | None = None   # moderator 标识（v1 单 moderator）
    reject_reason: str | None = None  # 完整文本，仅发起方可见完整（peer 视角脱敏，见 ADR-0009 §6）

    cancelled_at: datetime | None = None
    cancelled_reason: str | None = None  # 如 "agent_stopped"
```

- **永久日志**：所有 status 终态都保留在 `state.actions`，**不删除**。
- **FIFO 触发 SQLite 迁移**：超过 10000 条 → 触发 ADR-0004 迁移条件。
- **不删除 cancelled**：保留 `cancelled_at` 让事后复盘"为什么这条没批"。

### 7.3 ChatMessage 的 `kind` 字段

```python
class ChatMessage(BaseModel):
    id: str
    ts: datetime
    kind: Literal["peer", "cue", "system"]
    from_agent: str | None          # peer / cue 来源；system 消息为 None
    to_agent: str | None            # peer 目标；cue "all" 时为 None；system 消息为 None
    text: str                       # 消息正文
    metadata: dict | None = None    # cue target / system ack kind 等
```

| `kind` | 来源 | 例子 |
|---|---|---|
| `peer` | Agent A → Agent B 的 `【TO:B】` | "前端提议字段名为 `email`" |
| `cue` | moderator 的 `<moderator-cue target=X>` 或 Agent 的 `【求助人类】` | moderator "前端请确认字段" / Agent "数据库连不上" |
| `system` | 服务器自动生成的提示 | "<moderator-info kind='parse-warning'>已忽略嵌套" |

- **不存进度汇报**：进度进 `state.progress`，不进 `state.chat`（避免污染群聊历史）。
- **不存审批回执**：approve/reject 不进 `state.chat`（保留在 `state.actions` 的状态变化里）。
- **cue target=all**：metadata 字段存 `{"target": "all"}`；`to_agent=None` 表示群发。

### 7.4 HelpRequest 生命周期：cue 响应即移除

```python
class HelpRequest(BaseModel):
    id: str
    agent_name: str
    content: str
    ts: datetime
```

- **进入**：`【求助人类】` 触发 → push 到 `state.help_requests`。
- **离开**：moderator 调 `cue(target=<agent_name>, ...)` 时，服务器扫描 `state.help_requests` 删除匹配 `agent_name` 的条目。
- **为什么是 cue 而非 approve/reject**：approval 流程不解决"求助"。moderator 主动 cue 是显式响应。
- **不存 resolved_at**：离开即删除，不留永久记录。
- **多请求累积**：如果 moderator 在响应前又收到同一 Agent 的多个 `【求助人类】`，都保留（每个 id 独立）。
- **可视性**：`check` 工具显示 `help_requests` 列表（高优先级）+ 标记 `(X 个 Agent 待响应)`。

### 7.7 state.progress：list-of-records，每 Agent 最多 50 FIFO

```python
class ProgressEntry(BaseModel):
    ts: datetime
    text: str                       # 进度汇报全文（marker body）
```

- **形状**：`{ "<agent_name>": [ProgressEntry, ...] }`，每 Agent 最多 50 条。
- **FIFO 淘汰**：超过 50 → pop 最早一条。
- **时间戳必填**：Agent 发进度时由服务器补（Agent 不必自带时间）。
- **`check` 工具渲染**：
  - 默认：每 Agent 显示**计数 + 最近 1–3 行**。
  - 完整内容在 `state.progress`（moderator 可展开看）。
  - moderator 用 `cue` 可让 Agent 再汇报。

### 7.5/7.6/7.8 引用确认

| 缺口 | 决议出处 |
|---|---|
| 7.5 state OCC | ADR-0011：v1 单进程 + 文件锁够用；OCC 延期 |
| 7.6 additional_agents 硬失败 | ADR-0005：解析时硬失败 + `<moderator-info kind="parse-warning">` |
| 7.8 dedup 全局 vs 每 Agent | ADR-0013：全局 FIFO 5000；每 Agent 会让话痨 Agent 饿死其他 |

## Consequences

### 正面

- **schema 单一真源**：`core/models.py` 是 Pydantic 模型，运行时校验 + IDE 提示。
- **永久日志可复盘**：所有 action 历史、所有 chat 历史、所有 progress 历史（最近 50）都保留。
- **help_request 派生事件**：不需要额外状态字段（resolved_at）；moderator cue 是显式响应。
- **kind 区分清晰**：peer / cue / system 一目了然。

### 负面 / 后续工作

- **人工同步 Pydantic ↔ glossary**：术语表必须手动跟进；v1 接受，v2 加 CI lint。
- **永久 action 日志触发 SQLite 迁移**：10000 条 / 50MB 触发（ADR-0004）；moderator 需手动执行迁移脚本。
- **help_request 删除即丢**：不存"谁响应了"记录；moderator 想复盘只能查 `state.chat` 找对应 cue。
- **`state.progress` FIFO 50**：超 50 即丢；moderator 想保留进度历史需用 `state-inspect` 倒出来。

### 交叉引用

- Glossary: [AgentRecord](../glossary.md#agentrecord)、[AgentState](../glossary.md#agentstate)、[ApprovalAction](../glossary.md#approvalaction--执行申请)、[Approval queue](../glossary.md#approval-queue--审批队列)、[Marker](../glossary.md#marker--标记)
- ADR-0004: state 持久化（迁移触发条件）
- ADR-0005: 路由白名单（7.6 引用）
- ADR-0008: Agent 生命周期（状态枚举）
- ADR-0009: 审批语义（永久日志、reject 隐私）
- ADR-0010: marker 边界条件（`parse-warning` 来源）
- ADR-0011: 多 moderator（7.5 引用）
- ADR-0013: marker 速率（7.8 引用 + progress FIFO 50）
- ADR-0015: SSH TOFU（远程 ssh_fingerprint）
- ADR-0016: schema 版本迁移（schema_version 升级）

## Alternatives Considered

- **glossary 为枚举真源**：代码反向同步文档，脆弱。**不采用**。
- **ApprovalAction 只存 pending**：丢失复盘信息。**不采用**。
- **ChatMessage 不带 kind**：peer / cue / system 难区分。**不采用**。
- **HelpRequest 加 resolved_at 字段**：与"派生事件"语义冲突。**不采用**。
- **`state.progress` dict-of-list**：丢时间戳。**不采用**。
- **`state.progress` 只存最近一条**：丢历史。**不采用**。