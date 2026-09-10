---
title: 🗄️ 数据库仓储层
order: 5
---

# 🗄️ 数据库仓储层

`app/core/database/` 是 SQLite 仓储层，**无 ORM**，直接用 `sqlite3` + 参数化 SQL。

## 技术栈

- **数据库**：SQLite（Python 标准库 `sqlite3`）
- **PRAGMA**：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`
- **迁移**：`ALTER TABLE` 增量迁移函数，无需手写迁移脚本
- **默认路径**：`data/sync_records.db`

---

## Repository 模式

每个表对应一个 Repository 类，继承 `base_repository.py` 的基类，共享一个 `DatabaseConnection`（单例 `database_manager`），通过 `_lock` 串行化写操作。

| 表 | Repository | 说明 |
| --- | --- | --- |
| `sync_records` | `sync_records.py` | 同步记录，含 `match_trace` JSON、`match_score`、`source` |
| `pending_candidates` | `pending_candidates.py` | 待确认候选（匹配失败时沉淀），部分唯一索引去重 |
| `pending_sync_queue` | `pending_sync_queue.py` | Replay 待同步队列，部分唯一索引去重 |
| `in_app_notifications` | `inbox.py` | 站内信 |
| `trakt_config` / `trakt_sync_history` | `trakt.py` | Trakt 多用户配置与同步历史 |
| `feiniu_sync_history` / `feiniu_meta` | `feiniu.py` | 飞牛同步历史与启动水位 |
| `announcement_read_state` / `llm_usage` | `inbox.py` / `llm_usage.py` | 公告已读状态 / LLM 调用计量 |

---

## 常见参数（PRAGMA）

```python
cursor.execute("PRAGMA journal_mode=WAL")  # WAL 模式，并发读写稳定
cursor.execute("PRAGMA synchronous=NORMAL")  # 平衡性能与安全
cursor.execute("PRAGMA busy_timeout=5000")  # 写锁等待 5 秒
```

---

## 表结构演进

表结构通过 `connection.py` 中的 `ALTER TABLE` 增量迁移函数实现，应用启动时自动检测并执行。新增字段只需：

1. 在 `_init_database()` 的 `CREATE TABLE` 中加字段（新库直接有）
2. 写一个 `_ensure_xxx_field()` 迁移函数（老库自动补）

```python
def _ensure_sync_records_media_type(conn) -> None:
    """老库补 media_type 字段"""
    cursor = conn.execute("PRAGMA table_info(sync_records)")
    columns = {row[1] for row in cursor.fetchall()}
    if "media_type" not in columns:
        conn.execute(
            "ALTER TABLE sync_records ADD COLUMN media_type TEXT DEFAULT 'episode'"
        )
```

---

## 常见场景

### 写记录

```python
from app.core.database import database_manager

database_manager.log_sync_record(
    user_name="alice",
    title="测试番剧",
    season=1,
    episode=1,
    status="success",
    source="emby",
    match_trace=trace.to_json(),
)
```

### 查询记录

```python
records = database_manager.get_sync_records(
    user_name="alice",
    source="emby",
    limit=20,
)
```

### 测试中 mock 数据库

用 `tmp_path` fixture 创建临时 SQLite，或用 `MonkeyPatch` 替换 `database_manager._conn`：

```python
def test_log_record(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    # 用临时库替换单例连接
    ...
```

---

## Agent 通用会话（双职责模型）

Agent 会话数据由 `agent_runs` 与 `agent_steps` 两张表承载，采用 **trace / replay 双职责模型**：同一张表既是类型化观测日志（trace），又是可重放增量源（replay），区别仅在读取视角。

### 表职责

| 表 | 职责 | 核心字段 |
| --- | --- | --- |
| `agent_runs` | 一次会话的状态机 | `run_id`、`task_type`、`status`、`stop_reason`、`total_tokens`、时间列 |
| `agent_steps` | 每轮 LLM 调用 / 每次工具执行的 span | `span_id`、`name`、`status`、`model`、`tokens`、`latency_ms`、`tool_name`、`iteration`、`sequence`、`replay_delta` |

`agent_runs.status` 枚举：`pending` / `processing` / `succeeded` / `no_suggestion` / `failed` / `cancelled` / `applied` / `rejected`（`exhausted` 仅作 `stop_reason`，不作状态）。

### 双职责模型

**trace（观测视角）**：把 `agent_steps` 的 `name` / `status` / `model` / `tokens` / `latency_ms` / `tool_name` / `input_summary` / `iteration` / `sequence` / 时间列作为类型化观测字段，按 `(iteration, sequence)` 排序还原执行轨迹，用于前端展示与排错。

**replay（重放视角）**：把 `replay_delta` 列作为完整重放增量源。该列为 Fernet 加密、不截断、无大小上限，承载原执行每步的增量信息：

| span `name` | `replay_delta` 语义 |
| --- | --- |
| `seed` | `{"seed_messages": [...]}` — 种子前缀消息（run 启动时写入） |
| `llm_chat` | `{"response": {stop_reason, content, tool_calls}}` — LLM 返回 |
| `tool_execute` | `{"tool_result": {...}}` — 工具执行结果（含预算消息并入同轮最后 tool span） |

### span 记录位置与时序

- **chat span**：由 `llm_assist` 的 `chat_fn` 包装层写入（LLM 调用完成后落库）。
- **tool span**：由 `execute_batch` 的单工具包裹层写入。`start` 在工具执行**之前**（保证时序真实），每工具完成后**即刻独立事务落库**——批内部分成功时可从已完成 span 恢复，不会因后续工具失败丢失前序观测。
- **tool span 时序**：`started_at` 由 `start_span` 写入；`end_span` **不回写** `started_at`（仅当显式传入时才覆盖），避免用结束时间覆盖真实开始时间。`ended_at` 由 `end_span` 写入。
- **loop 层**：零 trace 逻辑，仅编排迭代。

### replay_delta 加密

- 落库为 `BGS1:` 前缀密文，由 `app/core/config_secret_crypto` 基于 Fernet 加密。
- 密钥由 `[auth] secret_key` 经 HKDF 派生。
- 仓储层**写加密 / 读解密透明**：`add_step` / `update_step` 写入前自动加密，`get_steps` 读取时自动解密（容错无前缀明文：原样返回）。
- 加密为 best-effort：失败时降级存明文，不中断主流程。
- 展示摘要（`display_json`）由 API 读取时从解密 `replay_delta` 截断生成，以 `...[shrinked]` 标记结尾（工具位于 `app/utils/truncate.py`）。
- `record_budget_message` 读改写（SELECT → 解密 → 改 → 加密写回）非原子：若解密阶段成功但写回前崩溃，存在窄窗导致 budget_message 未并入（best-effort 局限，可接受）。

### FK 级联与索引

```sql
CREATE TABLE agent_steps (
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id)
        ON DELETE CASCADE,
    ...
);
CREATE INDEX idx_agent_steps_run_id ON agent_steps(run_id);
```

- `PRAGMA foreign_keys=ON`。
- `agent_steps.run_id` 引用 `agent_runs(run_id)`，`ON DELETE CASCADE`——删除 run 时自动级联清除其所有 steps，避免脏数据。
- 辅助索引：`idx_agent_runs_sync_record_id`、`idx_agent_runs_status`。

### 轮转清理

由 `AgentRunsRepository.cleanup_expired(retention_days)` 执行，每轮 `llm_match` 调度触发。采用单条 DELETE，FK 级联删 steps。

**两腿 OR 语义**（时间比较统一为 epoch 秒整数）：

| 腿 | 条件 | 用途 |
| --- | --- | --- |
| 终态腿 | `status IN (终态) AND ended_at > 0 AND ended_at < cutoff` | 保留窗口外的已完成会话 |
| 活性腿 | `status IN (pending, processing) AND created_at < cutoff` | 崩溃遗留的过期死行一并删 |

- `retention_days` 来自 `[sync] llm_match_retention_days`，**默认 30 天**（滑动窗口）。
- `retention_days <= 0` 表示**永不清理**，直接返回 0。

### 断点续跑

`replay(run_id)`（位于 `app/services/agent/trace.py`）从 `agent_steps` 重建可续跑 `list[Message]`：

- 从 `seed` 行提取种子前缀消息，无需调用方提供 `seed_builder` / `max_iterations` 等入参。
- 按 `(iteration, sequence)` 排序逐轮重建：`llm_chat` → assistant 消息，`tool_execute` → 独立 user 消息（每条工具结果不合并）。
- 返回 `ReplayResult`：`messages`（含种子前缀）、`executed_iterations`、`missing_tool_calls`（最后一轮工具未执行完的缺失项）、`last_response`（终局响应供调用方消费）。
- 行缺失 / 空 delta 时在该轮 break，交回调用方从该轮重新 chat。
