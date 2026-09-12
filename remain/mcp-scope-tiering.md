# [暂缓] MCP Scope 分级（read / write / admin）

- **状态**：暂缓（2026-09-11 决策，PR #11 讨论）
- **范围**：`app/mcp/provider.py`、`app/mcp/tools.py`
- **决策**：暂不实施分级；保留现状 `read` / `write` 两档，且 `write` 可写敏感字段（`bangumi.access_token`、`llm.api_key`、`notify-*.smtp_password` 等）。`auth` 段始终禁写。

## 背景

- 当前 `valid_scopes = ["read", "write"]`（provider.py 两处），默认只发 `read`（客户端不请求时；DCR / CIMD 同理）。
- 工具映射：`read` → `get_logs` / `get_current_config`（敏感字段已掩码）；`write` → `update_config`（含敏感字段写入）。
- SDK 强制校验「请求的 scope ⊆ 客户端已注册 scope」，越权返回 `invalid_scope`。

## 未来可选方案（三档）

| 档位 | scope | 授予能力 |
| --- | --- | --- |
| 只读（默认） | `read` | get_logs + get_current_config |
| 读写 | `read write` | + update_config，但拒绝敏感字段（`is_sensitive_field` 命中即拒） |
| 全权 | `read write admin` | + 允许写敏感字段（auth 段仍禁） |

更细粒度（`logs:read` / `config:read` / `config:write` / `secrets:write`）暂不考虑：OAuth scope 无继承语义，consent 页与客户端适配成本高。

## 实施要点（如未来恢复）

- `provider.py`：更新两处 `valid_scopes`
- `tools.py`：新增 `admin` 常量；`update_config` 中「write 拒绝敏感字段 / admin 放行」
- 测试：每档正反用例、缺 scope 拒绝用例
- 文档：`docs/config/mcp.md` scope 表、consent 页展示
