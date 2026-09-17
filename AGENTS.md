# AGENTS.md

面向 AI 编码助手与本仓库协作者的快速上下文（人类贡献流程仍以 [README.md](https://github.com/SanaeMio/Bangumi-syncer/blob/main/README.md) 与 [CONTRIBUTING.md](https://github.com/SanaeMio/Bangumi-syncer/blob/main/CONTRIBUTING.md) 为准）。

## 项目概述

Bangumi-syncer 将常见媒体库（Plex、Emby、Jellyfin、Trakt、飞牛等）的观看进度同步到 [Bangumi（番组计划）](https://bgm.tv/) 官方 API。提供基于 **FastAPI** 的 Web 管理与同步接口，默认本地访问 `http://localhost:8000`。

## 技术栈与环境

- **Python**：`>=3.9`（见 [pyproject.toml](https://github.com/SanaeMio/Bangumi-syncer/blob/main/pyproject.toml) 中 `requires-python`）。
- **运行时**：FastAPI、Uvicorn、Jinja2、Pydantic、APScheduler 等（依赖见 `pyproject.toml`）。
- **包管理**：推荐 [uv](https://docs.astral.sh/uv/)。

## 仓库地图

| 路径 | 说明 |
| --- | --- |
| `app/` | 应用代码：`api/` 路由、`services/` 业务与调度、`core/` 配置与基础设施、`models/`、`utils/` |
| `app/main.py` | FastAPI 应用入口，`uvicorn app.main:app` |
| `tests/` | 单元测试与 API 测试 |
| `docs/` | 用户文档（VitePress），配图在 `docs/public/images/` |
| `templates/`、`static/` | Jinja 模板与静态资源 |
| `Dockerfile`、`entrypoint.sh` | 容器构建与启动 |
| `.github/workflows/` | CI：如 `lint.yml`、`ci-tests.yml`、Docker 相关工作流、`docs.yml` |

## 安装、构建与常用命令

在项目根目录执行：

```bash
uv sync --group dev
```

```bash
# 代码风格与静态检查（CI 中 lint 工作流使用 ruff format --check）
uv run ruff check .
uv run ruff format .

# 仅当修改了 Jinja 模板时
uv run djlint templates/ --reformat

# 单元测试与覆盖率
uv run pytest tests/ --cov=app --cov-report=term
```

**仅当修改了 `pyproject.toml` 中的运行时依赖时**：在 `uv lock` 之后执行下面命令生成根目录 `requirements.txt` 并一并提交，避免与 README、用户文档里的 `pip install -r requirements.txt` 不同步。

```bash
uv export --format requirements.txt --no-dev -o requirements.txt
```

本地启动 Web 服务（等价于仓库内 `start.bat` / 镜像内 uvicorn 目标模块）：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

更完整的说明（Fork、PR、Docker 集成测试路径等）见 [CONTRIBUTING.md](https://github.com/SanaeMio/Bangumi-syncer/blob/main/CONTRIBUTING.md)。

## 测试说明

- 行为或逻辑变更应**尽量附带或更新** `tests/` 下相关用例；难以覆盖时请在 PR 中说明，并优先用 Mock 等保持稳定。
- CI：`ci-tests` 工作流运行 `pytest tests/` 并上传覆盖率；`lint` 工作流负责 Ruff；模板由 djLint 单独检查。
- 若改动 **Dockerfile、entrypoint、镜像内权限或启动方式**，请关注 CONTRIBUTING 中提到的集成脚本 `tests/integration/test_docker_perms.sh` 及 Docker 相关 workflow。

## 代码风格与架构约定

- **风格**：以 [pyproject.toml](https://github.com/SanaeMio/Bangumi-syncer/blob/main/pyproject.toml) 中 **Ruff**、**djlint** 配置为准，勿在本文重复粘贴规则全文。
- **分层**：新 HTTP 接口放在 `app/api/`；复杂业务放在 `app/services/`（可参考现有 `sync_service`、`mapping_service` 及各媒体子包）。体量小的只读端点可参考 [`app/api/health.py`](https://github.com/SanaeMio/Bangumi-syncer/blob/main/app/api/health.py) 的组织方式。
- **文档**：配置项或面向用户的行为变更需同步更新 `docs/` 中对应 Markdown；图片放在 `docs/public/images/`，文内用根路径如 `![](/images/overview/xxx.png)`（见 CONTRIBUTING）。
- **协作**：与邻近文件保持一致的命名与注释习惯；避免无关大范围格式化或重命名。

## 安全与敏感信息

勿将 Bangumi Token、密码、私钥等**写入仓库**或提交到 Git。运行时密钥通过应用配置与环境管理；细节见在线文档与 CONTRIBUTING。

## PR 与提交前自检

推送或打开 PR 前建议至少执行上文 **Ruff + pytest**（若改模板则加 **djlint**）。**若改运行时依赖**，在 `uv lock` 后按上文单独一节执行 **`uv export` 并提交 `requirements.txt`**。单个 PR 尽量聚焦单一目的，便于审查与回滚。

## 评审复盘沉淀（2026-09-09，思考强度配置拆分）

圆桌验收高频问题与规避规则，规划与编码时直接遵守：

- **配置类参数函数禁用默认值兜底**：带默认值参数会掩盖"调用方必须显式传配置值"的契约。反面案例：`_build_default_chat_fn(thinking_level="medium")` 导致 `_continue_replay` 漏传后静默回退 medium（P0，见 `remain/llm_match_replay_thinking_level.md`）。配置值一律必填，由调用方显式传入。
- **删除全局配置项时必须全链路排查间接消费点**：不只删 schema/API/UI，还要 grep 所有消费点（含 replay/恢复等次要路径、`config.example.ini`、specs/ 手工测试文档）。本次 replay 路径正是因此遗漏。
- **配置值解析统一归一化**：从 ini/用户输入读取枚举值，先 `str(v).strip().lower()` 再校验，回落默认值；禁止在同一语义上出现"一层归一、一层严格比对"的分层不一致。
- **测试断言禁止恒真写法**：`assert not hasattr(x, "f") or x.f is None` 这类 `or` 后半句恒真；对"字段已删除"应断言 `f not in Model.model_fields`。验证传参用 monkeypatch 捕获实参，不要只调被测函数内部的 helper 间接断言。
- **多任务并行改同一文件需在规划期建依赖边**：本轮 `templates/config.html`、`app/core/config.py` 均因先串行后才避免了写冲突。

## 评审复盘沉淀（2026-09-11，trace/replay 单表双职责重构）

圆桌验收高频问题与规避规则，规划与编码时直接遵守：

- **遥测/记录职责迁移必须枚举全部调用路径**：把记录逻辑从共享循环收敛到包装层时，逐条检查正常、恢复/续跑、重试等路径，并为每条路径断言产物落库。反面案例：span 记录移出 loop 后 `_continue_replay` 两处漏接 `wrap_chat_fn`/`recorder=`，续跑零 trace、二次崩溃重放重执行写工具（P0，`app/services/llm_match_scheduler.py`）。
- **跨组件共享的轮次状态必须在轮次开始时推进**：共享的"当前轮"（iteration）在轮次开始设定、供同轮其他组件（tool span）读取，禁止在 `finally`/轮次结束时提前推进。反面案例：`wrap_chat_fn` 在 finally 中 `_next_iteration += 1`，同轮 tool span iteration 偏移 1，replay 按 `(iteration, sequence)` 分组错乱（P0）。
- **读改写路径失败时必须保留原值**：`SELECT→改→UPDATE` 的解析/解密失败分支必须原样保留 raw 并记 warning，禁止用空对象重建覆盖。反面案例：`record_budget_message` 解析失败以 `{}` 覆盖，丢 tool_result 致重放重执行写工具（P1）。
- **测试名与断言一一对应**：测试名/docstring 声称的每个对象都必须在断言中出现；mock 场景无法产生的对象（如 end_turn 无 tool span）不得写进测试名。反面案例：`test_recovery_path_writes_chat_and_tool_spans` 实际只断言 chat span（判弱断言后重命名）。
- **新增枚举型配置项必须复用归一化 helper**：读取枚举配置走单一归一化入口（`ConfigManager._normalize_thinking_level`），配大小写/空格/非法值参数化用例；消费侧禁止 `or "medium"` 冗余兜底。反面案例：`llm_match_thinking_level` 未归一化，`HIGH` 在 Anthropic 静默关闭思考（P1）。

## 评审复盘沉淀（2026-09-13，LLM 匹配业务键与失败语义）

圆桌验收高频问题与规避规则，规划与编码时直接遵守：

- **去重/重试键必须用稳定业务键，禁止用每次新生成的记录 id 做历史匹配**：每次新生成的 `sync_record_id` 是单调递增的，用它查 `agent_runs` 做 dedup/requeue 永远 miss。反面案例：`_enqueue_match_assist_run` 用刚 INSERT 的 `sync_record_id` 查 `agent_runs` 决定在途/重入队，导致同一剧集反复新建 run、失败无法重入（P0）。正确做法：用 `business_key`（`{task_type}|{user}|{normalized_title}|{season}`）作为身份，来源（`source`/`retry-*`）不参与。
- **外部依赖失败不得折叠为业务结论**：LLM client 重试耗尽返回空响应时，不得当作 `end_turn` 处理为 `no_suggestion`（丢失故障信号、误判为无建议）。反面案例：client 重试耗尽空响应 → 当作 `end_turn` → `no_suggestion`，后续调度器看不到失败、不再重试（P0）。正确做法：client 层抛 `LLMCallError(retryable=...)`，`llm_assist.run` 按 `retryable` 分流：`False` → 立即 `mark_failed(stop_reason='llm_error')`；`True` → `increment_attempts`，达 3 次转 `failed`。
- **生产代码禁止跨层直接赋值私有属性**：调度器跨层直接写 `span_recorder._next_iteration = ...` 绕过公开方法，破坏封装且易在恢复路径遗漏同步。反面案例：scheduler `span_recorder._next_iteration = ...`，恢复续跑时 `begin_replayed_round` 已设 `_next_iteration = iteration + 1`，两侧不一致导致 tool span iteration 错乱（P1）。正确做法：所有状态推进走公开方法（`begin_replayed_round` / `wrap_chat_fn`），禁止外部直接赋值 `_` 前缀属性。

## 评审复盘沉淀（2026-09-17，agent 匹配增强第三轮验收）

圆桌验收高频问题与规避规则，规划与编码时直接遵守：

- **限速/等待类原语必须"锁内检查、锁外休眠"**：持锁休眠会阻塞通知路径，使自适应机制失效（如 429 冻结延长无法生效、并发请求被串行拖垮）。反面案例：`RateLimiter.acquire()` 在 `with self._cond` 内 `time.sleep`，`notify_rate_limited` 无法更新 `_frozen_until`（P1，`app/utils/bangumi_api/rate_limit.py`）。正确做法：锁内只做状态检查与令牌扣减，休眠在锁外执行，醒来后重新进锁复查。
- **状态流转 SQL 必须带源状态守卫（`WHERE status IN (...)`）**：无守卫的 UPDATE 可改写终态行并导致计数双计。反面案例：`mark_failed` 无守卫，同一 run 可被重复置 failed 且 `total_attempts` 多计（P2，`app/core/database/agent_runs.py`）。正确做法：所有状态流转 WHERE 限定源状态集合，rowcount=0 返回 False。
- **策略/配置类参数禁止默认值兜底，由调用方显式传具名常量**：默认值会掩盖"调用方必须显式决策"的契约，漏传时静默回退到错误语义。反面案例：`enqueue_match_run(accepted_mapping_valid=True)` 漏传时把已删除映射误判为有效、无限期复用（P2）。正确做法：策略参数无默认值 + 调用方传具名常量（如 `MATCH_ASSIST_REUSE_WINDOW_DAYS = 30`），漏传即 `TypeError`。
- **函数内 import 全部提升到模块头部，AST 白名单测试守卫**：函数内 import 使 mock patch 路径不稳定、绕过导入环检测。反面案例：`llm_assist.py` 函数内 `get_llm_client` 导致 `patch("app.services.matching.llm_assist.get_llm_client")` 无法命中（P2）。正确做法：import 提头部；确有豁免需求（避免导入环）时用 AST 白名单测试登记（白名单为空 = 全禁），新豁免必须显式登记。
- **跨进程重入需 DB 级 CAS 抢占，进程内集合仅作单进程防护**：模块级 `set` 防不住多实例/多进程同轮双跑；恢复类操作应以读取到的状态值做 CAS（`UPDATE ... WHERE started_at=?`），仅一个执行者成功。反面案例：两进程同轮 `list_stale_processing` 各自恢复同一 run（P2）。配套：进程级共享状态（模块级集合/单例）必须在 `tests/conftest.py` 用 autouse fixture 前后清理，防跨测试污染。