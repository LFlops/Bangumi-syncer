# Phase 2.5: 窗口重叠去重

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2（key_findings 存结构化覆盖列表）
> 交付物：明细窗口与历史摘要重叠时，注入前识别重叠记录并标注，避免重复叙述
> 执行时机：Phase 2.4 之后、Phase 3 之前（一个 phase 只做一件事）

## 目标

处理**窗口重叠**场景：summary 任务每日触发但 `lookback_days=7` 时，第 2 天执行的明细（2-8 天）与昨日总结覆盖范围（1-7 天）重叠——2-7 天的记录被重复叙述。Phase 2 只提供"软去重"（注入历史摘要让 LLM 看到重复），本 phase 实现精确识别与标注。

## 设计

### 1. 数据基础（Phase 2 已存）

`agent_working_memory.key_findings` 中的 `covered` 列表（每次执行提取，规则从 records 生成）：

```json
{"covered": [{"title": "葬送的芙莉莲", "season": 1, "episode": 10}, ...]}
```

### 2. 重叠识别（注入前）

```python
# app/services/memory/retriever.py

async def find_overlaps(
    self,
    task_id: str,
    records: list[SyncRecord],
) -> list[dict]:
    """比对今日明细与最近一次记忆的 covered 列表，返回重叠记录。"""
    latest = await self._repo.get_latest(task_type="summary", task_id=task_id)
    if not latest or not latest.key_findings.get("covered"):
        return []
    covered = {(c["title"], c.get("season"), c.get("episode")) for c in latest.key_findings["covered"]}
    return [
        r for r in records
        if (r.bgm_title, r.season, r.episode) in covered
    ]
```

### 3. 注入行为：标注而非过滤

**默认标注**（不删除明细，避免改变"过去 7 天完整总结"的语义）：

```python
# _format_memory_context 或 _build_messages 中
overlaps = await retriever.find_overlaps(task_id, records)
if overlaps:
    overlap_note = (
        "以下记录已在上次总结中覆盖，可简述或跳过，不必重复展开：\n"
        + "\n".join(f"- {r.bgm_title} S{r.season}E{r.episode}" for r in overlaps[:20])
    )
    # 注入到 system prompt 的历史上下文部分
```

**语义风险说明**：用户可能故意要"过去 7 天的完整总结"。标注（而非过滤）让 LLM 自主决定：默认简述跳过，但用户可在 system_prompt 中要求"重复也详细总结"覆盖此行为。

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 修改 | `app/services/memory/retriever.py` | 新增 `find_overlaps()`（比对 covered 列表） |
| 修改 | `app/services/memory/retriever.py` | `AgentMemoryRepository.get_latest()`（按 task 取最近一条） |
| 修改 | `app/services/summary/service.py` | 注入前识别重叠并生成 overlap_note 注入 system prompt |

> 总计：修改 2 个文件

## BDD 测试场景

### Scenario D1 无重叠不注入提示
- **Given** 最近记忆无 covered 列表（或今日明细无重叠）
- **When** 注入前检查
- **Then** system prompt 不含 overlap_note

### Scenario D2 重叠记录识别
- **Given** 最近记忆 covered 含 `{"title": "芙莉莲", "season": 1, "episode": 10}`，今日明细含同一条
- **When** `find_overlaps()`
- **Then** 返回该记录

### Scenario D3 标注注入
- **Given** 有重叠记录
- **When** `_build_messages`
- **Then** system prompt 含"已在上次总结中覆盖，可简述或跳过"提示及重叠列表

### Scenario D4 非重叠记录不受影响
- **Given** 明细含新记录（不在 covered）
- **When** 注入
- **Then** 新记录正常呈现，不被标注

## 验证方式

1. 单元测试：D1-D4 全部通过
2. 手动：配置每日触发 + lookback_days=7，连续执行 2 次，检查第 2 次 LLM 请求日志的 system prompt 含 overlap_note 且仅列出重叠记录
3. 回归：`memory_limit=0` 时无注入（含无 overlap_note），行为与 Phase 2 一致
