# Moderator MCP Server — v1 Spec

> **2026-07-14**：greenfield 实施的 v1 行为规格。
>
> - **真源**：17 个 ADR（`docs/adr/0001-0017.md`）+ glossary（`docs/glossary.md`）。
> - **本文档**：把 ADR 的设计意图翻译成 user-facing 行为 + 测试契约。
> - **本文档不是**：实施细节（不写代码、不写文件路径、不写内部模块）。
> - **下一步**：本文档作为 `/to-tickets` 的输入，切 tracer-bullet 实施切片。

---

## Problem Statement

用户（"moderator"）希望**同时**让多个远程 AI 编码 Agent 在不同远端主机上自主推进工作，但又**不放任**——所有有副作用的操作（写文件、跑命令、git commit、安装依赖）必须经过自己审批。

当前痛点：

1. **多终端切换**：如果用户分别开多个终端手动管理多个 Agent，在终端之间来回切换效率低，且看不到 Agent 之间的对话。
2. **Agent 完全自治风险太高**：代码质量和架构一致性需要人类把关，特别是多个 Agent 操作同一个代码库时。
3. **统一接口缺失**：用户希望在**自己已有的编码会话**（Claude Code）里完成所有管理，不切到另一个工具。

Moderator MCP Server 解决这 3 个痛点：本地 MCP server 协调多个远端 Agent，用户在单一 Claude Code 会话内通过 6 个工具完成全局管理。

---

## Solution

Moderator MCP Server 是**本地 stdio MCP server**，被 Claude Code 作为子进程 spawn。它通过 SSH 在每台远端主机上为每个 Agent 创建一个 tmux session，监听 Agent stdout 解析**结构化文本 marker**，把：

- **Agent 之间的讨论**（`【TO:<name>】`）路由到目标 Agent
- **Agent 的执行申请**（`【申请执行】`）写入审批队列等 moderator 批准
- **Agent 的求助**（`【求助人类】`）写入紧急队列高亮提示
- **Agent 的进度**（`【进度汇报】`）写入进度历史

moderator 通过 6 个 MCP 工具反向控制 Agent：批准 / 驳回 / cue / 停止。所有跨进程事实存在一个 JSON state 文件里（5 个顶层集合），通过跨进程文件锁保证原子写入。

### 三条外部 seam

| Seam | 描述 | 可见方 |
|---|---|---|
| **协议文本契约**（Seam A） | 4 种 marker + 4 种 writeback + 路由 / 解析 / 大小约束 | Agent ↔ Server |
| **MCP 工具契约**（Seam B） | 6 个 tool 入参 / 出参 / 错误 | Moderator ↔ Server |
| **State JSON Schema**（Seam C） | 5 顶层集合 + 永久 action log + `kind` 标签 + schema_version | 内部跨进程契约 |

内部模块（agent_manager / marker_parser / poll_worker / ssh_pool / tmux 命令构造器 / cue_router / approval_queue 等）属于 implementation detail，**不在 spec 范围**。

---

## User Stories

### A. 主持人视角（MCP 工具契约）

#### A.1 启动 Agent

1. As a moderator, I want to call `start_session(agent_name, host, project_dir, role_prompt, additional_agents?)`, so that a new Agent is created on a remote host running my role_prompt.
2. As a moderator, I want `start_session` to fail fast with a clear error message, so that I know if the host is unreachable or tmux is missing.
3. As a moderator, I want `start_session` to fail with `AgentAlreadyExists` if `agent_name` is already in state (regardless of host), so that I don't accidentally clobber a running agent.
4. As a moderator, I want `start_session` to take 1–5 seconds to reach `running` state, so that I know the agent is live.
5. As a moderator, I want `additional_agents` to be a one-way routing allowlist, so that I declare who *this* agent accepts messages from.
6. As a moderator, I want role_prompt uploaded via SFTP to `/tmp/moderator/role-<name>.txt`, so that the agent sees it without depending on my local filesystem.
7. As a moderator, I want an error-state record on disk if `start_session` fails, so that I can inspect via `state-inspect` without losing context.
8. As a moderator, I want the role_prompt text NOT persisted in state JSON, only the path, so that secrets in role_prompt stay in one place (the remote host).

#### A.2 查看全局状态

9. As a moderator, I want to call `check()` to see a snapshot of all agents, pending approvals, urgent help, and recent chat, so that I get a global view at a glance.
10. As a moderator, I want `check` to render `help_requests` first (highest priority), then pending approvals, then agent states, then recent chat, so that I see what needs me first.
11. As a moderator, I want `check` to show progress entries as `count + last 1–3 lines` by default, so that I can skim without overflow.
12. As a moderator, I want `check` to flag agents in `stuck` / `offline` / `error` state clearly, so that I notice problems fast.
13. As a moderator, I want `check` to warn me if the state file `mtime` changed externally (other moderator session), so that I don't trust stale views.
14. As a moderator, I want a CLI subcommand `state-inspect --format jsonl` to dump state as machine-readable JSON, so that I can `jq` / `grep` for postmortem.

#### A.3 审批执行申请

15. As a moderator, I want to call `approve(action_id, note?)` to grant an agent permission to proceed, so that the unblocked agent resumes work.
16. As a moderator, I want to call `approve(action_ids=[...], note?)` for batch approval, so that I clear the queue efficiently.
17. As a moderator, I want batch `approve` to be all-or-nothing, so that I never end up in a half-applied state.
18. As a moderator, I want `note` to be embedded in the `<moderator-approve>` tag the agent receives, so that I can give feedback atomically with approval.
19. As a moderator, I want the agent to receive `<moderator-info action-id=X status=queued>` immediately after it sends `【申请执行】`, so that it knows I see it.
20. As a moderator, I want `reject(action_id, reason)` to NOT auto-create a new pending action, so that the agent's request log stays 1:1 with its submissions.
21. As a moderator, I want the agent to receive the full `reason` on rejection, so that it can learn and adjust.
22. As a moderator, I want peer agents to see rejected reasons marked `private_to_originator` (not the full text), so that one agent's mistake doesn't leak to others.

#### A.4 主动 cue

23. As a moderator, I want to call `cue(target=<agent_name>, message)` to send a directive to a specific agent, so that I can intervene mid-task.
24. As a moderator, I want `cue(target="all", message)` to broadcast to every agent, so that I can issue a global directive.
25. As a moderator, I want `cue` to remove matching `help_requests` for that agent, so that help state stays consistent.
26. As a moderator, I want `cue` to NOT unblock an agent (single-wait invariant), so that agents don't resume from a cue without my explicit approval.

#### A.5 停止 Agent

27. As a moderator, I want to call `stop_session(agent_name, grace_seconds=30)` to give an agent time to wrap up, so that I don't kill mid-write.
28. As a moderator, I want `stop_session(force=True)` to kill the tmux session immediately, so that I can recover from a stuck agent.
29. As a moderator, I want `stop_session` to auto-cancel all pending actions of that agent, so that the queue stays clean.
30. As a moderator, I want `stop_session` to keep the `stopped` AgentRecord on disk with full postmortem fields, so that I can audit later.
31. As a moderator, I want offline agents NOT to auto-restart, so that I retain control over recovery.

### B. Agent 视角（协议文本契约）

#### B.1 发 marker

32. As an agent, I want to send `【进度汇报】X【/进度汇报】` to report progress, so that the moderator knows I'm alive.
33. As an agent, I want to send `【TO:<peer>】X【/TO:<peer>】` to message a peer agent, so that we can discuss without moderator involvement.
34. As an agent, I want to send `【申请执行】X【/申请执行】` to request moderator approval before executing, so that I'm not blocked from the discussion phase.
35. As an agent, I want to send `【求助人类】X【/求助人类】` only when truly stuck, so that I don't pester the moderator.
36. As an agent, I want `【申请执行】` to put me in `blocked` state until I receive `<moderator-approve>` or `<moderator-reject>` for *that exact action_id*, so that I know I can't proceed.
37. As an agent, I want server to ack my `【申请执行】` with `<moderator-info status=queued>`, so that I know it's in the moderator's queue.
38. As an agent, I want `<moderator-cue>` to be informational only, NOT unblocking me, so that I don't accidentally resume from a side comment.
39. As an agent, I want my repeated identical markers to be deduplicated by the server, so that I don't pollute state if I retry.

#### B.2 收 writeback

40. As an agent, I want to receive `<moderator-approve action-id="..." note="...">` and resume work, so that the loop closes.
41. As an agent, I want to receive `<moderator-reject action-id="..." reason="...">` and adjust my plan, so that I learn from moderator feedback.
42. As an agent, I want to receive `<moderator-info kind="undelivered">` if my `【TO:X】` couldn't route, so that I know the message was lost.
43. As an agent, I want to receive `<moderator-info kind="truncated">` if my marker was truncated at 64 KiB, so that I can split future markers.
44. As an agent, I want to receive `<moderator-info kind="parse-warning">` if my marker was malformed, so that I can fix my output format.
45. As an agent, I want `【TO:moderator】` to be rejected at parse time, so that I learn to use `【求助人类】` instead.

#### B.3 marker 边界

46. As an agent, I want nested openers (`【TO:foo】【TO:bar】…`) to keep only the outer one, so that I don't have to be paranoid about my output.
47. As an agent, I want a malformed / unclosed marker to be held in partial buffer, so that split-across-reads work correctly.
48. As an agent, I want markers larger than 64 KiB to be truncated + warned, so that I learn the size limit.
49. As an agent, I want my markers routed to `additional_agents`-allowed peers only, so that I don't accidentally spam unrelated agents.

### C. 内部系统契约（State JSON Schema）

#### C.1 持久化

50. As the system, I store all cross-process facts in a single JSON file at `~/.moderator/moderator_state.json` (overridable), so that I survive restarts and stay human-readable.
51. As the system, I lock the file with `fcntl.flock` / `msvcrt.locking` cross-process, so that two MCP server processes don't tear writes.
52. As the system, I write atomically (temp + fsync + rename), so that readers never see partial JSON.
53. As the system, I read once per worker poll cycle, cache in memory, and refresh on every `check` call, so that `check` is fresh.
54. As the system, I include a `schema_version: 1` field at the top level, so that future migrations are explicit.
55. As the system, I trigger SQLite migration when `state.actions | length ≥ 10000` OR file ≥ 50 MiB OR `state.chat | length ≥ 10000`, so that v1 scale limits are bounded.

#### C.2 顶层集合

56. As the system, I expose 5 top-level collections: `agents`, `actions`, `chat`, `progress`, `help_requests`, so that the data shape is predictable.
57. As the system, I keep `actions` as a **permanent log** of all approval events (created / decided / cancelled), so that postmortem works.
58. As the system, I keep `chat` entries with `kind ∈ {peer, cue, system}`, so that rendering can group appropriately.
59. As the system, I keep `progress[<agent>]` as a list of `ProgressEntry { ts, text }` capped at 50 FIFO per agent, so that memory is bounded.
60. As the system, I keep `help_requests` as a flat list (no nested), removed on moderator `cue`, so that lifecycle is simple.

#### C.3 Schema 迁移

61. As the system, I refuse to start if `schema_version` is below the minimum supported, so that I don't silently load old data.
62. As the system, I provide a `state/migrate.py` script with per-version step functions and reverse functions, so that migrations are testable and reversible.
63. As the system, I never skip versions in a single migration, so that test matrix is manageable.

### D. 横切关注点

#### D.1 多 moderator

64. As the system, I never proactively notify other moderator processes about my updates, so that I stay simple.
65. As the system, I emit a `check`-level warning if the state file's `mtime` differs from my last-seen value, so that the moderator can decide if my view is stale.

#### D.2 安全 / 隐私

66. As the moderator, I understand state JSON is **plaintext** and may contain secrets posted by agents, so that I protect the file accordingly.
67. As the moderator, I understand role_prompt lives at `/tmp/moderator/role-<name>.txt` which is world-readable on most systems, so that I keep role_prompts free of secrets or accept the risk.
68. As the system, I do NOT encrypt state in v1, so that I don't introduce key-management failure modes.

#### D.3 marker 速率 / 审计

69. As the system, I dedup markers via `sha256(opener+content+closer)` in a global FIFO of 5000 hashes, so that I don't process the same marker twice.
70. As the system, I throttle each agent's marker processing to ≤10 markers/sec, so that one chatty agent can't starve others.
71. As the system, I provide `state-inspect --format jsonl` for audit, so that I can replay decisions offline.

#### D.4 SSH + tmux

72. As the system, I rely on the standard SSH `known_hosts` for host key TOFU, so that I don't reinvent SSH security.
73. As the moderator, I understand I must clear the host key from `known_hosts` if a remote host legitimately changes its key.

#### D.5 Open Items（v1 实施期间观察）

74. As the system, I expose `marker rate pressure test` as an empirical question, so that we can re-tune the 10/sec limit after M8+ lands.
75. As the system, I expose `JSON → SQLite migration script` as an M9 deliverable, so that the 10k/50MB trigger has a working path.

---

## Implementation Decisions

> 不写文件路径 / 代码片段。只列模块、接口、决策。

### ID.1 模块（建议划分，非强制）

- **协议层**：`marker_parser`（4 种 marker 正则 + 嵌套 / 畸形处理 + 大小截断）、`dedup`（sha256 + FIFO 5000）、`writeback_formatter`（`<moderator-…>` tag 构造器）。
- **状态机层**：`agent_manager`（7 状态转换）、`approval_queue`（单等不变量 + 全有全无）、`help_request_manager`（cue 即移除）、`chat_history`（bounded ring buffer）。
- **持久化层**：`state/store.py`（JSON 读写）、`state/locks.py`（跨进程锁）、`state/schema_v1.py`（Pydantic 模型）、`state/migrate.py`（M9）。
- **远端驱动层**：`remote/driver.py`（AgentDriver 协议 + RemoteTmuxDriver）、`remote/ssh_pool.py`（asyncssh 连接池）、`remote/tmux.py`（tmux 命令构造器）、`remote/log_tailer.py`（增量读 stdout）、`remote/role_prompt.py`（SFTP + load-buffer）。
- **轮询层**：`poll/supervisor.py`（asyncio 主管 N 个 worker）、`poll/worker.py`（per-agent: read → parse → dispatch）。
- **工具层**：`tools/{start_session, check, approve, reject, cue, stop_session}.py`（6 个 MCP tool）。
- **CLI**：`cli.py`（serve / state-inspect / doctor）。

> 这些模块名是参考；实施时若换名 / 合并 / 拆分，需更新主计划 Project Layout 备注。

### ID.2 Seam A（协议文本契约）

- **4 种 marker**：`【进度汇报】` / `【TO:<name>】` / `【申请执行】` / `【求助人类】`。闭合必须用对应 `【/<closer>】`。
- **4 种 writeback**：
  - `<moderator-approve action-id="..." note="...">` — 业务
  - `<moderator-reject action-id="..." reason="...">` — 业务
  - `<moderator-cue target="<name>|all">…</moderator-cue>` — 业务
  - `<moderator-info …>` — 4 种 ack 子类型（queued / undelivered / truncated / parse-warning）
- **大小**：每 marker 内容 ≤ 64 KiB；`partial_buffer` 总长 ≤ 64 KiB。
- **dedup**：全局 `sha256(opener+content+closer)` FIFO 5000。
- **路由**：`TO:` 必须在发送方的 `additional_agents` 白名单中（单等），且目标状态必须是 `running` / `blocked` / `starting` / `stuck`。
- **解析严格**：嵌套 opener 内层忽略 + 警告；未闭合 → 留 partial；孤立 closer → 忽略 + 警告。
- **顺序**：单 Agent 内 FIFO；跨 Agent 尽力而为，**不保证全局全序**。

### ID.3 Seam B（MCP 工具契约）

- **6 个 tool**（入参 / 出参按 glossary §1 工具表）：
  - `start_session(agent_name, host, project_dir, role_prompt, additional_agents?)` → 返回 `AgentRecord` + 错误用 `AgentAlreadyExists` / `RemoteDriverError`
  - `check(expand_chat=False, only_urgent=False, max_chat_lines=20)` → 纯文本快照，4 段（Urgent help / Pending approvals / Agents / Recent chat tail）
  - `approve(action_ids=[…], note?)` → 全有全无；返回批准的 actions 列表
  - `reject(action_ids=[…], reason)` → 全有全无；reason 全等
  - `cue(target, message)` → 群发（`target="all"`）或定向
  - `stop_session(agent_name, force=False, grace_seconds=None)` → grace 默认 30 秒
- **错误传播**：driver 层只抛 `RemoteDriverError`（及其子类），业务层捕获后写 `error` state 记录 + 抛回 moderator。

### ID.4 Seam C（State JSON Schema）

```jsonc
{
  "schema_version": 1,
  "created_at": "...",
  "updated_at": "...",
  "agents":          { "<name>": AgentRecord },
  "actions":         { "<id>": ApprovalAction },        // 永久日志
  "chat":            [ ChatMessage ],                   // kind ∈ {peer, cue, system}
  "progress":        { "<name>": [ ProgressEntry ] },   // 每 agent ≤ 50 FIFO
  "help_requests":   [ HelpRequest ],                   // cue 即移除
  "seen_marker_hashes": [ "<sha256>" ],                 // FIFO 5000
  "action_seq":      0
}
```

- **真源**：`core/models.py` 的 Pydantic models（AgentRecord / ApprovalAction / ChatMessage / ProgressEntry / HelpRequest）。
- **镜像**：`docs/glossary.md` §7 表格（人类阅读用）。
- **AgentState 枚举**：`starting / running / blocked / stuck / offline / stopped / error`（7 值）。

### ID.5 决策回顾

- **多 moderator**：v1 单数 + last-write-wins + `check` mtime 告警。
- **state 加密**：v1 不加密，文档化威胁模型。
- **marker 速率**：dedup 背压 + worker 限速 ≤10/s；marker 扩展 v1 改代码 + 写 ADR。
- **schema 迁移**：手动 `state/migrate.py`（M9）；启动时拒绝旧 schema。
- **offline 不自动重启**：moderator 主动 stop + start。
- **SSH TOFU**：依赖 SSH 客户端 `known_hosts`；v1 不在 MCP server 层独立校验。
- **库选型**（实施时可评估）：Python 3.11+ asyncio（已锁）；FastMCP / click / hatchling 是 M1-M4 实施反推的参考，不锁。

---

## Testing Decisions

### TD.1 测试原则

- **行为测试 > 实现测试**：所有测试只针对 seam 的外部可观察行为（marker 文本 / tool 输出 / state JSON 形状）。
- **避免 mock 内部模块**：parser / queue / ssh_pool 等内部实现变更时，测试应不需调整。
- **priority**：边界条件 > 正常路径 > 错误路径。

### TD.2 测试分层

| 层 | 工具 | 覆盖 |
|---|---|---|
| 协议文本（Seam A） | `pytest` 单元测试 + 字符串 fixture | 4 种 marker 正则；嵌套 / 畸形 / 64 KiB 截断；4 种 writeback 序列化；dedup sha256 一致性 |
| MCP 工具（Seam B） | `pytest` 单元测试 + `FakeDriver` | 6 个 tool 入参校验；单等不变量；全有全无批；error-state 路径；grace 行为 |
| State schema（Seam C） | `pytest` 单元测试 + JSON fixture | 5 顶层集合形状；永久 action log；progress FIFO 50；help cue 即移除；schema_version |
| 集成（gated by `MODERATOR_RUN_INTEGRATION=1`） | `pytest` + 真实 SSH + 真实 tmux | 端到端：start → progress → check → approve → cue → stop；file lock 并发写 |

### TD.3 已知测试用例（来自 ADR）

- **marker 解析**（ADR-0006 / 0010）：4 种 opener 各自最小 / 嵌套 / 畸形 / 超大 / 跨 poll split。
- **dedup**（ADR-0013）：同内容重发 → 不重复处理；5000 边界 + 替换。
- **审批**（ADR-0009）：单等（其他 action_id 不解除 block）；全有全无（部分 id 非法 → 整个 reject）；reject 不创建反提案。
- **生命周期**（ADR-0008）：7 状态合法转换；stuck 阈值（默认 30 分钟）；offline → running 自愈路径；stopped 不删 record；error 永不自愈。
- **路由**（ADR-0005 / 0010）：`additional_agents` 白名单外 → 解析硬失败 + parse-warning；`TO:moderator` → 拒绝；目标 offline → undelivered ack。
- **持久化**（ADR-0004 / 0014）：文件锁并发不撕裂；迁移触发条件（10k / 50 MiB）；schema_version 拒绝。

### TD.4 已知陷阱（必须测）

- **Windows 路径**：`/` 与 `\` 在 `partial_buffer` 切分时不要混淆。
- **CRLF**：`tmux capture-pane` 在 Windows 上返回 `\r\n`；解析时容忍。
- **SFTP 中文文件名**：`role_prompt` 文件名应只用 ASCII。
- **SSH 主机密钥变更**：标准 SSH 拒绝时 `start_session` 应能识别并返回 `RemoteDriverError`。

### TD.5 测试不覆盖的（v1 接受）

- 性能基准（marker 速率压力实测留作 R7 Open Item）。
- Windows 远端（v1 排除）。
- 非 Claude Agent 协议严格性（README §Non-Claude 软契约）。

---

## Out of Scope（v1 不做）

| 项 | 来源 | 何时升级 |
|---|---|---|
| 多 moderator 协同（OCC / 进程探测） | ADR-0011 | ≥3 个独立报告 |
| State 文件加密 | ADR-0012 | 出现 secret 泄漏报告 |
| `role_prompt_dir` 可配置化 | ADR-0012 | 用户场景需要 |
| `auto_restart` 配置 | ADR-0017 | ≥3 个独立报告 |
| 配置驱动 marker 类型 | ADR-0013 | 用户重复要求新 marker |
| Schema 自动迁移 | ADR-0016 | 出现"想启动自动迁移"报告 |
| MCP server 层 SSH fingerprint 校验 | ADR-0015 | 用户希望独立校验 |
| CSV / SQLite 审计导出（仅 JSONL） | ADR-0013 | 用户场景需要 |
| Windows 远端 | ADR-0003 | 用户场景需要 |
| HTTP / SSE MCP 传输 | ADR-0002 | 用户场景需要 |
| `delete_record` API | ADR-0005 | 用户场景需要 |
| GUI / Web UI | 主计划 | — |

---

## Further Notes

### FN.1 Tracer bullet 建议顺序

> 给 `/to-tickets` 时的纵切参考。每张 ticket 声明 blocking edges，先做 blocker。

1. **Hello Agent 端到端**：start_session → Agent 发 `【进度汇报】hello` → check 看到。覆盖 ADR-0003 / 0006 / 0008 / 0014 核心路径。**最先做**。
2. **审批往返**：Agent 发 `【申请执行】` → moderator approve → Agent 收到 `<moderator-approve>` → 状态 running。覆盖 ADR-0009。
3. **跨 Agent 消息**：A 发 `【TO:B】` → B 收到 → B 入 state.chat。覆盖 ADR-0005 / 0010。
4. **stuck / offline 检测**：模拟超时不输出 → 状态 stuck；模拟 tmux 死 → 状态 offline。覆盖 ADR-0008 / 0017。
5. **审计导出**：`state-inspect --format jsonl` + jq 过滤。覆盖 ADR-0013。
6. **JSON → SQLite 迁移触发**：mock state 写到 10k entries → 触发迁移。覆盖 ADR-0004 / 0016。

### FN.2 与 ADR 的关系

- ADR 是**设计真源**（why + what 决策）。本文档是**行为真源**（how it behaves）。
- 冲突时**优先 ADR**。本文档不重复 ADR 的 Alternatives Considered 节。
- 每个 Implementation Decision 标 `ID.x` 编号；每个 User Story 标 `A.x` / `B.x` / `C.x` / `D.x`；每个测试决策标 `TD.x`。

### FN.3 与主计划的关系

- 主计划 `~/.claude/plans/requirement-txt-atomic-canyon.md` 的 Project Layout 是**目标蓝图**（v1 实施时可调）。
- Build Order 是**历史参考**——tracer bullet 顺序（FN.1）覆盖之。
- Locked Architectural Decisions 表已加 ADR 编号列。

### FN.4 Issue tracker

- 本文档作为 `/to-tickets` 的输入。
- 如未来有项目级 issue tracker（`.scratch/moderator-mcp-v1/issues/`），可迁移；目前先放 `docs/spec-v1.md`。