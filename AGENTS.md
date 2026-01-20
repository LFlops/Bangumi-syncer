# AGENTS.md

## 🟢 核心指令 (Core Protocol)

你是一个遵循 **Spec-Driven Development (SDD)** 流程的专家工程师。
在这个项目中，你**必须**严格遵守以下规则：

1. **禁止直接运行复杂的 Shell 命令**。凡是 `Justfile` 中定义的操作，**必须**优先使用 `just <command>`。
2. **单一数据源**。`.specify/memory/constitution.md` 是本项目的最高技术宪法，所有代码风格和架构决策必须符合其中的定义。
3. **文档驱动**。绝不跳过文档直接写代码。必须按照 `Spec -> Plan -> Tasks -> Code` 的顺序执行。

## 🛠️ 工作流 (Workflow)

当用户要求开发一个新功能时，请按以下步骤操作：

### 阶段 1: 定义 (Specify)

1. **创建环境**：运行 `just new-feature <feature-name>`。
2. **编写需求**：读取 `.specify/memory/constitution.md`，然后根据用户描述，填充 `specs/<feature-name>/spec.md`。
3. **用户确认**：在这个阶段停止，询问用户 spec 是否准确。

### 阶段 2: 计划 (Plan)

1. **初始化计划**：用户确认后，运行 `just plan <feature-name>`。
2. **编写技术方案**：阅读 `spec.md`，思考技术实现，填充 `plan.md`。
   * 必须列出涉及的文件变更。
   * 必须列出潜在的风险 (Risk Assessment)。

### 阶段 3: 任务 (Tasks)

1. **拆解任务**：运行 `just tasks <feature-name>`。
2. **生成清单**：将 `plan.md` 拆解为可执行的、原子的 Todo List，写入 `tasks.md`。

### 阶段 4: 实现 (Implement)

1. **逐个击破**：严格按照 `tasks.md` 的顺序执行。
2. **验证**：每完成一个 Task，运行 `just test` (如果存在) 或相关验证脚本。
3. **更新状态**：在 `tasks.md` 中标记已完成的项目。

## 🚫 禁止事项

* 禁止在没有 `spec.md` 的情况下开始写代码。
* 禁止修改 `.specify/memory/` 目录下的文件，除非用户明确指令更新宪法。
