# ADR-0013: Marker Rate Handling & Audit Export（marker 速率与审计导出）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 5 轮 5.4 + 5.5 + 5.6。

## Context

三个相关但不同的问题：

1. **marker 速率**：Agent 可能狂发 `【进度汇报】`（bug 循环、Agent 失去理智、Agent 在做无效重试）。系统需要避免被单个 Agent 拖垮。
2. **审计导出**：moderator 事后想复盘"我批准 / 驳回了哪些？哪些 Agent 最活跃？"，需要把 state 倒出成机器可读格式。
3. **marker 扩展**：将来 Agent 想用新种类的 marker（如 `【报错】…【/报错】`），系统如何支持？

## Decision

### 5.4 Marker 速率：dedup 背压 + worker 调度限速

**Dedup 背压：**

- 已有 `seen_marker_hashes`（FIFO 5000）做精确 sha256 去重。
- Agent 重发完全相同的内容（同样的 `【进度汇报】X【/进度汇报】`）→ 不进 `state.progress`，不进 `check`，直接丢弃。
- 同一 Agent 重发同一 hash 5000 次都没问题（不重复处理）。

**Worker 调度限速：**

- 每个 Agent 的 worker 处理 marker 速率 ≤ 10 marker/秒。
- 实现：`asyncio` task 在 `await parse_and_dispatch()` 后强制 `await asyncio.sleep(0.1)`，保证每秒最多 10 个。
- 超限的 marker 进入 `partial_buffer` 等下一轮，自然背压（不会丢，但会延迟）。

**为什么不限制全局速率：**

- 全局限速让一个话痨 Agent 拖累其他 Agent 的响应。
- Per-agent 限速更公平。

**为什么不丢弃超限 marker：**

- 进度汇报是有价值的（哪怕重复）；丢弃会让 moderator 失去信号。
- 延迟处理可接受（用户感觉是"check 慢了一秒"）。

### 5.5 审计导出：`state-inspect --format jsonl`

**新增 CLI 子命令：**

```
moderator state-inspect --format jsonl [--agent=<name>] [--since=<ts>] [--until=<ts>]
```

**行为：**

- 每行一个 JSON 对象（newline-delimited JSON）。
- 默认输出全部 5 个顶层集合：`agents`、`actions`、`chat`、`progress`、`help_requests`。
- `--agent=<name>` 只输出与该 Agent 相关的条目。
- `--since` / `--until` 时间过滤（ISO-8601）。
- 可通过管道接 `jq` / `grep` / `awk` / 上传 BI 工具。

**示例：**

```bash
moderator state-inspect --format jsonl --agent=backend_agent --since=2026-07-13T00:00:00Z \
  | jq 'select(.kind == "action") | {id, status, decided_at}'
```

**为什么 v1 不做 CSV / SQLite：**

- CSV 需要先设计列 schema（每行一列是 action、chat 还是 agent？）。
- SQLite 需要额外的依赖（`sqlite3` 或 `aiosqlite`），且 state 仍要保留 JSON 作为单一真源。
- JSONL 是最低成本的"机器可读"形态，已经够 moderator 用 `jq` 做后续处理。

### 5.6 Marker 扩展：v1 只改代码

**v1 流程（加一种新 marker，如 `【报错】`）：**

1. 在 `core/marker_parser.py` 加正则：
   ```python
   ERROR_RE = re.compile(r"【报错】(.*?)【/报错】", re.DOTALL)
   ```
2. 在 `core/dispatcher.py` 加 dispatch 项：
   ```python
   if match := ERROR_RE.search(buffer):
       await agent_manager.record_error(agent_name, match.group(1))
       # ...
   ```
3. 更新 glossary（新 marker 类型 + 状态字段含义）。
4. 更新 role_prompt 模板（教 Agent 何时发这个 marker）。
5. 写一个新的 ADR 解释"为什么加这个 marker"。

**v1 不做的事：**

- **不做配置驱动 marker**：避免写 mini-DSL / 验证器；v1 不值得。
- **不做 plugin 系统**：复杂度高。

**v2 候选：**

- 如果用户重复要求类似 marker（如 `【警告】`、`【报错】`、`【指标】`），考虑加 mini-DSL。

## Consequences

### 正面

- **marker 速率压力可控**：dedup + per-agent 限速足以应对乱发场景。
- **审计导出开箱即用**：`jq` 一行命令就能拿到想看的数据。
- **marker 扩展成本透明**：5 步流程，每次都强制写 ADR，避免"魔法 marker"泛滥。

### 负面 / 后续工作

- **Per-agent 限速硬编码 10/秒**：不可配置（v1）。如果用户场景需要更高/更低，v2 加 `marker_rate_limit_per_agent` 配置。
- **JSONL 不带 schema**：下游脚本解析需要知道字段。建议 README 给一个 schema 说明。
- **改代码加 marker 慢**：v1 每次都要发版。如果用户迭代频繁，v2 评估配置驱动。

### 交叉引用

- Glossary: [Marker](../glossary.md#marker--标记)、[Poll cycle](../glossary.md#poll-cycle轮询周期)（待 ADR-0014 落定）、[Dedup hash](../glossary.md#dedup-hash)（待 ADR-0014 落定）
- ADR-0010: marker 边界条件（dedup 哈希算法细节）
- ADR-0014: state schema 对账（`seen_marker_hashes` 字段保留）

## Alternatives Considered

- **全局限速**：一个话痨 Agent 拖累其他。**不采用**。
- **丢弃超限 marker**：丢失信号。**不采用**。
- **CSV / SQLite 导出 v1**：schema 设计 + 额外依赖。**不采用**，jsonl 够用。
- **配置驱动 marker / plugin 系统**：复杂度高。**v2 候选**。