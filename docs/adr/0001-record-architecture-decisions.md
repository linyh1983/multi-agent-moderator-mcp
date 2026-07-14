# ADR-0001: Record Architecture Decisions（我们用 ADR）

## Status

Accepted — 2026-07-13（项目初始）。

## Context

Moderator MCP Server 项目正在从需求文档推进到实现。期间会有很多架构决策：

- 为什么是 stdio 而不是 HTTP 做 MCP 传输？
- 为什么用 SSH + tmux 而不是 Docker exec？
- 为什么 v1 不支持多 moderator？

如果这些决策只写在聊天 / 主计划的散文段落里，后来人（甚至原作者 6 个月后）必须重新论证。本 ADR 把"我们用 ADR"作为元决策定下来。

## Decision

**Moderator MCP Server 项目的所有架构决策都用 ADR（Architecture Decision Record）记录，Nygard 格式，存于 `docs/adr/`。**

### ADR 格式（Michael Nygard）

每个 ADR 包含 4 节：

```markdown
# ADR-NNNN: <Title>

## Status

Accepted | Proposed | Superseded by ADR-NNNN | Deprecated | ...

## Context

<决策时的背景；要解决什么问题；候选方案是什么；有什么约束；引用相关 ADR / 需求 / 计划。>

## Decision

<最终决定的内容；细节、参数、阈值、行为契约；可以多小节，每节是一个原子决定。>

## Consequences

### 正面
<这个决策带来的好处。>

### 负面 / 后续工作
<代价、技术债、未来可能要改的地方。>

## Alternatives Considered

<讨论过的其他选项；说明为什么不采用。>

### 交叉引用

<Glossary: <term>、ADR-NNNN 等。>
```

### 文件命名

`docs/adr/NNNN-<kebab-case-slug>.md`，编号从 `0001` 起，单调递增。已弃用的 ADR 不删除，重命名为 `NNNN-<slug>.deprecated.md` 或保留原名 + `Status: Deprecated`。

### 编号规则

- 前 10 个（0001–0010）预留给**主计划 locked decisions + 第一轮基础**。
- 0011+ 给**grilling / 实施中产生的决策**。
- 已写：0004 / 0005 / 0007 / 0008 / 0009 / 0010 / 0011 / 0012 / 0013 / 0014 / 0015 / 0016 / 0017。
- 待写：0002 / 0003 / 0006（主计划 locked decision 补全）。

### 何时写 ADR

- 锁定一个新决策（不再讨论）。
- 改变一个已锁定的决策（写新 ADR 并把旧的标 `Superseded by`）。
- 拒绝一个常见替代方案（写 ADR 解释为什么不采用，避免下次再争论）。

### 何时**不**写 ADR

- 临时实验 / spike。
- 代码风格 / 命名约定（用 lint 规则）。
- 库版本升级（CHANGELOG）。
- 与项目无关的外部决策。

### README / 索引

`docs/adr/` 下不需要单独 README——`README.md`（项目根）已含"必读 ADR"列表 + 全部 ADR 在 glossary §8 / §9 交叉引用。

## Consequences

### 正面

- **决策可追溯**：每个 ADR 是"当时为什么这么做"的快照。
- **降低重复论证**：下次有人问"为什么 stdio"，直接给 ADR-0002。
- **新人上手快**：按 glossary + ADR 列表读完就懂系统设计。

### 负面 / 后续工作

- **写 ADR 的负担**：每个决策多 ~30 分钟文档化。靠 ADR 模板和已知字段加速。
- **可能与代码漂移**：ADR 写在代码前；如果代码改了但没更新 ADR，需要 CI lint（v2 候选）。
- **过度文档化风险**：小事不值得 ADR；靠 ADR-0001 §"何时不写 ADR"约束。

### 交叉引用

- 本 ADR 是所有后续 ADR 的元模板。
- Glossary: 所有术语。

## Alternatives Considered

- **不做 ADR，决策散落主计划**：主计划变更历史不可见，后来人重读计划才知决策。**不采用**。
- **用 Google Doc / Wiki**：与代码不同步，链接腐烂。**不采用**。
- **用更轻量的"决策日志"（只记结论 + 日期）**：缺 Context / Alternatives 价值。**不采用**。
- **强制每个 PR 都要 ADR**：过度负担。**不采用**，靠 ADR-0001 §"何时写 ADR"约束。