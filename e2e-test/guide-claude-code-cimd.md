# 指南 / 测试用例：Claude Code 通过 CIMD 接入 BS MCP

> 状态：**已实测验证**（2026-09-13/14，Claude Code 2.1.236 + 本分支 Docker 镜像）
> 关联修复：CIMD 客户端 scope 允许集（`app/mcp/provider.py` 的 `_ALLOWED_SCOPES`）——修复前 Claude Code 会因 `invalid_scope` 无法接入

## 1. 原理：CIMD 是什么，和 DCR 有何不同

CIMD（Client ID Metadata Document）：**client_id 是一个 HTTPS URL**，指向客户端的元数据文档。授权服务器在授权时实时抓取该文档并校验，**不需要**客户端事先注册。

| 维度 | DCR（RFC 7591） | CIMD |
| --- | --- | --- |
| client_id | 服务端生成随机串 | 客户端提供的 HTTPS URL |
| 注册 | 先 `POST /register` | 无注册，授权时抓取文档 |
| 服务端存储 | 内存注册表 | 无状态 |
| Claude Code | 不使用 | **使用** |

Claude Code 的 CIMD 文档（公开实测）：

```
URL: https://claude.ai/oauth/claude-code-client-metadata
{
  "client_id": "https://claude.ai/oauth/claude-code-client-metadata",
  "client_name": "Claude Code",
  "client_uri": "https://claude.ai",
  "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

授权时序（实测日志路径）：

```
Claude Code                         BS（内置 MCP OAuth AS）
    │  POST /mcp（无 token）            │
    │ ────────────────────────────────► 401 + Protected Resource Metadata
    │  发现 AS metadata                  │  client_id_metadata_document_supported: true
    │  GET /authorize?client_id=https%3A%2F%2Fclaude.ai%2F...&scope=read+write
    │      &redirect_uri=http://localhost:<port>/callback&code_challenge=...S256
    │ ────────────────────────────────► 抓取并校验 CIMD 文档
    │                                    302 → /consent?request_token=...
    │  /consent（需 BS 登录会话）        │
    │ ────────────────────────────────► 200（未登录则 401）
    │  POST /consent action=allow       │
    │ ◄──────────────────────────────── 302 → http://localhost:<port>/callback?code=...
    │  POST /token（public client+PKCE）│
    │ ────────────────────────────────► 200 → JWT access + refresh
    │  POST /mcp（Bearer）              │
    │ ────────────────────────────────► 200（工具可用）
```

要点：

- Claude Code 请求 `scope=read write`（取自 BS PRM 广播的 `scopes_supported`）
- CIMD 文档未声明 `scope` 时，BS 使用 `_ALLOWED_SCOPES`（read write）作为**允许集**校验；**客户端未显式请求 scope 时仍只发 `read`**（安全默认不变）
- 文档中的 `redirect_uris` 是 `http://localhost/callback`（无端口），依赖 BS 的 **loopback 端口灵活匹配**（`http://localhost:3118/callback` 可命中）
- `token_endpoint_auth_method: none` → 公开客户端 + PKCE(S256)，无 client_secret

## 2. 前置条件

| 项 | 要求 |
| --- | --- |
| BS | 运行中且包含 CIMD 支持（`fastmcp>=4.0.3`）与两处修复：① `/mcp` 鉴权中间件迁移；② CIMD scope 允许集 |
| BS 网络 | **BS 进程能访问公网 `https://claude.ai`**（CIMD 抓取）；见 §6 的 fake-ip 排查 |
| `MCP_BASE_URL` | 与客户端实际访问地址一致（反代/端口映射场景尤其注意） |
| 客户端 | Claude Code（实测 2.1.236），已登录 Anthropic 账号 |
| 账号 | BS Web 登录凭据（`auth.enabled=true` 时 consent 需要会话） |

## 3. 接入步骤

```bash
BS=http://localhost:8000   # 换成你的 BS 地址

# 1) 确认 BS 宣告 CIMD 支持
curl -s $BS/.well-known/oauth-authorization-server   # 期望 client_id_metadata_document_supported: true
curl -s $BS/.well-known/oauth-protected-resource/mcp # 期望 scopes_supported: ["read","write"]

# 2) 登记 MCP 服务（--scope user 可全局；默认 local 仅当前项目）
claude mcp add --transport http bangumi-syncer $BS/mcp
claude mcp list          # 期望：bangumi-syncer ... Needs authentication

# 3) 授权（任选其一）
claude                    # 交互：/mcp → 选中 bangumi-syncer → Authenticate → 浏览器完成 BS 登录 + Allow
# 或 CLI：
claude mcp login bangumi-syncer
#   headless 环境：claude mcp login bangumi-syncer --no-browser（需可交互终端，按提示粘贴回调 URL）

# 4) 验证
claude mcp list           # 期望：bangumi-syncer ... ✔ Connected
claude -p "使用 bangumi-syncer MCP 的 get_current_config 工具，只回答 sync.match_confidence_threshold 的值" \
  --allowedTools "mcp__bangumi-syncer__get_current_config"
# 期望：返回当前阈值（如 "0.6"）
```

## 4. 服务端核对清单（预期证据）

| 观察点 | 预期 |
| --- | --- |
| 日志 `CIMD document fetched and validated` | 出现（抓取+校验成功） |
| `GET /authorize?...client_id=https%3A%2F%2Fclaude.ai%2Foauth%2Fclaude-code-client-metadata` | 302 |
| `GET /consent`（无会话 / 有会话） | 401（auth.enabled=true 且未登录）/ 200 |
| `POST /consent` | 302 → 本地回调 |
| `POST /token` | 200 |
| `POST /register` | **不应出现**（CIMD 不走 DCR） |
| `POST /mcp`（Bearer） | 200 |

## 5. 测试用例

### 正向

| # | 用例 | 操作 | 预期 | 自动化对应 |
| --- | --- | --- | --- | --- |
| T1 | CIMD 支持宣告 | 请求 AS metadata | `client_id_metadata_document_supported: true` | `tests/test_mcp_integration.py`（metadata 断言） |
| T2 | 完整接入 | §3 步骤 1–4 | `claude mcp list` 显示 Connected | 本次实测（见 §8） |
| T3 | 工具调用 | `claude -p ... --allowedTools mcp__bangumi-syncer__get_current_config` | 返回配置值 | 本次实测（返回 `"0.6"`） |
| T4 | 无 DCR 注册 | 授权全程观察服务端日志 | 无 `POST /register` | 本次实测 |
| T5 | 文档无 scope 注入允许集 | 单测：合成 client.scope | `"read write"`；`validate_scope("read write")` 通过 | `tests/test_mcp_cimd_real.py` |
| T6 | 安全默认 | 不请求 scope 的授权 | 最终只发 `read` | `tests/test_mcp_auth.py` / cimd 测试 |

### 负向

| # | 用例 | 预期 | 自动化对应 |
| --- | --- | --- | --- |
| N1 | BS 无法抓取 CIMD 文档（断网/DNS 异常） | `/authorize` 400 `Client ID ... not found`，日志 `CIMD fetch failed` | 手工（见 §6）+ `TestCIMDFetchFailure` |
| N2 | 文档 `client_id` 与 URL 不一致 | 拒绝（抓取返回 None） | `TestCIMDClientIdMismatch` |
| N3 | `redirect_uri` 不在文档列表 | `invalid_request`（不重定向） | `TestCIMDRealFetch` / SDK 校验 |
| N4 | 文档非法（404/500/非 JSON/缺字段/非法 auth method） | 抓取失败但不崩溃 | `TestCIMDFetchFailure` |
| N5 | 请求未注册 scope（如 `admin`） | `invalid_scope` 重定向回调 | cimd 测试（`validate_scope("admin")` 抛错） |
| N6 | 未登录访问 `/consent`（auth.enabled=true） | 401 | `tests/test_mcp_auth.py` |
| N7 | 本地回调端口 vs 文档端口 | loopback 任意端口可命中；非 loopback 严格匹配 | `TestCIMDLoopbackPortFlexibility` |

## 6. 故障排查

**症状 A：授权页 400 `Client ID '...' not found`，日志 `CIMD fetch failed ... blocked IP address`**
- 原因：BS 所在网络的 DNS 把 `claude.ai` 解析到 **fake-ip / 保留网段**（如 Clash 的 `198.18.x.x`），被 FastMCP 的 SSRF 防护拒绝
- 处理：让 BS 使用真实 DNS；或为 CIMD 域名配置 hosts/`--add-host`：
  ```bash
  dig +short @1.1.1.1 claude.ai            # 取真实公网 IP（如 160.79.104.10）
  docker run ... --add-host claude.ai:160.79.104.10 ...
  ```
  验证：容器内 `python -c "import socket; print(socket.getaddrinfo('claude.ai',443))"` 返回公网 IP

**症状 B：回调带 `error=invalid_scope`（Client was not registered with scope write）**
- 原因：CIMD 文档无 `scope`，旧实现把允许集注入为 `read`
- 处理：确认 BS 包含 `_ALLOWED_SCOPES`（read write）修复；文档无 scope 的 CIMD 客户端此时可正常请求 `read write`

**症状 C：consent 401 / 浏览器要求登录**
- 先在 BS Web 登录（`auth.enabled=true`），再重新触发授权

**症状 D：`/mcp` 一直 401（token 合法）**
- 确认 BS 包含“生产嵌入路径中间件迁移”修复（`fa231cf`… 以仓库提交为准）；现象为合法 Bearer 仍 `invalid_token`

**症状 E：工具报权限不足**
- 客户端未拿到 `write`：重新授权并确认 consent 页 scope 包含 write；检查 BS 版本是否含症状 B 的修复

## 7. 安全说明

- CIMD 免注册 **不授予权限**：仍需 BS 登录 + consent 页 Allow
- Claude Code 为公开客户端（`token_endpoint_auth_method=none`）+ PKCE(S256)
- BS 需要出网抓取 CIMD 文档；如网络受限，可限制出口白名单或为域名配置 hosts
- 授权结果页偶发 `close?result=error` 与实际成功不一致（观察项，建议核查前端提示）

## 8. 实测证据（2026-09-13/14）

- Claude Code `2.1.236`，命令：`claude mcp add --transport http bangumi-syncer http://localhost:18000/mcp`
- `claude mcp login bangumi-syncer` → 输出 `Authenticated with "bangumi-syncer". Its tools are now available in Claude Code.`
- `claude mcp list` → `bangumi-syncer: http://localhost:18000/mcp (HTTP) - ✔ Connected`
- 工具调用：`claude -p ... get_current_config` → `sync.match_confidence_threshold = "0.6"`
- 服务端日志（节选）：
  ```
  CIMD document fetched and validated: https://claude.ai/oauth/claude-code-client-metadata
  GET /authorize?...client_id=https%3A%2F%2Fclaude.ai%2F... 302 Found
  GET /consent?request_token=... 200 OK
  POST /consent 302 Found
  POST /token 200 OK
  ```
  全程无 `POST /register`
