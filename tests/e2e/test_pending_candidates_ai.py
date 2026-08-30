"""候选确认页 AI 推荐 / 徽标 / 评估过程折叠区 E2E（Task T17 / 场景 M31）

通过真实浏览器驱动验证前端读路径：
- 列表行徽标：processing → "AI 评估中"；succeeded + 建议 → "AI 推荐"
- 详情弹窗 AI 推荐区块（llm_subject_id 有值）：subject 链接 + reason + 「应用建议」按钮
- AI 评估过程折叠区（存在关联 run）：accordion 存在，可展开加载 steps

数据通过共享的 database_manager 直接落库（与 test_server 同源 SQLite 文件），
不触发真实 Bangumi 网络，避免 E2E 不稳定；「应用建议」点击走真实 confirm 会校验
subject_id（需联网），故此处仅断言按钮存在，点击行为由单测覆盖。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.e2e


def _seed_candidate(
    dbm, *, title, sync_record_id, llm_subject_id, llm_reason, run_status
):
    """落一条带 LLM 建议的候选 + 关联 agent run，返回 (candidate_id, run_id)。"""
    cid = dbm.log_pending_candidate(
        request_title=title,
        request_season=1,
        request_episode=1,
        user_name="admin",
        source="custom",
        candidates=[{"subject_id": "111", "name": "某番"}],
        sync_record_id=sync_record_id,
    )

    def _set_llm(conn):
        conn.execute(
            "UPDATE pending_candidates SET llm_subject_id=?, llm_reason=? WHERE id=?",
            (llm_subject_id, llm_reason, cid),
        )

    dbm._execute_with_lock(_set_llm)

    run_id = f"run-{uuid.uuid4()}"
    dbm.agent_runs.create_pending(run_id, "match", sync_record_id)
    if run_status == "processing":
        dbm.agent_runs.atomic_claim(run_id)
    elif run_status == "succeeded":
        dbm.agent_runs.atomic_claim(run_id)
        dbm.agent_runs.mark_succeeded(run_id, stop_reason="submit_suggestion")
    return cid, run_id


def test_list_ai_evaluating_badge(authed_page, base_url: str):
    """processing run → 列表行显示「AI 评估中」徽标。"""
    from app.core.database import database_manager

    title = f"E2E-AI评估中-{uuid.uuid4().hex[:8]}"
    _seed_candidate(
        database_manager,
        title=title,
        sync_record_id=91001,
        llm_subject_id="",
        llm_reason="",
        run_status="processing",
    )

    page = authed_page
    page.goto(f"{base_url}/pending-candidates")
    page.wait_for_load_state("networkidle")

    row = page.locator("#pending-candidates-table tbody tr", has_text=title)
    row.wait_for(timeout=10000)
    # 状态列含「AI 评估中」徽标
    assert "AI 评估中" in row.text_content(), "processing 候选未显示「AI 评估中」徽标"


def test_detail_ai_recommend_block_and_eval_accordion(authed_page, base_url: str):
    """succeeded + 建议 → 列表「AI 推荐」徽标 + 详情区块 + 评估过程折叠区。"""
    from app.core.database import database_manager

    title = f"E2E-AI推荐-{uuid.uuid4().hex[:8]}"
    cid, run_id = _seed_candidate(
        database_manager,
        title=title,
        sync_record_id=91002,
        llm_subject_id="777888",
        llm_reason="跨季判断为第一季",
        run_status="succeeded",
    )

    page = authed_page
    page.goto(f"{base_url}/pending-candidates")
    page.wait_for_load_state("networkidle")

    # 列表行「AI 推荐」徽标
    row = page.locator("#pending-candidates-table tbody tr", has_text=title)
    row.wait_for(timeout=10000)
    assert "AI 推荐" in row.text_content(), "succeeded+建议 候选未显示「AI 推荐」徽标"

    # 打开详情
    row.locator('button[title="查看候选"]').click()
    page.wait_for_selector("#candidate-detail-content", timeout=10000)

    content = page.locator("#candidate-detail-content")
    # AI 推荐区块：subject 链接 + 理由 + 应用建议按钮
    assert "AI 推荐" in content.text_content()
    assert "subject/777888" in content.text_content(), "AI 推荐区块未显示 subject 链接"
    assert "跨季判断为第一季" in content.text_content(), "AI 推荐区块未显示 reason"
    apply_btn = content.locator('button:has-text("应用建议")')
    assert apply_btn.count() > 0, "AI 推荐区块未显示「应用建议」按钮"

    # AI 评估过程折叠区
    accordion = page.locator("#agentRunAccordion-" + str(cid))
    assert accordion.count() > 0, "未渲染 AI 评估过程折叠区"
    assert "AI 评估过程" in accordion.text_content()

    # 展开折叠区应加载 steps（无步骤时返回空列表，渲染「暂无评估过程记录」
    # 或轮次统计，至少不报错）
    accordion.locator(".accordion-button").click()
    steps_body = page.locator("#agentRunSteps-" + str(cid))
    steps_body.wait_for(timeout=10000)
    assert steps_body.is_visible()
