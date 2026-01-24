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
- **定义**: 该目录存放喂给 AI 的静态领域知识（Domain Knowledge）、业务规则速查表及架构决策记录。
- **同步协议**:
  1.  **遵从 (Obey)**: 任务开始前，必须检索 `context/` 下的已有文档，并严格遵守其中的业务约束。
  2.  **修正 (Rectify)**: 若发现文档描述与实际代码逻辑不一致，**以代码为事实标准 (Code is Truth)**，必须立即修改文档以反映现状。
  3.  **创建 (Create)**: 若在开发过程中发现缺失必要的领域概念描述或隐藏规则，必须在 `context/` 下主动创建新的 Markdown 文档进行记录。

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