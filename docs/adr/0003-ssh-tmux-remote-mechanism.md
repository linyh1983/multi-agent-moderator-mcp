# ADR-0003: Remote Agent Mechanism is SSH + tmux（远端 Agent 机制用 SSH + tmux）

## Status

Accepted — 2026-07-13（主计划 locked decision 转 ADR）。

## Context

每个 Agent 跑在远端主机上。MCP server 如何启动 + 监控一个远端 Agent？

候选：

| 机制 | 优 | 劣 |
|---|---|---|
| **SSH + tmux** | 标准、人类可调试、复用远端 shell、ANSI 干净 | 依赖远端有 tmux + sshd |
| **SSH + nohup + log file** | 极简 | 无交互；无法 `cue`（写 stdin） |
| **Docker exec** | 隔离 | 需远端有 Docker；不必要隔离 |
| **HTTP API** | 易扩展 | 每个 Agent 都要实现 HTTP server |
| **直接远程 Python 子进程** | 简单 | 无 shell 持久；无法 `cue` |
| **VS Code Remote / SSH 协议** | 强 | 重 |

REQUIREMENT 强调"Agent 在远程机器的后台独立运行"。Agent 是**长会话**：需要 stdin（接收 `<moderator-cue>` 写回）+ stdout（输出 marker）+ 持续。

## Decision

**v1 远端 Agent 机制 = SSH + tmux**。每个 Agent 是远端主机上一个 tmux session，MCP server 通过 SSH 进 tmux send-keys / load-buffer / paste-buffer / capture-pane。

### 组件

- **SSH**：建立到远端主机的持久连接；走标准 `paramiko` / `asyncssh` 库。
- **tmux**：每个 Agent 一个命名 session（如 `mod-<agent_name>`）；持久后台跑，即使 MCP server 重启也不掉。
- **tmux 操作**：
  - **启动 Agent**：`tmux new-session -d -s mod-<name> "python -m claude"` 启动 Claude 子进程。
  - **写 stdin**：
    - 短单行：`tmux send-keys -t mod-<name> "line" Enter`。
    - 多行 / 长内容：`tmux load-buffer <tmp>` + `tmux paste-buffer -t mod-<name>`（避免转义）。
  - **读 stdout**：`tmux capture-pane -p -t mod-<name> -S -<N>` 取最近 N 行；增量从上次 `log_offset` 起算。
  - **存活检查**：`tmux has-session -t mod-<name>`。

### 远端平台范围

**v1 仅 macOS + Linux**（tmux 在两者都是默认）。**Windows 远端 deferred to v2**（v2 候选 WSL / PowerShell + Windows Terminal）。

### 凭证

- **SSH 密钥**：标准 `~/.ssh/id_<algo>`；v1 由 SSH 客户端自己处理（详见 ADR-0015）。
- **远端账号**：用户指定；不在 MCP server 内做凭据管理。
- **role_prompt 传输**：MCP server 通过 SFTP 把 `role-<name>.txt` 上传到 `/tmp/moderator/`（详见 ADR-0012 §"/tmp/moderator 软肋"）。

### 不做的事

- **v1 不支持 Windows 远端**：tmux 在 Windows 上不原生支持。
- **v1 不做 HTTP API 替代**：每 Agent 都要实现 HTTP 太重。
- **v1 不做 Docker 容器化**：增加远端依赖。
- **v1 不做 agent 间共享文件系统**：每 Agent 独立 host / dir。

### 何时升级

- 用户反馈"需要 Windows 远端" → v2 评估 WSL / 替代 shell。
- 用户反馈"想要 Agent 隔离" → v2 评估 Docker exec。

## Consequences

### 正面

- **标准、人类可调试**：用户可以 `ssh host && tmux attach -t mod-<name>` 直接看 Agent。
- **持久后台**：MCP server 重启不影响 Agent；server 重连后从 `log_offset` 增量读取。
- **复用远端 shell**：不需要在远端装额外 runtime。
- **契合拉取模式**（REQUIREMENT 第 43 行）：`capture-pane` 增量读 stdout。

### 负面 / 后续工作

- **远端必须有 tmux + sshd**：限制远端平台（v1 排除 Windows）。
- **SFTP + tmux 双通道**：实现稍复杂（SSH 客户端要同时支持 shell 和 SFTP）。
- **`/tmp/moderator` 软肋**：role_prompt 文件落 `/tmp` 多用户机器有风险（ADR-0012）。
- **无进程隔离**：Agent 是 tmux 子进程，与其他 shell 任务共享 host。

### 交叉引用

- Glossary: [Agent](../glossary.md#agent)、[Marker](../glossary.md#marker--标记)（远程 stdout）
- ADR-0001: 我们用 ADR
- ADR-0008: Agent 生命周期（tmux session 消失 → `offline` 状态）
- ADR-0012: state 文件不加密（`/tmp/moderator` 软肋）
- ADR-0015: SSH 指纹 TOFU（`known_hosts` 信任）
- ADR-0017: 不自动重启 offline（sshd 不可达 → 人工恢复）

## Alternatives Considered

- **SSH + nohup + log file**：无法 stdin。**不采用**。
- **Docker exec**：远端依赖重。**不采用**。
- **HTTP API**：每 Agent 都要实现。**不采用**。
- **VS Code Remote 协议**：依赖 VS Code Server。**不采用**。
- **远程 Python 子进程**：无 shell 持久。**不采用**。