# AGENTS.md

面向 AI 编码助手与本仓库协作者的快速上下文（人类贡献流程仍以 [README.md](https://github.com/SanaeMio/Bangumi-syncer/blob/main/README.md) 与 [CONTRIBUTING.md](https://github.com/SanaeMio/Bangumi-syncer/blob/main/CONTRIBUTING.md) 为准）。

## 项目概述

Bangumi-syncer 将常见媒体库（Plex、Emby、Jellyfin、Trakt、飞牛等）的观看进度同步到 [Bangumi（番组计划）](https://bgm.tv/) 官方 API。提供基于 **FastAPI** 的 Web 管理与同步接口，默认本地访问 `http://localhost:8000`。

## 技术栈与环境

- **Python**：`>=3.10`（见 [pyproject.toml](https://github.com/SanaeMio/Bangumi-syncer/blob/main/pyproject.toml) 中 `requires-python`）。
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

## 评审沉淀：高频问题规避

- **删除/重命名公共符号前先全仓 grep**（含 `docs/`）：删 `def`/`class` 前，先列出文档/注释/测试中的引用位置，改完 grep 确认零残留。例：删除 `create_auth_server` 后 `docs/development/mcp.md` 仍引用旧名，文档与实现漂移。
- **「关闸/降级」类安全参数必须正反对照的端到端测试**：只断言 `provider.required_scopes == []` 属属性层，中间件即使忽略该属性也仍绿。凡开关安全门槛的参数，BDD 须同时覆盖「开启→拒绝」与「关闭→放行」，走完整链路、不 mock。
- **测试 helper 封装生产入口时须断言参数对象同一性**：helper 构造 provider A 却把 provider B 传给 `create_mcp_app(...)` 时测试仍绿，未测到预期配置。至少一例用 spy/monkeypatch 断言 `called_kwargs["provider"] is <helper 构造的对象>`。
- **契约字段（尤其 OAuth metadata）用精确相等断言，禁止子集断言**：断言 `"read" in scopes_supported` 在无声扩权为 `["read","write","admin"]` 时仍绿，必须写 `== ["read","write"]` 精确锁定。
- **并行 coder 共享同一 git index，提交必须限定路径**：多任务并行时，不带路径的 `git commit` 会吞掉他人已暂存内容（曾把 A 任务的 `.gitignore`/`git rm --cached` 卷入 B 任务提交），事后用 `git reset` 修复又会互相覆盖（另一任务首次提交因 HEAD 锁竞争失败）。并行环境一律 `git commit -m "..." -- <本任务路径...>`，并禁止 `git reset` / `git stash` / `git checkout` / `git add -A`。
- **`git rm --cached` 与 `git commit -- <pathspec>` 组合是陷阱**：`git commit -- <pathspec>` 是 `--only` 语义，取**工作区**内容生成提交；而 `git rm --cached` 只删索引、工作区文件完好，该删除会被判定「无变化」——**既不进提交，还会清空已暂存的删除**（症状：`git status` 突然变干净，误以为成功）。停止追踪类改动须用独立索引：`GIT_INDEX_FILE=$(mktemp) git read-tree HEAD && git rm --cached <path> && git commit`；若要连工作区文件一起删，直接用 `git rm -r <path>`。
- **判定「缺失/未处理」前必须落到具体行号，不凭标题或推断下结论**：曾断言 `provider.py` 缺父目录自动创建，实际 `generate_keys()` / `_write_public_key()` 早已有 `os.makedirs(...)`，该「缺陷」实为回归护栏；多条评审评论也因代码演进变成 `is_outdated`（如「注释用中文」实际早已全中文、「哪来的授权页」实际 `/consent` 页面已实现）。规则：凡「没有 / 未覆盖 / 未实现」的结论必须附 grep 或 Read 证据；评审他人评论前先验证事实层面是否已满足。
- **BDD 场景清单与测试必须逐类对照**：场景列举 N 类失败输入（如「过期/签名错误/aud 不匹配」三类 token），测试必须逐类命中且用精确断言锁定，不允许「测了两类算覆盖三类」。例：verify_jwt 的 BDD 写了三类，实际只测 expired/signature，aud 错误零覆盖，直到圆桌评审才被发现。
- **报「缺少防护/校验」前先核对实现侧既有约束**：曾把「alg 篡改无防护」报为 P1，实际 `jwt.decode(..., algorithms=["RS256"])` 已锁定算法并由 PyJWT 层防御，属误报。报缺失前必须 grep 实现中的等价约束（算法白名单、Pydantic 边界、开关等）。

## 安全与敏感信息

勿将 Bangumi Token、密码、私钥等**写入仓库**或提交到 Git。运行时密钥通过应用配置与环境管理；细节见在线文档与 CONTRIBUTING。

## PR 与提交前自检

推送或打开 PR 前建议至少执行上文 **Ruff + pytest**（若改模板则加 **djlint**）。**若改运行时依赖**，在 `uv lock` 后按上文单独一节执行 **`uv export` 并提交 `requirements.txt`**。单个 PR 尽量聚焦单一目的，便于审查与回滚。