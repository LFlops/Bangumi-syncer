# Spec: LLM-Enhanced Notification Messages (Phase 1)

> **日期**: 2026-07-15  
> **状态**: 设计文档，暂不实现。先行实现 Phase 2（定时追番总结）。  
> **依赖**: 需要 Phase 2 的 `app/services/llm/` 基础模块就位。

## 概念

将 LLM 能力注入现有的实时通知链路，让用户选择将哪些 notification type 的消息由 LLM 改写/总结后再发送。

```
现有: event → Notifier → [cooldown] → 发送原始模板消息
Phase 1: event → Notifier → [LLM 模式?]
  ├─ No → 现有流程
  └─ Yes → LLMBuffer → 攒够一批 → LLMClient → 发送总结消息
```

## 场景举例

用户在 10 分钟内连续看了 3 集动画，触发了 3 次 `mark_success` 通知。现有模式会分别发送 3 条消息（cooldown 60s 内同 type 的会丢弃后两条，实际只发 1 条）。

LLM 模式：
- 3 次 mark_success 进入缓冲区
- 120s 窗口到期后，3 条事件合并发送给 LLM
- LLM 输出一条总结性消息："刚才你连续看完了《葬送的芙莉莲》S1E10-E12，进度追到最新了 🎉"

## 核心设计挑战

### 1. Cooldown vs Buffering 的语义冲突

| | Cooldown（现有） | Buffering（Phase 1） |
|---|---|---|
| 行为 | 丢弃同 type 的后续事件 | 蓄积同 type 的后续事件 |
| 发送时机 | 第一次立即发 | 窗口到期后发 |
| 单条事件 | 正常发送 | 跳过 LLM，用简单模板发送（省钱） |

需要修改 `send_notification_by_type`：LLM 模式下禁用 cooldown，改用 buffer 逻辑。

### 2. 批处理窗口

- 建议全局 120s（在 `[llm]` 中配置 `buffer_window`）
- 第一个事件进入 → 启动计时器
- 后续同 type 事件进入 → 追加到缓冲区 + 重置计时器
- 计时器到期 → 处理缓冲区
- 缓冲区只有 1 条 → 跳过 LLM，直接发原始模板
- 缓冲区 ≥ 2 条 → LLM 总结后发送

### 3. 线程安全

Notifier 在 ThreadPoolExecutor 中运行，缓冲区需要 `threading.Lock`。

### 4. 进程重启丢失

内存缓冲区，进程重启即丢。v1 可接受，需文档说明。

### 5. LLM 开关粒度

每个 webhook config 一个开关（新增 `llm_enabled` 字段），全局关闭时全部走现有流程。

```ini
[webhook-1]
url = https://oapi.dingtalk.com/robot/send?access_token=TOKEN
types = mark_success,mark_failed
llm_enabled = true            # 新增：该 webhook 使用 LLM 增强
llm_types = mark_success       # 可选：只增强特定 type，空=所有 types
```

### 6. LLM 失败降级

LLM 调用失败 → 回退到原始模板逐条发送（通知不能丢）。

## 实现复杂度评估

| 挑战 | 复杂度 | 说明 |
|------|--------|------|
| 缓冲 + 计时器 | 中 | 内存 dict + threading.Timer |
| Cooldown 互斥 | 中 | 需修改 Notifier 核心循环 |
| 线程安全 | 低 | Lock 包裹缓冲区操作 |
| 测试（时间敏感） | 中 | 需要 freezegun 或 mock timer |
| 前端（开关 UI） | 低 | webhook modal 加一个 checkbox |

**总体**: 比 Phase 2（定时 cron job）高一个量级。核心原因是需要改动 Notifier 的运行时常驻逻辑（cooldown/buffer 互斥），而 Phase 2 是独立的定时任务，不触碰现有通知链路。

## 依赖 Phase 2 的模块

- `app/services/llm/` — LLMClient, providers
- `app/core/database/llm_usage.py` — 用量日志

## 需要新增的模块

- `app/services/llm/buffer.py` — `LLMBuffer` 类：内存 dict + Lock + Timer
- Notifier 改动：cooldown/buffer 互斥逻辑

## 与其他方案的比较

| | Phase 2 (定时总结) | Phase 1 (LLM 通知) |
|---|---|---|
| 触发方式 | Cron 定时 | 事件驱动 + 计时器 |
| 数据来源 | sync_records 全量查询 | 当前通知事件的 data dict |
| LLM 输入 | 时间段内所有观看记录 | 当前批次的通知事件 |
| 延迟 | 定时（分钟-天级） | 实时（秒-分钟级） |
| 实现复杂度 | 低 | 高 |
| Notifier 改动 | 仅新增 type + payload | 需改 cooldown 逻辑 |
