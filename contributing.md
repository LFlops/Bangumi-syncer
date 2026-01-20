# 🤝 Contributing to Bangumi-syncer

首先，感谢你对 Bangumi-syncer 的兴趣！🎉

本项目采用了一套**现代化的、AI 友好的开发工作流**。如果你习惯了传统的开发方式，这里的一些概念（如 *Spec-Driven Development* 和 *Worktree Isolation*）可能会让你耳目一新。

别担心，这套流程旨在让你（以及你的 AI 助手）开发得更快、更稳、更爽。

## 🚀 核心理念 (The Philosophy)

1. **想清楚再动手 (Spec-Driven)**: 我们不鼓励上来就写代码。我们先写文档 (`Spec`)，再定计划 (`Plan`)，最后拆解任务 (`Tasks`)。
2. **AI 增强 (AI-Native)**: 我们鼓励使用 Claude/Cursor 等 AI 工具。为了防止 AI 产生幻觉，我们提供了一套严谨的上下文协议 (`AGENTS.md` & `Constitution.md`)。
3. **环境隔离 (Context Isolation)**: 为了让 AI 专注，我们不直接在主目录开发，而是在**同级目录**的独立工作区 (Git Worktree) 中进行。

---

## 🛠️ 准备工作 (Prerequisites)

本项目基于 **Astral Stack** 构建，追求极致速度。你需要安装以下工具：

1. **[uv](https://github.com/astral-sh/uv)**: Python 的极速包管理器（替代 pip/poetry）。
2. **[Just](https://github.com/casey/just)**: 一个通用的命令运行器（替代 Make）。
3. **Git**: 版本控制。

安装完成后，在项目根目录运行初始化：

```bash
just install
```

---

## ⚡ 开发流程：五步走 (The Workflow)

我们通过 `Justfile` 封装了所有复杂操作。请忘掉繁琐的 git 命令，跟着这个闭环流程走：

### 第一步：启动 (Start) ——`just new-feature`

想开发一个新功能（比如`login`）？不要直接创建分支，运行：

**Bash**

```bash
just new-feature login
```

**发生了什么？**

* 系统会在**项目同级目录**创建一个影子文件夹：`../Bangumi-syncer.worktrees/login`。
* 它为你创建了新分 `feature/login`。
* 它生成了初始的文档模板。

> **为什么？** 将开发环境移出主目录，能物理切断 AI 的视线，防止它读取到其他分支的代码，从而消除“上下文污染”。

### 第二步：进入 (Switch) —`cd ...`

根据终端提示，进入你的专属开发环境（隔离区）：

```bash
cd ../Bangumi-syncer.worktrees/login
just install  # 初始化这个隔离环境的依赖
```

### 第三步：循环 (Loop) —— Spec-Driven Development

现在，你处于一个纯净的环境中。请遵循 **SDD 循环** ：

1. **定义需求 (Spec)** : 编辑`specs/login/spec.md`。写清楚要做什么。
2. **制定计划 (Plan)** : 运行`just plan login`，让 AI 帮你生成技术方案。
3. **拆解任务 (Tasks)** : 运行`just tasks login`，生成具体的 Checklists。
4. **编写代码 (Code)** : 按照 Checklist 一项项完成。

### 第四步：验收 (Check) ——`just check`

代码写完了？**必须**通过质量验收。这一步包含了 Lint (Ruff)、类型检查 (Ty) 和测试 (Pytest)。

**Bash**

```bash
just check
```

* **原则** : 这里的检查极快（得益于 Astral Stack）。如果报错，请立即修复。
* **AI 提示** : 如果你用 Claude Code，它会自动调用`ruff` 和`ty` 的 Skill 来辅助你修复。

### 第五步：收尾 (Finish) ——`just clean`

功能开发完成，代码已 Push 并合并 (Merge) 后，你可以清理掉这个临时的开发环境：

1. 回到主目录：
   **Bash**

   ```bash
   cd ../Bangumi-syncer  # (假设原本在 ../Bangumi-syncer.worktrees/login)
   ```
2. 一键清理：
   

   ```bash
   just clean login
   ```

   *(这会删除对应的 Worktree 文件夹和本地分支，保持电脑整洁)*

---

## 🤖 如何高效使用 AI (AI Guidance)

如果你使用 Claude Code、Cursor 或 GitHub Copilot，请告诉它们：

> "请遵循项目根目录下的`AGENTS.md` 和 `.specify/memory/constitution.md` 进行操作。"

**关键点：**

* **不要** 让 AI 运行 **`pip install`，让它用`uv add`。
* **不要** 让 AI 盲目写代码，强制它先填好`spec.md` 和 `plan.md`。
* **利用 Skills** : 如果你用 Claude Code，它会自动调用 `ruff` 和 `ty` 的 Skill 来辅助你。

---

## ❓ 常见问题 (FAQ)

Q: 我找不到我的代码了！

A: 记得你是在 同级目录 (../Bangumi-syncer.worktrees/xxx) 下开发的，不在原本的项目文件夹里。

Q: ty 报错太严格了怎么办？

A: 尽量修复它！类型安全是我们代码健壮性的基石。实在无法解决时，可以使用 # ty: ignore，但必须写注释解释原因。

Happy Coding! 🚀
