# ADR-0016: Schema Version Migrations（schema 版本迁移）

## Status

Accepted — 2026-07-13 `/grill-with-docs` 第 7 轮 7.10。

## Context

`state` 顶层有 `schema_version: 1` 字段，但目前**没有**对应的迁移代码。一旦 v1 之后字段变化（增加 / 删除 / 改名），如何让旧 state 文件升级？

候选：

| 选项 | 优 | 劣 |
|---|---|---|
| 手动迁移脚本 | 透明、可测、可回滚 | moderator 必须手动跑 |
| 启动时自动迁移 | 透明 | 失败时 moderator 困惑；启动时间长 |
| 拒绝加载旧 schema | 强制升级 | 失去向后兼容 |
| 完全无 schema | 灵活 | 字段含义漂移 |

ADR-0004 已锁定"JSON → SQLite 迁移"，那是**后端**迁移。本 ADR 解决 **schema 字段**层面的迁移（即 `schema_version: 1 → 2` 这种）。

## Decision

**schema 迁移是 M9 交付物，手动触发。状态文件顶层含 `schema_version` 字段。**

### 迁移脚本骨架

文件：`state/migrate.py`

```python
def migrate(state: dict, from_version: int, to_version: int) -> dict:
    """逐步执行迁移；每步独立、可测、可回滚。"""
    assert from_version < to_version
    for v in range(from_version, to_version):
        step = MIGRATIONS[v]   # MIGRATIONS[1] = v1→v2 转换函数
        state = step(state)
        state["schema_version"] = v + 1
    return state

# 例子：v1 → v2 假设加了一个字段
def v1_to_v2(state: dict) -> dict:
    for agent in state.get("agents", {}).values():
        agent.setdefault("new_field", None)
    return state

MIGRATIONS = {
    1: v1_to_v2,
    # 2: v2_to_v3,
    # ...
}
```

### 触发流程

1. **检测**：MCP server 启动时读 state 文件，发现 `schema_version` 低于当前代码支持的最低版本（如 `MIGRATIONS` 字典里没有 `from_version = N`）。
2. **拒绝启动**：MCP server 报错并打印：
   ```
   State schema version 1 is below minimum supported version 2.
   Run: python -m state.migrate moderator_state.json
   ```
3. **手动迁移**：moderator 跑命令：
   ```bash
   python -m state.migrate ~/.moderator/moderator_state.json
   ```
   脚本：
   - 读 state → 调 `migrate()` → 写回（带 `.bak.<ts>` 备份）。
   - 校验：迁移后 `schema_version == to_version`。
   - 退出码 0 / 1。
4. **重启 MCP server**：迁移完重启，加载新 schema。

### 迁移原则

- **每次迁移必须可逆**：每个 `MIGRATIONS[v]` 都要有对应 `MIGRATIONS_REVERSE[v]`，用于测试和紧急回滚。
- **每次迁移必须有测试**：`tests/state/test_migrate_v1_to_v2.py` 必须覆盖正常 + 边界 case。
- **不允许跳过版本**：必须按 `1 → 2 → 3` 顺序升级；不允许 `1 → 3` 直接跳。
- **不允许跨多个 schema_version 同时迁移**：单次迁移只升一个版本。

### 向后兼容策略

- **新增字段**：`setdefault(default_value)` 兼容旧 state。
- **删除字段**：迁移时丢弃；老数据丢失不可逆。
- **改名字段**：迁移时 rename；老字段被删除。
- **类型变更**：迁移时显式转换（`str` → `int`）；失败抛 `MigrationError`。

### 迁移触发条件（除手动外）

| 触发 | 处理 |
|---|---|
| `seen_marker_hashes` 累积到 5000 仍无 SQLite 迁移（应触发 ADR-0004） | 不属本 ADR 范围 |
| `state.actions` 累积到 10000 条（ADR-0004 §"规模与迁移触发"） | 同上 |
| schema 字段变化 | 本 ADR |

## Consequences

### 正面

- **手动迁移**：moderator 明确知道何时升级、可测可回滚。
- **可逆**：每个迁移步骤都有 `MIGRATIONS_REVERSE` 对应。
- **不向后兼容永远保留**：每个版本的 schema 在迁移脚本里都有代码（v2 永远能迁回 v1）。

### 负面 / 后续工作

- **手动迁移负担**：moderator 必须记得"state schema 变了 → 跑迁移脚本"。
- **不跳过版本**：跨多版本升级要分多次跑（脚本可以自动，但复杂度 +1）。
- **没有 dry-run 默认**：moderator 可能误跑迁移；可加 `--dry-run` 选项（v2 候选）。

### 交叉引用

- Glossary: [Schema version](../glossary.md)（待补）
- ADR-0004: JSON → SQLite 迁移（后端迁移）
- ADR-0014: state schema（v1 字段定义）
- ADR-0015: SSH TOFU（指纹字段未来可能改）

## Alternatives Considered

- **启动时自动迁移**：失败时 moderator 困惑；启动时间长。**不采用**。
- **拒绝加载旧 schema**：失去向后兼容。**不采用**。
- **完全无 schema**：字段含义漂移。**不采用**。
- **跳过版本迁移**：增加测试矩阵复杂度。**不采用**。