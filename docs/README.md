# docs/

> 项目文档索引。语义与设计缘由的唯一真源。
>
> 来源：`/grill-with-docs` 会议（2026-07-13）+ 主计划 `requirement-txt-atomic-canyon.md` 的 locked decisions。
>
> **格式约定**：所有 ADR 用 Michael Nygard 格式（Context / Decision / Consequences / Alternatives Considered）。

---

## 目录结构

```
docs/
├── README.md           ← 本文件（索引）
├── glossary.md         ← 通用语言（角色、协议、审批、生命周期、横切、schema）
└── adr/
    ├── 0001-...md      ← ADR 元模板（"我们用 ADR"）
    ├── 0002-...md      ← 主计划 locked decisions（stdio / SSH+tmux / marker）
    ├── 0003-...md
    ├── ...
    └── 0017-...md      ← grilling R6 末项（offline 不自动重启）
```

---

## 建议阅读顺序

按 4 个主题分组。先读 §1（基础），再按需跳读 §2-§4。

### §1 必读基础（5 个）

1. [ADR-0001](./adr/0001-record-architecture-decisions.md) —— 我们用 ADR
2. [ADR-0002](./adr/0002-stdio-mcp-transport.md) —— MCP 传输用 stdio
3. [ADR-0003](./adr/0003-ssh-tmux-remote-mechanism.md) —— 远端 Agent 机制
4. [ADR-0008](./adr/0008-agent-lifecycle-states.md) —— Agent 7 状态生命周期
5. [ADR-0014](./adr/0014-state-schema-v1-reconciliation.md) —— State schema v1 形状

### §2 协议层（5 个）

- [ADR-0006](./adr/0006-marker-protocol-text-tags.md) —— Marker 协议（4 种 `【…】`）
- [ADR-0007](./adr/0007-moderator-writeback-protocol.md) —— 写回协议（`<moderator-…>` + ack）
- [ADR-0009](./adr/0009-approval-queue-semantics.md) —— 审批语义（单等 / 全有全无 / reject 隐私）
- [ADR-0010](./adr/0010-marker-routing-edge-cases.md) —— Marker 边界（路由 / 解析 / 64 KiB）
- [ADR-0005](./adr/0005-additional-agents-routing-allowlist.md) —— `additional_agents` 白名单 + 同名拒绝

### §3 持久化与运维（3 个）

- [ADR-0004](./adr/0004-json-file-state-with-file-locking.md) —— JSON + 文件锁 + 10k/50MB 迁 SQLite
- [ADR-0016](./adr/0016-schema-version-migrations.md) —— `schema_version` 手动迁移
- [ADR-0015](./adr/0015-ssh-fingerprint-tofu.md) —— SSH 指纹 TOFU

### §4 横切关注点（4 个）

- [ADR-0011](./adr/0011-multi-moderator-v1-singularity.md) —— 多 moderator 单数 + mtime 告警
- [ADR-0012](./adr/0012-state-file-not-encrypted.md) —— state / `role_prompt` 不加密 + 威胁模型
- [ADR-0013](./adr/0013-marker-rate-handling.md) —— Marker 速率 / 审计 / marker 扩展
- [ADR-0017](./adr/0017-no-auto-restart-on-offline.md) —— Offline 不自动重启

---

## ADR 总表（按数字）

| # | 标题 | 出处 | 一行摘要 |
|---|---|---|---|
| [0001](./adr/0001-record-architecture-decisions.md) | 我们用 ADR | 元模板 | Nygard 格式约定；何时写 / 不写 |
| [0002](./adr/0002-stdio-mcp-transport.md) | MCP 传输用 stdio | 主计划 locked | Claude Code spawn 子进程；JSON-RPC over stdin/stdout |
| [0003](./adr/0003-ssh-tmux-remote-mechanism.md) | 远端 Agent 用 SSH + tmux | 主计划 locked | 每 Agent 一个 tmux session；capture-pane 增量读 stdout |
| [0004](./adr/0004-json-file-state-with-file-locking.md) | JSON + 文件锁 | R6 | fcntl / msvcrt 跨进程锁；10k/50MB 触发 SQLite 迁移 |
| [0005](./adr/0005-additional-agents-routing-allowlist.md) | additional_agents 白名单 | R6 (6.2 + 6.5) | 单向白名单；同名全局拒绝 |
| [0006](./adr/0006-marker-protocol-text-tags.md) | Marker 协议 | 主计划 locked | 4 种 `【…】`；解析 / 路由 / dedup |
| [0007](./adr/0007-moderator-writeback-protocol.md) | 写回协议 | R6 (6.1) | `<moderator-…>` + 4 类 ack tag |
| [0008](./adr/0008-agent-lifecycle-states.md) | Agent 生命周期 | R2 | 7 状态：`starting/running/blocked/stuck/offline/stopped/error` |
| [0009](./adr/0009-approval-queue-semantics.md) | 审批语义 | R4 | 单等不变量 / 全有全无 / reject 3 级隐私 |
| [0010](./adr/0010-marker-routing-edge-cases.md) | Marker 边界条件 | R3 | 路由拒绝 / 嵌套 / 64 KiB / `TO:moderator` 禁 |
| [0011](./adr/0011-multi-moderator-v1-singularity.md) | 多 moderator 单数 | R5 (5.1) | Last-write-wins + `check` mtime 告警 |
| [0012](./adr/0012-state-file-not-encrypted.md) | state 不加密 | R5 (5.2 + 5.3) | 文档化威胁模型；`/tmp/moderator` 软肋 |
| [0013](./adr/0013-marker-rate-handling.md) | Marker 速率 + 审计 + 扩展 | R5 (5.4 + 5.5 + 5.6) | Dedup 背压 + per-agent ≤10/s + `state-inspect --format jsonl` |
| [0014](./adr/0014-state-schema-v1-reconciliation.md) | State schema 对账 | R7 (7.1-7.7) | 5 顶层集合 + `kind ∈ {peer,cue,system}` + progress FIFO 50 |
| [0015](./adr/0015-ssh-fingerprint-tofu.md) | SSH 指纹 TOFU | R7 (7.9) | 标准 `known_hosts`；v1 由 SSH 客户端校验 |
| [0016](./adr/0016-schema-version-migrations.md) | Schema 版本迁移 | R7 (7.10) | `state/migrate.py` 手动 + 逐步 + 可逆 |
| [0017](./adr/0017-no-auto-restart-on-offline.md) | Offline 不自动重启 | R6 (6.6) | moderator 主动 `stop_session + start_session` |

---

## Glossary 索引

[`docs/glossary.md`](./glossary.md) —— 系统的通用语言。

| 节 | 内容 | 关联 ADR |
|---|---|---|
| §1 角色与身份 | Moderator / Agent | 0002, 0003, 0008 |
| §2 协议文本 | Marker / Writeback tag | 0006, 0007, 0010 |
| §3 审批语义 | ApprovalAction / 队列 / 单等 / 全有全无 | 0009 |
| §4 生命周期 | AgentRecord / AgentState / 各状态子项 | 0008 |
| §5 行为契约 | Discussion vs Execution | 0006, 0009 |
| §6 横切关注点 | Last-write-wins / 威胁模型 / `/tmp` 软肋 / dedup 背压 / `state-inspect` / marker 扩展 | 0011, 0012, 0013 |
| §7 Schema 术语 | ChatMessage / HelpRequest / ProgressEntry / Schema version / ssh_fingerprint | 0014, 0015, 0016 |
| §8 占位 | 仍待细化的项 | — |
| §9 交叉引用 | 所有 ADR 文件路径 | — |

---

## ADR 依赖关系（文字版）

> 哪些 ADR 互相引用最多。

```
0001 (元模板)
  └─> 所有 ADR

0002 (stdio) ──> 0011 (多 moderator)
0003 (SSH+tmux) ──> 0008 (AgentState) ──> 0017 (offline 不重启)
0003 (SSH+tmux) ──> 0012 (/tmp 软肋), 0015 (TOFU)

0004 (JSON+锁) ──> 0014 (schema) ──> 0016 (迁移)
0004 (JSON+锁) ──> 0011 (多 moderator)

0005 (白名单) ──> 0010 (路由边界)
0006 (marker) ──> 0010 (边界), 0013 (速率)
0007 (写回) ──> 0009 (审批)
0009 (审批) ──> 0007 (写回 ack)

0011 (多 moderator) ──> 0004 (锁)
0012 (不加密) ──> 0011 (单用户假设)
0013 (marker 速率) ──> 0006, 0010
0014 (schema) ──> 0004, 0005, 0008, 0009, 0010, 0013, 0015, 0016
0015 (TOFU) ──> 0005
0016 (迁移) ──> 0004, 0014
```

---

## 维护说明

### 修改 ADR 的代价

| 改动类型 | 代价 |
|---|---|
| 修正 typo / 链接 | 直接改，状态保留 `Accepted` |
| 补充 Consequences 节 | 直接改，状态保留 `Accepted` |
| 改变 Decision | 写新 ADR，**不**改旧 ADR；旧 ADR 加 `Superseded by ADR-NNNN` |
| 推翻旧 Decision | 同上，且新 ADR 的 Context 节必须解释"为什么改" |

### 何时写新 ADR

- 锁定一个新决策（替代 ADR-0001 §"何时写 ADR"）。
- 加新 marker 类型（详见 ADR-0013 §"Marker 扩展路径" 5 步流程）。
- 加新 MCP 工具。
- 改 schema（schema_version +1，详见 ADR-0016）。

### CI / Lint（v2 候选）

- ADR 编号单调递增（lint：`docs/adr/` 内文件名）。
- ADR 必有 4 节（lint：`Context / Decision / Consequences / Alternatives Considered`）。
- ADR 状态字段必有值（lint：`Status: ...`）。
- Glossary ↔ ADR 双向链接（lint：交叉引用检查）。

---

## 相关

- **项目根 `README.md`**：用户视角的安装 / 协作模型 / 工具列表 / 安全声明 / 已知软肋。
- **主计划** `~/.claude/plans/requirement-txt-atomic-canyon.md`：实施里程碑 + Locked Architectural Decisions 表（已加 ADR 编号列）+ Open Items（已压到 ≤2 项）。
- **`REQUIREMENT.txt`**：需求意图真源；每个 ADR 的 Context 节都引用它。