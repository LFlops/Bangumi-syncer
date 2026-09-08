---
title: 🔌 MCP Server 子项目
order: 13
---

# 🔌 MCP Server 子项目

MCP Server 是 Bangumi-syncer 的**伴生服务**，通过 MCP 协议（Model Context Protocol）让 LLM 客户端读写 BS 配置、查询日志。它是 uv workspace 成员，独立 `pyproject.toml`，独立 `uv.lock`。

## 技术栈

- **Python**：`>=3.10`（与 BS 主项目一致，全仓已升级至 3.10）
- **MCP SDK**：`mcp>=1.27`（Streamable HTTP 传输）
- **HTTP 客户端**：`httpx>=0.25`（调用 BS 内部 API）
- **认证**：`cryptography`（RSA 密钥对）+ `PyJWT`（RS256 JWT 签发）
- **测试**：`pytest` + `pytest-asyncio` + `respx`

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

---

## 认证链路

### 1. OAuth Authorization Server（mcp_server 侧）

`auth.py` 实现 MCP SDK 的 `OAuthAuthorizationServerProvider` 接口：

| 端点 | 功能 |
| --- | --- |
| `/authorize` | 发起授权请求，重定向到 `/consent` |
| `/token` | 用 authorization code 换 JWT access_token |
| `/register` | 动态客户端注册（RFC 7591） |
| `/consent` | 用户确认页面（allow/deny） |

JWT claims：

```python
{
    "sub": "admin",  # 用户名
    "scope": "read write",  # 权限范围
    "iss": "http://localhost:8000",
    "aud": "bangumi-syncer",
    "iat": 1700000000,
    "exp": 1700003600,
    "jti": "...",  # 唯一标识（sign_jwt 自动添加）
}
```

### 2. auth.enabled 分流

认证分流由 **BS 侧** `auth.enabled` 配置决定，mcp_server 在运行时通过调用 `BS_BASE_URL/api/auth/status` 自动探测：

| 模式 | 行为 |
| --- | --- |
| BS `auth.enabled=true` | 登录时调用 `BS_BASE_URL/api/auth/status` 复用 BS 会话，身份为 BS 当前用户 |
| BS `auth.enabled=false` | consent 流程仍探测 `/api/auth/status`，BS 关闭认证时返回内置 admin 会话，身份沿用 BS 侧会话用户（`MCP_AUTH_USERNAME` 在生产入口下不可达） |

### 3. JWT 验签（FastMCP provider 侧）

`app/mcp/provider.py` 的 `RSAKeyManager.verify_jwt()` 用公钥验证 RS256 签名：

1. 检查 `exp`（过期时间）
2. 检查 `aud`（必须为 `"bangumi-syncer"`）
3. 公钥验签（RS256）

公钥由 `RSAKeyManager` 本地生成/加载，路径可通过 `MCP_RSA_PUBLIC_KEY` 环境变量配置。

---

## 密钥管理

### RSA 密钥对生成

`RSAKeyManager` 在 BS 启动时调用 `load_or_generate()`：

- 若磁盘已有密钥对 → 加载
- 若不存在 → 生成 2048 位 RSA 密钥对并写入磁盘

### 密钥路径

| 密钥 | 路径（默认） | 说明 |
| --- | --- | --- |
| 私钥 | `/tmp/mcp_private.pem` | **仅 BS 持有**，本地磁盘 |
| 公钥 | `/tmp/mcp_public.pem` | 本地生成，用于验签 JWT |

路径可通过环境变量 `MCP_RSA_PRIVATE_KEY` / `MCP_RSA_PUBLIC_KEY` 配置。

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

- **Python 版本**：`>=3.10`，CI 使用 3.11
- **UV_PYTHON**：若系统 Python < 3.10，需通过 `uv python install 3.11` 或设置 `UV_PYTHON` 环境变量指定解释器

---

## Docker 构建

```bash
docker build -t bangumi-syncer:latest .
```

BS 单容器部署，内置 MCP 服务。

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_RSA_PRIVATE_KEY` | `/tmp/mcp_private.pem` | RSA 私钥路径（本地磁盘） |
| `MCP_RSA_PUBLIC_KEY` | `/tmp/mcp_public.pem` | RSA 公钥路径 |
| `MCP_AUTH_USERNAME` | `admin` | 保留变量；生产入口下 consent 流程始终探测 BS `/api/auth/status`，`auth.enabled=false` 时 BS 返回内置 admin 会话，故此变量实际不可达 |
| `MCP_TOKEN_EXPIRY_SECONDS` | `3600` | JWT 有效期 |
| `MCP_ISSUER` | `http://localhost:8000` | OAuth issuer |
| `MCP_AUDIENCE` | `bangumi-syncer` | JWT audience |
