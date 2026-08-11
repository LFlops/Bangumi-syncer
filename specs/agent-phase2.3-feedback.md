# Phase 2.3: 用户反馈交互

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2.0.1（表 + repository）+ Phase 2.0.2（MemoryRetriever 注入）
> 交付物：用户对总结结果的反馈可写入记忆，并在后续执行注入为强约束
> 执行时机：Phase 2.2 之后、Phase 3 之前（一个 phase 只做一件事）

## 目标

让用户的偏好表达（如"不要太啰嗦"）进入记忆系统：用户在 Web UI 看到总结通知后反馈，反馈写入 `agent_working_memory`，后续 summary 执行注入 prompt 时以 `[用户反馈]` 前缀呈现为**强约束**（优先于执行摘要）。

## 交互流程

```
用户看到总结通知（inbox 站内信）
    │
    ▼
点击通知条目旁的"反馈"按钮
    │
    ▼
弹窗输入反馈文本（如"不要太啰嗦"）
    │
    ▼
POST /api/summary/jobs/{name}/feedback {user_name, feedback}
    │
    ▼
写入 agent_working_memory（task_type='summary'，summary 字段存反馈原文含
[用户反馈] 前缀，outcome='feedback'）
    │
    ▼
下次 summary 执行：MemoryRetriever 读取时反馈条目格式化加 [用户反馈] 前缀
```

## 存储设计（复用现有表，不加新表）

```python
# 写入 agent_working_memory 的反馈条目
MemoryEntry(
    task_type="summary",
    task_id="summary-{job_name}",
    run_id=str(uuid4()),
    summary="[用户反馈] alice: 不要太啰嗦",  # 前缀 + 用户标识（user_name 缺省当前登录用户）
    outcome="feedback",                     # success | partial | feedback 之一
    tokens_used=0,
)
```

**用户标识走前缀约定，不新增列**（feedback 是任务级偏好）：
- 当前消费方按 task_id 检索（get_feedback）——用户不影响（任务确定后该任务的所有反馈都该注入）
- 未来按用户文本检索：FTS5 命中 summary 字段（`alice` 可搜）✓
- 只有"按用户 SQL 精确过滤"才需加列——YAGNI，需求出现再升级（与 consumed_run_id 同 migration 模式）

**为什么用前缀约定而非结构化 priority 字段**（权衡结论见 Phase 2 spec"记忆策略"）：
- 前缀方案零表结构改动，retriever 的 `_deduplicate_and_rank` 识别 `[用户反馈]` 前缀即可排到最前
- 确认价值后（如反馈确实改变了行为）再升级为结构化字段（priority/importance）

**feedback 条目长期保留 + 注入不占摘要额度**：
- `outcome="feedback"` 条目**不被 prune 归档/删除**（用户偏好是长期有效强约束，"不要太啰嗦"3 年后依然生效）
- 注入时 feedback **全量排最前，不占执行摘要额度**（总量 = feedback 数 + memory_limit）——反馈是强约束（用户明确表达），不该被执行摘要挤掉；feedback 数量天然少，token 成本可控

### 对 2.0.1 / 2.0.2 的修改清单（feedback 是 2.3 的概念，统一在此引入）

2.0.1/2.0.2 **不认识 feedback**（outcome 只有 success/partial，prune/get_recent 朴素实现）。本 phase 引入：

| 修改 | 文件 | 内容 |
|------|------|------|
| 修改 | `app/services/memory/models.py` | outcome 取值扩展：`success \| partial \| feedback`（注释） |
| 修改 | `app/core/database/agent_memory.py` | `get_recent` 排除 `outcome='feedback'`；新增 `get_feedback`（全量取反馈条目，无 limit）；`prune` 跳过 feedback（长期保留） |
| 修改 | `app/services/memory/retriever.py` | `retrieve`/`_deduplicate_and_rank` 修改：feedback 全量优先、不占摘要额度（recent 路径分离 feedback 与摘要）；`_format_memory_context` 增加 `[用户反馈]` 前缀标记 |
| 修改 | `app/services/summary/service.py` | `record_feedback()` 写入 MemoryEntry(outcome="feedback") |

## API 设计

```python
# POST /api/summary/jobs/{name}/feedback
# 请求体
{
    "user_name": "alice",        # 可选，缺省取当前登录用户
    "feedback": "不要太啰嗦"
}

# 响应
{
    "status": "success",
    "message": "反馈已记录"
}
```

校验：
- `feedback` 非空，长度上限（如 500 字符）
- `name` 对应的 summary job 存在，否则 404
- 写入失败（DB 异常）返回 500，不影响总结功能

## 前端（inbox 入口）

在站内信（in-app notification）列表中，`watching_summary_*` 类型的通知条目增加"反馈"按钮：

```
[总结] 昨日追番总结：芙莉莲追到 S1E10...          [反馈] [详情]
                                                    │
                                                    ▼
┌────────────────────────────────┐
│ 反馈                                  │
│ ┌──────────────────────────────┐    │
│ │ 不要太啰嗦                      │    │
│ └──────────────────────────────┘    │
│              [提交]  [取消]          │
└────────────────────────────────┘
```

- 提交后 POST feedback 接口，成功提示"反馈已记录，下次总结会参考"
- 失败提示错误信息

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `app/api/summary_jobs.py` | 新增 `POST /api/summary/jobs/{name}/feedback` |
| 修改 | `app/services/summary/service.py` | 新增 `record_feedback()`（或独立 feedback 服务方法） |
| 修改 | `templates/`（inbox 通知列表） | watching_summary_* 通知条目加反馈按钮 + 弹窗 |
| 修改 | `static/js/`（inbox 相关 JS） | 反馈弹窗交互 + POST 调用 |
| 修改 | `app/services/memory/models.py` | outcome 取值扩展（feedback） |
| 修改 | `app/core/database/agent_memory.py` | get_recent 排除 + get_feedback 新增 + prune 跳过（见"对 2.0.1/2.0.2 的修改清单"） |
| 修改 | `app/services/memory/retriever.py` | retrieve 排序：feedback 全量优先、不占摘要额度（见"对 2.0.1/2.0.2 的修改清单"） |

> 总计：修改 7 个文件（无新增；其中 3 个是对 2.0.1/2.0.2 已有文件的演进修改）

## BDD 测试场景

### Scenario F1 反馈写入记忆
- **Given** 用户 alice 在 inbox 看到 summary 通知
- **When** POST `/api/summary/jobs/daily/feedback` `{user_name: "alice", feedback: "不要太啰嗦"}`
- **Then** 返回 success
- **And** `agent_working_memory` 新增一条 `task_id="summary-daily"`、`summary == "[用户反馈] alice: 不要太啰嗦"`（前缀 + 用户标识）的记录

### Scenario F2 反馈注入为强约束
- **Given** 已有一条反馈记忆（`[用户反馈] 不要太啰嗦`）
- **When** 下次 summary 执行时 retriever 读取
- **Then** 注入文本中反馈条目**排在最前**且带 `[用户反馈]` 前缀（优先于执行摘要）

### Scenario F3 空反馈拒绝
- **Given** 已登录用户
- **When** POST feedback `{feedback: ""}`
- **Then** 返回 422（校验失败）

### Scenario F4 未知 job 404
- **Given** 不存在的 job 名
- **When** POST `/api/summary/jobs/not-exist/feedback`
- **Then** 返回 404

### Scenario F5 前端反馈弹窗（Playwright E2E）
- **Given** inbox 有 watching_summary_* 通知
- **When** 点击"反馈"→ 输入文本 → 点击提交
- **Then** POST 请求携带 feedback
- **And** 页面提示"反馈已记录"

## 验证方式

1. 单元/接口测试：F1-F4 全部通过
2. E2E：F5 在本地/CI 验证
3. 手动：触发一次总结 → inbox 反馈"不要太啰嗦" → 再次触发总结 → 检查 LLM 请求日志的 system prompt 含 `[用户反馈] 不要太啰嗦` 且位于历史上下文最前
