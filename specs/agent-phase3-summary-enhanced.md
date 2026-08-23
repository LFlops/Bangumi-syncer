# Phase 3 实施总骨架：总结增强（Summary Enhanced）

> 所属计划：Bangumi-Syncer Agent 化增量计划
> 性质：Phase 3 实施 spec 母文档（对齐 `agent-phase3-4-decisions.md` §1.1.1 拆分草案）
> 状态：**骨架**——3.0 设计定稿后按 2.0.x 模式拆分为独立子 phase 文档
> 关联：`agent-phase3-4-decisions.md`（D/J/C 决策与待定稿清单）

---

## 0. 子 phase 规划（草案，见 decisions §1.1.1）

| 子 phase | 范围 | 状态 |
|---|---|---|
| 3.0 | 设计定稿收尾 + J1/J2 约定落地 + 低风险增强（D5 兜底 / D6 重试 / D7 status） | 未开始 |
| 3.1 | feedback 通道落地（原 2.3 全部内容 + 本文档 §A 移除项恢复） | 未开始 |
| 3.2+ | 效果类增强（D1 两段式 / D3 关键词 / D4 结构化 / D8 分解），顺序随 B1/B2 裁决定 | 未开始 |

---

## A. feedback 自 Phase 2 代码移除记录（2026-08 裁决，3.1 实施时恢复）

> 背景：Phase 2 重新定义为"记忆三件套"（decisions §10 A2）。原 2.3 在代码层留有的
> feedback 预留已全部移除；本节是**唯一权威的恢复清单**——3.1 实施时逐条恢复，
> 并以 `agent-phase2.3-feedback.md` 的完整设计为准。

### 已移除项与恢复要点

| # | 文件 | 移除内容 | 3.1 恢复动作 |
|---|---|---|---|
| R1 | `app/core/database/agent_memory.py` `prune()` | SQL 过滤条件 `AND outcome != 'feedback'`（feedback 长期保留语义） | 恢复过滤条件 + docstring 说明；对应测试 `test_prune_archives_all_outcomes` 改回 W9 断言 |
| R2 | `app/services/memory/models.py` `MemoryEntry.outcome` 注释 | "feedback 取值由 phase3.x 反馈通道引入"指向 | outcome 取值扩展为 `success \| partial \| feedback` |
| R3 | `app/services/memory/retriever.py` 三处注释 | retrieve 去重排序 / format_memory_context 的"feedback 全量优先、[用户反馈] 前缀"预留说明 | 实现：retrieve 时 feedback 全量优先不占 memory_limit；format 加 `[用户反馈]` 前缀 |
| R4 | `app/core/database/agent_memory.py` / `memory/service.py` / `api/summary_jobs.py` clear_task docstring | "含 feedback 条目（彻底清空语义）"表述 | 恢复该表述（行为本身未变：clear 从不筛 outcome） |
| R5 | `tests/core/test_agent_memory.py` | `test_prune_keeps_feedback_outside_window`（W9）改为 `test_prune_archives_all_outcomes`；`test_clear_task_includes_feedback`（C10）改用 partial outcome | 按 R1 恢复 feedback fixture 与断言 |

### 3.1 尚需新建（原 2.3 设计，从未实现）

- API：`POST /api/summary/jobs/{name}/feedback`（写入 agent_working_memory，outcome='feedback'）
- Web UI：inbox 通知条目旁"反馈"按钮 + 弹窗
- 注入链路：retriever 对 feedback 条目的全量优先排序（R3）

---

## B. 待决策依赖（阻塞 3.2+ 实施细节，见 decisions §10 B 系列）

- **B1**：D1 是否合并自检+精修（影响调用次数与 J3 解析器形态）
- **B2**：D3 关键词提取时机（推荐跨 job 复用上次摘要调用）
- **B3–B7**：见 decisions §10

## C. 验收标准草案（对应 decisions §10 C1，随定稿细化）

| 项 | 验收 |
|---|---|
| D5 兜底 | mock LLM 全失败路径下，通知仍送达且包含结构化统计（N 条记录/M 部番剧/K 次失败） |
| D8 分解 | 双用户 fixture 下输出按用户分段 |
| D1 两段式 | 构造测试集（≥12 条记录含 1 条易漏项）能捕获遗漏并修正 |
| 3.1 feedback | 反馈写入 → 下次执行注入 `[用户反馈]` 强约束 → prune 不归档 |
