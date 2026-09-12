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
| --- | --- |
| **BS（FastAPI）** | 主程序，端口 8000，内置 FastMCP 服务（工具 + OAuth AS） |
| **RSA 密钥** | 本地生成（RS256），私钥仅存于 BS 进程内存与本地磁盘，公钥用于验签 JWT |

::: tip 嵌入式优势
FastMCP 直接嵌入 BS 进程，工具函数调用同进程业务层（无需 HTTP），部署更简单（单进程、单端口），无需额外进程与共享卷。
:::

::: warning 工具函数直接嵌入进程
MCP 工具函数直接调用同进程业务层（无需 HTTP），通过 FastMCP `/mcp` 端点暴露。公网访问需要有效的 JWT（通过 OAuth 授权流程获取）。
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
| `MCP_RSA_PRIVATE_KEY` | `<系统临时目录>/mcp_private.pem` | RSA 私钥路径（本地磁盘，仅 BS 持有） |
| `MCP_RSA_PUBLIC_KEY` | `<系统临时目录>/mcp_public.pem` | RSA 公钥路径（本地生成，用于验签 JWT） |
| `MCP_BASE_URL` | `http://localhost:8000` | 服务公共 URL，用作 OAuth issuer / metadata 端点（解析优先级：参数 > `MCP_BASE_URL` > `dev.mcp_base_url` 配置 > 默认值） |
| `MCP_REFRESH_TOKEN_TTL` | `2592000`（30 天） | Refresh Token 有效期，**单位：秒**；每次轮换后重新计时（滑动窗口） |

默认 RSA 路径由 `tempfile.gettempdir()` 解析，随操作系统不同而变化（Linux 通常 `/tmp`，macOS 为 `$TMPDIR`）。

::: warning 当前不可配置
JWT `issuer`（= 解析后的 `MCP_BASE_URL`）、`audience`（=`bangumi-syncer`）、Access Token 有效期（= `3600` 秒）在 `app/mcp/server.py` 中**硬编码**，没有对应环境变量。
:::

::: warning RSA 密钥持久化
RSA 密钥对在 BS 启动时自动生成（RS256）并写入 `MCP_RSA_PRIVATE_KEY` / `MCP_RSA_PUBLIC_KEY` 指向的路径。**默认路径在系统临时目录**（`tempfile.gettempdir()`），容器重建或临时目录被清理会导致密钥丢失：

- 已签发的 Access Token（有效期 1 小时）验签失败，客户端需重新授权
- 多实例部署时各实例密钥不一致，需共享同一密钥

建议将两个环境变量指向持久卷/数据目录。Docker 示例（镜像内已预建 `/app/data`）：

```bash
docker run -d \
  -v bs_data:/app/data \
  -e MCP_RSA_PRIVATE_KEY=/app/data/mcp_private.pem \
  -e MCP_RSA_PUBLIC_KEY=/app/data/mcp_public.pem \
  -p 8000:8000 bangumi-syncer:latest
```
:::

### 3. 部署约束：单进程

OAuth 授权状态全部保存在**进程内存**中（`app/mcp/provider.py`）：已注册客户端 `_clients`、授权码 `_auth_codes`、Refresh Token `_refresh_tokens`、待授权请求 `_pending_auths`、已吊销 `jti` 记录 `_revoked_tokens`（jti → access token 到期时间）。过期状态按 TTL 惰性清理（授权码 5 分钟、待授权请求 10 分钟、Refresh Token 30 天、吊销记录随 access token 到期）。

仓库内置的启动方式均为**单进程**：`start.bat` 与 Dockerfile 的 `CMD` 都执行 `uvicorn app.main:app`，未使用 `--workers`。

因此当前**不支持 `--workers N` 或 gunicorn 多进程**部署：

- 授权码 / Refresh Token 可能因请求落到不同进程而无法换取
- 吊销状态在各进程间不一致
- Access Token 的 JWT 验签本身无状态（RS256），不受进程数影响

多实例部署时，除需共享同一 RSA 密钥（见上）外，进程内状态（授权码 / Refresh Token / 吊销集合）在各实例间不共享，授权与吊销互相独立。

## OAuth 授权流程

BS 内置的 FastMCP 服务实现了完整的 OAuth 2.1 授权服务器，支持 **consent 确认** 与 **自动续期**。

### auth.enabled=true（推荐）

首次授权流程：

1. AI 助手连接 BS 的 `/mcp` 端点 → 触发 OAuth 授权
2. BS 在**同进程内**读取请求 Cookie 中的 `session_token`，调用 `security_manager.validate_session()` 校验会话（不经 HTTP）
3. **已登录**：显示 consent 页，用户确认后发放 Token
4. **未登录或会话失效**：`/consent` 直接返回 **HTTP 401**（不会跳转到 BS 登录页）。请先在 BS Web 端登录，再重新触发授权
5. 用户点击 **Allow** → 发放 Access Token + Refresh Token

### auth.enabled=false

- 不校验 BS 会话，consent 时直接使用 BS 认证配置中的用户名（`auth_username`），无需登录
- **consent 确认仍保留**：用户仍需点击 Allow/Deny 授权
- 适合纯内网、无需区分用户身份的场景

### 自动续期

- Access Token 过期后，服务自动用 Refresh Token 换取新 Token
- Refresh Token 轮换（每次换取后旧 Token 失效）
- Refresh Token 默认有效期 **30 天**（`2592000` 秒，可用 `MCP_REFRESH_TOKEN_TTL` 调整）
- **每次轮换后重新计时**（滑动窗口）：只要在有效期内使用过，就会延长到「最近一次使用 + 30 天」
- 若连续 **30 天未使用**，Refresh Token 过期失效，客户端需**重新授权**
- **一次授权后无需重复登录**，除非 Token 被吊销或长时间未使用

::: tip 吊销方式
- **标准端点**：`POST /revoke`（由 `RevocationOptions(enabled=True)` 提供）可吊销 access / refresh token；access token 吊销后其 `jti` 进入进程内吊销记录（附带该 token 的到期时间，到期后惰性清理），验签时被拒绝
- **内存方式**：删除进程内存中的 Refresh Token 即可吊销（重启进程会清空）。重新连接会触发新的授权流程
:::

## 动态客户端注册（DCR）

BS 内置的 FastMCP 服务**默认开启**动态客户端注册（RFC 7591）：`ClientRegistrationOptions(enabled=True, valid_scopes=["read", "write"])`，暴露标准端点 `POST /register`（未认证可达）。

::: warning 注册 ≠ 授权
DCR 注册本身**不授予任何数据权限**。客户端注册成功后，仍需走完整授权流程：BS 登录会话（`auth.enabled=true` 时）+ consent 页点击 **Allow**，才能拿到 Access Token。
:::

- **存储与上限**：已注册客户端保存在进程内存，上限 `MAX_CLIENTS=1000`
- **关闭方式**：当前**没有配置开关**，需在装配代码中传入 `ClientRegistrationOptions(enabled=False)`。关闭后仅 CIMD（Client ID Metadata Document）客户端可授权，部分依赖 DCR 的客户端将不可用

::: tip 被扫描注册 / 出现陌生客户端怎么办？
**重启 BS 服务即可清空全部已注册客户端**（连带清空待授权请求、授权码、Refresh Token 与吊销状态；RSA 密钥若已持久化则保留）。已签发的 Access Token 在其 1 小时有效期内仍然有效（JWT 自包含验签，不查询注册表），到期后客户端会重新走授权流程。
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

BS 内置的 MCP 服务提供 3 个工具，AI 助手通过它们与 BS 交互：

### get_logs — 读取日志

获取 BS 运行日志，支持多维度过滤。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `level` | string | 否 | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`（`WARN` 等价于 `WARNING`） |
| `search` | string | 否 | 关键词搜索（匹配日志内容） |
| `limit` | integer | 否 | 返回条数，默认 50，最大 10000 |
| `since` | string | 否 | 起始时间（ISO 格式，如 `2026-09-01T10:00:00`）。带时区输入按服务器本地时间对齐（不换算，直接丢弃时区偏移） |
| `until` | string | 否 | 结束时间（ISO 格式）。带时区输入按服务器本地时间对齐（不换算，直接丢弃时区偏移） |

### get_current_config — 查看配置

获取 BS 全量配置，敏感字段（如密码、Token）已自动脱敏（显示为 `***`）。

**无参数**。

### update_config — 修改配置

修改 BS 的某项配置并**直接生效**（无需重启）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `section` | string | 是 | 配置段名（如 `sync`、`dev`；`auth` 段禁止通过 MCP 修改） |
| `key` | string | 是 | 配置键名（如 `log_level`、`enabled`） |
| `value` | any | 是 | 配置值，支持任意 JSON 值（字符串、数字、布尔、数组、对象等） |

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
| **Scope 分离** | Token 默认只发放 `read`；`write` 需客户端**显式请求**，且请求的 scope 必须属于客户端已注册/声明的 scope。读操作不要求 write 权限 |
| **动态注册受限** | DCR 注册不授予数据权限，仍需登录会话 + consent；客户端上限 1000，重启即清空 |
| **Consent 确认** | 每次新客户端授权都需用户点击 Allow，防止未授权访问 |

::: tip 部署建议
- BS 单进程部署（端口 8000），无需额外进程
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
