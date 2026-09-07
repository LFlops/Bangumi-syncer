---
title: 🔌 MCP Server 子项目
order: 13
---

# 🔌 MCP Server 子项目

MCP Server 是 Bangumi-syncer 的**伴生服务**，通过 MCP 协议（Model Context Protocol）让 LLM 客户端读写 BS 配置、查询日志。它是 uv workspace 成员，独立 `pyproject.toml`，独立 `uv.lock`。

## 技术栈

- **Python**：`>=3.10`（独立于 BS 主项目的 `>=3.9`）
- **MCP SDK**：`mcp>=1.27`（Streamable HTTP 传输）
- **HTTP 客户端**：`httpx>=0.25`（调用 BS 内部 API）
- **认证**：`cryptography`（RSA 密钥对）+ `PyJWT`（RS256 JWT 签发）
- **测试**：`pytest` + `pytest-asyncio` + `respx`

---

## 项目结构

```
mcp_server/
├── pyproject.toml          # 独立项目配置（requires-python >=3.10）
├── uv.lock                 # 独立锁文件
├── Dockerfile              # 多阶段构建（python:3.11-slim-bookworm）
├── src/
│   └── mcp_server/
│       ├── __init__.py     # 版本号
│       ├── server.py       # MCP 服务入口 + 工具注册
│       ├── auth.py         # OAuth AS（authorize/token/register）+ RSA 密钥管理
│       ├── client.py       # BSClient：调用 /api/mcp/*
│       └── tools.py        # 工具实现（get_logs / get_current_config / update_config）
└── tests/
    ├── test_mcp_server.py  # 服务端工具注册测试
    ├── test_mcp_client.py  # BSClient HTTP 调用测试
    └── test_mcp_oauth.py   # OAuth 流程测试
```

### uv Workspace 关系

根目录 `pyproject.toml` 声明：

```toml
[tool.uv.workspace]
members = ["mcp_server"]
```

BS 主项目（`requires-python = ">=3.9"`）与 mcp_server（`requires-python = ">=3.10"`）共享同一个 uv 虚拟环境时，uv 会按各自 `pyproject.toml` 解析依赖。`uv sync --group dev` 在根目录执行会同时安装两边依赖。

---

## 架构概览

```
┌─────────────────┐     MCP (Streamable HTTP)     ┌─────────────────┐
│   LLM Client    │ ◄────────────────────────────► │   MCP Server    │
│  (Claude/Cursor)│    OAuth 2.1 + RS256 JWT      │  (port 3000)    │
└─────────────────┘                                └────────┬────────┘
                                                             │
                                                    Bearer JWT
                                                    (RS256 透传)
                                                             │
                                                             ▼
                                                    ┌─────────────────┐
                                                    │  Bangumi-syncer │
                                                    │   (BS, port 8000)│
                                                    │  /api/mcp/*     │
                                                    └─────────────────┘
```

两侧职责：

| 侧 | 路径 | 职责 |
| --- | --- | --- |
| **BS 侧** | `app/api/mcp_logs.py`、`app/api/mcp_config.py` | 内部 API 实现（日志读取、配置读写） |
| **BS 侧** | `app/core/mcp_auth.py` | RS256 JWT 验签（公钥验签） |
| **BS 侧** | `app/api/deps.py` → `get_mcp_client()` | FastAPI 依赖注入（JWT 验签 + scope 校验） |
| **mcp_server 侧** | `src/mcp_server/server.py` | MCP 服务入口 + 工具注册 |
| **mcp_server 侧** | `src/mcp_server/auth.py` | OAuth AS（authorize/token/register）+ RSA 密钥管理 |
| **mcp_server 侧** | `src/mcp_server/client.py` | `BSClient`：调用 BS 内部 API |
| **mcp_server 侧** | `src/mcp_server/tools.py` | 工具实现（委托给 BSClient） |

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
    "iss": "http://localhost:3000",
    "aud": "bs",
    "iat": 1700000000,
    "exp": 1700003600,
    "jti": "...",  # 唯一标识
}
```

### 2. auth.enabled 分流

认证分流由 **BS 侧** `auth.enabled` 配置决定，mcp_server 在运行时通过调用 `BS_BASE_URL/api/auth/status` 自动探测：

| 模式 | 行为 |
| --- | --- |
| BS `auth.enabled=true` | 登录时调用 `BS_BASE_URL/api/auth/status` 复用 BS 会话，身份为 BS 当前用户 |
| BS `auth.enabled=false` | consent 流程仍探测 `/api/auth/status`，BS 关闭认证时返回内置 admin 会话，身份沿用 BS 侧会话用户（`MCP_AUTH_USERNAME` 在生产入口下不可达） |

### 3. JWT 验签（BS 侧）

`app/core/mcp_auth.py` 的 `verify_jwt()` 用公钥验证 RS256 签名：

1. 解析 JWT 结构（header.payload.signature）
2. 检查 `exp`（过期时间）
3. 检查 `aud`（必须为 `"bs"`）
4. 公钥验签（PKCS1v15 + SHA256）

公钥路径：`/mcp_auth/mcp_public.pem`（可通过 `MCP_PUBLIC_KEY_PATH` 配置）

### 4. 依赖注入

`app/api/deps.py` 的 `get_mcp_client(require_scope="")` 返回 FastAPI 依赖函数：

```python
# 只读
@router.get("/api/mcp/logs")
async def get_logs(user=Depends(get_mcp_client())): ...


# 需要 write scope
@router.post("/api/mcp/config/update")
async def update_config(
    payload, user=Depends(get_mcp_client(require_scope="write"))
): ...
```

返回值：`{"username": "admin", "scope": ["read", "write"], "mcp": True}`

---

## 密钥管理

### RSA 密钥对生成

`RSAKeyManager` 在 mcp_server 启动时调用 `load_or_generate()`：

- 若磁盘已有密钥对 → 加载
- 若不存在 → 生成 2048 位 RSA 密钥对并写入磁盘

### 密钥路径

| 密钥 | 路径（默认） | 说明 |
| --- | --- | --- |
| 私钥 | `/app/keys-private/mcp_private.pem` | **仅 mcp_server 持有**，容器本地目录，不落共享卷 |
| 公钥 | `/app/keys/mcp_public.pem` | 写入共享卷供 BS 读取 |

### 共享卷部署

Docker 部署时，公钥通过共享卷传递给 BS（**仅公钥**，私钥留在容器本地）：

```
mcp_server → /app/keys/mcp_public.pem       (公钥，落入共享卷)
                    │
                    ▼ (volume mount)
BS → /mcp_auth/mcp_public.pem               (只读挂载，验签 JWT)

mcp_server → /app/keys-private/mcp_private.pem  (私钥，容器本地，不挂载)
```

私钥**永远不离开** mcp_server 容器。

---

## 内部 API 契约

mcp_server 通过 `BSClient` 调用 BS 内部 API，所有响应包络格式：

```json
{"status": "success", "data": {...}}
```

### GET /api/mcp/logs

获取日志内容。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `level` | string? | 日志级别：DEBUG / INFO / WARNING / ERROR（WARN 自动映射为 WARNING） |
| `search` | string? | 关键字搜索 |
| `limit` | int? | 返回行数（1-10000，默认 50） |
| `since` | string? | 起始时间（ISO 格式） |
| `until` | string? | 结束时间（ISO 格式） |

### GET /api/mcp/config

获取全量配置（敏感字段已脱敏为 `***`）。

### GET /api/mcp/config/schema

获取配置段元数据（`SectionMeta` 序列化）。

### POST /api/mcp/config/update

更新配置（需要 `write` scope）。

请求体：

```json
{
  "section_name": {
    "key": "value"
  }
}
```

段名下划线自动归一化为连字符（`notify_webhook` → `notify-webhook`）。空值 / 掩码值 `***` 跳过，避免误覆盖。

---

## 测试方式

### BS 侧测试

BS 侧的 MCP 相关测试（`tests/api/` 下）使用标准模式：

- `httpx.AsyncClient` + `ASGITransport` 直接调用 FastAPI app
- `dependency_overrides` 覆盖 `get_mcp_client` 依赖，跳过真实 JWT 验签

```python
app.dependency_overrides[get_mcp_client()] = lambda: {
    "username": "test",
    "scope": ["read", "write"],
    "mcp": True,
}
```

### mcp_server 侧测试

mcp_server 独立测试（`mcp_server/tests/`）：

- **HTTP mock**：`respx` mock `httpx` 请求，模拟 BS 响应
- **环境变量**：`BS_BASE_URL` 控制 `BSClient` 行为（JWT 由 mcp_server OAuth AS 签发并透传）
- **OAuth 测试**：`create_auth_server()` 工厂创建带认证的 Starlette app，直接测试 authorize/token/consent 流程

```python
# 示例：mock BS 响应
mock_client = respx.post("http://bs:8000/api/mcp/logs").mock(
    return_value=httpx.Response(200, json={"status": "success", "data": {...}})
)
```

---

## 本地开发

### 安装依赖

```bash
# 根目录（同时安装 BS + mcp_server）
uv sync --group dev

# 仅 mcp_server
cd mcp_server && uv sync --group dev
```

### 运行 mcp_server

```bash
# 开发用（BS 侧 auth.enabled 决定认证行为）
MCP_PRIVATE_KEY_PATH=/app/keys-private/mcp_private.pem \
MCP_PUBLIC_KEY_PATH=/app/keys/mcp_public.pem \
python -m mcp_server.server
```

默认监听 `0.0.0.0:3000`，可通过 `MCP_HOST` / `MCP_PORT` 调整。

### 运行测试

```bash
# mcp_server 独立测试（推荐：进入子项目目录后执行）
cd mcp_server && uv run python -m pytest tests/

# 或从根目录（显式指定 python -m，避免 spawn 失败）
uv run --directory mcp_server python -m pytest tests/
```

### 环境限制

- **Python 版本**：mcp_server 要求 `>=3.10`，CI 使用 3.11
- **UV_PYTHON**：若系统 Python < 3.10，需通过 `uv python install 3.11` 或设置 `UV_PYTHON` 环境变量指定解释器
- **工作目录**：mcp_server 的 `pytest.pythonpath = ["src"]`，测试需从 `mcp_server/` 目录运行

---

## Docker 构建

```bash
docker build -f mcp_server/Dockerfile -t bangumi-syncer-mcp:latest .
```

多阶段构建：
1. **Builder**：`uv sync --frozen --no-dev` 安装依赖
2. **Runtime**：拷贝依赖 + 业务代码，`gosu` 切非 root 用户启动

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BS_BASE_URL` | `http://bs:8000` | BS 服务地址（容器内网） |
| `BS_PUBLIC_URL` | 未设置 | 浏览器可访问的 BS 地址（登录跳转用，未配置时 fallback 到 `BS_BASE_URL`） |
| `MCP_HOST` | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | `3000` | 监听端口 |
| `MCP_AUTH_USERNAME` | `admin` | 保留变量；生产入口下 consent 流程始终探测 BS `/api/auth/status`，`auth.enabled=false` 时 BS 返回内置 admin 会话，故此变量实际不可达 |
| `MCP_PRIVATE_KEY_PATH` | `/app/keys-private/mcp_private.pem` | RSA 私钥路径（容器本地，不落共享卷） |
| `MCP_PUBLIC_KEY_PATH` | `/app/keys/mcp_public.pem` | RSA 公钥路径 |
| `MCP_TOKEN_EXPIRY_SECONDS` | `3600` | JWT 有效期 |
| `MCP_ISSUER` | `http://localhost:3000` | OAuth issuer |
| `MCP_AUDIENCE` | `bs` | JWT audience |
