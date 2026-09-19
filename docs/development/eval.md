---
title: 🧪 LLM 匹配 Eval 方案
order: 7
---

# 🧪 LLM 匹配 Eval 方案

面向 LLM 匹配增强的**两层评测体系**：L1 离线回放（零成本，进 CI，防回归）+
L2 真实模型 eval（本地/手动，量质量）。本文档给出设计、数据格式、运行手册与
CI 策略。

## 为什么是两层

| | L1 离线回放 | L2 真实模型 eval |
| --- | --- | --- |
| 何时跑 | 每个 PR（进 CI） | 本地 / 手动 / 定期 |
| 成本 | **0**（不发任何 API） | 按量计费（详见「成本」） |
| 数据 | cassette（真实执行的录制） | 黄金集 + 真实 LLM + 异源 judge |
| 验证什么 | prompt 构建 / 消息序列 / 解析 / 状态流转**没被改坏** | agent 真的变好了还是变坏了 |
| 回答 | "没回归" | "变好还是变坏" |

核心机制（对齐 VCR 模式）：**首次真实执行录制 cassette，之后全部回放**；
请求变化 → 指纹不匹配 → L1 失败（提示重录）；**CI 环境物理上禁止录制**。

## 目录结构

```
eval/
├── golden/public_v1.jsonl   # 黄金集（公开案例，人工标注期望）
├── fixtures/<case_id>.json  # cassette：口令录制（含请求指纹与期望终局）
├── lib.py                   # harness：临时 DB、回放/录制注入器、指纹、cassette IO
├── run_eval.py              # CLI：record / replay / live（CI 禁 record 机制守卫）
├── judge.py                 # LLM-as-judge（经本地 opencode CLI 调异源模型）
└── report.py                # markdown 报告（指标 + delta + judge 问题清单）
```

## 数据格式

### 黄金集（`golden/*.jsonl`，每行一条）

```json
{"id": "m001", "source": "issue#182",
 "input": {"title": "无职转生：到了异世界就拿出真本事", "ori_title": "", "season": 3,
           "episode": 1, "release_date": "2026-07-04", "media_type": "episode"},
 "expect": {"subject_id": 501963, "acceptable_ids": [501963], "expect_stop": "submit_suggestion"},
 "tags": ["cjk", "season-shift"], "note": "期望 501963"}
```

- `expect_stop`：`submit_suggestion`（应给出建议）或 `no_suggestion`（负样本）。
- 案例来源建议：GitHub issues 中的匹配错误报告（输入 + 正确条目号），标注后用
  Bangumi API 校验 `subject_id` 存在性与标题吻合。

### cassette（`fixtures/<case_id>.json`）

```json
{"schema_version": 1, "case_id": "m001",
 "meta": {"model": "deepseek-chat", "provider": "anthropic_compat", "thinking_level": "medium"},
 "rounds": [{"request_fingerprint": "sha256:...",
             "response": {"stop_reason": "...", "content": "...", "tool_calls": [], "usage": {}},
             "tool_results": [{"tool_use_id": "...", "tool_name": "...", "input": {},
                                "content": "...", "is_error": false}]}],
 "expected_outcome": {"run_status": "succeeded", "stop_reason": "submit_suggestion",
                       "candidate_subject_id": "501963", "notifications": 1}}
```

- **请求指纹** = `sha256(模型 + tools schema + tool_choice + 规范化消息序列)`；
  回放逐轮校验，任何 prompt/消息序列/工具协议变化都会失败（硬锁）。
- cassette 是测试夹具：与 golden 一起走代码评审；提交前做敏感信息扫描
  （不得含 token/key 等模式）。

## 运行手册

```bash
# 录制（本地，需要 key；CI 环境被硬拒绝）
EVAL_LLM_API_KEY=... uv run python eval/run_eval.py --mode record --all

# 回放（离线、零成本；CI 用）
uv run python eval/run_eval.py --mode replay --all

# 真实模型 eval + judge（本地/手动）
EVAL_LLM_API_KEY=... uv run python eval/run_eval.py --mode live --all --judge
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EVAL_LLM_API_KEY` | （必填） | record / live 的 LLM key（**不得入库**） |
| `EVAL_LLM_PROVIDER` | `anthropic_compat` | 或 `openai_compat` |
| `EVAL_LLM_BASE` | `https://api.deepseek.com/anthropic/v1` | OpenAI 兼容填 `https://api.deepseek.com` |
| `EVAL_LLM_MODEL` | `deepseek-chat` | 思考模型（如 `deepseek-v4-pro`）建议配大 max_tokens |
| `EVAL_LLM_MAX_TOKENS` | `2000` | **思考模型需显著调大**（如 16000），否则响应可能被截断 |
| `EVAL_LLM_TEMPERATURE` | `0.0` | 评测稳定性 |
| `EVAL_JUDGE` / `--judge-model` | `opencode-go/qwen3.7-plus` | judge 走本地 opencode CLI（异源） |

## CI 策略（零成本接入）

- **L1 进 CI**：将回放封装为 pytest 用例（`tests/eval/`），由现有 `ci-tests.yml`
  的 `pytest tests/` 收集——不需要新 workflow、不需要 secrets，fork PR 同样可跑。
- **机制守卫**：`--mode record` 在 `CI=true` 下直接报错退出——"CI 绝不录音"由代码
  保证，而非依赖纪律；回放路径完全不触网（chat/工具结果全部来自 cassette）。
- **L2 不进 PR 门禁**：真实模型调用成本与结果非确定性不适合作为合并条件；
  由贡献者/维护者本地或 fork 运行，报告贴 PR 作为质量证据。

## judge 可信度

- **异源判分**：judge 模型与生成模型不同源（生成 DeepSeek / judge Qwen）；
- 结构化输出（`{"verdict","score","issues"}`）+ 解析失败重试，失败计 error；
- 主指标用人工标注的 ground truth（命中率），judge 只补开放质量维度——judge 有偏差
  也不动摇主结论；
- 建议定期人工抽检校准（记录 judge 与人工一致率）。

## 首轮结果（2026-09-19，3 条案例）

| 模型 / 路径 | top1 | judge |
| --- | --- | --- |
| deepseek-chat · anthropic_compat | 1/3 | 5 / 2 / 2 |
| deepseek-chat · openai_compat | 1/3 | 5 / 1 / 1 |
| deepseek-v4-pro · anthropic_compat | **3/3** | 5 / 5 / 5 |
| deepseek-v4-pro · openai_compat | 2/3 | 5 / 5 / 1 |

首轮即发现并修复 2 个真实缺陷（已在 commit `57baaab` / `445cb00` 修复，含回归测试）：

1. **anthropic_compat 多工具并行 400**：loop 逐条追加 tool_result 违反 Anthropic
   "同一条消息紧跟"约束 → provider 归一化合并；
2. **thinking 模型兼容**：thinking 块需随 tool_use 回传；thinking 模式拒绝强制
   `tool_choice` → loop 保留响应块 + client/provider 三层降级（`auto`）。

遗留问题（恢复路径 thinking 回传、no_suggestion 的 tokens 口径、预算调优等）
见项目内部归档（`remain/eval_round1_findings.md`）。

## 演进方向

1. 黄金集扩至 20~30 条（继续从 issues 的匹配错误报告挖掘），覆盖 CJK / 罗马音 /
   季偏移 / 同名多义 / typo / 负样本；
2. L1 进 CI（`tests/eval/test_golden_replay.py` + fixture 安全扫描 + 指纹守卫自测）；
3. 恢复/重放路径的 thinking 块支持（与 `agent/runtime` 的 replay 重建同步升级）；
4. judge 人工抽检校准与成本上报（`llm_usage_logs`，`job_name="eval"`）；
5. 维护者可选 opt-in：为 L2 配置 secrets 后按 label/定时触发（默认不启用）。
