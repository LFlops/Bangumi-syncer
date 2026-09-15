# [暂缓] MCP OAuth 状态持久化（当前全部进程内存态）

- **状态**：暂缓（2026-09-12 决策：TTL 清理按内存态实现，不做持久化）
- **范围**：`app/mcp/provider.py`（`_clients` / `_auth_codes` / `_refresh_tokens` / `_pending_auths` / `_revoked_tokens`）
- **决策**：维持内存态。重启即全量清空，作为"硬重置"兜底（DCR 被刷注册时可用，已写入用户文档）。

## 背景（内存态的既有边界）

| 数据 | 重启后 | 影响 |
| --- | --- | --- |
| `_clients`（DCR 注册） | 清空 | 客户端需重新注册/授权 |
| `_auth_codes` / `_pending_auths` | 清空 | 短命状态，用户重试即可 |
| `_refresh_tokens` | 清空 | 客户端需重新走一次 OAuth（Access Token 1h 内仍可用） |
| `_revoked_tokens` | 清空 | **被吊销的 Access Token 会"复活"到自然过期（≤1h）** |

- Access Token 为自包含 JWT（无状态），不占用任何表；
- 唯一落盘的 MCP 认证状态是 RSA 密钥对（`MCP_RSA_PRIVATE_KEY` / `MCP_RSA_PUBLIC_KEY`）；
- 因此当前仅支持单进程（多 worker 会导致 refresh/revoked 状态不一致）。

## 触发持久化的条件

1. 支持多 worker / 多副本横向扩展；
2. 希望"重启后客户端无感续期"（不重新授权）；
3. 需要吊销立即生效且不因重启回退。

## 未来方案（若触发）

**需要持久化的对象**
- DCR 客户端：client_id / client_secret（哈希存储）/ redirect_uris / scope / 注册时间
- Refresh Token：建议存 SHA-256 哈希（不落明文）+ client_id + expires_at
- 吊销名单：jti + exp
- 无需持久化：`_pending_auths`、`_auth_codes`（分钟级短命，重启丢弃即可）

**方案 A：SQLite（单机推荐，零新依赖）**
- 复用现有 `data/` 数据库设施，新增表：`oauth_clients` / `oauth_refresh_tokens` / `oauth_revoked`
- 启动时加载或按需查询；清理可交给 SQL（`DELETE WHERE expires_at < now`）或沿用惰性 sweep
- 注意：写频度低、并发小，SQLite 足够；refresh token 建议哈希后存储

**方案 B：Redis（多 worker / 多副本）**
- 原生 TTL 直接接管过期（`SETEX`），吊销用 `SETEX jti`，天然支持多进程共享
- 需要引入外部依赖与部署运维成本

**迁移注意**
- 切换存储时需定义"旧内存贡献"的处理（重启窗口内 token 无效属可接受）
- 持久化后仍需保留"重启清零"的管理手段时，应提供显式清理入口（见 `remain/mcp-dcr-reset-web-api.md`）
- 安全：refresh token / client_secret 落盘需哈希或加密；备份策略需覆盖该库
