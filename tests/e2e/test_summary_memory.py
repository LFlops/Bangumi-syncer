"""Summary 任务记忆表单 E2E 测试（Phase 2.0.2/2.0.3）。

覆盖：记忆开关/条数渲染与禁用态、保存携带 memory_enabled/memory_limit、
编辑回显、清空记忆按钮确认流程（C5/S6 部分）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _open_config_page(page, base_url: str):
    page.goto(f"{base_url}/config")
    page.wait_for_load_state("networkidle")


def _open_new_summary_modal(page):
    _open_config_page(page, "http://127.0.0.1:8000")
    page.get_by_role("button", name="新建任务").first.click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)


def test_memory_switch_renders_disabled_by_default(authed_page, base_url: str):
    """Scenario 2.0.2-R6 E2E：新建表单记忆开关默认关、条数输入禁用。"""
    page = authed_page
    _open_config_page(page, base_url)

    page.get_by_role("button", name="新建任务").first.click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)

    switch = page.locator("#summary-job-memory-enabled")
    assert switch.is_visible()
    assert not switch.is_checked()
    limit = page.locator("#summary-job-memory-limit")
    assert limit.is_disabled()
    assert limit.input_value() == "5"


def test_memory_switch_enables_limit_input(authed_page, base_url: str):
    """开启记忆开关 → 条数输入框启用。"""
    page = authed_page
    _open_config_page(page, base_url)

    page.get_by_role("button", name="新建任务").first.click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)

    page.locator("#summary-job-memory-enabled").check()
    assert not page.locator("#summary-job-memory-limit").is_disabled()


def test_save_creates_job_with_memory_fields(authed_page, base_url: str):
    """保存新建任务 → POST 携带 memory_enabled/memory_limit。"""
    page = authed_page
    _open_config_page(page, base_url)

    page.get_by_role("button", name="新建任务").first.click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)

    page.locator("#summary-job-name").fill("E2E记忆任务")
    page.locator("#summary-job-memory-enabled").check()
    page.locator("#summary-job-memory-limit").fill("3")

    with page.expect_response(
        lambda r: "/api/summary/jobs" in r.url and r.request.method == "POST"
    ) as resp_info:
        page.get_by_role("button", name="保存", exact=True).click()

    resp = resp_info.value
    assert resp.status == 200
    body = resp.request.post_data_json
    assert body["memory_enabled"] is True
    assert body["memory_limit"] == 3

    # 回显验证：编辑该任务时开关开启、条数=3
    page.wait_for_selector(".toast", timeout=5000)
    page.locator("#summary-jobs-list .card", has_text="E2E记忆任务").get_by_role(
        "button", name="编辑"
    ).click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)
    assert page.locator("#summary-job-memory-enabled").is_checked()
    assert page.locator("#summary-job-memory-limit").input_value() == "3"


def test_clear_memory_button_confirms_and_posts(authed_page, base_url: str):
    """Scenario C5：清空记忆按钮 → 确认弹窗 → POST confirm=true。"""
    page = authed_page
    _open_config_page(page, base_url)

    page.get_by_role("button", name="新建任务").first.click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)
    page.locator("#summary-job-name").fill("E2E清空任务")
    with page.expect_response(
        lambda r: "/api/summary/jobs" in r.url and r.request.method == "POST"
    ):
        page.get_by_role("button", name="保存", exact=True).click()

    # 新建态清空按钮禁用 → 编辑已存在任务后可用
    page.wait_for_selector(".toast", timeout=5000)
    btn = page.locator("#summary-job-clear-memory-btn")
    assert btn.is_disabled()
    page.locator("#summary-jobs-list .card", has_text="E2E清空任务").get_by_role(
        "button", name="编辑"
    ).click()
    page.wait_for_selector("#summaryJobModal.show", timeout=5000)
    assert not btn.is_disabled()

    page.on("dialog", lambda dialog: dialog.accept())
    with page.expect_response(
        lambda r: "/clear-memory" in r.url and r.request.method == "POST"
    ) as resp_info:
        btn.click(timeout=10000)

    resp = resp_info.value
    assert resp.status == 200
    body = resp.request.post_data_json
    assert body["confirm"] is True
