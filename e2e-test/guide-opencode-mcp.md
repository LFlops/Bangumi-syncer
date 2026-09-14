# 指南 / 测试用例：OpenCode 接入 BS MCP（DCR + OAuth 2.1）

> 状态：**已实测验证**（2026-09-13/14，OpenCode `1.18.20` + 本分支 Docker 镜像）
> 关键差异：OpenCode 走 **DCR（RFC 7591）**，不是 CIMD —— **BS 无需出网访问客户端**（注册数据由客户端 POST 上来），因此不存在 CIMD 的 fake-ip/SSRF 类问题。

## 1. 原理与流程

OpenCode 对 `type: remote` 的 MCP 服务自动启用 OAuth（除非显式 `oauth: false`）：发现授权服务器 → PKCE → 服务端支持动态客户端注册（DCR）时自动注册，无需手工配置 client 凭据。

实测时序：

```
OpenCode                                    BS（内置 MCP OAuth AS）
    │  POST /mcp（无 token）                  │
    │ ──────────────────────────────────────► 401 + Protected Resource Metadata
    │  发现 AS metadata（scopes_supported）    │
    │  POST /register（DCR，RFC 7591）         │
    │ ──────────────────────────────────────► 201 Created（client_id=UUID，内存注册表）
    │  GET /authorize?client_id=<UUID>&code_challenge=...S256
    │      &redirect_uri=http://127.0.0.1:<port>/mcp/oauth/callback
    │      &scope=read+write&resource=...      │
    │ ──────────────────────────────────────► 302 → /consent?request_token=...
    │  /consent（需 BS 登录会话）              │  未登录 401 / 已登录 200
    │  POST /consent action=allow             │
    │ ◄────────────────────────────────────── 302 → 本地回调（OpenCode 监听）
    │  POST /token（public client + PKCE）    │
    │ ──────────────────────────────────────► 200 → JWT access + refresh
    │  POST /mcp（Bearer）                    │
    │ ──────────────────────────────────────► 200（工具可用）
```

要点：

- 服务端注册表为**进程内存态**（`MAX_CLIENTS=1000`）：BS 重启后注册记录与 Refresh Token 清空，需重新授权；已签发 Access Token 在 1 小时内仍有效（JWT 自包含）
- **scope 默认只发 `read`**；需要 `update_config` 时必须在 OpenCode 配置里显式声明 `read write`（实测 authorize 请求携带 `scope=read+write`）
- OpenCode 使用 loopback 回调（`http://127.0.0.1:<随机端口>/...`），依赖 BS 的 loopback 端口灵活匹配

## 2. 前置条件

| 项 | 要求 |
| --- | --- |
| BS | 运行中，且包含 `/mcp` 鉴权中间件修复（合法 Bearer 必须能通，不能被恒 401） |
| OpenCode | 实测 `1.18.20`；v2 的配置结构不同（见 §3 注） |
| 账号 | BS Web 登录凭据（`auth.enabled=true` 时 consent 需要会话） |
| 网络 | 客户端能访问 BS 的 `MCP_BASE_URL`（反代场景注意地址一致性） |

## 3. 接入步骤

### 3.1 配置 MCP 服务

`opencode.json`（**v1.x 格式**，本指南实测样例）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "bangumi-syncer": {
      "type": "remote",
      "url": "http://localhost:8000/mcp",
      "oauth": { "scope": "read write" }
    }
  }
}
```

说明：

- `oauth` 是对象或 `false`，**不能写 `true`**（无效值）；省略即自动 OAuth
- 仅需只读时可省略 `scope`（客户端不请求 scope 时，服务端按安全默认只发 `read`）
- 需要修改配置（`update_config`）时必须包含 `read write`
- **v2 差异**：配置结构为 `"mcp": { "servers": { "<name>": { ... } } }`，OAuth 字段用 snake_case（`client_id` / `client_secret` / `scope` / `callback_port`）

### 3.2 触发授权

```bash
# 查看状态（首次会显示 needs authentication）
opencode mcp list

# 发起 OAuth 授权（会打开浏览器）
opencode mcp auth bangumi-syncer
#   TUI 中也可在 MCP 管理界面触发；headless 环境见 §5

# 浏览器流程：BS 登录（auth.enabled=true 时）→ consent 页点 Allow
```

### 3.3 验证

```bash
opencode mcp auth list          # 期望：✓ bangumi-syncer authenticated
opencode mcp list               # 期望：✓ bangumi-syncer connected (OAuth)

# 工具调用（agent 内自动加载技能与工具）
opencode run "只做一件事：调用 bangumi-syncer MCP 的 get_current_config 工具，回答 sync.match_confidence_threshold 的值。"
# 期望：返回当前阈值（如 0.6）
```

## 4. 服务端核对清单（预期证据）

| 观察点 | 预期 |
| --- | --- |
| `POST /register` | **201 Created**（DCR；与 CIMD 的关键区别） |
| `GET /authorize?...client_id=<UUID>` | 302（client_id 是 UUID，不是 URL） |
| authorize 请求中的 `scope` | 配置 `read write` 时为 `read+write` |
| `GET /consent`（无会话 / 有会话） | 401（auth.enabled=true 且未登录）/ 200 |
| `POST /consent` | 302 → 本地回调（`127.0.0.1:<port>`） |
| `POST /token` | 200 |
| `POST /mcp`（Bearer） | 200 |
| CIMD 抓取日志 | **不应出现**（OpenCode 不走 CIMD） |

## 5. Headless / 自动化授权（CI、无浏览器环境）

`opencode mcp auth` 会打印并打开授权 URL。无浏览器时可用等价流程（实测通过）：

1. 后台启动 `opencode mcp auth bangumi-syncer`，从 BS 日志提取回调地址与 `request_token`
   （`docker logs <bs容器> | rg 'consent\?request_token='`）
2. 用 BS 账号 `POST /api/login` 取会话 Cookie
3. `GET /consent?request_token=...`（带 Cookie）解析 `csrf_token`
4. `POST /consent`（`action=allow`）→ 拿到 302 的本地回调 URL
5. `curl <回调URL>` 把 code 交给 OpenCode 的监听端口，等待 `authenticated`

> 注意：`docker exec` 操作 BS 数据目录请使用与容器内服务相同的用户（默认 root 会破坏 SQLite WAL 文件属主，导致 `disk I/O error`）。只读/自动化场景不要直接改库。

## 6. 测试用例

### 正向

| # | 用例 | 操作 | 预期 | 自动化对应 |
| --- | --- | --- | --- | --- |
| T1 | AS metadata 可达 | 请求 `/.well-known/oauth-authorization-server` | 含 `registration_endpoint`、`scopes_supported: [read, write]` | `tests/test_mcp_integration.py` |
| T2 | DCR 注册 | 授权流程中观察服务端 | `POST /register` → 201，返回 UUID client_id | `test_mcp_integration.py`（注册步骤） |
| T3 | 完整授权 | `opencode mcp auth bangumi-syncer` | `authenticated` / `connected (OAuth)` | 本次实测（§8） |
| T4 | 工具调用（读） | agent 调 `get_current_config` / `get_logs` | 正常返回（不要求 write） | 实测 + `tests/test_mcp_integration.py` |
| T5 | 工具调用（写） | agent 调 `update_config`（scope 含 write） | 成功并复读验证 | 实测（阈值 0.6→0.65→0.6） |
| T6 | Refresh 自动续期 | access token 过期后继续调用 | 自动刷新，无需重新授权 | `tests/test_mcp_auth.py`（refresh 流程） |
| T7 | 重启后 1h 内 access token 仍有效 | 重启 BS 后用旧 token 调 `/mcp` | 200（JWT 自包含验签） | 实测（E2E 期间重启复验） |

### 负向

| # | 用例 | 预期 | 自动化对应 |
| --- | --- | --- | --- |
| N1 | BS 未启动 / URL 错误 | `opencode mcp list` 连接失败 | 手工 |
| N2 | 未登录访问 consent（auth.enabled=true） | 401 | `tests/test_mcp_auth.py` |
| N3 | 无 write scope 调 `update_config` | `ToolError` 权限不足 | `tests/test_mcp_tools.py`（scope 校验用例） |
| N4 | 无效 Bearer 调 `/mcp` | 401 | `tests/test_mcp_integration.py` |
| N5 | 吊销后调用 | 401（`/revoke` 生效） | `tests/test_mcp_integration.py::TestRevocationFlow` |
| N6 | Refresh Token 过期/丢弃 | 重新授权（30 天 TTL，滑动续期） | `tests/test_mcp_auth.py`（TTL 用例） |
| N7 | 重启后重新连接刷新 | 因注册表/Refresh 内存态需重新 `opencode mcp auth` | 实测（重启复验） |
| N8 | 多 worker 部署 | 不支持（授权/吊销状态不共享） | 文档约束（见 `docs/development/mcp.md`） |

## 7. 故障排查

**症状 A：`opencode mcp list` 显示 needs authentication 且授权后仍反复失败**
- 确认 BS 版本包含 `/mcp` 鉴权中间件修复（否则合法 Bearer 恒 401、OpenCode 会陷入「刷新→401→再刷新」循环）

**症状 B：授权页 401 / 要求登录**
- 先在 BS Web 登录（`auth.enabled=true`），再 `opencode mcp auth` 重新触发

**症状 C：`update_config` 报权限不足**
- 配置未声明 `read write`：修改 `opencode.json` 的 `oauth.scope` 后执行
  `opencode mcp logout bangumi-syncer && opencode mcp auth bangumi-syncer` 重新授权

**症状 D：BS 重启后调用失败**
- 注册表与 Refresh Token 为内存态：重新 `opencode mcp auth bangumi-syncer`；旧 access token 在 1h 内仍可用

**症状 E：配置改完不生效 / 报 schema 错误**
- 检查 OpenCode 版本：v1 用 `mcp.<name>`；v2 用 `mcp.servers.<name>`（字段 snake_case）
- 不要写 `"oauth": true`

**症状 F：callback 打不通 / 端口占用**
- OpenCode 每次授权使用回调端口（实测 `127.0.0.1:19876`）；确保本机无端口冲突、无额外代理拦截 localhost

## 8. 安全说明

- **DCR 注册 ≠ 授权**：`/register` 未认证可达，但拿 token 仍需 BS 登录 + consent 页 Allow
- scope 最小化：默认 `read`；仅在需要改配置时授予 `read write`
- 注册表有上限（`MAX_CLIENTS=1000`）；被刷注册时**重启 BS 即可清空**（内存态）
- OpenCode 凭据由 OpenCode 自身存储（`opencode mcp logout` 可清除）

## 9. 实测证据（2026-09-13/14）

**环境**：OpenCode `1.18.20`；BS 当前分支镜像（含中间件/CIMD 修复）；`auth.enabled=true`

**本次复跑（DCR）**：

```
服务端日志：
  POST /register 201 Created
  GET /authorize?...client_id=85bfd76d-dd3c-4172-8433-17ceb1d08b95&...&scope=read+write 302
  GET /consent?request_token=... 401（无会话）→ 200（带会话）
  POST /consent 302 → POST /token 200
客户端：
  opencode mcp auth list → ✓ bangumi-syncer authenticated
  opencode run "...get_current_config..." → bangumi-syncer_get_current_config → "0.6"
```

**此前从 0 到 1（同一环境）**：

- 加载 `bangumi-syncer` 技能 → 只读验证（`get_current_config` / `get_logs`）→ 写权限确认
- 变更 `sync.match_confidence_threshold`：`0.6 → 0.65 → 0.6`（计划→执行→复读），落库与配置实变均已核验

**相关服务端缺陷修复记录**（均由 OpenCode/E2E 发现）：

| 修复 | commit（示例） | 说明 |
| --- | --- | --- |
| 生产嵌入 `/mcp` 恒 401 | `fa661cf` | 路由摊平时缺失鉴权中间件 |
| 下划线段名掩码绕过 | `ecf869c` | `get_current_config` 明文泄露敏感字段 |
| CIMD scope 允许集 | `7ecb724` | 仅影响 CIMD（Claude Code）；DCR 路径不受影响 |
