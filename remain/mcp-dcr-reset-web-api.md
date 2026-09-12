# [待办] DCR 客户端管理应做成 Web 接口（非 MCP 工具）

- **状态**：暂缓；当前已有兜底（重启进程）
- **范围**：`app/mcp/provider.py`、`app/api/`、配置页
- **决策**：管理 / 重置接入客户端属于 **Web 管理面**，不应做成 MCP 工具——MCP 不应管理自身的会话与授权。若未来实现，应挂在 BS 会话鉴权的 Web API 下，可同时在配置页展示「当前接入数」并提供重置入口。

## 当前兜底（已写入用户文档）

`docs/config/mcp.md`「动态客户端注册（DCR）」：

- 已注册客户端为进程内存态（`MAX_CLIENTS=1000`），**重启 BS 服务即清空全部注册**（连带清空 pending / 授权码 / refresh / 吊销状态）
- 已签发的 Access Token 在 1 小时有效期内仍有效（JWT 自包含验签），到期后客户端重新授权

## 未来方案（若公网暴露 / 需要不停服重置）

1. Web 管理 API（BS 会话鉴权）：
   - `GET /api/mcp/clients`：列出当前注册客户端（client_id、名称、注册时间）
   - `POST /api/mcp/clients/reset`：清空 `_clients`（可选同时清 pending / code / refresh / revoked）
2. 配置页：展示当前接入数 + 一键重置按钮
3. 如需立即失效已签发的 access token：记录 `not_before` 时间戳并在 `load_access_token` 拒绝 `iat < not_before` 的 JWT；或轮换 RSA 密钥（影响面更大）
4. 其余可选：`/register` 限流、DCR 配置开关（`ClientRegistrationOptions(enabled=False)`）

## 注意

- MCP 工具面不应新增「重置自身会话」类工具
- 关闭 DCR 后仅 CIMD 客户端可授权，依赖 DCR 的客户端将不可用
