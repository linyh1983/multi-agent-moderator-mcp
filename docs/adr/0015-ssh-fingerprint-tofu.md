# ADR-0015: SSH Fingerprint TOFU（SSH 主机密钥 TOFU）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 7 轮 7.9。

## Context

Agent 通过 SSH 连接到远端主机。SSH 协议默认在第一次连接时把远端主机的公钥指纹（fingerprint）存到 `~/.ssh/known_hosts` —— 这就是 **TOFU**（Trust On First Use）。后续连接会校验指纹是否匹配，不匹配则拒绝。

当前 `state.agents[X].ssh_fingerprint` 字段已经存了指纹，但**从未被校验过**，**也从未在 host 字段变化时更新**。这是已知缺口。

问题：

1. 如果远端主机重装系统 / 更换密钥，`start_session` 会失败（SSH 自身会拒绝），但 moderator 不知道为什么。
2. 如果中间人攻击成功，第一次连接的 fingerprint 就是攻击者的——永远信任错。
3. 指纹存在 `state.agents` 里，但 host 不在身份里（见 ADR-0005）；同名 Agent 跨主机时 fingerprint 会覆盖——审计不清。

## Decision

**v1 采用 SSH 标准 TOFU 行为：首次成功连接后存指纹，后续不匹配拒绝。**

### 行为

1. **`start_session(host=X)`**：
   - SSH 客户端尝试 `X` 的密钥认证。
   - 第一次 → SSH 客户端问"未见过这个 fingerprint，是否信任"。
   - v1 **由用户手动回答**（v1 走标准 SSH 流程，不做 fingerprint 自动采集脚本）。
   - 一旦连接成功，`ssh_fingerprint` 写入 `state.agents[X]`（v1 不强制校验，依赖 SSH 客户端）。
2. **后续 `start_session(host=X)`**：
   - SSH 客户端比对 `~/.ssh/known_hosts` 中的 fingerprint。
   - 不匹配 → SSH 客户端拒绝连接 → `start_session` 返回错误 `SSHHostKeyChanged(name=X, host=X, expected=<旧指纹>, actual=<新指纹>)`。
   - moderator 必须**显式**从 `known_hosts` 删旧条目才能接受新指纹。
3. **`state.agents[X].ssh_fingerprint` 字段**：
   - v1 **作为记录字段**，由 SSH 客户端写入（v1 不在 MCP server 层做独立校验）。
   - 审计日志：如果 `ssh_fingerprint` 与已知值不同，记一条 `MarkerParseWarning`（不阻断，但 moderator 在 `check` 里能看到）。
4. **同名 Agent 跨主机（ADR-0005 §6.5）**：
   - 已同名拒绝，所以 fingerprint 不存在覆盖问题。

### 不做的事

- **v1 不做"MCP server 层独立 fingerprint 校验"**：依赖 SSH 客户端自己的 `known_hosts`。
- **v1 不做 fingerprint 变更告警 UI**：依赖标准 SSH 错误消息。
- **v1 不做 fingerprint 滚动备份**：与 known_hosts 行为一致。

### 何时升级

- 用户反馈"想看到 fingerprint 变化的可视化提示" → v2 在 `check` 里高亮 fingerprint mismatch。
- 用户反馈"想强制 TOFU 校验到 MCP server 层"（不依赖 SSH 客户端）→ v2 加 `core/ssh.py` 的 fingerprint 校验。

## Consequences

### 正面

- **贴合 SSH 标准**：用户已有的 SSH 习惯（首次确认 + 后续自动）继续生效。
- **零额外实现**：v1 把 `ssh_fingerprint` 字段作为"审计字段"，不阻挡连接。
- **中间人攻击防御靠 SSH 客户端**：与系统其他部分一致。

### 负面 / 后续工作

- **远端主机更换密钥时 moderator 必须手动处理**：标准 SSH 行为；moderator 需要知道怎么改 `known_hosts`。
- **`state.agents[X].ssh_fingerprint` 不在 MCP server 层校验**：依赖 SSH 客户端；用户换 SSH 客户端（libssh2 / paramiko）可能行为不一致。
- **没有 TOFU 撤销机制**：v1 不提供"我不再信任 X"的 API；moderator 直接从 `known_hosts` 删。

### 交叉引用

- Glossary: [Agent](../glossary.md#agent)（`ssh_fingerprint` 字段）
- ADR-0005: additional_agents（同名拒绝 → fingerprint 不被覆盖）
- ADR-0008: Agent 生命周期（`host` 是 AgentRecord 字段之一）
- ADR-0014: state schema（`ssh_fingerprint?` 字段定义）

## Alternatives Considered

- **MCP server 层独立校验 fingerprint**：与 SSH 客户端重复；v1 收益小。**不采用**。
- **强制 PinnedKey（moderator 提前提供 fingerprint）**：配置负担重，v1 不必要。**不采用**。
- **不存 fingerprint**：与 `.test-state/moderator_state.json` 实际形状冲突。**不采用**。
- **v1 完全依赖 SSH 客户端不写 fingerprint**：与实际代码状态不符。**不采用**。