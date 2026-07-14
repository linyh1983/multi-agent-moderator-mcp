# Moderator MCP Server

本地运行的 MCP Server，让用户在一个 Claude Code 会话中同时管理多个远程 AI 编码 Agent。

> **核心理念**：讨论自由，执行受控。
>
> Agent 之间可以自由讨论技术方案，但执行任何实际操作前必须经主持人（用户）批准。

详见 [`REQUIREMENT.txt`](./REQUIREMENT.txt)。

---

## 它做什么

主持人（即用户本人）通过本地 Claude Code 的 6 个 MCP 工具管理远端 Agent：

| 工具 | 作用 |
|---|---|
| `start_session` | 在远程机器上启动一个新 Agent |
| `check` | 查看所有 Agent 的状态、待批操作、紧急求助、群聊动态 |
| `approve` | 批准一条或多条待批操作 |
| `reject` | 驳回一条或多条待批操作 |
| `cue` | 向指定 Agent 或所有 Agent 发送指令 |
| `stop_session` | 停止并清理指定 Agent 的会话 |

完整的工具语义见 `docs/glossary.md` 和 `docs/adr/`。

---

## 协作模型（速览）

**主持人** = 用户本人，本地 Claude Code 会话中通过工具调用管理 Agent。拥有最终决策权。

**Agent** = 远程机器上独立运行的进程；通过四种结构化标记（marker）与系统交互。

### 四种 marker

| Marker | 方向 | 作用 |
|---|---|---|
| `【进度汇报】…【/进度汇报】` | Agent → 服务器 | 心跳 |
| `【TO:<agent_name>】…【/TO:<agent_name>】` | Agent → Agent | 同行消息（不经 moderator） |
| `【申请执行】…【/申请执行】` | Agent → 服务器 | 执行申请（moderator 必审批） |
| `【求助人类】…【/求助人类】` | Agent → moderator | 紧急求助（仅这一种能 ping moderator） |

详细协议见 [ADR-0006](./docs/adr/0006-marker-protocol-text-tags.md)（marker 协议）和 [ADR-0007](./docs/adr/0007-moderator-writeback-protocol.md)（写回协议）。

### 讨论 vs 执行

- **讨论（自由）**：读文件、`grep` / `cat` / `ls` / `git log` / 只读 API。Agent 自由进行。
- **执行（需审批）**：写文件、删文件、shell 命令、test / build / install、git commit、副作用 API 调用。Agent 必须发 `【申请执行】` 等批准。

边界原则：是否留下持久状态变更或对外部系统产生副作用。

---

## Non-Claude Agents（软契约）

Moderator MCP Server 协议本身**不依赖** Claude Code 的特定行为。任何满足以下最低要求的进程都可作为 Agent 接入：

1. **能读 stdin**：服务器通过 stdin 发送 `<moderator-…>` 写回 tag。
2. **能写 stdout**：Agent 通过 stdout 输出 marker。
3. **能保持长会话**：进程不主动退出，除非收到停止信号。
4. **尊重 marker 语法**：完整闭合 `【…】…【/…】`；嵌套 / 畸形时协议容错（详见 [ADR-0010](./docs/adr/0010-marker-routing-edge-cases.md)）。
5. **解析 writeback tag**：能从 stdin 文本中提取 `<moderator-approve>` / `<moderator-reject>` / `<moderator-cue>` / `<moderator-info>` 并按 `kind` 分发。

### v1 不做

- **不做 schema 强制**：Agent 端解析由 Agent 自己负责，moderator 不验证。
- **不做适配器**：Hermes / Gemini CLI / 自定义 Agent 脚本需自己解析 tag。`docs/adr/0007-moderator-writeback-protocol.md` 提供完整语法。
- **不做发现机制**：Agent 启动需要 moderator 显式 `start_session(name, host, ...)` 拉起。

### v2 候选

- **mini-DSL for marker 协议**：如果用户频繁接入新 Agent 类型，考虑提供 SDK。
- **plugin 系统**：把 marker / writeback 写成可注册的 schema。复杂度高，仅在明确需求时再做。

### 推荐的非 Claude 接入示例

任何能在 tmux 会话里跑的脚本都可以。例如：

```bash
# 简单 shell Agent：echo marker，read stdin
#!/bin/bash
while read -r line; do
  case "$line" in
    "<moderator-approve"*) echo "【进度汇报】收到批准，开始执行【/进度汇报】" ;;
    "<moderator-cue"*) echo "【进度汇报】收到 cue【/进度汇报】" ;;
  esac
done
```

更严肃的 Agent（Hermes / Aider / 自定义脚本）应按 [ADR-0007](./docs/adr/0007-moderator-writeback-protocol.md) 的语法解析。

---

## 安全声明

⚠️ **state 文件和 `role_prompt` 是明文存储**。可能包含 secret（API key、密码、个人信息）。

- **state 文件**：`~/.moderator/moderator_state.json`（用户可覆盖）。请按 secret-grade 资源保护（chmod 600 / Windows ACL）。
- **role_prompt 路径**：远端主机的 `/tmp/moderator/role-<name>.txt`，通常**全局可读**。
- **不适用于多用户机器**：假设 moderator 是机器唯一用户。
- **v1 不做加密**：密钥管理是开放问题。详见 [ADR-0012](./docs/adr/0012-state-file-not-encrypted.md)。

---

## 架构与设计文档

- **术语表**：[`docs/glossary.md`](./docs/glossary.md) —— 系统的通用语言（角色、marker、审批、生命周期、横切关注点）。
- **架构决策记录**：[`docs/adr/`](./docs/adr/) —— 所有 ADR 按数字排列。每项决策的 Context / Decision / Consequences / Alternatives 都已记录。

### 必读 ADR

新贡献者建议按以下顺序阅读：

1. [ADR-0001](./docs/adr/0001-record-architecture-decisions.md) —— 我们用 ADR（元模板）
2. [ADR-0002](./docs/adr/0002-stdio-mcp-transport.md) —— MCP 传输用 stdio
3. [ADR-0003](./docs/adr/0003-ssh-tmux-remote-mechanism.md) —— 远端 Agent 机制
4. [ADR-0004](./docs/adr/0004-json-file-state-with-file-locking.md) —— 持久化
5. [ADR-0005](./docs/adr/0005-additional-agents-routing-allowlist.md) —— 路由白名单 + 同名拒绝
6. [ADR-0006](./docs/adr/0006-marker-protocol-text-tags.md) —— Marker 协议
7. [ADR-0007](./docs/adr/0007-moderator-writeback-protocol.md) —— 写回协议
8. [ADR-0008](./docs/adr/0008-agent-lifecycle-states.md) —— Agent 生命周期
9. [ADR-0009](./docs/adr/0009-approval-queue-semantics.md) —— 审批语义
10. [ADR-0010](./docs/adr/0010-marker-routing-edge-cases.md) —— Marker 边界条件
11. [ADR-0014](./docs/adr/0014-state-schema-v1-reconciliation.md) —— State schema v1 对账
12. [ADR-0015](./docs/adr/0015-ssh-fingerprint-tofu.md) —— SSH 指纹 TOFU
13. [ADR-0016](./docs/adr/0016-schema-version-migrations.md) —— Schema 版本迁移

---

## 安装与启动

> 占位。v1 实施推进中。

```bash
# 占位
git clone <repo>
cd multi-agent-moderator-mcp
pip install -e .
```

启动后 moderator 在 Claude Code 会话里会看到 6 个工具可用：`start_session` / `check` / `approve` / `reject` / `cue` / `stop_session`。

---

## 状态文件

默认路径：`~/.moderator/moderator_state.json`（可通过环境变量覆盖）。

JSON 形态，包含 5 个顶层集合（`agents` / `actions` / `chat` / `progress` / `help_requests`），外加辅助字段（`action_seq` 计数器、`seen_marker_hashes` FIFO、`schema_version`）。详细 schema 见 [ADR-0014](./docs/adr/0014-state-schema-v1-reconciliation.md)（待写）。

### 审计导出

```bash
moderator state-inspect --format jsonl [--agent=<name>] [--since=<ts>]
```

详见 [ADR-0013](./docs/adr/0013-marker-rate-handling.md)。

---

## 已知软肋（v1 显式不解决的）

| 项 | 文档 | 何时升级 |
|---|---|---|
| 多 moderator 协同 | [ADR-0011](./docs/adr/0011-multi-moderator-v1-singularity.md) | ≥3 个独立报告 |
| State 文件加密 | [ADR-0012](./docs/adr/0012-state-file-not-encrypted.md) | 出现 secret 泄漏报告 |
| `role_prompt` 落 `/tmp` | [ADR-0012](./docs/adr/0012-state-file-not-encrypted.md) | v2 候选 |
| 自动重启 offline Agent | [ADR-0017](./docs/adr/0017-no-auto-restart-on-offline.md) | ≥3 个独立报告 |
| 配置驱动 marker 类型 | [ADR-0013](./docs/adr/0013-marker-rate-handling.md) | 用户重复要求新 marker |
| Schema 自动迁移 | [ADR-0016](./docs/adr/0016-schema-version-migrations.md) | 出现"想启动自动迁移"报告 |
| MCP server 层 SSH fingerprint 校验 | [ADR-0015](./docs/adr/0015-ssh-fingerprint-tofu.md) | 用户希望独立校验 |

---

## 开发

- **M1–M8 实施**：见主计划 `~/.claude/plans/requirement-txt-atomic-canyon.md`。
- **设计文档同步**：本 README 跟 `docs/` 一起维护；改协议时先写 ADR 再改代码。

---

## 许可

待定。