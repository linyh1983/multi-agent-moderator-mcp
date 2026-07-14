# ADR-0011: Multi-Moderator v1 Singularity（多 moderator v1 单数）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 5 轮 5.1。

## Context

Moderator MCP Server 通过 stdio 与 Claude Code 进程通信。如果用户在同一台机器上启动**两个** Claude Code 会话（例如一个用于开发、一个用于运维），每个会话都注册了 `moderator-mcp` 工具，会发生什么？

- 两个 MCP server 进程同时读写同一个 `moderator_state.json`。
- 跨进程文件锁（`fcntl.flock` / `msvcrt.locking`）能保证写操作不撕裂文件，但**不能保证两个进程的内存视图同步**。
- 一个进程对 `state.agents` 的修改，另一进程的内存缓存看不到；下一次读操作才重新加载——但中间可能丢失中间状态。

REQUIREMENT 写"用户是主持人"（单数），所以理论上**只会有一个 moderator**。但代码层面不能假设用户的运行方式。

## Decision

**v1 不支持多 moderator 协同，采用 last-write-wins + 状态陈旧告警。**

### 行为

1. **文件锁保证写入原子性**：两个 MCP server 进程对 `state.agents` / `state.actions` / `state.chat` 的修改不会撕裂 JSON 文件。
2. **进程内存视图独立**：每个进程加载自己的内存副本，**不**主动广播更新给另一个进程。
3. **Last-write-wins**：后写覆盖前写。
4. **`check` 告警**：每次 `check` 调用前检查 state 文件的 `mtime`：
   - 如果 `mtime` 与本进程上次读到的 `mtime` 不一致 → `check` 输出顶部插入一段：
     ```
     ⚠️  State changed externally since you last read it (other moderator session?).
     mtime: <旧 mtime> → <新 mtime>
     ```
   - moderator 可以决定是否信任当前视图。

### 不做的事

- **不做 OCC（乐观并发控制）**：v1 没有 `version_vector` / `etag` / `revision` 字段。
- **不做进程间通知**（inotify / file watcher）：太复杂，v1 用"读时检查"代替。
- **不做"启动时探测另一个 moderator"**：用户可能误启动两个进程，告警足矣，不强制阻止。

### 何时升级

- 如果实际场景出现"两个 moderator 互相覆盖对方决定"的明确报告 → v2 加 OCC 或进程探测。
- 触发条件：≥3 个独立报告，或主计划用户主动要求。

## Consequences

### 正面

- **实现简单**：不需要 OCC、进程间通信、文件 watcher。
- **贴合 REQUIREMENT**：用户是单数 moderator，理论上不会多开。
- **告警足矣**：即使误开两个 moderator，至少 moderator 会看到"状态被外部修改了"，可以人工协调。

### 负面 / 后续工作

- **最后写赢 = 静默数据丢失**：进程 A 批准 action X，进程 B 不知道，仍按"X pending"显示，A 的批准可能被 B 后续写覆盖。靠 `check` 告警 + moderator 人工协调缓解。
- **`mtime` 比较只能检测外部修改**：同一进程内的多次写不触发告警（正常）。
- **不适用于"两个 moderator 故意协作"**：v1 不支持。

### 交叉引用

- Glossary: [Moderator](../glossary.md#moderator--主持人)
- ADR-0014: state schema 对账（v1 是否加 `version` 字段待此 ADR）

## Alternatives Considered

- **OCC（version vector / etag）**：实现复杂，v1 收益不确定。**不采用**。
- **进程探测 / 启动时拒绝**：限制用户，不必要。**不采用**。
- **CRDT 状态合并**：复杂度远超 v1 需求。**不采用**。
- **强制单 moderator（启动时检查并杀掉其他进程）**：太激进。**不采用**。