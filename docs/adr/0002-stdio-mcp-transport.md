# ADR-0002: MCP Transport is stdio（MCP 传输用 stdio）

## Status

Accepted — 2026-07-13（主计划 locked decision 转 ADR）。

## Context

Moderator MCP Server 是 MCP 协议的实现。MCP 协议有多种传输方式：

| 传输 | 适用场景 |
|---|---|
| **stdio** | 本地 MCP server（Claude Code 作为父进程 spawn MCP server 子进程，通过 stdin/stdout 通信） |
| **HTTP / SSE** | 远程 MCP server（独立进程 / 远端主机 / 跨网络） |
| **WebSocket** | 双向长连接场景 |

Moderator MCP Server 的设计目标：

- 用户在**本地** Claude Code 会话里运行（REQUIREMENT 第 1 段："这是一个本地运行的 MCP Server"）。
- 一个 Claude Code 进程 = 一个 moderator = 一个 MCP server 子进程。
- 不跨网络暴露给其他机器。

## Decision

**MCP transport 是 stdio**。Claude Code spawn moderator-mcp 作为子进程，通过 stdin/stdout 双向通信。

### 行为

- **spawn**：Claude Code 启动时按 `.claude/settings.json` 配置 spawn `python -m moderator` 子进程。
- **通信**：MCP 协议在 stdin（Claude Code → server 的工具调用）和 stdout（server → Claude Code 的工具结果）上跑 JSON-RPC。
- **stderr**：server 的日志输出，不混入 stdout（避免污染 JSON-RPC 流）。
- **生命周期**：Claude Code 进程退出 → server 子进程自然终止。

### 不做的事

- **v1 不做 HTTP / SSE 传输**：单用户单进程场景不需要；增加部署复杂度。
- **v1 不做远程 MCP server**：moderator 永远在本地。
- **v1 不做多 server**：一个 Claude Code 会话 = 一个 server 子进程。

### 何时升级

- 如果用户反馈"想从远程机器用 moderator 控制其他机器的 Agent" → v2 评估 HTTP / SSE 传输。
- 如果用户反馈"想多个 Claude Code 会话共享一个 server" → v2 评估跨进程 / 网络传输。

## Consequences

### 正面

- **零部署**：spawn 即可，Claude Code 退出自动清理。
- **无端口冲突**：不需要管 firewall / port。
- **天然隔离**：每个 Claude Code 会话 = 独立 server 进程，互不干扰。
- **与 ADR-0011 一致**：单进程假设成立时 stdio 是最简形式。

### 负面 / 后续工作

- **多 moderator 场景需各自 spawn**：不共享 server 状态（ADR-0011 已覆盖）。
- **无法从远程主机用 moderator**：v1 不支持；v2 候选 HTTP。
- **server 日志走 stderr**：用户必须看 stderr 才能 debug（README 文档化）。

### 交叉引用

- Glossary: [Moderator](../glossary.md#moderator--主持人)
- ADR-0001: 我们用 ADR（元模板）
- ADR-0011: 多 moderator（stdio + 单进程假设）
- ADR-0012: state 文件不加密（stdio 不跨网络，明文假设成立）

## Alternatives Considered

- **HTTP / SSE**：增加部署复杂度、端口冲突、防火墙问题。**不采用**。
- **WebSocket**：双向长连接是 stdio 的超集，单用户用不到。**不采用**。
- **本地 socket file**：与 stdio 效果类似但需要管 socket 路径。**不采用**。
- **共享一个 server 子进程给多个 Claude Code 会话**：违反"moderator 是单数"的 REQUIREMENT。**不采用**。