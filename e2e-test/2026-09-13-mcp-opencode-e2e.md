# 2026-09-13 · OpenCode + MCP 端到端实测（Docker 部署 → 从 0 到 1 → 真实授权 → 真实同步）

> 执行方式：以「用户 + planner」角色，用当前分支构建镜像、/tmp 全新目录部署，本地 OpenCode 通过 MCP 完成接入、配置与排障验证。
> 报告中不含任何敏感值。

## 一、目标与范围

1. 用**当前分支**代码构建 Docker 镜像并在全新目录部署 Bangumi-syncer（BS）
2. 通过**技能 + MCP** 让 OpenCode 从 0 到 1 完成：接入 → 只读验证 → 配置变更
3. 切换 `auth.enabled=true`，验证**真实登录授权**链路（未登录拒绝 / 登录后 consent）
4. 用**模拟 Emby webhook** 触发一次真实同步，观察同步记录

## 二、环境与方法

| 项 | 内容 |
| --- | --- |
| 镜像 | 当前分支 `Dockerfile` → `bs-e2e:local`（约 296MB） |
| 容器 | `bs-e2e`，端口 `18000:8000`（避让已有实例的 9092），挂载 `/tmp/bs-e2e/{config,data,logs}` |
| 环境变量 | `MCP_BASE_URL=http://localhost:18000`、RSA 私钥/公钥指向 `/app/data`（持久化） |
| 配置基线 | 参考 `docker_run_tmp/config` 复制；测试期间按阶段调整 `auth.enabled` |
| 账号数据 | 参考 `data/sync_records.db` 热备份导入（含 1 个激活 Bangumi 账号），`media_server_usernames` 追加测试用户 `emby-e2e` |
| OpenCode | 项目目录 `/tmp/bs-e2e/project`：`opencode.json`（remote MCP + `oauth.scope=read write`）+ 技能安装到 `.opencode/skills/bangumi-syncer/` |
| 授权方式 | headless 自动化：`opencode mcp auth` 发起 → 从服务端日志取 `request_token` → 用 curl 完成 consent（模拟浏览器操作） |

## 三、阶段一：`auth.enabled=false`，OpenCode 从 0 到 1 ✅

| 步骤 | 结果 |
| --- | --- |
| 技能加载 | `→ Skill "bangumi-syncer"`，按技能「场景 C」执行只读验证 |
| 只读验证 | `get_current_config` / `get_logs(limit=10)` 正常；报告连接正常、具备写权限 |
| 配置变更 | 计划表 → `update_config(sync.match_confidence_threshold, 0.65)` → 复读确认；第二轮改回 `0.6` 并再次复读 |
| 敏感字段 | `auth.password` / `auth.secret_key` / `bangumi_oauth.client_secret` 等均显示 `***`；全程无敏感值回显 |

### 本阶段发现并修复的 2 个 P0

| # | 问题 | 根因 | 修复 |
| --- | --- | --- | --- |
| P0-1 | **生产嵌入路径 `/mcp` 恒 401**，任何合法 Bearer 都 `invalid_token`，MCP 功能在生产完全不可用 | `app/main.py` 把 `mcp_app.routes` 摊平进 FastAPI 时未迁移 `mcp_app.user_middleware`（`RequestContextMiddleware` / `AuthenticationMiddleware` / `AuthContextMiddleware`），`RequireAuthMiddleware` 永远拿不到 `scope["user"]` | `fa661cf`：按 `reversed(user_middleware)` 迁移三层中间件；新增生产组合（`app.main.app`）Bearer 端到端回归测试（修复前 401 → 修复后 200） |
| P0-2 | **敏感字段掩码绕过**：`get_current_config` 明文返回 `bangumi_oauth.client_secret`（及 `notify_email_N.smtp_password` 等） | `get_all_config` 输出段名为下划线形态（`bangumi_oauth`），而 `is_sensitive_field` 只识别连字符形态 | `ecf869c`：`is_sensitive_field` 入口统一归一化 `_ → -`；测试改用真实下划线形态并补多实例/多账号覆盖 |

> 两个缺陷现有测试均未覆盖：前者集成测试直接测 `TestClient(mcp_app)`（带中间件），后者测试 mock 用了与真实输出不符的连字符段名。

## 四、阶段二：`auth.enabled=true`，真实授权链路 ✅

| 场景 | 结果 |
| --- | --- |
| 未登录访问受保护接口 `GET /api/config` | `401` |
| Web 登录 `POST /api/login`（测试口令写入哈希） | 成功，下发 `session_token` Cookie |
| 浏览器（未登录会话）访问 `GET /consent` | **401**（真实鉴权生效，不跳登录页） |
| 带会话访问 `GET /consent` | `200` |
| `POST /consent action=allow` | `302` → 回调 → `POST /token` `200` |
| OpenCode 授权状态 | `authenticated`；`POST /mcp` 200 / 202 |

## 五、阶段三：模拟 Emby webhook 触发真实同步 ⚠️

请求：

```bash
curl -X POST http://localhost:18000/Emby -H 'Content-Type: application/json' -d '{
  "Event": "item.markplayed",
  "User":  {"Name": "emby-e2e", "Id": "emby-user-e2e"},
  "Item":  {"Type": "Episode", "SeriesName": "藤本タツキ17-26",
            "ParentIndexNumber": 1, "IndexNumber": 1,
            "PremiereDate": "2025-01-01T00:00:00.0000000Z", "Name": "第1话"}
}'
```

响应：`{"status":"accepted","task_id":"emby_1_1789310316"}`

链路日志（已脱敏）：

```
[run:emby_1_1789310316] 同步开始: 藤本タツキ17-26 S01E01 (emby)
[run:emby_1_1789310316] 接收到同步请求：title='藤本タツキ17-26' season=1 episode=1 user='emby-e2e' source='emby'
[ERROR] Bangumi API 认证失败: access_token可能已过期（有效期1年）或无效，请更新token
[INFO]  同步结束: status=error
```

数据库落了一条记录（`sync_records`）：

| id | source | user | title | S/E | status | message |
| --- | --- | --- | --- | --- | --- | --- |
| 142 | emby | emby-e2e | 藤本タツキ17-26 | 1/1 | error | Bangumi API 认证失败: access_token可能已过期或无效，请更新token |

**结论**：

- ✅ **Webhook → 权限校验 → 标题匹配 → Bangumi API 调用 → 同步记录** 全链路已跑通；
- ❌ 参考账号的 Bangumi `access_token`（`expires_at=2026-08-26`）与 `refresh_token` 均已失效，自动刷新返回 `bgm.tv/oauth/access_token 400 Bad Request`；
- 🔜 完成"成功同步"需要**用户在 Web 端重新完成 Bangumi OAuth 授权**（浏览器操作，无法由 Agent 代做）。重新授权后重发上面的 curl 即可看到 `success` 记录。

## 六、发现汇总

| 类型 | 内容 | 状态 |
| --- | --- | --- |
| P0 | 生产嵌入 `/mcp` 恒 401（中间件丢失） | ✅ 已修复（`fa661cf`）+ 回归测试 |
| P0 | 下划线段名导致敏感字段掩码绕过 | ✅ 已修复（`ecf869c`）+ 回归测试 |
| 环境 | 参考账号 Bangumi token/refresh token 已失效 | ⏳ 待用户 Web 端重新授权 |
| 小尾巴 | 中间件迁移后 `/mcp` 的 `transport_type` 读取为 None（当前 3 个工具不依赖） | 📌 建议记入 `remain/` 备查 |

全量测试：**3667 passed**（两项修复合并后）。

## 七、证据与复现

| 内容 | 路径 |
| --- | --- |
| 构建日志 | `/tmp/bs-e2e/build/build*.log` |
| OAuth 自动化输出 | `/tmp/bs-e2e/build/auth*.out`、`cookies.txt`（含会话，勿外发） |
| OpenCode 0→1 运行日志 | `/tmp/bs-e2e/build/opencode-run.log`、`opencode-run2.log` |
| 测试配置/数据 | `/tmp/bs-e2e/config/config.ini`、`/tmp/bs-e2e/data/sync_records.db` |

清理现场：

```bash
docker rm -f bs-e2e && rm -rf /tmp/bs-e2e
cd /tmp/bs-e2e/project && opencode mcp logout bangumi-syncer   # 如目录尚在
```

## 八、后续建议

1. **继续真实同步验证**：用户在 Web 端重新授权 Bangumi 后，重发第五节的 curl，确认记录变为 `success`
2. **补测试盲区**：生产组合（`app.main.app`）的 MCP 鉴权回归已补；建议后续为新加入的 MCP 工具沿用同一测试形态
3. **`transport_type` 小尾巴**：如未来工具有依赖，需要在主 app 显式设置 `app.state.transport_type`
