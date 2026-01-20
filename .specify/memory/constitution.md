# 项目宪法 (Project Constitution)

## 1. 🎯 愿景与技术栈 (Vision & Tech Stack)

本项目构建于 **Astral Stack** 之上，追求极致的性能与统一的工具链。

* **语言**: Python 3.9+
* **包管理**: `uv`
* **Linter/Formatter**: `ruff`
* **类型检查**: `ty`
* **核心理念**: "若无类型，即不存在。"

## 2. ⚙️ 开发工作流 (Workflow)

### 2.1 🌳 版本控制策略 (Git Worktree - Sibling Mode)

为了防止 AI 上下文污染 (Context Pollution) 并确保绝对隔离，我们采用 **Sibling Worktree** 模式：

* **严禁子目录 Worktree**: 禁止在项目根目录下创建 `.worktrees/`，这会导致 AI 索引重复代码。
* **同级影子目录**: 所有 Feature Worktree 必须创建在项目的**上一级同级目录**中。
  * 命名规范: `../<project-name>.worktrees/<feature-name>`
* **单一上下文**: AI 每次只能在一个特定的目录（主项目或某个 Worktree）中启动，确保它只看得到当前分支的代码。

### 2.2 🛠️ 交互与交付 (The "Just" Way)

AI Agent 必须优先使用 `Justfile` 进行操作：

* **环境同步**: `just install` (在当前 worktree 中同步 uv 环境)
* **代码质量**: `just check` (Lint + Types + Test)
* **任务生命周期**:
  1. **启动**: `just new-feature <name>` (自动在 ../ 创建隔离环境)
  2. **切换**: `cd ../<project>.worktrees/<name>`
  3. **开发**: 在隔离环境中运行 `just plan` -> `just tasks` -> Code
  4. **合并与清理**: 回到主目录，合并代码，然后运行 `just clean <name>`

## 3. 📝 代码规范 (Coding Standards)

* **严格类型 (`ty`)**: 所有函数必须包含类型提示。
* **代码风格 (`ruff`)**: 依赖 Ruff 自动格式化。
* **配置**: 仅允许修改 `pyproject.toml`。

## 4. 🤖 AI 交互准则

1. **位置感知**: 在执行任务前，先确认当前路径。
2. **环境隔离**: 记住每个 Worktree 都有独立的 Virtual Environment。
3. **工具优先**: 坚持使用 Skills 调用 `ruff`/`ty` 进行微观检查。
