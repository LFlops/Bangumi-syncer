# FastMCP 嵌入 BS + Python 3.10 升级 —— 执行计划（support_fastmcp 分支）

## 一、架构决策（已确认）

| 决策 | 结论 |
|------|------|
| **部署形态** | **A 方案：FastMCP 嵌入 BS**（mount 进 FastAPI app），不做 sidecar |
| **Python 版本** | BS 从 **3.9 升级到 3.10**（3.9 已 EOL；FastMCP 4 要求 ≥3.10） |
| **MCP 框架** | **FastMCP >= 4.0.3**（最新稳定，从零构建直接用 4，避免二次 breaking） |
| **认证组件** | `OAuthProvider` 抽象类（native 自建 AS）+ 官方 `CIMDClientManager`（复用，不自造） |
| **客户端注册** | **CIMD 为主**（Claude Code/Desktop、ChatGPT、VS Code 官方托管 URL）+ **DCR 兜底**（Codex/OpenCode 未支持 CIMD） |
| **认证链** | 同进程 token 校验（**省掉共享卷公钥交换、HTTP 层、BSClient**）；登录复用 BS `security_manager` |

## 二、阶段划分

```
阶段 1：Python 3.10 升级（U1-U3）──▶ 全绿闸门
阶段 2：FastMCP 嵌入 + CIMD（S1 Spike → T1-T7）
```

**阶段 1 必须先全绿**（Starlette 1.x 兼容性是最大不确定），再进入阶段 2。

---

## 阶段 1：Python 3.9 → 3.10 升级

### U1 依赖升级

| 项 | 现状 | 目标 |
|----|------|------|
| `requires-python` | `>=3.9` | `>=3.10` |
| `fastapi` | `>=0.126.0` | `>=0.133.0`（首个兼容 Starlette 1.x） |
| `starlette`（间接） | <0.47 | 1.x（**最大不确定点**） |
| `fastmcp` | 无 | `>=4.0.3` |
| `sse-starlette` | `>=2.0.0` | 验证与 Starlette 1.x 兼容，必要时升级 |
| `pydantic` | `>=2.12.5` | 不变（已满足 FastMCP 4 要求）✅ |
| ruff `target-version` | `py39` | `py310` |

产出：`pyproject.toml`、重新 `uv lock` + `uv export -o requirements.txt`

### U2 环境升级

| 项 | 现状 | 目标 |
|----|------|------|
| `Dockerfile` | `python:3.9-slim-bookworm` ×2 | `python:3.10-slim-bookworm` |
| `.python-version` | `3.9` | `3.10` |
| `ci-tests.yml` BS job | `uv python install 3.9` | `3.10` |
| `e2e-tests.yml` | `uv python install 3.9` | `3.10` |

### U3 回归验证（安全闸门）

- `uv run pytest tests/ -m 'not e2e' -q`（现 3576 用例）
- **重点验证**：Starlette 1.x 下的中间件（request_context/csp）、lifespan、Jinja2 模板、SSE 端点
- 红则逐项修，**全绿才进入阶段 2**

---

## 阶段 2：FastMCP 嵌入 + CIMD（TDD）

### S1 Spike（前置，必须）

验证 FastMCP 4 实际 API，回答 5 个风险点：
1. `OAuthProvider` 抽象方法签名（与我们现有 auth.py 的 10 方法一致性）
2. `CIMDClientManager` 直接用于 OAuthProvider 子类的可行性（还是需独立实例化）
3. FastMCP 4 `auth=` 参数接入方式 + `http_app()` mount 进 BS FastAPI 的方式（`combine_lifespans`）
4. Claude 官方 metadata（redirect_uri 无端口）实际匹配行为
5. httpx2 对现有代码的影响（BSClient 内部 HTTP 是否受影响）

产出：最小 demo（FastMCP mount 进 FastAPI + Claude 官方 URL 走通 authorize→token）+ 调研结论

### BDD 测试用例（与测试一一对应）

```gherkin
功能: CIMD 客户端授权
  场景 1.1: Claude Code 官方 URL 授权全流程
    给定 client_id = "https://claude.ai/oauth/claude-code-client-metadata"
    当 authorize → 登录 → consent → token
    那么 识别 URL、抓取官方文档校验、redirect_uri 匹配（loopback 端口灵活）、签发 JWT
  场景 1.2: VS Code 官方 URL（https://vscode.dev/oauth/client-metadata.json）
  场景 1.3: metadata client_id != URL → 拒绝（invalid_client）
  场景 1.4: redirect_uri 不在白名单 → 拒绝
  场景 1.5: loopback 端口灵活性（RFC 8252 §7.3）无端口声明匹配随机端口
  场景 1.6: 非 URL client_id → 走 DCR 注册表
  场景 1.7: metadata 抓取失败（404/超时/超大小）→ 明确错误 + 负缓存
  场景 1.8: metadata 缓存生效（Cache-Control 感知）

功能: 认证分流（复用 BS）
  场景 2.1: auth.enabled=true → 登录复用 BS（同进程 security_manager）
  场景 2.2: auth.enabled=false → 跳过登录，身份=BS 会话用户，consent 保留
  场景 2.3: consent 拒绝 → 不签发 code

功能: DCR 兜底
  场景 3.1: DCR 客户端（非 URL）授权正常
  场景 3.2: CIMD 与 DCR 共存互不影响

功能: 工具与认证链
  场景 4.1: 3 工具注册（get_logs/get_current_config/update_config）
  场景 4.2: 同进程 token 校验（无公钥交换、无 HTTP 层）
  场景 4.3: token 过期与 refresh
  场景 4.4: scope 越权（read 调 update_config → 403）

功能: 部署
  场景 5.1: /.well-known/oauth-authorization-server 含 client_id_metadata_document_supported: true
  场景 5.2: 单容器部署（无共享卷、无公钥交换）
  场景 5.3: Dockerfile 可构建

功能: private_key_jwt（可选增强，暂缓）
  场景 6.1: ChatGPT 私钥断言验证（jwks_uri）——标记暂缓
```

### 任务拆解（TDD，红-绿-重构）

| 任务 | 内容 | 涉及文件 | 依赖 |
|------|------|----------|------|
| **T1** FastMCP 嵌入骨架：`FastMCP` 实例 + `http_app()` + `app.mount("/mcp", ...)` + `combine_lifespans` | `app/main.py`, `app/mcp/`(新) | S1 |
| **T2** 3 工具注册：get_logs/get_current_config/update_config（直接调 config_manager/_read_log_file，**不再走 HTTP**） | `app/mcp/tools.py`(新) | T1 |
| **T3** `OAuthProvider` 子类：实现抽象方法（BS 登录复用、consent/CSRF、JWT RS256 签发、refresh、token 校验）——迁移现有 auth.py 逻辑 | `app/mcp/auth.py`(新) | T2 |
| **T4** CIMD 集成：`get_client()` 识别 URL client_id → 官方 `CIMDClientManager`；广告 `client_id_metadata_document_supported`；loopback 端口灵活 | `app/mcp/auth.py` | T3 |
| **T5** DCR 兜底：`register_client` 保留，非 URL client_id 走注册表 | `app/mcp/auth.py` | T4 |
| **T6** 测试全量：BDD 用例全覆盖 + 原 mcp_server 测试迁移 | `tests/` | T5 |
| **T7** 文档：docs 更新（FastMCP 嵌入架构、CIMD 接入、客户端兼容矩阵、Python 3.10 要求） | `docs/` | T4 |
| **T8** CI/部署收尾：撤销 mcp_server 独立 job、Dockerfile 集成 mcp 路由、单容器验证 | `.github/workflows/`, `Dockerfile` | T6 |

**调度**：S1→T1→T2→T3→T4→T5→T6 串行（同文件强耦合）；T7 依赖 T4 可并行；T8 收尾。

## 三、风险与前置确认

1. **Starlette 1.x** 是阶段 1 最大不确定（中间件/lifespan/模板/SSE）——U3 全量测试兜底
2. FastMCP 4 `OAuthProvider` + `CIMDClientManager` 组合的官方支持度——S1 Spike 确认
3. 嵌入后 `/api/mcp/*` 内部 API 的存废：工具直接调业务层后，`/api/mcp/*` HTTP 接口是否保留（供外部调用）还是废弃——T2 时决策
4. CORS 中间件与 OAuth 路由冲突（FastMCP 文档提醒）——嵌入时注意

## 四、验证命令

```bash
# 阶段 1 闸门
uv run pytest tests/ -m 'not e2e' -q    # 全绿（3576+）
uv run ruff check . && uv run ruff format --check .

# 阶段 2 每任务
uv run pytest tests/ -q                  # 含新增 MCP 用例
```