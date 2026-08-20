# Phase 2 实现检视报告

> 检视对象：commit `9cd2bccd7da5d984835951f50011e30aa4e46a0a`
> 主题：phase2.0.1 & phase2.0.2 & phase2.0.3 开发完成 && 测试用例通过
> 检视范围：`app/core/database/*`、`app/services/memory/*`、`app/services/summary/*`、`app/api/summary_jobs.py`、`templates/config.html`、相关测试

---

## 一、总体结论

实现质量高，忠实落地了 spec 的 D1–D16 全部决议：

- 事务语义正确（`store_and_mark` 原子、`prune` 独立 best-effort、`clear_task` 先收集 run_id 再删表 + `UPDATE SET NULL` 清标记而非删记录）
- 类型方案正确（`SummaryRecord` dataclass、`MemoryEntry` dataclass、facade 公开 `memory`/`sync_records` 属性）
- system 注入正确（拼进 system prompt，无多 system 风险）
- migration 幂等（`__ensure_agent_memory` / `__ensure_sync_records_consumed`，trigram 降级兜底）
- 测试覆盖全面（core/API/e2e/service 各层，493 行 agent_memory 测试）

**结论：可合入，但有 1 个业务逻辑 bug 需修复（多关键词 FTS 用 AND 而非 OR），另有 3 处次要清理项。**

---

## 二、提交概览

| 类别 | 文件 | 变更 |
|---|---|---|
| 新增·记忆仓储 | `app/core/database/agent_memory.py` | store_and_mark / prune / rename_task / clear_task / get_recent / search_fts / search_archive |
| 修改·迁移 | `app/core/database/connection.py` | 记忆主表+归档表+FTS5+触发器；sync_records 补 consumed_run_id |
| 修改·facade | `app/core/database/__init__.py` | 公开 `memory` / `sync_records` 属性 |
| 修改·查询 | `app/core/database/sync_records.py` | `get_records_in_date_range` SELECT 补 consumed_run_id |
| 新增·记忆模块 | `app/services/memory/{models,extractor,retriever,service}.py` | MemoryEntry / MemoryExtractor / MemoryRetriever / MemoryService |
| 修改·summary | `app/services/summary/{service,models}.py` | 前置重构 + 记忆注入 + SummaryRecord |
| 修改·API | `app/api/summary_jobs.py` | clear-memory 端点 + 改名联动 |
| 修改·配置 | `app/core/config.py` / `app/models/summary.py` | memory_enabled / memory_limit 透传 |
| 修改·前端 | `templates/config.html` | 记忆开关/条数/清空按钮（内联 JS） |
| 新增·测试 | `tests/{core,api,e2e,services}` | 见第六节 |

---

## 三、🔴 必须修复（业务 bug）

### 3.1 `search_fts` 多关键词用 AND，应为 OR

**位置**：`app/core/database/agent_memory.py` → `search_fts()`

```python
terms = ['"葬送的芙莉莲"', '"鬼灭之刃"']
match = " AND ".join(terms)   # '"葬送的芙莉莲" AND "鬼灭之刃"'
```

**问题**：关键词是「今日看的番剧标题」（`_build_memory_context` 提取 bgm_title 去重取前 5），目的是**捞回与今日任一标题相关的历史记忆**。业务语义应是 **OR**（命中任意一个标题即相关），而非 AND（必须同时命中所有标题）。

**后果**：今天看《芙莉莲》《鬼灭之刃》，昨天总结只提过《芙莉莲》——AND 会**漏掉**这条相关记忆，使关键词检索在常见场景下近乎失效（仅当某条历史记忆恰好同时提及今日全部番剧时才命中）。

**修复**：

```python
match = " OR ".join(terms)
```

**连带**：`tests/core/test_agent_memory.py` 的 `test_search_fts_multiple_keywords_and` 固化了错误语义（断言只返回同时含两词的那条），需改为 OR 语义（两个单标题记忆均命中）。

---

## 四、🟡 建议修复（次要）

### 4.1 `MemoryExtractor._llm` 构造时缓存，与 summary_service 不一致

**位置**：`app/services/memory/extractor.py` → `__init__`

```python
self._llm = llm_client or get_llm_client()   # 构造时缓存
```

而 `summary_service.llm_client` 用 property 每次现取（`return get_llm_client()`），理由是「LLM 配置保存会 `reset_llm_client()`，缓存实例会失效」。

**问题**：两处策略不一致。LLM 配置保存后，extractor 里缓存的 `self._llm` 会滞留旧配置（旧 api_base/key/model）。影响小（`_summarize` 有规则截断兜底），但语义上不一致。

**建议**：`MemoryExtractor._llm` 改为惰性取值（property 或 `_summarize` 内 `get_llm_client()`），与 summary_service 对齐。

### 4.2 `_deduplicate_and_rank` 的 `limit` 参数未使用

**位置**：`app/services/memory/retriever.py` → `_deduplicate_and_rank(entries, limit)`

`limit` 传入但函数体未使用（spec 有意「不按 limit 收束」）。建议删除该参数，或加注释说明「有意不使用，recent 已在源头受限」。

### 4.3 `search_archive` 的 params 构造绕

**位置**：`app/core/database/agent_memory.py` → `search_archive()`

```python
params = [like]
if task_type:
    type_clause = "AND task_type = ?"
    params.append(task_type)
params.append(limit)
cursor.execute(sql, [like, like] + params[1:])
```

逻辑正确（占位符与参数一一对应），但 `params[1:]` 的拼接方式可读性差。建议直接写：

```python
args = [like, like] + ([task_type] if task_type else []) + [limit]
```

---

## 五、⚪ 观察项（非 bug，记录备查）

### 5.1 trigram 分词器降级

`connection.py._ensure_agent_memory` 优先 `tokenize='trigram'`（SQLite ≥ 3.34），`OperationalError` 时降级默认 unicode61。降级后 CJK 子串检索失效（整段 CJK 为一个 token），代码已注释说明。属已知限制。

### 5.2 短词（< 3 字符）过滤

`search_fts` 对 `len(t) >= 3` 的词才检索，2 字符标题（罕见）会被跳过，属 trigram 固有约束，已注释。

### 5.3 记忆读取未隔离在独立 try/except

`execute_job` 里 `_build_memory_context`（retrieve + find_overlaps）在**外层 try** 内。虽然底层 `_run_read(default=[])` 已吞错不抛，但结构上「记忆读取失败」理论上会阻断整个总结。当前安全（repo 读方法都不 raise），仅提示后续若加会抛错的读路径需注意。

### 5.4 前端 JS 内联于 config.html，spec 文件清单写了 static/js

spec 2.0.2 文件清单写「`templates/config.html` + `static/js/`（summary 相关）」，实际 JS（`toggleSummaryMemoryInput`/`clearSummaryMemory`/`renderSummaryJobs` 扩展）**内联在 config.html**，无独立 static/js 文件。实现正确，spec 清单不精确（无功能影响）。

### 5.5 两个 MemoryService 实例

`summary_service` 构造一个，`app/api/summary_jobs.py` 模块级又构造一个 `memory_service`。两者包裹同一 repo，是无状态薄壳，不构成 bug；但 `MemoryExtractor` 各缓存一份 llm_client（见 4.1），略浪费。

### 5.6 归档表 `id` 与主表 `id` 不同

`search_archive` 返回 `MemoryEntry` 的 `id` 是归档表自增 id（非主表原 id）。跨表追溯用 `run_id`（唯一键），当前无消费方依赖 `id`，无影响。

---

## 六、测试覆盖评估

| 层 | 文件 | 覆盖点 |
|---|---|---|
| core | `test_agent_memory.py`（493 行） | 建表/触发器/facade/store_and_mark 事务/prune 归档/feedback 保留/rename/clear/search_fts/search_archive/迁移幂等 |
| core | `test_database_records_range.py` | get_records_in_date_range 含 consumed_run_id |
| services | `test_extractor.py` / `test_retriever.py` / `test_service.py` | 摘要生成/规则兜底/空响应/检索去重/format/find_overlaps/rename/clear |
| services | `test_models.py` / `test_service.py` | SummaryRecord 转换 / memory_enabled/memory_limit 解析 / execute_job 注入 |
| api | `test_summary.py` | clear-memory 端点（confirm 校验）/改名联动/CRUD 透传 |
| e2e | `test_summary_memory.py` | 前端表单渲染/开关禁用/清空按钮 |

覆盖充分，BDD 场景（W/R/D/C/S 系列）基本落到测试。**缺口**：`search_fts` 多关键词语义（AND/OR）虽有测试但**固化了错误语义**（见 3.1）。

---

## 七、Spec vs 实现差异（无碍或需知悉）

| 项 | spec | 实现 | 评估 |
|---|---|---|---|
| `user_filter` 作为关键词来源 | 2.0.2 execute_job 伪代码 `keywords = [job_config.user_filter or ""]` | 实现未用（`SummaryJobConfig` 无 `user_filter` 字段） | 实现正确，spec 伪代码引用了不存在字段 |
| `self.llm_client` 构造方式 | `__init__` 中 `self.llm_client = get_llm_client()` | property 每次现取 | 实现优于 spec（规避 reset_llm_client 失效） |
| 前端 JS 位置 | `static/js/` | 内联 `config.html` | 见 5.4 |
| `search_fts` task_type 过滤 | 「JOIN 后 WHERE」 | `WHERE agent_memory_fts MATCH ? AND m.task_type = ?` | 一致 |

---

## 八、行动清单

| 优先级 | 项 | 动作 |
|---|---|---|
| 🔴 P0 | `search_fts` AND → OR | 改 1 行 + 改/补测试 |
| 🟡 P1 | `MemoryExtractor._llm` 惰性化 | 可选，与 summary_service 对齐 |
| 🟡 P1 | `_deduplicate_and_rank` 删未用参数 | 可选清理 |
| 🟡 P2 | `search_archive` params 直写 | 可选，提升可读性 |
