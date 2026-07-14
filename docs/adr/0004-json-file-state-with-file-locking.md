# ADR-0004: JSON File State with File Locking（JSON 文件状态 + 文件锁）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 6 轮 6.4。

## Context

Moderator MCP Server 是**单进程 + 多 Agent worker** 的架构。所有 Agent 共享的"事实"（谁在 running、谁发过什么 marker、有哪些 pending action、谁在 help_requests 里）需要一个共享存储。`state` 是 5 个顶层集合的统称（`agents` / `actions` / `chat` / `progress` / `help_requests`，外加 `seen_marker_hashes` FIFO、`action_seq` 计数器等小型辅助字段）。

候选：

| 选项 | 优 | 劣 |
|---|---|---|
| 进程内 dict | 零开销、无锁 | MCP server 重启 = 状态全丢 |
| JSON 文件 + 文件锁 | 简单、跨重启保留、人类可读、跨进程原子写 | 大文件慢；并发写入争抢锁 |
| SQLite | 持久化 + 高并发查询 + 大文件友好 | 额外依赖；schema 迁移复杂；JSONL/JSON 导出还是得做 |
| Redis / 外部 KV | 高性能 | 部署重、不在用户控制范围 |

REQUIREMENT 强调"简单 + 可复盘"，状态文件本身要被 `jq` / `cat` 看一眼就知道状态。

## Decision

**v1 用 JSON 文件 + 跨进程文件锁。达到阈值后自动迁移 SQLite。**

### v1 形态

- **路径**：`~/.moderator/moderator_state.json`（用户可覆盖）。
- **锁**：跨进程文件锁。
  - POSIX：`fcntl.flock(fd, LOCK_EX)`。
  - Windows：`msvcrt.locking(fd, LK_NBLCK, size)`（如不可用则用 `os.open` + `LockFileEx`）。
- **写入**：先 `lock → write 临时文件 → fsync → rename 覆盖 → unlock`。原子 rename 保证读侧不会读到半写文件。
- **读取**：每次 worker poll cycle 读一次，缓存到内存视图；`check` 工具每次输出前刷新（与 ADR-0011 mtime 告警配合）。
- **格式**：UTF-8 JSON，缩进 2 空格，`schema_version: 1` 顶层字段（详见 ADR-0016）。
- **schema 镜像**：`core/models.py`（Pydantic 模型）为唯一真源；术语表 `AgentState` 镜像一份枚举表格。

### 规模与迁移触发

**触发条件（任一满足即触发迁移）：**

1. `state.actions | length ≥ 10_000`（永久 action 日志积累，详见 ADR-0014）。
2. JSON 文件 ≥ **50 MiB**。
3. `state.chat | length ≥ 10_000`。

**迁移路径（M9 交付物，详见 ADR-0016）：**

- 一次性脚本 `state/migrate_json_to_sqlite.py`：
  1. 锁定当前 JSON。
  2. 创建 SQLite 副本。
  3. 校验行数一致 + checksum。
  4. 备份 JSON 到 `moderator_state.json.bak.<ts>`。
  5. 把 `state.path` 切换到 `.db`。
- 迁移是**手动触发**（moderator 主动跑脚本 + 重启 MCP server），不自动后台执行。
- 迁移后 SQLite 写入路径由 `core/persistence.py` 切换；JSON 写入代码保留作为 v1 fallback 6 个月。

### 不做的事

- **不做版本号 / OCC**：ADR-0011 last-write-wins 足以应付。
- **不做事务**：单进程 + 文件锁 = 无并发回滚问题。
- **不做 in-memory cache 同步**：每次 worker poll 重新读文件（开销可接受）。

## Consequences

### 正面

- **可读**：`cat moderator_state.json | jq '.agents'` 一行看到所有 Agent。
- **可复盘**：事故时直接看 JSON。
- **零外部依赖**：M1–M8 期间不需要 SQLite 库。
- **迁移路径明确**：JSON 不够用时不必重写代码。

### 负面 / 后续工作

- **大文件慢**：50 MiB 的 JSON 全量写一次需要 ~50ms；用增量写路径（只改部分）可缓解。
- **锁争抢**：两个 MCP server 同时写入时一方等锁（见 ADR-0011）。
- **迁移脚本是手动**：moderator 需要在合适窗口执行。

### 交叉引用

- ADR-0011: 多 moderator（文件锁的最后一道防线）
- ADR-0013: marker 速率（state 频繁写入的源头）
- ADR-0014: state schema（`actions` / `chat` / `progress` / `help_requests` 详细形状）
- ADR-0016: schema 版本迁移（M9 脚本）

## Alternatives Considered

- **进程内 dict**：重启丢状态。**不采用**。
- **v1 直接 SQLite**：实现 +1 复杂度，v1 需求不达。**不采用**。
- **Redis / 外部 KV**：部署重。**不采用**。
- **永远 JSON 不迁移**：规模大了慢；用户的"~10k 条"用例迟早会超。**不采用**。