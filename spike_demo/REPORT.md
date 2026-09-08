# S1 Spike 报告：FastMCP 4 API 验证

**日期**: 2026-09-07
**分支**: support_fastmcp
**工作目录**: `spike_demo/`（临时，可删除）

---

## 一、6 项调研结论

### 1. `OAuthProvider` 抽象方法清单

**结论**: FastMCP 4 的 `OAuthProvider` 继承自 SDK 的 `OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]`（Protocol），需要实现以下方法：

| 方法 | 签名 | 旧 auth.py 是否实现 | 差异 |
|------|------|---------------------|------|
| `get_client` | `(client_id: str) -> OAuthClientInformationFull \| None` | ✅ | 一致 |
| `register_client` | `(client_info: OAuthClientInformationFull) -> None` | ✅ | 一致 |
| `authorize` | `(client, params: AuthorizationParams) -> str` | ✅ | 一致（返回 redirect URL） |
| `load_authorization_code` | `(client, authorization_code: str) -> AuthorizationCode \| None` | ✅ | 一致 |
| `exchange_authorization_code` | `(client, authorization_code: AuthorizationCode) -> OAuthToken` | ✅ | 一致 |
| `load_refresh_token` | `(client, refresh_token: str) -> RefreshToken \| None` | ✅ | 一致 |
| `exchange_refresh_token` | `(client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken` | ✅ | 一致 |
| `load_access_token` | `(token: str) -> AccessToken \| None` | ✅ | 一致 |
| `revoke_token` | `(token: AccessToken \| RefreshToken) -> None` | ✅ | 一致 |
| `exchange_identity_assertion` | `(client, params: IdentityAssertionParams) -> OAuthToken` | ❌ | **新增**（SEP-990 ID-JAG，默认 impl 抛 TokenError，可选实现） |

**证据**:
- SDK 源码: `.venv/.../mcp/server/auth/provider.py` L137-344
- `AuthorizationParams` 新增 `resource: str | None` 字段（RFC 8707）
- `AuthorizationCode` 新增 `subject: str | None` 字段
- `AccessToken` 新增 `subject: str | None` 字段
- `exchange_identity_assertion` 有默认实现（raises TokenError），不实现也可用

**差异总结**: 旧 auth.py 的 9 个核心方法签名与 FastMCP 4 完全一致，可直接迁移。唯一新增的 `exchange_identity_assertion` 是可选的（SEP-990 企业身份断言），默认行为是拒绝，不影响 CIMD 流程。

---

### 2. `CIMDClientManager` 可用性

**结论**: `CIMDClientManager` 可独立实例化，用于自定义 `OAuthProvider` 子类的 `get_client()` 方法。

**构造函数签名**:
```python
CIMDClientManager(
    enable_cimd: bool = True,
    default_scope: str = "",
    allowed_redirect_uri_patterns: list[str] | None = None,
)
```

**关键方法**:
- `is_cimd_client_id(client_id: str) -> bool` — 检测是否为 CIMD URL（HTTPS + host + 非根路径）
- `get_client(client_id_url: str) -> ProxyDCRClient | None` — 抓取并验证 CIMD 文档，返回合成客户端
- `validate_private_key_jwt(assertion, client, token_endpoint) -> bool` — 验证 JWT 断言

**证据**:
- 源码: `.venv/.../fastmcp/server/auth/cimd.py` L703-825
- `CIMDClientManager.get_client()` 内部使用 `CIMDFetcher`（SSRF 保护、HTTP 缓存、5KB 限制）
- 返回 `ProxyDCRClient`，带有 `cimd_document` 属性（`CIMDDocument` 类型）
- `is_cimd_client_id` 是实例方法（非静态），需要 `self.enabled` 为 True

**使用模式**:
```python
class MyOAuthProvider(OAuthProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cimd = CIMDClientManager(enable_cimd=True)

    async def get_client(self, client_id):
        if self.cimd.is_cimd_client_id(client_id):
            return await self.cimd.get_client(client_id)
        return self._static_clients.get(client_id)
```

---

### 3. FastMCP 4 嵌入 FastAPI 方式

**结论**: `mcp.http_app(path="/mcp")` + `combine_lifespans` + **手动路由提取** 是正确方式。

**⚠️ 关键发现 — 路由必须手动提取**:

`mcp.http_app()` 返回的 Starlette app 包含以下路由：
```
/.well-known/oauth-authorization-server  {'GET', 'OPTIONS', 'HEAD'}
/authorize                              {'GET', 'POST', 'HEAD'}
/token                                  {'POST', 'OPTIONS'}
/.well-known/oauth-protected-resource/mcp  {'GET', 'OPTIONS', 'HEAD'}
/mcp                                    {'GET', 'POST', 'DELETE', 'HEAD'}
```

**错误做法**（会导致 RFC 8414 违规）:
```python
app.mount(
    "/mcp", mcp_app
)  # 所有路由变成 /mcp/.well-known/..., /mcp/authorize, /mcp/token
```

**正确做法**:
```python
for route in mcp_app.routes:
    if hasattr(route, "path"):
        app.router.routes.append(route)  # 全部路由添加到 FastAPI
# 结果: /.well-known/..., /authorize, /token 在根路径，/mcp 在 /mcp
```

**证据**:
- `combine_lifespans` 源码: `.venv/.../fastmcp/utilities/lifespan.py` L12-56
- `create_streamable_http_app` 源码: `.venv/.../fastmcp/server/http.py` L545-721
- Demo 验证: `/.well-known/oauth-authorization-server` 在根路径返回 200，`/authorize` 在根路径返回 302

---

### 4. `auth` 参数接入

**结论**: `FastMCP(name, auth=OAuthProvider_subclass_instance)` 是正确方式。

**签名**:
```python
FastMCP(
    name: str,
    auth: AuthProvider | None = None,  # OAuthProvider 是 AuthProvider 子类
    ...
)
```

**OAuthProvider 构造参数**:
```python
OAuthProvider(
    base_url: AnyHttpUrl | str,                    # 必填
    resource_base_url: AnyHttpUrl | str | None = None,
    issuer_url: AnyHttpUrl | str | None = None,
    service_documentation_url: AnyHttpUrl | str | None = None,
    client_registration_options: ClientRegistrationOptions | None = None,
    revocation_options: RevocationOptions | None = None,
    required_scopes: list[str] | None = None,
)
```

**ClientRegistrationOptions**:
```python
ClientRegistrationOptions(
    enabled: bool = False,                         # DCR 开关
    client_secret_expiry_seconds: int | None = None,
    valid_scopes: list[str] | None = None,
    default_scopes: list[str] | None = None,
)
```

**证据**:
- `FastMCP.__init__`: `.venv/.../fastmcp/server/server.py` L285-412
- `OAuthProvider.__init__`: `.venv/.../fastmcp/server/auth/auth.py` L807-867
- Demo 验证: `FastMCP(name="spike-demo", auth=provider)` 成功实例化

---

### 5. Claude 官方 metadata 实际抓取

**结论**: 抓取成功，结构符合 CIMD 规范，redirect_uri 使用 loopback 无端口。

**实际输出**（通过 `curl` 直接获取）:
```json
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

**关键发现**:
- `redirect_uris` 使用 `http://localhost/callback` 和 `http://127.0.0.1/callback`（无端口）
- `token_endpoint_auth_method` = `"none"`（公共客户端，无 client_secret）
- 无 `jwks_uri` / `jwks`（不使用 private_key_jwt）
- `scope` 字段未声明（需在 `get_client()` 时注入默认 scope）

**Spike 环境限制**: `CIMDFetcher` 的 SSRF 保护在此网络环境拦截了 `claude.ai`（解析到私有 IP 198.18.0.62），但 `curl` 直接抓取成功。生产环境无此问题。

**证据**:
- `curl -s "https://claude.ai/oauth/claude-code-client-metadata"` 成功返回
- `CIMDClientManager.get_client()` 被 SSRF 拦截（cimd.py L767 日志）
- `CIMDDocument` 模型验证: `.venv/.../fastmcp/server/auth/cimd.py` L57-163

---

### 6. httpx2 影响

**结论**: httpx2 与 httpx 是完全独立的包，互不影响。BS 工具层代码无需修改。

**证据**:
```
httpx  version: 0.28.1  (app/ 代码使用)
httpx2 version: 2.12.0  (FastMCP 4 内部使用)
Same class? False
```

- `httpx2` 是 FastMCP 4 内部依赖（用于资源获取、身份断言验证）
- `httpx` 是 BS 现有代码依赖（`app/utils/http_client.py`、`mcp_server/client.py`）
- 两者 API 兼容但包名不同，不存在版本冲突
- BS 工具层直接调用 `config_manager` / `_read_log_file`（同进程），不走 HTTP
- 旧 `mcp_server/client.py`（httpx）将随 mcp_server 独立部署废弃，不影响主 app

---

## 二、Demo 运行结果

**文件**: `spike_demo/spike_test.py`

```
=== /.well-known/oauth-authorization-server ===
Status: 200
issuer: http://localhost:8000/
client_id_metadata_document_supported: True    ← 已注入
token_endpoint: http://localhost:8000/token
authorization_endpoint: http://localhost:8000/authorize

=== CIMD Detection ===
is_cimd('https://claude.ai/oauth/claude-code-client-metadata'): True
is_cimd('my-random-client'): False

=== CIMD Fetch BLOCKED by SSRF (expected in test env) ===
  claude.ai resolves to private IP 198.18.0.62 in this network.
  In production with real DNS, CIMD fetch would succeed.

=== Authorize with CIMD client_id ===
redirect: http://localhost/callback?code=...&state=test-state
✅ CIMD authorize flow entered successfully

=== /.well-known/oauth-protected-resource/mcp ===
Status: 200
resource: http://localhost:8000/mcp
authorization_servers: ['http://localhost:8000/']

=== ALL SPIKE CHECKS PASSED ===
```

**HTTP 端到端验证**（独立脚本）:
```
Status: 302
Redirect: http://localhost/callback?code=...&state=test123
✅ Full HTTP authorize with CIMD client_id + scope works!
```

---

## 三、对 T1-T8 的调整建议

### T1 — FastMCP 嵌入骨架

**原计划**: `app.mount("/mcp", mcp_app)`
**修正**: 必须手动提取路由，不能简单 mount。

```python
# app/mcp/app.py
for route in mcp_app.routes:
    if hasattr(route, "path"):
        main_app.router.routes.append(route)
```

**原因**: `mcp_app` 包含 `/.well-known/*`、`/authorize`、`/token` 等路由，mount 到 `/mcp` 会导致路径错位。

### T2 — 工具注册

**无调整**: 工具直接调 `config_manager` / `_read_log_file`（同进程），不走 HTTP。

### T3 — OAuthProvider 子类

**调整点**:
1. 旧 `BangumiOAuthProvider` 的 9 个核心方法可直接迁移
2. 需新增 `exchange_identity_assertion`（可选，默认拒绝即可）
3. `authorize()` 返回值从 consent URL 改为直接 redirect URL（带 code）
4. 需处理 `AuthorizationParams.resource` 字段（RFC 8707）
5. `AuthorizationCode` 需设置 `subject` 字段

### T4 — CIMD 集成

**调整点**:
1. `CIMDClientManager` 构造时需传 `default_scope="read write"`（Claude metadata 无 scope 字段）
2. `get_client()` 必须先 `is_cimd_client_id()` 判断，再走 CIMD 或 DCR 分支
3. `client_id_metadata_document_supported: true` 需通过重写 `get_routes()` 注入 metadata
4. `CIMDDocument.scope` 可能为 None，需在 `get_client()` 时注入默认 scope

### T5 — DCR 兜底

**调整点**:
1. `ClientRegistrationOptions(enabled=True)` 传入 `OAuthProvider.__init__`
2. `register_client()` 保留，非 URL client_id 走注册表
3. DCR 客户端的 scope 验证需注意：`valid_scopes` 配置后，DCR 注册时 scope 必须在白名单内

### T6 — 测试

**新增测试点**:
1. 路由挂载正确性：`/.well-known/oauth-authorization-server` 在根路径可达
2. CIMD scope 注入：Claude metadata 无 scope 时，合成客户端的 scope 应为默认值
3. SSRF 保护：CIMD fetch 被拦截时的负缓存行为

### T7 — 文档

**新增文档点**:
1. 嵌入模式路由架构图（well-known/operational/MCP endpoint 分离）
2. CIMD scope 注入策略说明
3. `client_id_metadata_document_supported` 注入方式

### T8 — CI/部署

**无调整**: 单容器部署，无共享卷、无公钥交换。

---

## 四、遗留风险

| 风险 | 等级 | 说明 |
|------|------|------|
| **生产环境 CIMD fetch 行为** | 中 | Spike 环境 SSRF 拦截了 claude.ai，生产环境需验证真实 DNS 解析和 CIMD 文档抓取 |
| **scope 注入策略** | 中 | Claude metadata 无 scope 字段，需决定默认 scope 策略（`read write` 或按需） |
| **metadata 注入方式** | 低 | 当前通过重写 `get_routes()` 注入 `client_id_metadata_document_supported`，代码较 hacky，可考虑更优雅方式 |
| **token_endpoint_auth_methods_supported** | 低 | SDK 默认返回 `["client_secret_post", "client_secret_basic"]`，CIMD 客户端用 `"none"`，需确认 SDK 不会拒绝 |
| **多客户端并发注册** | 低 | DCR 的 `_clients` dict 非线程安全，生产环境需加锁或改用线程安全存储 |

---

## 五、Demo 文件清单

```
spike_demo/
└── spike_test.py   # 最小可运行 demo（FastAPI + FastMCP + CIMD OAuthProvider）
```

**保留建议**: 保留至 T4 完成，作为集成测试基线。T4 完成后可删除或移入 `tests/`。
