# Phase 2.4: 失败路径记忆

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2.0.1（表 + MemoryExtractor 签名已预留 error_message）
> 交付物：任务执行失败也写入记忆，后续注入时 LLM 可识别历史异常模式
> 执行时机：Phase 2.3 之后、Phase 3 之前（一个 phase 只做一件事）

## 目标

summary 任务执行失败（LLM 调用失败、数据查询异常等）时，失败信息写入 `agent_working_memory`（`outcome="failed"` + `error_message`），下一次执行注入历史时 LLM 能看到"上周这个时候挂了"这类模式。

**不需要专门的模式检测**：失败记录入库后，注入时由 LLM 自行识别模式。自动模式检测是 Phase 3 诊断 Agent 的能力。

## 设计

### 1. MemoryExtractor 支持失败输入

```python
# app/services/memory/retriever.py（Phase 2 的签名，Phase 2.4 实现失败分支）

async def extract_and_store(
    self,
    task_type: str,
    task_id: str,
    run_id: str,
    llm_response: str,
    decisions: list[str],
    outcome: str,
    tokens_used: int,
    error_message: str | None = None,
) -> None:
    # 失败路径（llm_response 为空）不调 LLM，用规则生成摘要
    #（失败时 LLM 也不可靠，避免二次失败）
    if not llm_response:
        summary = f"执行失败: {error_message or 'unknown'}"[:200]
    else:
        summary = await self._summarize(llm_response)
    ...
```

### 2. execute_job 失败分支写入记忆

```python
# app/services/summary/service.py

except Exception as e:
    logger.error(f"Summary job '{job_config.name}' failed: {e}")
    # === 新增：失败也提取记忆 ===
    try:
        await memory_extractor.extract_and_store(
            task_type="summary",
            task_id=task_id,
            run_id=str(uuid4()),
            llm_response="",                 # 失败时无响应 → 规则摘要
            decisions=[],
            outcome="failed",
            tokens_used=0,
            error_message=str(e)[:500],      # 截断防膨胀
        )
    except Exception:
        logger.exception("Failed to store memory on job failure")
```

- 外层 try 包裹，记忆写入失败不影响原有错误处理流程
- `error_message` 截断 500 字符防表膨胀

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `app/services/memory/retriever.py` | `extract_and_store` 失败输入分支（空响应 → 规则摘要） |
| 修改 | `app/services/summary/service.py` | `execute_job` except 分支调 extract_and_store |

> 总计：修改 2 个文件

## BDD 测试场景

### Scenario M1 失败写入记忆
- **Given** execute_job 抛异常（如 LLM 调用失败）
- **When** 执行 except 分支
- **Then** `agent_working_memory` 新增 `task_id="summary-{name}"`、`outcome="failed"`、`error_message` 含异常信息的记录
- **And** summary 字段为规则摘要（`执行失败: ...` 前缀），未调用 LLM

### Scenario M2 记忆写入失败不影响主流程
- **Given** extract_and_store 抛异常（如 DB 错误）
- **When** except 分支执行
- **Then** 不抛新异常（logger.exception 记录），原有错误处理流程不受影响

### Scenario M3 失败记录注入
- **Given** 记忆表已有失败记录（outcome="failed"）
- **When** 下一次 summary 执行 retriever 读取
- **Then** 失败记录与其他记忆一起注入（LLM 可识别历史异常模式）

## 验证方式

1. 故意触发一次失败（如临时填错 LLM key）→ 确认记忆表出现 `outcome="failed"` 且带 error_message 的记录
2. 再次执行成功 → 确认注入历史中包含上一条失败记录（LLM 请求日志可见）
3. 单元测试：M1-M3 全部通过
