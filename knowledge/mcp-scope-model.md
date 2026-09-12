# MCP Scope 模型与权限边界

> 代码：`app/mcp/provider.py`、`app/mcp/tools.py`；用户文档：`docs/config/mcp.md`「安全设计」。

## 现状（read / write 两档）

| scope | 工具 | 说明 |
| --- | --- | --- |
| `read`（默认） | `get_logs`、`get_current_config` | 配置读取时敏感字段已掩码（含 `auth.password` / `auth.secret_key`） |
| `write` | `update_config` | 可写配置，**当前包含敏感字段**（`bangumi.access_token`、`llm.api_key`、`notify-*.smtp_password` 等） |

硬性边界（与 scope 无关）：

- `auth` 段永远禁写；
- MCP 工具集固定为 3 个，不存在任意 API / 命令执行；
- 客户端请求的 scope 必须 ⊆ 其注册/声明的 scope（SDK `validate_scope` 校验，越权返回 `invalid_scope`）；
- 客户端不请求 scope 时默认只发 `read`（DCR 注册、CIMD `default_scope`、authorize 三处一致）。

## 未来可选：三档分级（暂缓）

| 档位 | scope | 授予能力 |
| --- | --- | --- |
| 只读（默认） | `read` | get_logs + get_current_config |
| 读写 | `read write` | + update_config，但拒绝敏感字段（`is_sensitive_field` 命中即拒） |
| 全权 | `read write admin` | + 允许写敏感字段（auth 段仍禁） |

更细粒度（`logs:read` / `config:read` / `config:write` / `secrets:write`）暂不考虑：OAuth scope 无继承语义、consent 页与客户端适配成本高。

## 决策记录

- 2026-09-12：暂缓分级，保留现状（write 可写敏感字段）。实施要点与恢复条件见 `remain/mcp-scope-tiering.md`。

## 代码索引

- `_require_read_scope()`（tools.py）：read 校验；`update_config` 内 write 校验
- `valid_scopes`：provider.py 两处（provider 默认参数、装配函数）
- consent 页展示 scopes（provider.py `handle_consent`）
