---
title: 🔌 MCP 接入
order: 40
---

# 🔌 MCP 接入

Bangumi-syncer 提供 **MCP（Model Context Protocol）** 服务，让 AI 助手（Claude Desktop、OpenCode 等）能够读取日志、查看与修改配置，无需打开 Web 管理页。

::: tip 适用场景
- 让 AI 助手帮你查看同步日志、排查问题
- 让 AI 助手帮你修改配置项（如调整同步阈值、开关功能）
- 让 AI 助手演示配置变更效果

所有操作都经过 OAuth 2.1 授权，**无静态 Token**，支持 consent 确认与吊销。
:::

## 架构概述

MCP 采用双服务架构：

```
┌─────────────────┐         OAuth 2.1          ┌─────────────────┐
│   AI 助手        │ ◄──────────────────────────► │   mcp_server     │
│ (Claude Desktop │    Streamable HTTP :3000     │  (Sidecar)       │
│  / OpenCode)    │                              │                  │
└─────────────────┘                              └────────┬─────────┘
                                                          │
                                                          │ HTTP :8000
                                                          │ (Bearer JWT)
                                                          ▼
                                                 ┌─────────────────┐
                                                 │   BS (FastAPI)   │
                                                 │   主程序 :8000    │
                                                 │                  │
                                                 │  /api/mcp/*      │
                                                 │  内部 API        │
                                                 └─────────────────┘
```

| 组件 | 说明 |
| --- | --- |
| **BS（FastAPI）** | 主程序，端口 8000，提供 `/api/mcp/*` 内部 API |
| **mcp_server（Sidecar）** | MCP 服务进程，端口 3000，暴露 3 个工具，OAuth 2.1 授权服务器 |
| **共享卷** | mcp_server 写入 RSA 公钥，BS 读取公钥验签（bind mount 共享同一宿主机目录） |

::: warning 内部 API 不暴露公网
`/api/mcp/*` 是内部 API，仅供 mcp_server 调用。公网访问 BS 时无法直接访问这些端点（需要有效的 JWT），mcp_server 是唯一入口。
:::

## 部署前提

### 1. 运行 mcp_server

mcp_server 是独立 Python 包，可通过以下方式运行：

**方式一：直接运行**

```bash
cd mcp_server
uv sync
python -m mcp_server.server
```

**方式二：Docker 镜像**

```bash
docker build -t bangumi-syncer-mcp -f mcp_server/Dockerfile mcp_server/
```

Dockerfile 已内置默认环境变量：

```dockerfile
ENV BS_BASE_URL=http://bs:8000 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=3000
```

### 2. 共享卷挂载（公钥交换）

mcp_server 启动时生成 RSA 密钥对（RS256），BS 需要读取公钥来验签 JWT。两侧通过 **bind mount** 共享同一宿主机目录，但**仅共享公钥**，私钥留在容器本地目录、永不挂载：

| 组件 | 容器内路径 | 说明 |
| --- | --- | --- |
| mcp_server 写入 | `/app/keys/mcp_public.pem` | 公钥（BS 侧读取，落入共享卷） |
| mcp_server 写入 | `/app/keys-private/mcp_private.pem` | 私钥（**仅 mcp_server 持有，不落共享卷**） |
| BS 读取 | `/mcp_auth/mcp_public.pem` | 公钥验签 JWT（只读挂载共享卷） |

::: warning 私钥隔离
私钥默认写入 `/app/keys-private/mcp_private.pem`（容器本地目录，**不挂载**）。共享卷 `mcp_keys` 仅包含公钥 `mcp_public.pem`，即使卷被泄露也不会导致 Token 被伪造（签名需要私钥）。
:::

**Docker Compose 示例**：

```yaml
services:
  bs:
    image: bangumi-syncer
    volumes:
      - mcp_keys:/mcp_auth:ro          # 只读挂载公钥
    environment:
      - MCP_PUBLIC_KEY_PATH=/mcp_auth/mcp_public.pem

  mcp_server:
    image: bangumi-syncer-mcp
    volumes:
      - mcp_keys:/app/keys             # 读写挂载公钥目录（仅公钥落共享卷）
    environment:
      - MCP_PRIVATE_KEY_PATH=/app/keys-private/mcp_private.pem  # 私钥留在容器本地
      - MCP_PUBLIC_KEY_PATH=/app/keys/mcp_public.pem            # 公钥写入共享卷

volumes:
  mcp_keys:                            # 共享卷，仅包含公钥 mcp_public.pem
```

::: tip 路径映射说明
上例中 `mcp_keys` 卷在两侧分别挂载到不同路径，但**宿主机目录是同一个**。mcp_server 写入 `/app/keys/mcp_public.pem` 后，BS 从 `/mcp_auth/mcp_public.pem` 即可读到（两侧文件名统一为 `mcp_public.pem`）。

私钥通过 `MCP_PRIVATE_KEY_PATH` 指向容器本地路径 `/app/keys-private/mcp_private.pem`，该目录不挂载，私钥永远不会离开 mcp_server 容器。
:::

### 3. 环境变量

**mcp_server 侧**：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BS_BASE_URL` | `http://bs:8000` | BS 主程序的访问地址（容器内网） |
| `BS_PUBLIC_URL` | 未设置 | 浏览器可访问的 BS 地址（用于登录跳转，未配置时 fallback 到 `BS_BASE_URL`） |
| `MCP_PORT` | `3000` | mcp_server 监听端口 |
| `MCP_HOST` | `0.0.0.0` | mcp_server 监听地址 |
| `MCP_AUTH_USERNAME` | `admin` | 保留变量；生产入口下 consent 流程始终探测 BS `/api/auth/status`，`auth.enabled=false` 时 BS 返回内置 admin 会话，故此变量实际不可达 |
| `MCP_PRIVATE_KEY_PATH` | `/app/keys-private/mcp_private.pem` | RSA 私钥路径（容器本地，不落共享卷） |
| `MCP_PUBLIC_KEY_PATH` | `/app/keys/mcp_public.pem` | RSA 公钥路径 |
| `MCP_TOKEN_EXPIRY_SECONDS` | `3600` | JWT 有效期（秒） |
| `MCP_ISSUER` | `http://localhost:3000` | OAuth Issuer URL |
| `MCP_AUDIENCE` | `bs` | JWT audience |

**BS 侧**：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_PUBLIC_KEY_PATH` | `/mcp_auth/mcp_public.pem` | 公钥文件路径，用于验签 JWT |

## OAuth 授权流程

mcp_server 实现了完整的 OAuth 2.1 授权服务器，支持 **consent 确认** 与 **自动续期**。

### auth.enabled=true（推荐）

首次授权流程：

1. AI 助手连接 mcp_server → 触发 OAuth 授权
2. mcp_server 检查 BS 会话（调用 `GET /api/auth/status`）
3. **已登录**：直接显示 consent 页，用户确认后发放 Token
4. **未登录**：跳转到 BS 登录页，用户输入账号密码登录后回到 consent 页
5. 用户点击 **Allow** → 发放 Access Token + Refresh Token

### auth.enabled=false

- consent 流程仍探测 BS `GET /api/auth/status`；BS 关闭认证时 `get_current_user_flexible` 返回内置 admin 会话，**身份沿用 BS 侧返回的会话用户**（`MCP_AUTH_USERNAME` 在生产入口下不可达）
- **consent 确认仍保留**：用户仍需点击 Allow/Deny 授权
- 适合纯内网、无需区分用户身份的场景

### 自动续期

- Access Token 过期后，mcp_server 自动用 Refresh Token 换取新 Token
- Refresh Token 轮换（每次换取后旧 Token 失效）
- **一次授权后无需重复登录**，除非 Token 被吊销或过期时间过长

::: tip 吊销方式
删除 mcp_server 进程内存中的 Refresh Token 即可吊销（重启进程会清空）。重新连接会触发新的授权流程。
:::

## Claude Desktop 接入

在 `claude_desktop_config.json` 中添加 `mcpServers` 配置：

```json
{
  "mcpServers": {
    "bangumi-syncer": {
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

::: tip 远程连接
如果 mcp_server 运行在远程主机上，将 `localhost` 替换为实际主机地址。确保 AI 助手能访问该地址。
:::

配置后重启 Claude Desktop，首次连接会触发 OAuth 授权流程（浏览器弹出 consent 页）。

## OpenCode 接入

在 `opencode.json` 中添加 MCP 配置：

```json
{
  "mcp": {
    "bangumi-syncer": {
      "type": "remote",
      "url": "http://localhost:3000/mcp",
      "oauth": true
    }
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `type` | `"remote"` 表示远程 MCP 服务 |
| `url` | mcp_server 的 Streamable HTTP 端点 |
| `oauth` | `true` 表示启用 OAuth 授权 |

## 工具说明

mcp_server 提供 3 个工具，AI 助手通过它们与 BS 交互：

### get_logs — 读取日志

获取 BS 运行日志，支持多维度过滤。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | string | 否 | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`（`WARN` 等价于 `WARNING`） |
| `search` | string | 否 | 关键词搜索（匹配日志内容） |
| `limit` | integer | 否 | 返回条数，默认 50，最大 10000 |
| `since` | string | 否 | 起始时间（ISO 格式，如 `2026-09-01T10:00:00`）。带时区输入按服务器本地时间对齐（不换算，直接丢弃时区偏移） |
| `until` | string | 否 | 结束时间（ISO 格式）。带时区输入按服务器本地时间对齐（不换算，直接丢弃时区偏移） |

**内部端点**：`GET /api/mcp/logs`

### get_current_config — 查看配置

获取 BS 全量配置，敏感字段（如密码、Token）已自动脱敏（显示为 `***`）。

**无参数**。

**内部端点**：`GET /api/mcp/config`

::: tip 查看 schema
BS 额外提供 `GET /api/mcp/config/schema` 端点，可获取配置段的元数据（字段类型、可选值等），便于 AI 助手理解配置结构。
:::

### update_config — 修改配置

修改 BS 的某项配置并**直接生效**（无需重启）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `section` | string | 是 | 配置段名（如 `sync`、`auth`、`dev`） |
| `key` | string | 是 | 配置键名（如 `log_level`、`enabled`） |
| `value` | string | 是 | 配置值（字符串形式） |

**内部端点**：`POST /api/mcp/config/update`

::: warning 配置回滚
`update_config` 直接生效，不会自动创建备份。如需回滚：
1. 在 Web 管理页的「配置管理」中找到对应项手动改回
2. 或使用 BS 的配置备份页（如有）手动恢复
:::

## 安全说明

| 方面 | 说明 |
| --- | --- |
| **无静态 Token** | 不使用固定 API Key，JWT 短期有效（默认 1 小时），Refresh Token 可吊销 |
| **私钥隔离** | RSA 私钥仅存在于 mcp_server 进程内存与存储目录，BS 只持有公钥 |
| **公钥非机密** | 公钥用于验签 JWT，泄露不会导致 Token 被伪造（签名需要私钥） |
| **内部 API 不暴露公网** | `/api/mcp/*` 需要有效 JWT 才能访问，mcp_server 是唯一入口 |
| **Scope 分离** | Token 带 `read` / `write`  scope，读操作不要求 write 权限 |
| **Consent 确认** | 每次新客户端授权都需用户点击 Allow，防止未授权访问 |

::: tip 部署建议
- 将 mcp_server 与 BS 部署在同一内网或 Docker 网络中
- 避免将 mcp_server 端口（3000）直接暴露到公网
- 如需公网访问，建议通过 VPN 或反向代理 + TLS 保护
:::

## Agent 配置 Prompt

可直接粘贴给 AI 助手，指导它完成 MCP 接入配置：

```
你是 bangumi-syncer 的配置助手。请帮我完成 MCP 接入配置：
1. 阅读 docs/config/mcp.md，确认 BS 与 mcp_server 双服务已运行、公钥已分发。
2. 将 mcp_server 注册到 opencode.json（type: remote + oauth）或 Claude Desktop。
3. 确认 auth.enabled 状态：开启则 OAuth 需登录，关闭则跳过登录但仍需 consent 授权。
4. 验证 get_logs / get_current_config 可用，并演示 update_config。
请只执行只读与配置类操作，不要改动同步数据。
```

## 接下来

- 配置完成后，可通过 AI 助手查看 [同步记录](/usage/) 或调整 [配置项](/config/configuration)
- 遇到连接问题？检查 [故障排除](/troubleshooting)
