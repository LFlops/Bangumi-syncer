"""测试 SummaryService：generate_summary 和 execute_job（任务 3.2）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm.models import ChatResponse, Usage
from app.services.memory.models import MemoryEntry
from app.services.memory.service import MemoryService
from app.services.summary.models import SummaryJobConfig, SummaryRecord
from app.services.summary.service import SummaryService

# ── helpers ────────────────────────────────────────────────────────────


def _make_config(**overrides) -> SummaryJobConfig:
    """使用默认测试值构建最小 SummaryJobConfig。"""
    defaults = {
        "name": "test_job",
        "enabled": True,
        "cron": "0 21 * * *",
        "lookback_days": 1,
        "user_name": "",
        "system_prompt": "You are a helpful assistant.",
        "max_records": 200,
    }
    defaults.update(overrides)
    return SummaryJobConfig(**defaults)


def _sample_records() -> list[dict]:
    """返回两条示例观影记录供测试使用（对齐 get_records_in_date_range 输出键）。"""
    return [
        {
            "id": 1,
            "user_name": "dad",
            "title": "葬送的芙莉莲",
            "season": 1,
            "episode": 10,
            "source": "bangumi",
            "status": "success",
            "bgm_title": "葬送的芙莉莲",
            "timestamp": "2026-07-14 20:30:00",
            "media_type": "episode",
            "consumed_run_id": None,
        },
        {
            "id": 2,
            "user_name": "dad",
            "title": "鬼灭之刃",
            "season": 3,
            "episode": 5,
            "source": "bangumi",
            "status": "success",
            "bgm_title": "",
            "timestamp": "2026-07-14 21:00:00",
            "media_type": "movie",
            "consumed_run_id": None,
        },
    ]


def _mock_chat_response(
    content: str = "Test summary",
    model: str = "test-model",
    usage: Usage | None = None,
) -> ChatResponse:
    if usage is None:
        usage = Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    return ChatResponse(content=content, model=model, usage=usage)


def _summary_record(**overrides) -> SummaryRecord:
    defaults = {
        "id": 1,
        "timestamp": "2026-07-14 20:30:00",
        "user_name": "dad",
        "title": "葬送的芙莉莲",
        "bgm_title": "葬送的芙莉莲",
        "season": 1,
        "episode": 10,
        "media_type": "episode",
        "source": "bangumi",
        "status": "success",
        "consumed_run_id": None,
    }
    defaults.update(overrides)
    return SummaryRecord(**defaults)


def _records() -> list[SummaryRecord]:
    """两条 SummaryRecord 记录（execute_job 测试用）。"""
    return [
        _summary_record(id=1),
        _summary_record(
            id=2,
            title="鬼灭之刃",
            bgm_title="",
            season=3,
            episode=5,
            media_type="movie",
        ),
    ]


def _temp_db(temp_dir, name="svc.db"):
    """创建临时 DatabaseManager（memory 测试用）。"""
    with patch("app.core.database.logger"):
        from app.core.database import DatabaseManager

        return DatabaseManager(str(temp_dir / name))


# ── generate_summary ────────────────────────────────────────────────────


class TestGenerateSummary:
    """SummaryService.generate_summary() 测试。"""

    @pytest.mark.asyncio
    async def test_date_calculation(self):
        """lookback_days=1 应产生 date_from=昨天, date_to=今天。"""
        svc = SummaryService()
        config = _make_config(lookback_days=1)
        mock_records = _sample_records()

        # Patch LLM 客户端，使 chat() 返回 mock 响应。
        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = mock_records

            result = await svc.generate_summary(config)

        # 验证日期
        now = datetime.now()
        expected_date_to = now.strftime("%Y-%m-%d")
        expected_date_from = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        assert result["date_from"] == expected_date_from
        assert result["date_to"] == expected_date_to

    @pytest.mark.asyncio
    async def test_user_name_filter_passed_to_db(self):
        """设置 user_name 时，应将其转发给数据库查询。"""
        svc = SummaryService()
        config = _make_config(user_name="dad")

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = _sample_records()

            await svc.generate_summary(config)

        # 验证数据库调用参数
        call_kwargs = mock_db.get_records_in_date_range.call_args.kwargs
        assert call_kwargs["user_name"] == "dad"
        assert "date_from" in call_kwargs
        assert "date_to" in call_kwargs

    @pytest.mark.asyncio
    async def test_user_name_none_when_empty(self):
        """空 user_name 以 None 传给数据库查询。"""
        svc = SummaryService()
        config = _make_config(user_name="")

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = []

            await svc.generate_summary(config)

        call_kwargs = mock_db.get_records_in_date_range.call_args.kwargs
        assert call_kwargs["user_name"] is None

    @pytest.mark.asyncio
    async def test_system_prompt_in_messages(self):
        """LLM 被调用时 messages[0].content 为 system_prompt（role='system'）。"""
        svc = SummaryService()
        config = _make_config(system_prompt="Custom system instruction.")

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = _sample_records()

            await svc.generate_summary(config)

        args, _ = mock_llm_client.chat.call_args
        messages = args[0]
        assert len(messages) >= 2
        assert messages[0].role == "system"
        assert messages[0].content == "Custom system instruction."

    @pytest.mark.asyncio
    async def test_default_system_prompt_when_empty(self):
        """当 system_prompt 为空/空白时，使用类的默认值。"""
        svc = SummaryService()
        config = _make_config(system_prompt="   ")

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = _sample_records()

            await svc.generate_summary(config)

        args, _ = mock_llm_client.chat.call_args
        messages = args[0]
        # 应使用类的默认值，而非空白字符串
        assert messages[0].content == SummaryJobConfig.system_prompt

    @pytest.mark.asyncio
    async def test_user_prompt_template_rendered(self):
        """用户消息包含日期范围、记录数和记录文本。"""
        svc = SummaryService()
        config = _make_config(lookback_days=7)

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = _sample_records()

            await svc.generate_summary(config)

        args, _ = mock_llm_client.chat.call_args
        messages = args[0]
        user_content = messages[1].content  # role="user"

        assert messages[1].role == "user"
        assert "葬送的芙莉莲" in user_content
        assert "共 2 条" in user_content  # record_count=2

    def test_records_formatting(self):
        """记录被格式化为紧凑的文本表格。"""
        svc = SummaryService()
        records = [
            _summary_record(
                timestamp="2026-07-14 20:30:00",
                title="葬送的芙莉莲",
                bgm_title="葬送的芙莉莲",
                season=1,
                episode=10,
            ),
            _summary_record(
                id=2,
                timestamp="2026-07-14 21:00:00",
                title="鬼灭之刃",
                bgm_title="",
                season=3,
                episode=5,
                media_type="movie",
            ),
        ]
        formatted = svc._format_records(records)
        lines = formatted.split("\n")

        # 第一条记录：剧集类型
        assert "葬送的芙莉莲" in lines[0]
        assert "S1E10" in lines[0]
        assert "dad" in lines[0]

        # 第二条记录：电影类型 → 剧场版
        assert "鬼灭之刃" in lines[1]
        assert "剧场版" in lines[1]

    def test_empty_records_formatting(self):
        """空记录列表产生'（无记录）'。"""
        svc = SummaryService()
        formatted = svc._format_records([])
        assert formatted == "（无记录）"

    @pytest.mark.asyncio
    async def test_empty_records_in_generate_summary(self):
        """空记录时 generate_summary 正常工作，返回 record_count=0。"""
        svc = SummaryService()
        config = _make_config()

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=_mock_chat_response())

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = []

            result = await svc.generate_summary(config)

        assert result["record_count"] == 0
        # 验证用户提示中包含"（无记录）"
        args, _ = mock_llm_client.chat.call_args
        user_content = args[0][1].content
        assert "（无记录）" in user_content

    @pytest.mark.asyncio
    async def test_returns_llm_response_fields(self):
        """返回的字典包含 summary_text、model、usage、record_count、dates。"""
        svc = SummaryService()
        config = _make_config()
        expected_usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        expected_response = ChatResponse(
            content="summary here", model="gpt-4", usage=expected_usage
        )

        mock_llm_client = MagicMock()
        mock_llm_client.chat = AsyncMock(return_value=expected_response)

        with (
            patch("app.services.summary.service.database_manager") as mock_db,
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_llm_client,
            ),
        ):
            mock_db.get_records_in_date_range.return_value = _sample_records()

            result = await svc.generate_summary(config)

        assert result["summary_text"] == "summary here"
        assert result["model"] == "gpt-4"
        assert result["usage"] is expected_usage
        assert result["record_count"] == 2
        assert result["date_from"] is not None
        assert result["date_to"] is not None


# ── execute_job ─────────────────────────────────────────────────────────


class TestExecuteJob:
    """SummaryService.execute_job() 测试（2.0.2 重构后：_query_records → chat → 通知）。"""

    @staticmethod
    def _make_svc() -> SummaryService:
        return SummaryService()

    def _patch_llm(self, response: ChatResponse):
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(return_value=response)
        return patch(
            "app.services.summary.service.get_llm_client",
            return_value=mock_client,
        ), mock_client

    @pytest.mark.asyncio
    async def test_notification_type_empty_user(self):
        """notification_type 使用任务名称，而非 user_name。"""
        svc = self._make_svc()
        config = _make_config(user_name="")

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
        ):
            await svc.execute_job(config)

        mock_ns.notify.assert_called_once()
        call_args = mock_ns.notify.call_args
        assert call_args.args[0] == "watching_summary_test_job"

    @pytest.mark.asyncio
    async def test_notification_type_with_user(self):
        """notification_type 使用任务 ID，而非 user_name。"""
        svc = self._make_svc()
        config = _make_config(user_name="dad")

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
        ):
            await svc.execute_job(config)

        mock_ns.notify.assert_called_once()
        call_args = mock_ns.notify.call_args
        assert call_args.args[0] == "watching_summary_test_job"

    @pytest.mark.asyncio
    async def test_data_dict_has_required_fields(self):
        """传递给 notifier 的数据字典包含所有预期的键和值。"""
        svc = self._make_svc()
        config = _make_config(name="my_job", user_name="dad", lookback_days=3)
        usage = Usage(prompt_tokens=200, completion_tokens=100, total_tokens=300)
        response = _mock_chat_response(
            content="AI generated summary", model="claude-3", usage=usage
        )

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-12", "2026-07-15"),
            ),
            self._patch_llm(response)[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
        ):
            await svc.execute_job(config)

        data = mock_ns.notify.call_args.kwargs

        assert data["job_name"] == "my_job"
        assert data["user_name"] == "dad"
        assert data["summary_text"] == "AI generated summary"
        assert data["date_range"] == "2026-07-12 ~ 2026-07-15"
        assert data["record_count"] == 2
        assert data["lookback_days"] == 3
        assert data["model"] == "claude-3"
        assert data["tokens_used"] == 300

    @pytest.mark.asyncio
    async def test_notifier_called_once(self):
        """每次 execute_job 调用恰好触发一次 Notifier。"""
        svc = self._make_svc()
        config = _make_config()

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
        ):
            await svc.execute_job(config)

        assert mock_ns.notify.call_count == 1

    @pytest.mark.asyncio
    async def test_exception_in_query_is_caught(self):
        """_query_records 抛出异常时，发送失败通知并记录错误日志。"""
        svc = self._make_svc()
        config = _make_config(name="failing_job")

        with (
            patch.object(svc, "_query_records", side_effect=RuntimeError("LLM down")),
            patch("app.services.summary.service.notification_service") as mock_ns,
            patch("app.services.summary.service.logger") as mock_logger,
        ):
            await svc.execute_job(config)

        mock_ns.notify.assert_called_once()
        call_args = mock_ns.notify.call_args
        assert call_args.args[0] == "watching_summary_failing_job"
        kwargs = call_args.kwargs
        assert kwargs["in_app_type"] == "summary_job_failed"
        assert "LLM down" in kwargs["summary_text"]
        assert "执行异常" in kwargs["in_app_body"]

        # 日志应记录该错误
        error_msgs = [c[0][0] for c in mock_logger.error.call_args_list if c[0]]
        assert any("failing_job" in m and "LLM down" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_chat_exception_sends_llm_failed_notification(self):
        """chat 异常：按统一策略也要通知，且文案标注入阶段（summary_llm_failed）。"""
        svc = self._make_svc()
        config = _make_config(name="chat_fail_job")
        mock_client = MagicMock()
        mock_client.chat = AsyncMock(side_effect=RuntimeError("API down"))

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            patch(
                "app.services.summary.service.get_llm_client",
                return_value=mock_client,
            ),
            patch("app.services.summary.service.notification_service") as mock_ns,
            patch("app.services.summary.service.logger") as mock_logger,
        ):
            await svc.execute_job(config)

        # 统一策略：出错就通知，但类型/文案按阶段区分（chat → summary_llm_failed）
        mock_ns.notify.assert_called_once()
        call_args = mock_ns.notify.call_args
        assert call_args.args[0] == "watching_summary_chat_fail_job"
        kwargs = call_args.kwargs
        assert kwargs["in_app_type"] == "summary_llm_failed"
        assert "chat_fail_job" in kwargs["in_app_title"]
        assert "LLM 调用阶段" in kwargs["summary_text"]
        assert "API down" in kwargs["summary_text"]
        assert "LLM 调用失败" in kwargs["in_app_body"]

        # 日志记录失败阶段，便于排查
        error_msgs = [c[0][0] for c in mock_logger.error.call_args_list if c[0]]
        assert any("chat_fail_job" in m and "stage=chat" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_store_failure_sends_failed_notification(self, temp_dir):
        """记忆写入失败：统一策略下也通知（summary_job_failed），文案标注记忆写入阶段。"""
        svc, _ = self._svc_with_real_memory(temp_dir, job_name="store_fail_job")
        config = _make_config(name="store_fail_job", memory_enabled=True)

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
            patch("app.services.summary.service.logger") as mock_logger,
        ):
            svc.memory.extract_and_store = AsyncMock(
                side_effect=RuntimeError("db locked")
            )
            await svc.execute_job(config)

        mock_ns.notify.assert_called_once()
        call_args = mock_ns.notify.call_args
        assert call_args.args[0] == "watching_summary_store_fail_job"
        kwargs = call_args.kwargs
        assert kwargs["in_app_type"] == "summary_job_failed"
        assert "记忆写入阶段" in kwargs["summary_text"]
        assert "db locked" in kwargs["summary_text"]
        assert "记忆写入失败" in kwargs["in_app_body"]

        error_msgs = [c[0][0] for c in mock_logger.error.call_args_list if c[0]]
        assert any("store_fail_job" in m and "stage=store" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_notification_failure_no_second_notification(self):
        """spec 失败语义：通知失败 → 已写消费标记，不二次通知（只尝试一次）。"""
        svc = self._make_svc()
        config = _make_config(name="notify_fail_job")

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
            patch("app.services.summary.service.logger") as mock_logger,
        ):
            mock_ns.notify.side_effect = RuntimeError("notify down")
            await svc.execute_job(config)

        # 只尝试一次通知（失败后不落入通用 summary_job_failed 二次通知）
        mock_ns.notify.assert_called_once()
        error_msgs = [c[0][0] for c in mock_logger.error.call_args_list if c[0]]
        assert any("notify_fail_job" in m and "notify down" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_empty_llm_content_sends_llm_failed_notification(self):
        """LLM 返回空内容时，发送 summary_llm_failed 通知并写入收件箱。"""
        svc = self._make_svc()
        config = _make_config(name="empty_llm_job")
        empty = ChatResponse(content="", model="", usage=None, latency=5)

        with (
            patch.object(
                svc, "_query_records", return_value=([], "2026-07-14", "2026-07-15")
            ),
            self._patch_llm(empty)[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
            patch("app.services.summary.service.logger") as mock_logger,
        ):
            await svc.execute_job(config)

        mock_ns.notify.assert_called_once()
        call_args = mock_ns.notify.call_args
        assert call_args.args[0] == "watching_summary_empty_llm_job"
        kwargs = call_args.kwargs
        assert kwargs["in_app_type"] == "summary_llm_failed"
        assert "empty_llm_job" in kwargs["in_app_title"]
        assert "LLM 返回空内容" in kwargs["summary_text"]
        assert kwargs["model"] == ""

        # 应记录错误日志
        error_msgs = [c[0][0] for c in mock_logger.error.call_args_list if c[0]]
        assert any("empty_llm_job" in m and "LLM 返回空内容" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_tokens_used_zero_when_usage_is_none(self):
        """当 LLM 返回 usage=None 时，tokens_used 默认为 0。"""
        svc = self._make_svc()
        config = _make_config()
        response = ChatResponse(content="test", model="gpt-4", usage=None)

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(response)[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
        ):
            await svc.execute_job(config)

        data = mock_ns.notify.call_args.kwargs
        assert data["tokens_used"] == 0

    # ── 记忆注入（Phase 2.0.2）──────────────────────────────────────

    @staticmethod
    def _svc_with_real_memory(temp_dir, job_name="test_job"):
        """构造绑定了临时 DB 记忆 repo 的 SummaryService（真实 MemoryService）。"""
        db = _temp_db(temp_dir)
        svc = SummaryService()
        svc.memory = MemoryService(db.memory)
        # 真实检索链路 + 可断言的 extract（不真正调 LLM 摘要）
        svc.memory.extract_and_store = AsyncMock()
        return svc, db

    @pytest.mark.asyncio
    async def test_memory_disabled_short_circuits(self, temp_dir, reset_singletons):
        """R4：memory_enabled=false（默认）→ 不注入不写入。"""
        svc, db = self._svc_with_real_memory(temp_dir)
        db.memory.store_and_mark(
            MemoryEntry(
                task_type="summary",
                task_id="summary-test_job",
                run_id="run-1",
                summary="昨日看了芙莉莲",
            ),
            [],
        )
        config = _make_config()  # memory_enabled=False
        _llm_patch, mock_client = self._patch_llm(_mock_chat_response())

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            _llm_patch,
            patch("app.services.summary.service.notification_service"),
        ):
            await svc.execute_job(config)

        messages = mock_client.chat.call_args.args[0]
        assert messages[0].content == "You are a helpful assistant."
        assert "历史执行上下文" not in messages[0].content
        svc.memory.extract_and_store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_injects_recent_context(self, temp_dir, reset_singletons):
        """R1：memory_enabled=true → system prompt 含历史上下文（最近 2 条摘要）。"""
        svc, db = self._svc_with_real_memory(temp_dir)
        db.memory.store_and_mark(
            MemoryEntry(
                task_type="summary",
                task_id="summary-test_job",
                run_id="run-1",
                summary="昨日看了芙莉莲",
            ),
            [],
        )
        db.memory.store_and_mark(
            MemoryEntry(
                task_type="summary",
                task_id="summary-test_job",
                run_id="run-2",
                summary="用户反馈：不要太啰嗦",
            ),
            [],
        )
        config = _make_config(memory_enabled=True, memory_limit=5)
        _llm_patch, mock_client = self._patch_llm(_mock_chat_response())

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            _llm_patch,
            patch("app.services.summary.service.notification_service"),
        ):
            await svc.execute_job(config)

        messages = mock_client.chat.call_args.args[0]
        assert len([m for m in messages if m.role == "system"]) == 1
        assert "## 历史执行上下文" in messages[0].content
        assert "- 昨日看了芙莉莲" in messages[0].content
        assert "- 用户反馈：不要太啰嗦" in messages[0].content
        # 原文 system prompt 保留在注入内容之后
        assert "You are a helpful assistant." in messages[0].content

    @pytest.mark.asyncio
    async def test_memory_limit_passed_to_retrieve(self, temp_dir, reset_singletons):
        """R8：memory_limit 配置透传给 retrieve。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        config = _make_config(memory_enabled=True, memory_limit=3)

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service"),
            patch.object(svc, "memory") as mock_memory,
        ):
            mock_memory.retrieve.return_value = []
            mock_memory.format_memory_context.return_value = ""
            mock_memory.find_overlaps.return_value = []
            await svc.execute_job(config)

        mock_memory.retrieve.assert_called_once_with(
            task_type="summary",
            task_id="summary-test_job",
            limit=3,
            keywords=["葬送的芙莉莲"],
        )

    @pytest.mark.asyncio
    async def test_extract_failure_does_not_block_notification(
        self, temp_dir, reset_singletons
    ):
        """R7：提取记忆失败（DB 异常）不影响 _dispatch_notification。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        svc.memory.extract_and_store = AsyncMock(side_effect=RuntimeError("db down"))
        config = _make_config(memory_enabled=True)

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service") as mock_ns,
            patch("app.services.summary.service.logger") as mock_logger,
        ):
            await svc.execute_job(config)

        mock_ns.notify.assert_called_once()
        assert mock_logger.error.called  # 异常已记录日志

    @pytest.mark.asyncio
    async def test_extract_called_with_full_context_when_enabled(
        self, temp_dir, reset_singletons
    ):
        """记忆开启时：extract_and_store 收到完整上下文与今日记录 id。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        config = _make_config(memory_enabled=True)

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(
                _mock_chat_response(
                    "summary here",
                    usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
                )
            )[0],
            patch("app.services.summary.service.notification_service"),
        ):
            await svc.execute_job(config)

        svc.memory.extract_and_store.assert_awaited_once()
        kwargs = svc.memory.extract_and_store.call_args.kwargs
        assert kwargs["task_type"] == "summary"
        assert kwargs["task_id"] == "summary-test_job"
        assert kwargs["outcome"] == "success"
        assert kwargs["tokens_used"] == 3
        assert kwargs["record_ids"] == [1, 2]
        assert kwargs["response"].content == "summary here"

    # ── 窗口重叠去重（D1/D3/D4）─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_overlap_no_note(self, temp_dir, reset_singletons):
        """D1：明细全部未消费 → system prompt 不含 overlap_note。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        config = _make_config(memory_enabled=True)
        _llm_patch, mock_client = self._patch_llm(_mock_chat_response())

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(_records(), "2026-07-14", "2026-07-15"),
            ),
            _llm_patch,
            patch("app.services.summary.service.notification_service"),
        ):
            await svc.execute_job(config)

        assert "已在上次总结中覆盖" not in mock_client.chat.call_args.args[0][0].content

    @pytest.mark.asyncio
    async def test_overlap_note_injected_into_context(self, temp_dir, reset_singletons):
        """D3：有重叠记录 → 标注并入历史上下文（同一 system prompt 内）。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        records = [
            _summary_record(id=1, consumed_run_id="run-abc12345"),
            _summary_record(id=2, consumed_run_id=None),
        ]
        config = _make_config(memory_enabled=True)
        _llm_patch, mock_client = self._patch_llm(_mock_chat_response())

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(records, "2026-07-14", "2026-07-15"),
            ),
            _llm_patch,
            patch("app.services.summary.service.notification_service"),
        ):
            await svc.execute_job(config)

        system_prompt = mock_client.chat.call_args.args[0][0].content
        # 标注位于历史上下文部分（## 历史执行上下文 内），非独立 system message
        assert "## 历史执行上下文" in system_prompt
        assert "已在上次总结中覆盖，可简述或跳过，不必重复展开" in system_prompt
        assert "葬送的芙莉莲 S1E10" in system_prompt
        assert "已消费于总结 run-abc1" in system_prompt
        assert "鬼灭之刃" not in system_prompt  # 未消费记录不标注

    @pytest.mark.asyncio
    async def test_new_records_rendered_normally(self, temp_dir, reset_singletons):
        """D4：非重叠（未消费）记录正常呈现，不被标注。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        records = [
            _summary_record(id=1, consumed_run_id=None),
            _summary_record(id=2, consumed_run_id=None),
        ]
        config = _make_config(memory_enabled=True)
        _llm_patch, mock_client = self._patch_llm(_mock_chat_response())

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(records, "2026-07-14", "2026-07-15"),
            ),
            _llm_patch,
            patch("app.services.summary.service.notification_service"),
        ):
            await svc.execute_job(config)

        messages = mock_client.chat.call_args.args[0]
        user_content = messages[1].content
        assert "葬送的芙莉莲" in user_content
        assert "已在上次总结中覆盖" not in messages[0].content

    @pytest.mark.asyncio
    async def test_keywords_from_today_records(self, temp_dir, reset_singletons):
        """关键词 = 今日明细 bgm_title 提取（去重取前 5）。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        records = [
            _summary_record(id=1, bgm_title="葬送的芙莉莲"),
            _summary_record(id=2, bgm_title="葬送的芙莉莲"),
            _summary_record(id=3, bgm_title="", title="无bgm标题"),
        ]
        config = _make_config(memory_enabled=True)

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(records, "2026-07-14", "2026-07-15"),
            ),
            self._patch_llm(_mock_chat_response())[0],
            patch("app.services.summary.service.notification_service"),
            patch.object(svc, "memory") as mock_memory,
        ):
            mock_memory.retrieve.return_value = []
            mock_memory.format_memory_context.return_value = ""
            mock_memory.find_overlaps.return_value = []
            await svc.execute_job(config)

        kwargs = mock_memory.retrieve.call_args.kwargs
        assert kwargs["keywords"] == ["葬送的芙莉莲"]  # 空 bgm_title 已过滤

    @pytest.mark.asyncio
    async def test_overlap_note_with_empty_context_no_leading_blank_line(
        self, temp_dir
    ):
        """#9：无历史记忆仅有重叠标注时，system prompt 不产生前导空行。"""
        svc, _ = self._svc_with_real_memory(temp_dir)
        records = [
            _summary_record(id=1, consumed_run_id="run-abc12345"),
            _summary_record(id=2, consumed_run_id=None),
        ]
        config = _make_config(memory_enabled=True)
        _llm_patch, mock_client = self._patch_llm(_mock_chat_response())

        with (
            patch.object(
                svc,
                "_query_records",
                return_value=(records, "2026-07-14", "2026-07-15"),
            ),
            _llm_patch,
            patch("app.services.summary.service.notification_service"),
            patch.object(svc, "memory") as mock_memory,
        ):
            mock_memory.retrieve.return_value = []  # 无历史记忆
            mock_memory.format_memory_context.return_value = ""  # context 为空
            mock_memory.find_overlaps.return_value = records[
                :1
            ]  # 仅已消费记录（真实实现会过滤 None）
            await svc.execute_job(config)

        system_prompt = mock_client.chat.call_args.args[0][0].content
        # 标注直接作为「## 历史执行上下文」的开头，无前导空行
        assert "## 历史执行上下文\n以下记录已在上次总结中覆盖" in system_prompt
        assert "## 历史执行上下文\n\n" not in system_prompt
