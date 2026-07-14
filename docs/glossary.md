# Moderator MCP Server 术语表

> 第 1 轮（通用语言）+ 第 2 轮（Agent 生命周期）+ 第 3–5 轮敲定的术语。第 6、7 轮继续扩展。
>
> 来源：`/grill-with-docs` 会议，2026-07-13。
> 见 `docs/adr/` 获取与每个术语绑定的设计决策。

---

## 1. 角色与身份

### Moderator / 主持人

- **角色**：用户本人，本地 Claude Code 会话里通过工具调用来管理所有 Agent。
- **权限**：唯一审批权；所有执行操作必须经其批准。
- **代码 / ADR 命名**：用英文 **Moderator**（与 MCP 服务器名 `moderator-mcp` 一致）。
- **中文文档**：可写作"主持人"或"moderator"，术语表保留两种。

### Agent

- **定义**：远端主机上独立运行的进程；读 stdin（接收写回 tag）、写 stdout（产出 marker）。
- **身份**：由 `agent_name` 字符串**全局唯一**标识。`state.agents` 以 `agent_name` 作主键。
- **host**：Agent 的属性之一（远端主机名），**不参与身份**。
  - 即：在 host A 上 `probe` 和在 host B 上 `probe` 视为同一身份；后启动的会覆盖前者。
  - 同名冲突在 v1 拒绝（待第 6 轮确认 ADR-0005 的细化）。
- **相关条目**：见 `AgentRecord`、`AgentState`、`Blocked agent`。

---

## 2. 协议文本

### Marker / 标记

- **方向**：Agent → 服务器。出现在 Agent stdout 中的结构化文本段。
- **格式**：`【<opener>】<content>【/<closer>】`，使用全角方括号。
- **四种类型**（来自 REQUIREMENT）：
  - `【进度汇报】…【/进度汇报】` —— 心跳
  - `【TO:<agent_name>】…【/TO:<agent_name>】` —— 同行消息（**目标只能是 Agent，不能是 moderator**）
  - `【申请执行】…【/申请执行】` —— 执行申请
  - `【求助人类】…【/求助人类】` —— 紧急求助（仅这一种能 ping moderator）
- **去重**：通过 `seen_marker_hashes`（FIFO 5000）做精确 sha256 去重。
- **大小限制**：每 marker 内容上限 **64 KiB**（UTF-8 字节数），超出截断 + 警告。`partial_buffer` 总长上限也是 64 KiB。
- **解析严格度**（ADR-0010）：
  - 嵌套 opener 在 body 内 → 警告 + 忽略内层 + 保留外层。
  - 未闭合 / 畸形 → 留 `partial_buffer`，超 64 KiB 仍不闭合则丢弃 + `MarkerParseWarning`。
  - 孤立 closer → 警告 + 忽略，不抛错。
- **路由**（ADR-0010）：
  - `TO:target` 的 target 不在发送方 `additional_agents` → 拒绝（见 ADR-0005）。
  - target 状态非 `running` / `blocked` / `starting` / `stuck` → 路由拒绝 + 标记 `(undelivered)` 写入 `state.chat` + writeback 提示发送方。
  - **不排队补送**：peer 聊天不保证顺序，target 上线后不补历史消息。
- **进度汇报渲染**：完整内容存 `state.progress[agent]`（FIFO 50）；`check` 默认只显示**计数 + 最近 1–3 行**。
- **顺序保证**：每 Agent 内部 FIFO（partial_buffer 状态机）；Agent 间尽力而为，**不保证全局全序**。
- **协议 ADR**：完整协议见 ADR-0006（marker 协议）+ ADR-0010 ✅（边界条件）+ ADR-0014（state schema 对账）。

### Writeback tag / 写回标签

- **方向**：服务器 → Agent。服务器写进 Agent stdin 的结构化文本段。
- **格式**：`<moderator-<kind> …>`（尖括号；与 marker 的全角方括号相反，便于视觉区分）。
- **常见类型**：
  - `<moderator-approve action-id="a-X" note="…">`
  - `<moderator-reject action-id="a-X" reason="…">`
  - `<moderator-cue target="<agent|all>">…</moderator-cue>`
  - `<moderator-info action-id="a-X" status="queued">` —— 仅 ack，不触发动作
- **新增 ack 类型**（ADR-0010 引入）：
  - `<moderator-info kind="undelivered" target="X">peer X 不可达` —— 告诉发送方它发的 `TO:` 没送达。
  - `<moderator-info kind="truncated" marker="申请执行">your last marker was truncated at 64 KiB` —— 告诉发送方它发的 marker 被截断。
  - `<moderator-info kind="parse-warning" detail="…">…` —— 解析告警（嵌套 / 畸形 / 不在白名单等）。
- **传输**：多行 / 长内容用 tmux `load-buffer` + `paste-buffer`；短单行用 `send-keys`。
- **协议 ADR**：完整协议见 ADR-0007（待写）+ ADR-0010。

---

## 3. 审批语义

### ApprovalAction / 执行申请

- **定义**：一条执行申请的对象（含 pending / decided 状态历史）。
- **标识**：`a-<seq>`，其中 `<seq>` 取自 `counters.action_seq` 单调递增计数器。
- **状态机**：`pending` → `approved` | `rejected` | `cancelled`。
- **持久化**：`state.actions` 是**永久日志**，不只是 pending 队列（待第 7 轮 ADR-0014 确认）。cancelled / approved / rejected 都保留，含时间戳。
- **创建**：Agent 发 `【申请执行】` 触发。
- **关键时间戳字段**：`created_at`、`decided_at?`、`cancelled_at?`、`decided_by`（moderator 标识）、`cancelled_reason?`（如 `agent_stopped`）。
- **Reject 原因字段**：`reject_reason?`，始终存完整文本（moderator 可见），但群聊历史里以 `private_to_originator` 标签脱敏（见 ADR-0009）。
- **相关条目**：见 `Approval queue`、`Single-wait invariant`、`All-or-nothing batch`。

### Approval queue / 审批队列

- **定义**：跨所有 Agent 的 `pending` ApprovalAction 集合（`state.actions` 的子集）。
- **可见性**：`check` 工具按优先级渲染（紧急求助 > 待审批 > …）。
- **批量语义**：跨 Agent 的 `approve(action_ids=[…])` / `reject(action_ids=[…])` 是**全有全无** —— 先全校验再 apply，任一失败不改任何状态（见 ADR-0009）。
- **Auto-cancel**：Agent 进入 `stopped` 状态时，其全部 pending actions 自动转 `cancelled`，`cancelled_reason="agent_stopped"`。已 approved / rejected 不动。
- **相关条目**：见 `ApprovalAction`、`Single-wait invariant`。

### Single-wait invariant / 单等不变量

- **定义**：blocked Agent 等待**唯一**一个 action_id 的一次 approve/reject 决定；其他任何互动（cue、其他 action 的 approve/reject、peer 消息）**都不解除 block**。
- **理由**：避免 moderator 随口 cue 无意中唤醒不该走动的 Agent。
- **例外**：`<moderator-info action-id=X status=queued>` 这种 ack tag 只是确认"收到了"，不算解除。
- **定义出处**：ADR-0009。

### All-or-nothing batch / 全有全无批处理

- **定义**：批量 `approve` / `reject` 必须先校验全部 id 合法（存在 + 处于 `pending`），任一失败整个调用报错、不改任何状态。
- **Apply**：一次性写入全部决定；每条 action 写 `decided_at` + `decided_by`。
- **理由**：moderator 必须能预测整批结果，不接受"批准了前 3 条但第 4 条失败"的灰色地带。
- **定义出处**：ADR-0009。

---

## 4. Agent 生命周期状态（ADR-0008）

### AgentRecord

- **定义**：`state.agents` 字典中每个 Agent 对应的持久化条目。
- **关键字段**：`name`, `host`, `project_dir`, `role_prompt_path`, `tmux_session`, `log_path`, `state`, `stuck_threshold_minutes`, `last_output_at`, `log_offset`, `started_at`, `stopped_at?`, `error?`, `ssh_fingerprint?`, `pid?`。
- **生命周期**：`start_session` 时创建，`stop_session` 后保留 `stopped` 记录不删除。

### AgentState

- **定义**：AgentRecord 的 `state` 字段，规范枚举为以下 7 个值（**已删除 `paused`，新增 `blocked`**）。

| 值 | 含义 | 进入条件 | 退出条件 |
|---|---|---|---|
| `starting` | 启动中 | `start_session` 调用 | tmux 会话就绪 + 角色 prompt 投递完成 → `running` |
| `running` | 活跃 | `starting` 完成；或 `blocked`/`stuck`/`offline` 恢复 | 见其他行 |
| `blocked` | 等审批 | running 状态下 Agent 发 `【申请执行】` | 针对该 action_id 的 `approve` / `reject` 到达 → `running` |
| `stuck` | 静默超时 | running 状态下 `now - last_output_at > stuck_threshold_minutes` 且 tmux 还活着 | 读出新字节 → `running` |
| `offline` | 远端会话消失 | running 状态下 1 次失败 poll + 1 次重试后仍 `is_alive() == False` | tmux 重新出现 + 读出新字节 → `running` |
| `stopped` | moderator 主动终止 | 任何状态下 moderator 调 `stop_session` | 终态；记录保留 |
| `error` | 启动失败 | `start_session` 内部异常 | 终态；moderator 必须 `stop_session` 后重新 `start_session` |

### Starting agent

- **状态**：`starting`。
- **典型持续时间**：1–5 秒（SSH 连接 + tmux 创建 + role_prompt 投递）。
- **超时**：未配置硬超时；30 秒无进展由 `stuck` 兜底。

### Running agent

- **状态**：`running`。
- **含义**：tmux 会话活跃 + worker 能正常 poll + 不在 `blocked` 状态。
- **最大持续时间**：无限制。

### Blocked agent

- **状态**：`blocked`（`running` 的特殊形态，但作为独立状态以提高可见性）。
- **触发**：Agent 发 `【申请执行】` 后进入。
- **行为契约**：Agent 此时**不能再发任何 marker**；必须等待针对该精确 action_id 的 `approve` 或 `reject`。
- **解除条件**：只有针对该 action_id 的 `approve` / `reject`；`cue` 不解除 block。
- **相关条目**：见 ADR-0009 `Single-wait invariant`（待第 4 轮）。

### Stuck agent

- **状态**：`stuck`。
- **判定**：`now - last_output_at > stuck_threshold_minutes` **且** `is_alive()`（tmux 会话存在）。
- **`last_output_at` 更新规则**：worker 读到任何字节就更新，**不论是不是 marker**。
- **含义**：跑 `find /` 10 分钟没输出 = stuck。
- **恢复**：poll cycle 重新读到 stdout → `running`。

### Offline agent

- **状态**：`offline`。
- **判定**：1 次失败 poll + 1 次重试（间隔 = 当前 poll interval，默认 2 秒）后仍 `is_alive() == False`。
- **与 `error` 的区别**：
  - `offline` = 启动成功后，远程会话消失（瞬态，可恢复）。
  - `error` = 启动失败（持久，直到 moderator 干预）。
- **恢复**：`is_alive() == True` 且 worker 读出新字节 → `running`。

### Error agent

- **状态**：`error`。
- **进入条件**：`start_session` 内部抛异常（SSH 失败、SFTP 失败、tmux 命令失败等）。
- **行为**：永不自动恢复。moderator 必须调 `stop_session`（→ `stopped`）后重新 `start_session`。
- **理由**：贴合 REQUIREMENT "系统不应该代替 Agent 做决定或自动修复 Agent 的问题"。

### Stopped agent

- **状态**：`stopped`。
- **进入条件**：moderator 调 `stop_session(agent_name, force=False, grace_seconds=None)`。
- **grace 行为**：默认 30 秒；到期后 `tmux kill-session`。
- **记录保留**：`stop_session` 不删除 AgentRecord；保留 `started_at`、`stopped_at`、最终状态等字段用于事后复盘。

---

## 5. 行为契约

### Discussion vs Execution / 讨论 vs 执行

- **边界原则**：是否**留下持久状态变更**或**对外部系统产生副作用**。
- **讨论（自由）**：纯读操作（cat / grep / ls / git log / find / read-only API）。
- **执行（需审批）**：写文件、删除文件、shell 命令、test / build / install、git commit、网络副作用 API 调用。
- **执行依据**：REQUIREMENT "需要申请的操作类型包括创建修改删除文件、执行 shell 命令、运行测试和构建、git 操作、安装依赖"。

---

## 6. 横切关注点术语（第 5 轮）

### Last-write-wins + mtime 告警

- **场景**：用户误启动两个 Claude Code 会话，两个 MCP server 进程同时读写同一 state 文件。
- **行为**：文件锁保证写入原子性；进程内存视图独立；**last-write-wins**；每次 `check` 前对比文件 `mtime`，若与上次读到的不同则在输出顶部插入告警段。
- **不做**：OCC（version vector / etag）、进程间通知、启动时探测。
- **出处**：ADR-0011。

### Threat model / 威胁模型

- **声明**：v1 **不**对 state 文件加密，`role_prompt` 落远端 `/tmp/moderator/` 全局可读。
- **威胁假设**：moderator 是机器唯一用户；disk 访问权限由 moderator 控制；备份策略由 moderator 控制。
- **缓解**：状态文件按 secret-grade 资源保护（chmod 600 / Windows ACL）；不用于多用户机器。
- **何时升级**：出现明确 secret 泄漏报告 → v2 评估 OS keyring + AES-GCM、`role_prompt_dir` 可配置化。
- **出处**：ADR-0012。

### `/tmp/moderator` 软肋

- **路径**：远端主机的 `/tmp/moderator/role-<name>.txt`。
- **已知风险**：大多数发行版 `/tmp` 全局可读；同一台机器其他用户可读 `role_prompt` 文本（可能含敏感信息，如"你有 prod DB 权限"）。
- **v1 决定**：保留。**v2 候选** `role_prompt_dir` 配置项（落到 `$HOME/.moderator/`）。
- **出处**：ADR-0012。

### Dedup 背压 / Per-agent marker rate limit

- **Dedup 背压**：Agent 重发完全相同的 marker 内容（hash 一致）→ 直接丢弃，不进 `state.progress`，不进 `check`。
- **Per-agent 限速**：每个 Agent 的 worker 处理 marker 速率 ≤ 10 marker/秒。超限 marker 进 `partial_buffer` 等下一轮（自然背压，**不丢**）。
- **不做**：全局限速（会拖累其他 Agent）、丢弃超限 marker（丢信号）。
- **出处**：ADR-0013。

### `state-inspect --format jsonl`

- **CLI**：`moderator state-inspect --format jsonl [--agent=<name>] [--since=<ts>] [--until=<ts>]`。
- **形态**：每行一个 JSON 对象（newline-delimited JSON）。
- **覆盖**：默认全部 5 个顶层集合（`agents`、`actions`、`chat`、`progress`、`help_requests`）。
- **管道友好**：可接 `jq` / `grep` / `awk`。
- **为什么不是 CSV / SQLite**：v1 不设计 schema、不加依赖，JSONL 够用。
- **出处**：ADR-0013。

### Marker 扩展路径

- **v1 流程（加一种 marker，如 `【报错】`）**：
  1. `core/marker_parser.py` 加正则。
  2. `core/dispatcher.py` 加 dispatch 项。
  3. 更新 glossary。
  4. 更新 role_prompt 模板。
  5. 写一个新 ADR 解释"为什么加这个 marker"。
- **不做**：配置驱动 marker / plugin 系统。
- **v2 候选**：mini-DSL（如果用户重复要求类似 marker）。
- **出处**：ADR-0013。

---

## 7. State schema 术语（第 7 轮 ADR-0014）

### ChatMessage

- **形状**：`{ id, ts, kind, from_agent?, to_agent?, text, metadata? }`。
- **`kind ∈ {peer, cue, system}`**：
  - `peer`：Agent A → Agent B 的 `【TO:B】` 路由结果。
  - `cue`：moderator 的 `<moderator-cue target=X>` 或 Agent 的 `【求助人类】`。
  - `system`：服务器自动生成的提示（如 `<moderator-info kind="parse-warning">` 提示）。
- **不存**进度汇报（进 `state.progress`）、审批回执（保留在 `state.actions` 的状态变化里）。
- **出处**：ADR-0014。

### HelpRequest

- **形状**：`{ id, agent_name, content, ts }`。
- **进入**：`【求助人类】` 触发 → push 到 `state.help_requests`。
- **离开**：moderator 调 `cue(target=<agent_name>, ...)` 时，扫描并删除匹配 `agent_name` 的条目。**派生事件**，不存 `resolved_at`。
- **不永久记录**：离开即删除；moderator 想复盘只能查 `state.chat` 找对应 cue。
- **出处**：ADR-0014。

### ProgressEntry

- **形状**：`{ ts, text }`（ts 由服务器补）。
- **存储**：`state.progress[<agent_name>] = [ProgressEntry, ...]`，每 Agent 最多 50 条 FIFO。
- **渲染**：`check` 工具默认显示计数 + 最近 1–3 行；完整内容在 state。
- **出处**：ADR-0014。

### Schema version

- **位置**：`state` 顶层字段 `schema_version: 1`。
- **迁移**：见 ADR-0016（手动 + 逐步 + 可逆）。
- **出处**：ADR-0016。

### AgentRecord 的 `ssh_fingerprint` 字段

- **类型**：字符串（`str | None`）。
- **写入时机**：首次成功 SSH 连接后；v1 由 SSH 客户端写入 `known_hosts`，MCP server 不独立校验。
- **TOFU 行为**：标准 SSH `known_hosts` 机制；不匹配则 SSH 拒绝连接。
- **出处**：ADR-0015。

---

## 8. 待后续轮次敲定的术语（占位）

以下术语已在主计划中出现，但完整定义将在后续轮次敲定。当前仅占位：

- `Poll cycle`、`Dedup hash` 的精确状态机 —— 待后续轮次细化（v1 接受主计划定义）。
- ADR-0002 / 0003 / 0006（stdio transport / SSH+tmux / marker protocol）—— ✅ **已补**（主计划 locked decisions 转 ADR）

---

## 9. 踩过的坑（Gotchas）

实现过程中真正浪费过时间的陷阱，写在 `docs/gotchas.md`。
每条 gotcha 都附 **symptom / root cause / fix / discovered-in** 四段，
并指向对应的 ADR（如果有的话）。新坑一律 append，不改写历史。

当前条目：

- **G-001** — `msvcrt.locking` 不能跨进程阻塞，用 atomic-take-lockfile（ADR-0004）。
- **G-002** — 同进程内 `with state_lock(): write_state()` 自死锁。
- **G-003** — Windows `spawn` 启动慢，跨进程测试用 ready-file 同步，别 fixed sleep。
- **G-004** — MCP Python SDK stdio 走 NDJSON，不是 LSP 的 `Content-Length:` 帧。
- **G-005** — Pydantic `extra="forbid"` 意味着字段名必须严格匹配 schema。
- **G-006** — PowerShell `git -m @'...'@` 把字面 `@'` 塞进 commit subject，用 `git -F - <<EOF` 走 stdin。

写新坑之前先 grep 一下这里；新坑的"如果它泛化"就升级成 ADR。

---

## 10. 交叉引用

- `docs/adr/0006-marker-protocol-text-tags.md` —— ✅ **主计划 locked decision 转 ADR（已补）**
- `docs/adr/0007-moderator-writeback-protocol.md` —— 写回协议（待写）
- `docs/adr/0008-agent-lifecycle-states.md` —— ✅ **第 2 轮已写**
- `docs/adr/0009-approval-queue-semantics.md` —— ✅ **第 4 轮已写**
- `docs/adr/0010-marker-routing-edge-cases.md` —— ✅ **第 3 轮已写**
- `docs/adr/0011-multi-moderator-v1-singularity.md` —— ✅ **第 5 轮已写**
- `docs/adr/0012-state-file-not-encrypted.md` —— ✅ **第 5 轮已写**
- `docs/adr/0013-marker-rate-handling.md` —— ✅ **第 5 轮已写**
- `docs/adr/0014-state-schema-v1-reconciliation.md` —— ✅ **第 7 轮已写**
- `docs/adr/0015-ssh-fingerprint-tofu.md` —— ✅ **第 7 轮已写**
- `docs/adr/0016-schema-version-migrations.md` —— ✅ **第 7 轮已写**
- `docs/adr/0017-no-auto-restart-on-offline.md` —— ✅ **第 6 轮已写**