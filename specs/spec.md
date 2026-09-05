# MCP 能力接入 bangumi-syncer —— 细化方案与执行计划（v5）

## 一、范围界定

**只做 MCP，忽略 RAG**。初版设计文档中 **Phase 3（RAG 增强）** 及其所有相关内容全部排除：`sqlite-vec`、向量库、`search_anime` Tool、GitHub Actions 向量构建、文本分块、`bangumi-data` 拉取、`.db` 分发。

**聚焦目标**：给 Claude / OpenCode 提供一个 MCP（远程 HTTP 形态），**读取日志调试 + 读取/修改配置**。

**纳入范围**：
- **工具集（3 个）**：`get_logs`、`get_current_config`、`update_config`
- **认证**：OAuth 2.1 + JWT（单通道，唯一形态；登录步骤跟随 `auth.enabled`，内部 API 始终验 JWT）
- **部署**：uv workspace 多项目结构、mcp_server 独立服务（Streamable HTTP 传输）

**显式不做**：本地 stdio 模式、`mcp_auth_enabled` 独立开关（认证跟随 `auth.enabled`）、`trigger_sync`、配置两阶段提交、配置自动备份、Agent Skill 工作流、内部静态 token、per-tool scope 精细控制、RAG 一切内容。

---

## 二、代码库现状与复用结论（调研结果）

| 能力 | 现状 | 复用判断 |
|------|------|----------|
| 日志读取 | `app/api/logs.py` 已有 `GET /api/logs`，复用 `_read_log_file` + `resolved_dev_log_file_path`；级别=弱文本过滤、**无时间范围** | ✅ 复用核心逻辑；内部 API 层补时间范围 |
| 配置读取 | `GET /api/config` + `serialize_schema()`（`config_schema.SECTIONS` 为权威字段源） | ✅ 直接复用 |
| 配置写入 | `POST /api/config` 直接落盘 INI，线程安全 + 热更新；已有 `config_backups` 手动备份/恢复（Web 可用，MCP 不新增备份） | ✅ 复用落盘 |
| 鉴权 | `auth.enabled=False` 放行 + 身份取配置用户（`get_current_user` 范式）；`get_current_user_flexible` 双通道范式可参考 | ⚠️ 需**新增 JWT 验签**；`auth.enabled` 只控制 OAuth 登录步骤 |
| 登录 | `POST /api/login` + `GET /api/auth/status`（session 校验） | ✅ OAuth 登录**复用**；`auth.enabled=False` 时跳过登录 |
| 密钥加密 | `config_secret_crypto` Fernet（`auth.secret_key`） | ✅ 直接复用；敏感字段自动加密 |
| 非对称验签 | `cryptography` 已在依赖中（RSA 可用） | ✅ JWT RS256 验签直接用它，py3.9 兼容 |
| OAuth | **仅出站**（BS 作 client 连 Bangumi/Trakt）；**无入站 OAuth** | ❌ 需新增 AS（放 mcp_server，SDK 承担协议层） |
| MCP 依赖 | 全仓**无 mcp/fastmcp** | ❌ 需新增，仅 `mcp_server/` 子项目 |
| 测试 | `httpx.AsyncClient + ASGITransport`，参照 `tests/api/test_logs_comprehensive.py` | ✅ 遵循现有模式 |

**关键决策 D1~D4**：

- **D1 认证（单通道 OAuth JWT，无独立开关）**：`mcp_server` 作授权服务器（AS）。**认证规则跟随 `auth.enabled`**：开启则 OAuth 需登录（复用 BS 账号密码）；关闭则跳过登录（身份取 `auth.username`）。**内部 API 始终验 JWT**（`cryptography` 公钥 RS256 验签，py3.9 兼容），consent 始终保留作为门禁。无 `mcp_auth_enabled` 独立配置项。
- **D2 内部 API 形态**：独立内部 API 前缀 `/api/mcp/*`，用 `get_mcp_client`（JWT 验签）依赖保护，转发复用现有核心逻辑（`_read_log_file`、`config_manager`）。不改动现有公开端点鉴权。
- **D3 Sidecar**：`mcp_server/` 作为 **uv workspace 独立子项目**（`requires-python = ">=3.10"`），仅 **Streamable HTTP 传输**，与 BS 走内部网络。
- **D4 配置修改**：`update_config` **直接生效**（Web 现有 `config_backups` 可手动备份/恢复，MCP 不新增备份逻辑）。不做 stage/apply 两阶段、不做自动备份。

---

## 三、架构设计

### 3.1 uv workspace 多项目结构

```
bangumi-syncer/                      # workspace 根
├── pyproject.toml                   # [tool.uv.workspace] members = ["mcp_server"]
│                                    #   BS 主包：requires-python = ">=3.9"（不动）
├── app/  tests/  ...                # BS 现有代码
│
└── mcp_server/                      # 独立子项目（Sidecar）
    ├── pyproject.toml               # requires-python = ">=3.10"
    │                                # dependencies = ["mcp[auth]>=1.27", "httpx", "pydantic"]
    ├── Dockerfile                   # 独立镜像（python:3.11-slim + uv 安装，Streamable HTTP 入口）
    ├── src/mcp_server/
    │   ├── server.py                # MCP Server（Streamable HTTP 传输入口）
    │   ├── auth.py                  # OAuth AS（authorize/token/register + JWT 签发 + auth.enabled 分流）
    │   ├── client.py                # BS 内部 API 客户端（JWT 透传）
    │   └── tools.py                 # get_logs / get_current_config / update_config
    └── tests/
```

- **Python 隔离**：`uv sync` 按各 member 的 `requires-python` 自动拉对应解释器
- **依赖隔离**：mcp SDK 只在 `mcp_server/`，BS 运行时零污染
- **独立迭代**：mcp_server 有自己的 lock、版本节奏、Docker 镜像、CI job

### 3.2 镜像构建（两个独立镜像）

| 镜像 | Dockerfile | 基础镜像 | 内容 | 说明 |
|------|-----------|----------|------|------|
| **BS 主服务** | 根 `Dockerfile`（**已有，基本不动**） | `python:3.9-slim-bookworm` | BS 应用 | MCP 改造不改变其构建：公钥经共享卷挂载运行时读取，无需镜像内固化 |
| **mcp_server** | `mcp_server/Dockerfile`（**新增**） | `python:3.11-slim-bookworm` | mcp_server 子项目 | py3.11 ≥ 3.10 满足 mcp[auth]；Streamable HTTP 入口 `uvicorn` 或 mcp SDK 启动器；EXPOSE 3000 |

**mcp_server/Dockerfile 设计要点**（仿根 Dockerfile 的 uv 构建模式）：
- `FROM python:3.11-slim-bookworm AS builder` + `uv sync --frozen --no-install-project --no-dev`（以 `mcp_server/` 为 workspace 成员，`--directory mcp_server` 或独立 `uv.lock`）
- 非 root 用户（`PUID/PGID` 可配，对齐 BS 惯例）
- 共享卷挂载点：`/mcp_auth`（读 `mcp_public_key.pem` 公钥）
- 环境变量：`BS_BASE_URL=http://bs:8000`（内部网络）、`BS_PUBLIC_KEY_PATH=/mcp_auth/mcp_public_key.pem`
- `EXPOSE 3000`，启动 `python -m mcp_server.server`（Streamable HTTP）
- 不新增 `docker-compose.yml`：双容器编排与共享卷挂载由 `docs/config/mcp.md` 指导（现有部署模式为单容器 + 卷挂载，MCP Sidecar 为可选附加服务）

### 3.3 认证架构：OAuth 2.1 + JWT（唯一形态）

```
┌─────────┐  ① 首次调用 → 401 + WWW-Authenticate     ┌──────────────┐
│  Agent  │ ───────────────────────────────────────▶ │  mcp_server  │
│ (MCP客户端)│                                        │  (Sidecar+AS) │
└─────────┘                                          └──────────────┘
    │  ② 读 /.well-known 发现授权服务器地址                │
    │  ③ DCR 注册自己 → 拿 client_id                     │
    │  ④ 打开浏览器 → GET /authorize?client_id=...&code_challenge=...
    │ ──────────────────────────────────────────────▶ │
    │  ⑤ 认证分流（见 3.3）                             │
    │  ⑥ consent 页："允许 Agent 访问 bangumi-syncer？"  │
    │    用户点【允许】                                  │
    │  ⑦ 签发一次性 authorization code → 302 回 Agent  │
    │  ⑧ code + code_verifier → POST /token            │
    │ ──────────────────────────────────────────────▶ │
    │  ⑨ AS 校验 code + PKCE → 用【AS RSA 私钥】签发：    │
    │     access_token(JWT, 1h, scope: read write)     │
    │     + refresh_token(可吊销)                       │
    │ ◀────────────────────────────────────────────── │
    │  ⑩ 每次调工具：Bearer <JWT> ──透传──▶ BS          │
    │                                  BS 用【AS 公钥】验签→解析 sub/scope→放行
```

**核心机制**：
- **JWT 由 AS（mcp_server）私钥签发**（RS256 非对称），与账号密码无关；账号密码只用于认证步骤
- **Agent 永远不知道密码**：只持有短期 JWT（claims：`sub=username`、`scope=read write`、`iss`、`aud`、`exp`）
- **BS 验签**：`cryptography` 公钥验签（RS256）→ 验 exp → 读 sub/scope。**本地验签，无需 introspection 回调，不需要 mcp SDK，py3.9 兼容**
- **公钥分发**：AS 公钥写入共享卷 `mcp_public_key.pem`（非机密，可公开），BS 启动读取并缓存；私钥只在 mcp_server 进程内，**不落共享卷**

### 3.4 认证规则：跟随 `auth.enabled`（无独立开关）

```
authorize 端点认证分流：

① auth.enabled = True（Web 认证开启）
   → 完整 OAuth：未登录跳 BS 登录页 → 用户输入账号密码 → AS 校验 BS session
   → 身份 sub = 登录用户的 username
   → 已登录浏览器直接进 consent（OAuth 标准"一次授权、token 自动续期"，用户仅首次授权）

② auth.enabled = False（Web 认证关闭）
   → 跳过登录步骤（部署者声明"不需要身份认证"，与 Web 放行行为一致）
   → 身份 sub = auth.username（当前配置用户，动态读取）
   → 直接进入 consent

③ consent 页（①② 都保留）
   → 用户点击【允许】后才签发 code → token
   → MCP 层面的授权确认，独立于 Web 认证，防止 Agent 静默拿权限

④ 内部 API（/api/mcp/*）始终验 JWT
   → 无论 auth.enabled 状态，无有效 JWT 一律 401
   → consent 授权是拿到 JWT 的唯一途径（或 refresh 自动续期）
```

**关键规则**：
- **无 `mcp_auth_enabled` 独立开关**：`auth.enabled` 只决定"OAuth 是否需要登录步骤"；内部 API 永远要求有效 JWT
- **consent 始终保留**：远程 + Web 认证关闭时，跳过登录但保留人工授权，安全不降级到零
- 与 Web 行为完全对齐：`auth.enabled=False` 时 Web 也是"放行 + 身份取配置用户"，无行为漂移
- **授权是一次性的**：用户仅首次授权（登录 → consent）；之后 access token 短期有效、refresh token 自动续期，无需重复授权——OAuth 标准生命周期，无额外开关

### 3.5 内部 API 与身份

- `/api/mcp/*` 独立前缀，`get_mcp_client` 依赖保护
- **单通道**：JWT 验签 → 身份 = `sub`（username 动态，不写死）
- **scope 默认全量 `read write`**：单管理员用户，默认签发全量；客户端可经 OAuth `scope` 收紧（可选）。BS 侧仅基础校验：写操作（`update_config`）需 `write`，读操作需 `read`
- 返回 `{"username": <动态>, "scope": [...], "mcp": True}`，业务层用现有 `current_user.get("username")` 读取，零改动
- `auth.enabled=False` 时身份取 `auth.username`（动态读取 `get_auth_config()["username"]`，不复刻 `deps.py` 写死 "admin" 的隐患）

---

## 四、功能分析

- **角色**：AI 客户端（Claude Desktop / OpenCode）通过远程 MCP 读日志调试、读写配置；Docker 私有化部署管理员。
- **目标**：以最小工具集（3 个）实现"读日志 + 读配置 + 改配置"，统一 OAuth 认证，无静态凭证，认证规则跟随 Web 配置。
- **价值**：单一部署形态（远程 HTTP）；OAuth 自动授权；无静态 token；身份与 Web 用户一致。
- **验收标准**：
  1. OpenCode / Claude Desktop 远程接入 mcp_server，OAuth 全流程（登录或跳过 → consent → JWT）可用。
  2. `get_logs` / `get_current_config` / `update_config` 三个工具返回正确数据。
  3. `update_config` 直接生效，配置修改后可通过现有 `config_backups` 手动回滚。
  4. `auth.enabled=True` 时 OAuth 需登录；`auth.enabled=False` 时跳过登录、consent 保留、身份取 `auth.username`；**两种状态下内部 API 都要求有效 JWT**。
  5. 无有效 JWT 返回 401；写操作无 `write` scope 返回 403。
  6. `uv run pytest tests/` 全绿；`mcp_server/` 子包测试（py3.10）单独跑全绿；`uv.lock` 提交。

---

## 五、BDD 场景（Gherkin）

> 测试用例与场景一一对应，覆盖正常/边界/异常。以下按功能分组。

### 功能 1：OAuth 2.1 认证与授权

```gherkin
功能: MCP OAuth 认证
  作为 Docker 部署管理员
  我想要 Agent 通过 OAuth 2.1 获取短期 JWT 访问内部 API
  以便无静态明文 token、凭证可吊销

  场景: OAuth 首次授权全流程（Web 认证开启）
    给定 Agent 首次连接 mcp_server 且 auth.enabled=True
    当 Agent 触发 MCP 调用
    那么 返回 401 且含 WWW-Authenticate 指向授权服务器
    且 Agent 读取 /.well-known 元数据并 DCR 注册成功
    且 浏览器完成 登录→consent→code→token 流程
    且 Agent 获得 access_token(JWT, scope=read write) + refresh_token
    且 携带 JWT 调用 /api/mcp/* 返回 200

  场景: 登录复用 BS 账号密码
    给定 浏览器访问 authorize 端点且 BS 无有效 session
    当 跳转 BS 登录页并输入现有账号密码
    那么 登录成功回跳 authorize，AS 校验 session 有效并放行

  场景: 已登录浏览器直接进 consent（一次授权）
    给定 浏览器已有有效 BS session
    当 访问 authorize 端点
    那么 不重复要求密码，直接进入 consent 页
    且 用户点【允许】后完成授权
    且 授权后 access token 短期有效、refresh token 自动续期，无需重复授权

  场景: Web 认证关闭时跳过登录（consent 保留）
    给定 auth.enabled=False
    当 Agent 触发 OAuth 授权流程
    那么 authorize 端点跳过登录步骤
    且 身份 sub = auth.username（当前配置用户）
    且 consent 页仍显示并要求用户点击【允许】
    且 授权成功后签发 JWT，携带调用 /api/mcp/* 返回 200

  场景: Web 认证关闭时内部 API 仍要求 JWT
    给定 auth.enabled=False 且 Agent 未完成授权
    当 Agent 直接携带空/伪造凭证调用 /api/mcp/*
    那么 返回 401，不会因 Web 认证关闭而放行

  场景: 用户在 consent 页拒绝
    给定 浏览器停留在 consent 页
    当 用户点击【拒绝】
    那么 不签发 code，Agent 无法获得 token，调用返回 401

  场景: JWT 有效调用（身份动态）
    给定 Agent 持有有效 JWT（sub="mylogin"）
    当 调用 /api/mcp/* 携带 Bearer <JWT>
    那么 BS 验签通过且 get_mcp_client 返回 username="mylogin"

  场景: JWT 过期
    给定 JWT 已超过 exp
    当 调用 /api/mcp/*
    那么 返回 401，Agent 用 refresh_token 换新 JWT 后调用成功

  场景: JWT 签名无效或公钥不匹配
    给定 JWT 由其他密钥签名
    当 调用 /api/mcp/*
    那么 返回 401 且不泄露任何业务数据

  场景: 写操作缺 write scope
    给定 JWT 仅含 read scope（客户端 OAuth 配置收紧）
    当 调用 update_config
    那么 返回 403 Forbidden

  场景: 无凭证访问
    当 调用 /api/mcp/* 且无 Authorization 头
    那么 返回 401
```

### 功能 2：内部 API（/api/mcp/*）

```gherkin
功能: BS 内部 MCP API
  作为 MCP Sidecar
  我想要通过受 JWT 保护的内部 API 读取日志与配置、修改配置
  以便封装成 MCP Tools 供 AI 客户端使用

  场景: 查询日志列表
    给定 有效 JWT
    当 GET /api/mcp/logs?level=ERROR&limit=50&since=...&until=...
    那么 返回 {status:success, data:{content, stats}} 且仅含窗口内 ERROR 级

  场景: 日志级别非法值
    给定 level=DEBUG2（非法级别）
    当 GET /api/mcp/logs
    那么 返回 400 或归一化为合法级别，不抛 500

  场景: 读取当前配置
    当 GET /api/mcp/config
    那么 返回全量配置且敏感字段已脱敏（密码/Token 掩码）

  场景: 读取配置 schema
    当 GET /api/mcp/config/schema
    那么 返回 serialize_schema() 的分段字段元数据

  场景: 修改配置
    当 POST /api/mcp/config/update {section:{key:value}}
    那么 调用 set_config/save_config 落盘并热生效
    且 返回变更摘要

  场景: 修改不存在的配置段
    当 POST /api/mcp/config/update {不存在段:{...}}
    那么 返回 400 或明确错误提示合法段名
```

### 功能 3：MCP Sidecar 部署与编排

```gherkin
功能: MCP Sidecar 部署
  作为 Docker 管理员
  我想要按文档引导运行 BS 与 Sidecar 双服务
  以便启用 MCP 能力

  场景: 共享卷公钥分发
    给定 mcp_server 首启生成 RSA 密钥对
    当 将公钥写入共享卷 mcp_public_key.pem
    那么 BS 启动读取公钥并缓存，验签可用

  场景: 共享卷公钥缺失
    给定 共享卷中无 mcp_public_key.pem
    当 BS 启动
    那么 记录明确错误并等待/重试，不静默吞错

  场景: 私钥不落共享卷
    那么 AS 私钥仅存于 mcp_server 进程内
    且 共享卷/磁盘上不存在明文私钥文件

  场景: 内部网络隔离
    给定 mcp_server 与 BS 在同一内部网络
    当 Agent 通过公网访问 mcp_server
    那么 BS 的 /api/mcp/* 不直接暴露公网，仅 mcp_server 可达
```

---

## 六、CI 与文档

### CI 方案

1. **扩展现有 `lint.yml`**：`mcp_server/` 有独立 `pyproject.toml`（`target-version = "py310"`）。lint 拆两步：`uv run ruff check app tests`（BS）+ `uv run --directory mcp_server ruff check .`（Sidecar）。
2. **扩展现有 `ci-tests.yml`**：`uv run pytest tests/ --cov=app ...`（BS 不变）；mcp_server 单独 job（py3.11）。
3. **新增 `mcp-server-ci.yml`**：
   ```yaml
   name: MCP Server CI
   on: [push, pull_request]
   jobs:
     mcp_test:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: astral-sh/setup-uv@v4
         - name: Set up Python
           run: uv python install 3.11
         - name: Sync (workspace)
           run: uv sync --group dev
         - name: Lint
           run: uv run --directory mcp_server ruff check . --output-format=github
         - name: Test
           run: uv run --directory mcp_server pytest mcp_server/tests/
   ```

4. **镜像构建验证**：`docker-build-test.yml` 现有 BS 镜像构建；新增 mcp_server 镜像构建步骤（或并入 `mcp-server-ci.yml` 尾部）：`docker build -f mcp_server/Dockerfile -t bangumi-syncer-mcp:test .` 验证 `mcp_server/Dockerfile` 可构建、`EXPOSE 3000`、启动入口正确。

### 文档方案

**不新增 `docker-compose.yml`**。部署（共享卷公钥分发、双服务运行、内部网络隔离）交由 `docs/` 指导。新增两份文档：

| 文档 | 位置 | 内容 |
|------|------|------|
| **用户文档** | `docs/config/mcp.md` | MCP 架构图、远程接入（OpenCode/Claude Desktop）、OAuth 授权流程、`auth.enabled` 开关影响、共享卷公钥分发 |
| **开发文档** | `docs/development/mcp.md` | 内部 API 契约、OAuth AS 结构、`auth.enabled` 分流逻辑、JWT 验签机制、测试方式 |

**OpenCode 接入**（写入 `docs/config/mcp.md`）：

```jsonc
{
  "mcp": {
    "bangumi-syncer": {
      "type": "remote",
      "url": "http://localhost:3000/mcp",
      "oauth": true            // OpenCode 自动处理 DCR + 授权；scope 默认 read write，可在此收紧
    }
  }
}
```

Claude Desktop：
```json
{
  "mcpServers": {
    "bangumi-syncer": {
      "url": "http://localhost:3000/mcp"
      // Claude Desktop 自动完成 OAuth 授权
    }
  }
}
```

**Agent 配置 prompt**（写入 `docs/config/mcp.md`）：
```text
你是 bangumi-syncer 的配置助手。请帮我完成 MCP 接入配置：
1. 阅读 docs/config/mcp.md，确认 BS 与 mcp_server 双服务已运行、公钥已分发。
2. 将 mcp_server 注册到 opencode.json（type: remote + oauth）或 Claude Desktop。
3. 确认 auth.enabled 状态：开启则 OAuth 需登录，关闭则跳过登录但仍需 consent 授权。
4. 验证 get_logs / get_current_config 可用，并演示 update_config。
请只执行只读与配置类操作，不要改动同步数据。
```

---

## 七、执行计划（DAG）

> 按文件读写范围消除写冲突，不重叠任务并行。BS 侧任务（T2~T6）与 mcp_server 侧任务（T7/T8）文件独立，可并行，仅通过 API 契约耦合。

### 依赖分析关键点
- BS JWT 验签模块（公钥从共享卷加载，无需 config 字段）→ T2 无依赖。
- `get_mcp_client` 依赖 T2 → T5 依赖 T2；内部 API T3/T4 依赖 T5（import `get_mcp_client`）。
- `main.py` 挂载依赖 T2 + T3/T4/T5 → T6 依赖 T2,T3,T4,T5。
- workspace 骨架 T7 无依赖（独立目录）；OAuth AS T8 依赖 T7（同目录，文件不重叠：T7 写 `pyproject/server.py/tools.py`，T8 写 `auth.py`）。
- 文档 T9 依赖 T6（API 契约）+ T7/T8（Sidecar/OAuth）；CI T10 依赖 T7/T8（workspace 结构与 AS 测试）。

```
T2 ──▶ T5 ──▶ T3 ─┐
        │      └──▶ T4 ─┘
        └──▶ T6 ────────┘
T7 ──▶ T8 ──┬──▶ T9（需 T6）
            └──▶ T10
```

| 任务 | 场景 | 文件 | 依赖 |
|------|------|------|------|
| **T2** BS JWT 验签模块（共享卷公钥加载/缓存 + RS256 验签 + 解析 sub/scope） | 功能1-全部 | `app/core/mcp_auth.py`(新), `tests/core/test_mcp_auth.py` | 无 |
| **T3** 日志内部 API（含 since/until 时间范围） | 功能2-日志 | `app/api/mcp_logs.py`(新), `tests/api/test_mcp_logs.py` | T5 |
| **T4** 配置内部 API（读/schema/update） | 功能2-配置 | `app/api/mcp_config.py`(新), `tests/api/test_mcp_config.py` | T5 |
| **T5** `get_mcp_client` 鉴权依赖（JWT 验签，身份动态，scope 校验） | 功能1-鉴权 | `app/api/deps.py`, `tests/api/test_mcp_auth.py` | T2 |
| **T6** main.py 挂载（lifespan 公钥读取 + 注册 /api/mcp 路由） | 功能1/2/3-部署 | `app/main.py`, `tests/test_main_mcp.py` | T2, T3, T4, T5 |
| **T7** workspace 骨架 + mcp_server HTTP 入口 + Dockerfile（pyproject/server/tools/client） | 功能3-部署、镜像构建 | 根 `pyproject.toml`（[tool.uv.workspace]）, `mcp_server/pyproject.toml`(新), `mcp_server/Dockerfile`(新), `mcp_server/src/mcp_server/server.py`(新), `tools.py`(新), `client.py`(新), `mcp_server/tests/test_mcp_server.py` | 无 |
| **T8** OAuth AS：authorize/token/register + JWT 签发（scope 默认 read write）+ `auth.enabled` 分流 + 登录复用 BS | 功能1-全部 | `mcp_server/src/mcp_server/auth.py`(新), `mcp_server/tests/test_mcp_oauth.py` | T7 |
| **T9** 用户文档：远程接入 + OpenCode/Claude 配置 + `auth.enabled` 说明 + Agent prompt | 功能3、文档 | `docs/config/mcp.md`(新), `docs/public/images/`(新配图) | T6, T7, T8 |
| **T10** 开发文档 + CI（mcp-server-ci.yml + 扩展 lint/ci-tests + 镜像构建验证） | CI 方案、镜像构建 | `docs/development/mcp.md`(新), `.github/workflows/mcp-server-ci.yml`(新), `.github/workflows/lint.yml`, `.github/workflows/ci-tests.yml` | T7, T8 |

**批量调度**（严格消除隐含依赖，每批并行度 ≤ 4）：
- **批1（并行）**：T2、T7（均无依赖）
- **批2（并行）**：T5（依赖 T2）、T8（依赖 T7）
- **批3（并行）**：T3、T4（依赖 T5）
- **批4**：T6（依赖 T2,T3,T4,T5）
- **批5（并行）**：T9（依赖 T6,T7,T8）、T10（依赖 T7,T8）

---

## 八、需要主 agent 确认的决策点

当前方案已收敛，无待确认决策点。OAuth 授权采用标准一次性流程（登录 → consent → token 生命周期自动续期），不引入额外配置开关。

---

## 九、下一步（主 agent 执行指引）

按上述 DAG 分批派发 `coder` subagent 并行执行：
1. **批1**：T2、T7 → **批2**：T5、T8 → **批3**：T3、T4 → **批4**：T6 → **批5**：T9、T10
2. 每批内并行，批间等待入度归零后再派发下一批。
3. 每个任务严格 TDD（红-绿-重构），完成后跑该模块测试 + 全量 `uv run pytest tests/`；`mcp_server/` 子包测试（py3.10）单独跑。
4. workspace 落地后 `uv lock` 生成 `uv.lock` 并提交；若 BS 运行时依赖变化，执行 `uv export --format requirements.txt --no-dev -o requirements.txt` 并提交。
5. 全部完成且测试全绿后，执行 `/roundtable` 多模型圆桌验收。