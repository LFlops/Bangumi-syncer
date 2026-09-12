---
title: 🔌 MCP Server（内置模块）
order: 13
---

# 🔌 MCP Server（内置模块）

MCP Server 是 Bangumi-syncer 的**内置模块**，通过 MCP 协议（Model Context Protocol）让 LLM 客户端读写 BS 配置、查询日志。它随 BS 单进程启动，**不是独立的 workspace 成员**，没有独立 `pyproject.toml` / `uv.lock`；依赖（如 `fastmcp`）统一声明在根 `pyproject.toml`。

## 技术栈

- **Python**：`>=3.10`（与 BS 主项目一致，全仓已升级至 3.10）
- **MCP SDK**：[`fastmcp>=4.0.3`](https://github.com/jlowin/fastmcp)（Streamable HTTP 传输，见根 `pyproject.toml`）
- **认证**：`cryptography`（RSA 密钥对）+ `PyJWT`（RS256 JWT 签发）
- **测试**：`pytest` + `pytest-asyncio`（嵌入路由用 `httpx.AsyncClient` + `ASGITransport` 验证）

---

## 项目结构

```
app/mcp/
├── __init__.py
├── server.py               # FastMCP 服务工厂 + 工具注册
├── provider.py             # OAuth AS（authorize/token/register）+ RSA 密钥管理 + CIMD
└── tools.py                # 工具实现（get_logs / get_current_config / update_config）

tests/
├── test_mcp_tools.py       # 工具函数测试
├── test_mcp_auth.py        # OAuth 流程测试
├── test_mcp_cimd_real.py   # CIMD（Client ID Metadata Document）流程测试
├── test_mcp_embed.py       # FastMCP 嵌入 FastAPI 测试
├── test_mcp_integration.py # 端到端集成测试
└── test_main_mcp.py        # main.py MCP 集成测试
```

---

## 架构概览

```
┌─────────────────┐     MCP (Streamable HTTP)     ┌─────────────────────────────┐
│   LLM Client    │ ◄────────────────────────────► │  Bangumi-syncer (BS)        │
│  (Claude/Cursor)│    OAuth 2.1 + RS256 JWT      │  (port 8000)                │
└─────────────────┘                                │                             │
                                                   │  内置 FastMCP 服务           │
                                                   │  - app/mcp/server.py        │
                                                   │  - app/mcp/provider.py      │
                                                   │  - app/mcp/tools.py         │
                                                   │  工具直接调用同进程业务层      │
                                                   └─────────────────────────────┘
```

职责分布：

| 路径 | 职责 |
| --- | --- |
| `app/mcp/server.py` | FastMCP 服务工厂 + 工具注册 |
| `app/mcp/provider.py` | OAuth AS（authorize/token/register）+ RSA 密钥管理 + CIMD |
| `app/mcp/tools.py` | 工具实现（get_logs / get_current_config / update_config） |
| `app/main.py` | FastAPI 应用入口，嵌入 FastMCP 路由 + combine_lifespans |

::: warning 状态均为进程内存储，当前仅支持单进程
`BangumiOAuthProvider`（`app/mcp/provider.py`）的客户端注册表、授权码、Refresh Token、待授权请求与吊销记录全部保存在**进程内存**中。仓库启动方式（`start.bat`、Dockerfile `CMD`）均为 `uvicorn app.main:app` 单进程，**不支持 `--workers N` / gunicorn 多进程**：否则授权码 / Refresh Token 会因请求落到不同进程而失效，吊销状态也会各进程不一致。Access Token 的 JWT 验签本身无状态（RS256），不受进程数影响。
:::

各状态表的 TTL 与清理策略（均为进程内存态，由 `_cleanup_expired_state()` **惰性清理**，调用时机为 `authorize` / `get_consent_context` / `exchange_authorization_code` / `exchange_refresh_token` / `revoke_token`）：

| 状态表 | 键 | TTL | 说明 |
| --- | --- | --- | --- |
| `_pending_auths` | `request_token` | 10 分钟（`PENDING_AUTH_TTL`） | 待 consent 的授权请求；过期即删除 |
| `_auth_codes` | 授权码 | 5 分钟（`AUTH_CODE_TTL`） | 已兑换/过期的授权码被清除；SDK 兑换时另行校验过期 |
| `_refresh_tokens` | Refresh Token | 30 天（`REFRESH_TOKEN_TTL`，环境变量 `MCP_REFRESH_TOKEN_TTL` 可配） | 每次轮换**重新计时**（滑动窗口）；`expires_at=None` 视为不过期 |
| `_revoked_tokens` | `jti` → access token `exp` | 随 access token 到期 | 吊销记录在对应 access token 过期后清理，避免只增不减 |

---

## 认证链路

### 1. OAuth Authorization Server（BS 侧）

`app/mcp/provider.py` 的 `BangumiOAuthProvider`（继承 FastMCP 4 的 `OAuthProvider`）实现 OAuth 授权服务：

| 端点 | 功能 |
| --- | --- |
| `/authorize` | 发起授权请求，重定向到 `/consent` |
| `/token` | 用 authorization code 换 JWT access_token |
| `/register` | 动态客户端注册（RFC 7591） |
| `/revoke` | 吊销 access / refresh token（`RevocationOptions(enabled=True)`） |
| `/consent` | 用户确认页面（allow/deny） |

JWT claims：

```python
{
    "sub": "admin",  # 用户名
    "scope": "read write",  # 权限范围
    "iss": "http://localhost:8000",  # = 解析后的 base_url（不可单独配置）
    "aud": "bangumi-syncer",  # 硬编码
    "iat": 1700000000,
    "exp": 1700003600,  # 硬编码 3600 秒
    "jti": "...",  # 唯一标识（sign_jwt 自动添加）
}
```

::: tip 默认 scope 为 read
上面的 `"scope": "read write"` 仅为示例。实际实现中 `_default_scopes = ["read"]`（`provider.py`）：客户端**不请求 scope** 时只发放 `read`（authorize 的 `params.scopes or self._default_scopes`、DCR 注册时未声明 scope 也回填 `read`、CIMD 的 `default_scope` 同为 `read`）。`write` 需客户端在授权请求中**显式请求**，且 SDK（`OAuthClientInformationFull.validate_scope`）会校验请求的 scope 属于客户端已注册/声明的 scope，否则返回 `invalid_scope`。

权限对照：`read` → `get_logs` / `get_current_config`（敏感字段已掩码）；`write` → `update_config`（`auth` 段始终禁写）。
:::

### 2. auth.enabled 分流

认证分流由 **BS 侧** `auth.enabled` 配置决定，`handle_consent` 在**同进程内**通过 `security_manager.validate_session()` 校验 BS 会话，不走 HTTP：

| 模式 | 行为 |
| --- | --- |
| BS `auth.enabled=true` | consent 时读取请求 Cookie 中的 `session_token`，调用 `security_manager.validate_session()` 复用 BS 会话，身份为 BS 当前用户 |
| BS `auth.enabled=false` | 不校验会话，直接使用 `auth_username`（来自 BS 认证配置），无需登录 |

### 3. JWT 验签（FastMCP provider 侧）

`app/mcp/provider.py` 的 `RSAKeyManager.verify_jwt()` 用公钥验证 RS256 签名：

1. 检查 `exp`（过期时间）
2. 检查 `aud`（必须为 `"bangumi-syncer"`）
3. 公钥验签（RS256）

公钥由 `RSAKeyManager` 本地生成/加载，路径可通过 `MCP_RSA_PUBLIC_KEY` 环境变量配置。

### 4. consent 未登录行为

`auth.enabled=true` 且请求未携带有效 `session_token` 时，`handle_consent`（GET 与 POST allow）**直接返回 HTTP 401**，并不会跳转到 BS 登录页。用户需先在 BS Web 端登录，再重新触发授权。

### 5. 动态客户端注册（DCR）

`BangumiOAuthProvider` 默认传入 `ClientRegistrationOptions(enabled=True, valid_scopes=["read", "write"])`（`provider.py`），因此 `/register`（RFC 7591）默认开启且未认证可达（路由在 `app/main.py` 中平铺注册）。

- **注册 ≠ 授权**：注册只登记客户端元数据，不授予任何数据权限；仍需 BS 登录会话（`auth.enabled=true` 时）+ consent 页点 Allow 才能拿到 Token
- **存储与上限**：已注册客户端存于进程内存 `_clients`，上限 `MAX_CLIENTS=1000`，达到上限后注册抛 `RegistrationError`
- **兜底**：重启进程即清空 `_clients`（及 `_auth_codes` / `_refresh_tokens` / `_pending_auths` / `_revoked_tokens`）；RSA 密钥若已持久化则保留。已签发 Access Token 在 1 小时有效期内仍有效（JWT 自包含验签，不查注册表）
- **关闭方式**：当前无配置开关，需在装配代码（`_create_provider` / `create_auth_server`）传入 `ClientRegistrationOptions(enabled=False)`；关闭后仅 CIMD 客户端可授权（`get_client` / `authorize` 对 CIMD client_id 走独立分支，不依赖 `_clients`）

---

## 密钥管理

### RSA 密钥对生成

`RSAKeyManager` 在 BS 启动时调用 `load_or_generate()`：

- 若磁盘已有密钥对 → 加载
- 若不存在 → 生成 2048 位 RSA 密钥对并写入磁盘

### 密钥路径

| 密钥 | 路径（默认） | 说明 |
| --- | --- | --- |
| 私钥 | `<系统临时目录>/mcp_private.pem` | **仅 BS 持有**，本地磁盘 |
| 公钥 | `<系统临时目录>/mcp_public.pem` | 本地生成，用于验签 JWT |

默认路径由 `tempfile.gettempdir()` 解析（`app/mcp/server.py`），因此会随操作系统不同而变化（Linux 通常为 `/tmp`，macOS 为 `$TMPDIR` 指向的目录）。路径可通过环境变量 `MCP_RSA_PRIVATE_KEY` / `MCP_RSA_PUBLIC_KEY` 配置。

::: warning 默认路径不持久
默认路径位于系统临时目录，容器重建或临时目录被清理会导致密钥丢失：已签发的 Access Token（有效期 1 小时）将验签失败，客户端需重新授权；多实例部署也需要各实例共享同一密钥。生产部署应将 `MCP_RSA_PRIVATE_KEY` / `MCP_RSA_PUBLIC_KEY` 指向持久卷/数据目录（如 `/app/data/mcp_private.pem`，镜像内已预建 `/app/data`）。
:::

---

## 工具实现

工具函数位于 `app/mcp/tools.py`，直接调用 BS 同进程业务层（不经 HTTP）。所有响应包络格式：

```json
{"status": "success", "data": {...}}
```

### get_logs

获取日志内容。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `level` | string? | 日志级别：DEBUG / INFO / WARNING / ERROR（WARN 自动映射为 WARNING） |
| `search` | string? | 关键字搜索 |
| `limit` | int? | 返回行数（1-10000，默认 50） |
| `since` | string? | 起始时间（ISO 格式） |
| `until` | string? | 结束时间（ISO 格式） |

### get_current_config

获取全量配置（敏感字段已脱敏为 `***`）。无参数。

### update_config

修改配置（需要 `write` scope）。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `section` | string | 配置段名（支持下划线，自动归一化为连字符） |
| `key` | string | 配置键名 |
| `value` | any | 配置值 |

段名下划线自动归一化为连字符（`notify_webhook` → `notify-webhook`）。`auth` 段禁止通过 MCP 修改。

---

## 测试方式

### 工具函数测试

`tests/test_mcp_tools.py` 直接测试工具函数（不经 MCP server），用 mock/patch 隔离 config 写入。

### OAuth 测试

`tests/test_mcp_auth.py` 使用 `create_auth_server()` 工厂创建带认证的 Starlette app，直接测试 authorize/token/consent 流程。

### 嵌入测试

`tests/test_mcp_embed.py` 验证 FastMCP 路由正确嵌入 FastAPI app（`httpx.AsyncClient` + `ASGITransport`）。

### 集成测试

`tests/test_mcp_integration.py` 端到端验证：
- `list_tools` → 3 工具且 schema 正确
- 未认证调工具 → 401
- 走完授权流程 → token → 调工具成功
- `/.well-known/oauth-authorization-server` 含 `client_id_metadata_document_supported: true`

---

## 本地开发

### 安装依赖

```bash
# 根目录
uv sync --group dev
```

### 运行 BS（含内置 MCP）

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
# 全部测试
uv run pytest tests/

# 仅 MCP 相关
uv run pytest tests/test_mcp_*.py -v
```

### 环境限制

- **Python 版本**：`>=3.10`，CI 使用 3.10
- **UV_PYTHON**：若系统 Python < 3.10，需通过 `uv python install 3.10` 或设置 `UV_PYTHON` 环境变量指定解释器

---

## Docker 构建

```bash
docker build -t bangumi-syncer:latest .
```

BS 单容器部署，内置 MCP 服务。

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_RSA_PRIVATE_KEY` | `<系统临时目录>/mcp_private.pem` | RSA 私钥路径（本地磁盘） |
| `MCP_RSA_PUBLIC_KEY` | `<系统临时目录>/mcp_public.pem` | RSA 公钥路径 |
| `MCP_BASE_URL` | `http://localhost:8000` | 服务公共 URL，用作 OAuth issuer / metadata 端点；解析优先级：`create_mcp_server(base_url=...)` 参数 > `MCP_BASE_URL` > `dev.mcp_base_url` 配置 > 默认值 |
| `MCP_REFRESH_TOKEN_TTL` | `2592000`（30 天） | Refresh Token 有效期，单位：秒；每次轮换后重新计时（滑动窗口） |

::: warning 不可配置项
以下值当前在 `app/mcp/server.py` 中硬编码，**没有对应环境变量**：

- OAuth `issuer` = 解析后的 `base_url`
- JWT `audience` = `"bangumi-syncer"`
- Access Token 有效期 = `3600` 秒
:::
