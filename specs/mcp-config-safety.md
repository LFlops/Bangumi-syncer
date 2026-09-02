# 配置安全增强设计（暂缓实现）—— 从主方案抽离

> 本文件记录两项**配置安全增强**设计，原属 MCP 方案 v5 的一部分。经评审决定**暂缓实现**，从 `spec.md` 抽离为独立演进记录，不阻塞 MCP 主方案落地。

## 背景

MCP 提供 `update_config` 工具，允许 AI 客户端直接修改 BS 配置。为降低"AI 改坏配置"的风险，曾设计了两层保护：

| 层次 | 设计 | 状态 |
|------|------|------|
| BS 侧改前自动备份 | 服务端强制快照 | 暂缓 |
| Agent Skill 工作流 | 客户端"诊断→修改→验证→回滚"闭环 | 暂缓 |

当前主方案（`spec.md`）仅保留：`update_config` 直接生效，利用 BS **现有** `config_backups` 机制可手动备份/恢复（Web 页面已有该能力，MCP 不新增）。

---

## 一、BS 侧改前自动备份（暂缓）

### 设计意图

- `update_config` 内部 API 在落盘前**自动创建配置备份快照**，返回备份文件名
- 强制、服务端执行，**不依赖客户端/Agent 配合**——Agent 忘不忘记都执行
- 与 Web 的 `config_backups` 机制复用同一存储与清理逻辑

### 方案要点

- 复用：`app/api/config.py` 的备份端点逻辑（`POST /api/config/backup`、`config_backups/` 目录、时间戳命名、清理机制）
- 触发点：`POST /api/mcp/config/update` 处理函数内，`set_config/save_config` **之前**调用备份
- 响应：`{"status": "success", "backup": "<filename>", "changed": {...}}`
- 回滚：`config_backups` 已有 restore 能力，可指导用户通过 Web 或后续 MCP 工具恢复

### 决策点（暂缓时未定）

1. 每次修改都建快照（可能产生大量备份）还是仅在敏感段（`auth`）变更时？—— 倾向每次修改都建（已有清理机制）
2. 是否需要 MCP 工具暴露备份列表/恢复能力（`list_backups` / `restore_config`），还是仅 Web 操作？

---

## 二、Agent Skill 工作流（暂缓）

### 设计意图

在 opencode 中提供一个 `bangumi-debug` skill，封装"诊断→修改→验证→回滚"闭环，让 Agent **改得聪明**（改前确认、改后验证、失败回滚），而非把逻辑塞进 MCP 协议。

### Skill 定义草案

```markdown
# bangumi-debug Skill
用于对 bangumi-syncer 进行日志诊断与配置调优。

## 工作流
1. 诊断：调用 get_logs（按级别/时间窗口过滤）定位问题。
2. 分析：调用 get_current_config 检查相关配置项。
3. 修改：确认后调用 update_config（返回变更结果）。
4. 验证：修改后再次 get_logs 观察效果。
5. 回滚（如失败）：告知用户备份文件名，必要时引导恢复。

## 安全约束
- 只读操作（get_logs / get_current_config）可直接执行。
- 写操作（update_config）必须先向用户展示变更内容并确认。
- 不修改与同步数据相关的配置，除非用户明确要求。
```

### 决策点（暂缓时未定）

1. 交付形态：文档内提供（用户手动安装）还是仓库附带 `.opencode/skills/bangumi-debug/`（开箱即用）？—— 倾向仓库附带
2. Skill 是否需要配套脚本（如自动触发备份/恢复），还是纯提示词编排？

---

## 恢复时机建议

- BS 侧自动备份：待 MCP 主方案落地、`update_config` 稳定后，作为小增量任务补上（改动集中在 `mcp_config.py` 一个文件）
- Agent Skill：随用户文档（`docs/config/mcp.md`）一起发布，或用户提出诊断体验需求时补上