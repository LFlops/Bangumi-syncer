# Phase 2.0.3: 记忆清理与重置

> 所属计划：Bangumi-Syncer Agent 化三步增量计划
> 前置依赖：Phase 2.0.1（表 + repository）+ Phase 2.0.2（读取注入）
> 交付物：改名迁移、显式清空、MemoryService 服务化、clear-memory API + 前端按钮
> 执行时机：Phase 2.0.2 之后、Phase 2.1 之前（一个 phase 只做一件事）
> 设计总览见 `agent-phase2-memory.md`（清理机制小节）

## 目标

记忆的**清理与重置**能力：改名时记忆跟随（`rename_task`）、显式清空（`clear_task`）、服务层统一包装（MemoryService）、clear-memory API + 前端入口。解决"任务上下文被污染"的用户场景。

## task_id 规则定稿（始终由 name 派生，无独立生成规则）

| 操作 | task_id | 记忆处理 |
|---|---|---|
| 改名（跟随） | `summary-{新name}` | `rename_task` 迁移旧 task_id 记忆（事务 UPDATE 主表+归档） |
| 改名 + 重置 | `summary-{新name}` | 不迁移 + `clear_task` 清空旧 task_id（防孤儿——旧 task_id 无法再被引用） |
| 同名重置 | 不变 | `clear_task` |
| 复制新 job | `summary-{新name}`（必填新名） | 旧 job 完整保留（可回滚） |

- 不引入 task_id 版本后缀（v2 等）——映射表/稳定 ID 思路已否决（task_id 从"派生"变"查询"代价大）
- 消费标记（`consumed_run_id`）只关联 run_id，不依赖 task_id——改名/清空时天然处理：改名无需迁移消费标记；清空需联动清理该 task 相关消费标记

## MemoryService（memory 层统一包装，业务层不直接碰 repository）

```python
# app/services/memory/service.py

class MemoryService:
    """记忆服务层：统一包装记忆操作，业务层只调此入口。

    2.0.1/2.0.2 的底层能力（extract_and_store / retrieve）与 2.0.3 的
    清理能力（rename_task / clear_task / mark_consumed）统一经此暴露。
    """

    def __init__(self, memory_repo: AgentMemoryRepository, sync_records_repo=None):
        self._memory = memory_repo
        self._sync = sync_records_repo  # 消费标记联动

    def rename_task(self, task_type: str, old_task_id: str, new_task_id: str) -> int:
        """改名迁移：同一事务 UPDATE 主表 + 归档表的 task_id（记忆跟随任务）。
        消费标记无需迁移（consumed_run_id 只关联 run_id）。"""
        ...

    def clear_task(self, task_type: str, task_id: str) -> int:
        """显式清空（不可恢复，调用方二次确认）：
        同一事务删除主表 + 归档表 + 该 task 相关 sync_records 消费标记
        （防悬挂引用：否则 find_overlaps 标注"已消费于已删除的 run_id"）。

        **含 feedback 条目**（彻底清空语义）——用户要重置就全清（偏好也重来）；
        想保留偏好用"复制新 job"（旧 job 完整保留）。"""
        ...
```

- **层次**：业务层（summary service / API 层）只调 MemoryService；AgentMemoryRepository / SyncRecordsRepository 是纯 SQL 层
- **实例化**：`MemoryService(database_manager.memory, database_manager.sync_records)`——两个 repo 经 facade 公开属性注入（见 2.0.1 文件清单中 `__init__.py` 的 `memory`/`sync_records` 公开属性）
- 消费标记（sync_records 表）经 MemoryService 统一入口（mark_consumed / clear_task 联动）——业务层不直接碰 sync_records
- `extract_and_store` / `retrieve` 的调用也统一收口到 MemoryService（替代 2.0.1/2.0.2 文档中业务层直接构造 extractor/retriever 的方式——实现时以 MemoryService 为入口，extractor/retriever 作为其内部组件）

## API 与前端

```python
# POST /api/summary/jobs/{name}/clear-memory
# 请求体（显式确认，防误删）
{
    "confirm": true
}

# 响应
{
    "status": "success",
    "message": "任务记忆已清空",
    "deleted_records": 42
}
```

- 校验：`confirm` 必须为 true（二次确认语义）；job 不存在 404
- 前端：summary job 表单"记忆"区域加"清空记忆"按钮（危险操作样式 + 确认弹窗）
- 改名联动：summary job 改名 API（`save_summary_config` 的 old_name 流程）调用 `MemoryService.rename_task`（记忆跟随）——与 `rename_notification_type` 同流程

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/services/memory/service.py` | MemoryService（rename_task/clear_task + 统一入口） |
| 修改 | `app/core/database/agent_memory.py` | repository 纯 SQL（rename 的 UPDATE、clear 的 DELETE） |
| 修改 | `app/core/database/sync_records.py` | `clear_consumed_by_task`（clear_task 联动） |
| 修改 | `app/api/summary_jobs.py` | `POST /api/summary/jobs/{name}/clear-memory`；改名流程联动 rename_task |
| 修改 | `app/services/summary/service.py` | 调用收口 MemoryService（改名联动） |
| 修改 | `templates/config.html` + `static/js/` | "清空记忆"按钮（二次确认） |

> 总计：新增 1 个文件，修改 5 个文件

## BDD 测试场景

### Scenario C1 改名迁移记忆
- **Given** task_id `summary-daily` 有 3 条记忆（主表）+ 1 条归档
- **When** `rename_task("summary", "summary-daily", "summary-daily2")`
- **Then** 主表 + 归档表记录 task_id 全部更新为 `summary-daily2`
- **And** 消费标记不受影响（consumed_run_id 只关联 run_id）

### Scenario C2 改名 + 重置（不迁移 + 清空）
- **Given** 改名流程选择"重置"
- **When** 改名 + `clear_task`（旧 task_id）
- **Then** 旧 task_id 记忆删除（主表 + 归档 + 消费标记联动）
- **And** 新 task_id 从零开始

### Scenario C3 clear_task 消费标记联动
- **Given** task 有记忆 + sync_records 含该 task 的消费标记
- **When** `clear_task`
- **Then** 主表 + 归档 + 消费标记同一事务删除
- **And** 无悬挂引用（find_overlaps 不再标注"已消费于已删除的 run_id"）

### Scenario C4 clear-memory API 二次确认
- **Given** 已登录用户
- **When** POST `/api/summary/jobs/daily/clear-memory` body 无 `confirm`
- **Then** 返回 422（校验失败，不删除）
- **When** body `{"confirm": true}`
- **Then** 返回 success + deleted_records

### Scenario C5 前端清空按钮（Playwright E2E）
- **Given** summary job 表单
- **When** 点击"清空记忆"→ 确认弹窗 → 确认
- **Then** POST clear-memory 携带 confirm=true
- **And** 页面提示"任务记忆已清空"

### Scenario C6 改名不存在的 task_id（幂等）
- **Given** task_id `summary-nonexist` 无任何记忆
- **When** `rename_task("summary", "summary-nonexist", "summary-new")`
- **Then** 不报错，无操作（影响行数 0）

### Scenario C7 改名后消费标记仍有效
- **Given** task 改名（summary-daily → summary-daily2），sync_records 有消费标记（run_id 关联）
- **When** 下次执行 `find_overlaps`
- **Then** 仍命中已消费记录（consumed_run_id 只关联 run_id，改名不破坏）

### Scenario C8 clear_task 后 retrieve 为空
- **Given** task 有 5 条记忆，执行 `clear_task`
- **When** `retrieve(task_type, task_id)`
- **Then** 返回空列表（新上下文从零开始，无历史注入）

### Scenario C9 clear_task 隔离性
- **Given** task A 与 task B 各有记忆
- **When** `clear_task`（task A）
- **Then** task B 的记忆不受影响（可正常 retrieve）

### Scenario C10 clear_task 含 feedback 条目
- **Given** task 含 1 条 `outcome="feedback"` 条目
- **When** `clear_task`
- **Then** feedback 条目一并删除（彻底清空语义——保留偏好用"复制新 job"）

### Scenario C11 clear_task 幂等
- **Given** task 已无记忆
- **When** 再次 `clear_task`
- **Then** 不报错，影响行数 0

## Summary 层利用（改名联动 / 清空入口）

Summary 业务层通过 MemoryService 利用清理能力：

1. **改名联动**：job 改名流程（`save_summary_config(old_name)`）→ 调 `MemoryService.rename_task(task_type, old, new)`——记忆跟随；与 `rename_notification_type`（通知类型迁移）同流程
2. **清空入口**：`POST /api/summary/jobs/{name}/clear-memory`（confirm 校验）→ `MemoryService.clear_task`
3. **前端**：summary job 表单"清空记忆"按钮（危险操作样式 + 确认弹窗）

### Summary 集成测试场景（S1-S6）

### Scenario S1 改名后记忆跟随
- **Given** job "daily" 有记忆（task_id=summary-daily）
- **When** 改名为 "daily2"（PUT /api/summary/jobs 或 rename 流程）
- **Then** 记忆表 task_id 迁移为 `summary-daily2`
- **And** 下次执行注入延续历史（retrieve(summary-daily2) 有记录）

### Scenario S2 改名后通知类型迁移
- **Given** job "daily" 改名 "daily2"
- **When** 检查通知配置
- **Then** `watching_summary_daily2` 类型生效（rename_notification_type 联动）

### Scenario S3 清空记忆 API
- **Given** 已登录用户 + job "daily" 有记忆
- **When** `POST /api/summary/jobs/daily/clear-memory` `{"confirm": true}`
- **Then** 返回 success + deleted_records
- **And** 记忆表该 task 无记录

### Scenario S4 清空后执行从零开始
- **Given** clear-memory 后（无记忆）
- **When** 触发一次执行
- **Then** 无历史上下文注入（retrieve 空，system prompt 无 `## 历史执行上下文`）
- **And** 新记忆开始积累（本次执行写入第 1 条）

### Scenario S5 清空失败不影响 job
- **Given** clear_task 抛异常（如 DB 错误）
- **When** `POST clear-memory`
- **Then** 返回错误响应
- **And** job 配置与下次执行不受影响（总结功能正常）

### Scenario S6 前端改名 + 清空（Playwright E2E）
- **Given** summary job 表单
- **When** 改 job 名并保存 → 点击"清空记忆"→ 确认弹窗 → 确认
- **Then** 改名 PUT 携带新 name（记忆跟随）→ 清空 POST 携带 confirm=true
- **And** 页面提示保存成功与"任务记忆已清空"

## 验证方式

1. **Memory 单元测试**：C1-C11 全部通过（repository/service 层，无网络依赖）
2. **Summary 集成测试**：S1-S5 全部通过（API 层 + service 层）
3. **E2E**：C5、S6 在本地/CI 验证
4. 手动：改名 job → 检查记忆表 task_id 迁移 + 通知类型迁移；清空记忆 → 检查主表/归档/消费标记联动删除（含 feedback）
