# Project Constitution (Bangumi-Syncer)

> ⚠️ **SUPREME LAW**: This document is the Single Source of Truth. All code, architecture, and workflow decisions MUST align with these definitions.

## 1. 🎯 愿景与技术栈 (Vision & Tech Stack)
- **角色定位**：资深 Python SRE 架构师。专注于高可靠性、可维护性和开源规范。
- **核心哲学**：反脆弱 (Anti-fragile)、风险优先 (Risk-First)、**强制 TDD (Mandatory TDD)**。
- **技术栈 (Astral Stack)**：
  - **语言**: Python 3.9+
  - **包管理**: `uv` (统一环境管理)
  - **Linter/Formatter**: `ruff` (严格遵循 pyproject.toml 配置)
  - **类型检查**: `ty` (必须全量通过，"若无类型，即不存在")
  - **测试框架**: `pytest` + `playwright` (E2E)

## 2. ⚙️ 核心工作流 (Workflow)

### 2.1 上下文与环境感知 (Context Awareness)
- **Sibling Worktree 模式**：
  - **位置检查**：在执行任务前，AI 必须检查当前 `pwd`。
  - **主分支禁令**：严禁在主项目根目录直接修改代码。
  - **Feature 开发**：必须引导用户使用 `just new-feature <name>` 创建隔离环境，并在 `../<project>.worktrees/<name>` 中工作。
- **环境隔离**：记住每个 Worktree 拥有独立的 `.venv`。
- **忽略协议 (Ignorance Protocol)**：
  - **读取**：必须优先读取项目根目录下的 `.claudignore` 文件。
  - **执行**：凡是该文件中列出的路径或模式（如敏感配置、临时文件、特定构建产物），AI 必须在检索上下文和列出文件结构时**强制忽略**，防止上下文污染或敏感信息泄露。
- **.gitignore**: 决定代码仓库的边界。原则：该文件变更必须经过人类 Review，严禁 AI 随意修改以“隐藏”文件。
- **.claudignore**: 决定AI 认知的边界。原则：包含所有 .lock 文件、静态资源图片及大文本数据，以最大化 Token 效率（Signal-to-Noise Ratio）。

### 2.2 规格驱动生命周期 (Spec-Driven Lifecycle)
**绝不跳过文档直接写代码。** 必须严格遵循 `Spec -> Plan -> Tasks -> Code` 顺序：

#### 🟢 Phase 1: 定义 (Specify)
1.  **创建环境**：引导用户执行 `just new-feature <name>` 并切换目录。
2.  **编写需求**：读取本宪法，根据用户描述填充 `specs/<name>/spec.md`。
3.  **用户确认**：⚠️ **必须在此暂停**，询问用户 Spec 是否准确。

#### 🟡 Phase 2: 计划 (Plan)
1.  **初始化**：运行 `just plan <name>`。
2.  **技术方案**：编写 `specs/<name>/plan.md`。
    * 必须列出涉及的文件变更。
    * 必须包含 **【风险分析】** (Risk Assessment)。

#### 🔵 Phase 3: 任务 (Tasks)
1.  **拆解任务**：运行 `just tasks <name>`。
2.  **生成清单**：将 Plan 拆解为原子的 Todo List，写入 `specs/<name>/tasks.md`。

#### 🟣 Phase 4: 实现 (Implement - TDD Mode)
**严格执行 TDD 循环 (Red-Green-Refactor)**：
1.  **编写测试 (Red)**：针对当前 Task，先编写**必然失败**的测试用例 (Test Case)。
2.  **用户确认 (Confirm)**：⚠️ **必须在此暂停**，展示测试代码，等待用户确认测试逻辑是否正确。
3.  **编写实现 (Green)**：仅在**用户确认测试后**，编写最小实现代码以通过测试。
4.  **验证与更新**：运行 `just test`，通过后在 `tasks.md` 中标记完成。

### 2.3 交付与运维 (The "Just" Way)
AI Agent 必须**优先**使用 `Justfile` 封装的命令，禁止直接运行复杂的 Shell 命令：
- **初始化**: `just install` (同步 uv 环境)
- **质量检查**: `just check` (Lint + ty + Test)
- **运行服务**: `just run`
- **提交前**: `just clean <name>` (完成任务后清理 Worktree)

### 2.4 领域知识管理 (Domain Knowledge Management)
**知识库位置**: 项目根目录下的 `context/` 文件夹。

#### 边界与持久化 (Boundary & Persistence)
- **Specs vs Context**: 
  - `specs/` 是**瞬时**的，仅描述当前 Feature 的变更，随任务结束而归档。
  - `context/` 是**持久**的，存放全生命周期的领域知识（如 Glossary, ADR, Business Rules）。
- **禁止混淆**：严禁将长期有效的业务逻辑仅写在 `specs/` 中，必须沉淀至 `context/`。

#### 同步协议 (Synchronization Protocol)
1.  **遵从 (Obey)**: 任务开始前，必须检索 `context/` 下的已有文档。
2.  **双重验证 (Double-Check Rectification)**: 
    - 若发现文档与代码不一致，**切勿盲目修改文档**。
    - **必须暂停**并进行反向验证：确认代码逻辑是否为 BUG。
    - 仅在确认“代码正确但文档过时”时，才更新文档；否则应修复代码。
3.  **创建 (Create)**: 发现缺失的领域概念时，主动创建 Markdown 记录。

#### 原子性提交 (Atomic Commits)
- **同生共死**：`context/` 文件夹下的变更被视为代码的一部分。
- **操作要求**：在提交 Feature 代码时，必须检查是否有 `context/` 的更新。若有，**必须包含在同一个 Commit 中**，严禁将文档更新滞后处理。

## 3. 🛡️ 代码与质量规范

### 3.1 Python 核心规范
- **类型铁律**：所有函数签名必须包含 Type Hints。
- **路径处理**：严禁 `os.path`，必须使用 `pathlib`。
- **配置管理**：依赖仅允许修改 `pyproject.toml`。

### 3.2 自动化与韧性 (SRE)
- **TDD 铁律**：**没有失败的测试，就没有实现代码。** (No Code Without Failing Tests)
- **Playwright**: 仅用于 E2E 测试。禁止 `time.sleep()`，必须使用 `expect()`。
- **Httpx**: 必须显式设置 `timeout`，关键请求必须包含指数退避重试 (Exponential Backoff)。
- **黑天鹅控制**：架构决策必须评估 Worst-Case 场景。

## 4. 🤖 AI 交互准则 (Core Protocol)
1.  **单一数据源**：本文件是最高准则。严禁修改 `.specify/memory/` 下的文件，除非用户明确指令。
2.  **工具优先**：凡是 `Justfile` 中定义的操作，必须优先使用 `just`。
3.  **位置感知**：时刻确认自己是在主仓库还是 Worktree 中。
4.  **状态存档**：在会话结束或 Token 过多时，生成 `_progress.md` 并建议用户清理上下文。