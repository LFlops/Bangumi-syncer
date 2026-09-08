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

MCP 采用**嵌入式架构**（FastMCP 4 直接嵌入 BS 进程）：

```
┌─────────────────┐         OAuth 2.1          ┌─────────────────────────────┐
│   AI 助手        │ ◄──────────────────────────► │   BS (FastAPI) :8000        │
│ (Claude Desktop │    Streamable HTTP           │                             │
│  / OpenCode)    │    /mcp（工具端点）            │  内置 FastMCP 服务           │
│                 │                              │  - 3 个工具（get_logs/       │
│                 │                              │    get_current_config/       │
│                 │                              │    update_config）           │
│                 │                              │  - OAuth 2.1 授权服务器       │
│                 │                              │  - RSA 密钥本地生成           │
└─────────────────┘                              └─────────────────────────────┘
```

| 组件 | 说明 |
| --- | --- | --- | --- |
| **BS（FastAPI）** | 主程序，端口 8000，内置 FastMCP 服务（工具 + OAuth AS） |
| **RSA 密钥** | 本地生成（RS256），私钥仅存于 BS 进程内存与本地磁盘，公钥用于验签 JWT |

::: tip 嵌入式优势
FastMCP 直接嵌入 BS 进程，工具函数调用同进程业务层（无需 HTTP），部署更简单（单进程、单端口），无需 Sidecar 与共享卷。
:::

::: warning 内部 API 不暴露公网
`/api/mcp/*` 是内部 API，工具函数直接调用同进程业务层。公网访问 BS 时需要有效的 JWT 才能调用 MCP 工具。
:::

## 部署前提

### 1. 运行 BS（含内置 MCP）

MCP 服务已嵌入 BS 进程，启动 BS 即可使用：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Docker 部署同理，单容器即可。

### 2. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_RSA_PRIVATE_KEY` | `/tmp/mcp_private.pem` | RSA 私钥路径（本地磁盘，仅 BS 持有） |
| `MCP_RSA_PUBLIC_KEY` | `/tmp/mcp_public.pem` | RSA 公钥路径（本地生成，用于验签 JWT） |
| `MCP_TOKEN_EXPIRY_SECONDS` | `3600` | JWT 有效期（秒） |
| `MCP_AUTH_USERNAME` | `admin` | 保留变量；生产入口下 consent 流程始终探测 BS `/api/auth/status`，`auth.enabled=false` 时 BS 返回内置 admin 会话，故此变量实际不可达 |
| `MCP_ISSUER` | `http://localhost:8000` | OAuth Issuer URL（同 BS base_url） |
| `MCP_AUDIENCE` | `bangumi-syncer` | JWT audience |

::: tip RSA 密钥
BS 启动时自动生成 RSA 密钥对（RS256），私钥仅存于本地磁盘（`MCP_RSA_PRIVATE_KEY`），公钥用于验签 JWT。无需共享卷或 Sidecar。
:::

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
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

::: tip 远程连接
如果 BS 运行在远程主机上，将 `localhost` 替换为实际主机地址。确保 AI 助手能访问该地址。
:::

配置后重启 Claude Desktop，首次连接会触发 OAuth 授权流程（浏览器弹出 consent 页）。

## OpenCode 接入

在 `opencode.json` 中添加 MCP 配置：

```json
{
  "mcp": {
    "bangumi-syncer": {
      "type": "remote",
      "url": "http://localhost:8000/mcp",
      "oauth": true
    }
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `type` | `"remote"` 表示远程 MCP 服务 |
| `url` | BS 的 MCP Streamable HTTP 端点（端口 8000） |
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
| **私钥隔离** | RSA 私钥仅存在于 BS 进程内存与本地磁盘 |
| **公钥非机密** | 公钥用于验签 JWT，泄露不会导致 Token 被伪造（签名需要私钥） |
| **工具端点受保护** | `/mcp` 需要有效 JWT 才能调用工具 |
| **Scope 分离** | Token 带 `read` / `write` scope，读操作不要求 write 权限 |
| **Consent 确认** | 每次新客户端授权都需用户点击 Allow，防止未授权访问 |

::: tip 部署建议
- BS 单进程部署（端口 8000），无需 Sidecar
- 避免将 BS 端口直接暴露到公网
- 如需公网访问，建议通过 VPN 或反向代理 + TLS 保护
:::

## Agent 配置 Prompt

可直接粘贴给 AI 助手，指导它完成 MCP 接入配置：

```
你是 bangumi-syncer 的配置助手。请帮我完成 MCP 接入配置：
1. 阅读 docs/config/mcp.md，确认 BS 已运行（端口 8000，内置 MCP 服务）。
2. 将 BS 的 MCP 端点注册到 opencode.json（type: remote + oauth）或 Claude Desktop。
3. 确认 auth.enabled 状态：开启则 OAuth 需登录，关闭则跳过登录但仍需 consent 授权。
4. 验证 get_logs / get_current_config 可用，并演示 update_config。
请只执行只读与配置类操作，不要改动同步数据。
```

## 接下来

- 配置完成后，可通过 AI 助手查看 [同步记录](/usage/) 或调整 [配置项](/config/configuration)
- 遇到连接问题？检查 [故障排除](/troubleshooting)
