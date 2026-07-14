# ADR-0012: State File Not Encrypted（state 文件不加密）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 5 轮 5.2 + 5.3。

## Context

`state.agents[].role_prompt_path` 记录了远端主机上 role_prompt 文件的路径，**不**记录 role_prompt 的内容。但 `state.chat` 包含 Agent 之间的**完整**对话文本。如果 Agent 在聊天里贴了密码、API key、个人信息，这些会以明文形式存在 state JSON 里。

另外，`role_prompt` 本身（文本）被 SFTP 上传到远端主机的 `/tmp/moderator/role-<name>.txt`，这个文件通常**全局可读**。

如果 disk 被未授权访问（备份泄漏、共享机器的另一个用户、磁盘镜像被复制），secret 可能泄漏。

## Decision

**v1 不对 state 文件加密，role_prompt 落 `/tmp/moderator/` 是已知软肋。**

### 威胁模型（显式文档化）

| 风险 | 威胁假设 | 缓解 |
|---|---|---|
| Disk 被未授权读取 | 用户控制 disk 访问权限（chmod 600 / Windows ACL） | moderator 自己负责保护 disk |
| 同一台机器多用户 | 假设 moderator 是机器的唯一用户 | 不适用于共享机器；建议用专用账号 |
| role_prompt 在 `/tmp` 被其他用户读取 | 同上 | 未来可配置 `role_prompt_dir` 到 `$HOME/.moderator/` |
| State 文件备份泄漏 | 用户控制备份策略 | 文档化"state 含 secret，按 secret 同等级别保护" |
| 加密密钥丢失 | 不适用 | 不加密就没有密钥 |

### 不做的事

- **v1 不做 state 文件加密**：密钥管理是个开放问题（OS keyring? 密码派生? TPM?），每种方案都有自己的失败模式。
- **v1 不做 role_prompt 内容加密**：同上的密钥问题。
- **v1 不做 secret 自动检测 / 脱敏**：难界定什么算 secret。
- **v1 不做 role_prompt 路径变更**：保留 `/tmp/moderator/role-<name>.txt` 默认路径。

### 何时升级

- 出现明确"secret 泄漏"的事件报告。
- 用户在 `start_session` 时可以加可选参数 `role_prompt_dir`（v2），把 role_prompt 落到更安全的位置。
- v2 评估"用 OS keyring + AES-GCM 加密 state 中所有 secret-bearing 字段"的方案。

### 文档化要求

README 必须在显眼位置说明：

> ⚠️ **安全声明**：state 文件和 role_prompt 是**明文**存储，可能包含 secret（API key、密码、个人信息）。请把 state 文件和远端 `~/.moderator` 目录视为 **secret-grade** 资源。

## Consequences

### 正面

- **实现简单**：无密钥管理负担。
- **state-inspect 可直接读**：debug / 复盘不需要先解密。
- **不引入加密失败的可能故障模式**。

### 负面 / 后续工作

- **Secret 泄漏风险**：依赖用户正确保护 disk。
- **`/tmp` 全局可读**：多用户机器风险。文档化为软肋。
- **`role_prompt` 文本本身**可能含敏感信息（如 "you have access to the prod DB"），落 `/tmp` 是已知风险。

### 交叉引用

- Glossary: [Moderator](../glossary.md#moderator--主持人)
- ADR-0011: 多 moderator（同样假设单机单用户）
- ADR-0013: marker 速率（与 secret 无关）

## Alternatives Considered

- **v1 上 OS keyring + AES-GCM**：实现复杂；不同 OS 的 keyring API 差异大。**不采用**。
- **Secret 自动脱敏**：难界定（"password" 是 secret，但 "the password is X" 呢？）。**不采用**。
- **拒绝 secret 进入 state**：靠 role_prompt 教 Agent 不要发敏感 marker。文档化但不强校验。**v2 候选**。
- **role_prompt 落 `$HOME/.moderator/`**：v1 不实现，v2 候选。