"""死代码删除守卫：防止已移除的方法/配置回归。

这些符号在历史实现中曾存在（或仅残留于注释），现已删除；本文件逐一断言其不存在，
避免后续改动把死分支、悬空配置或空壳方法重新引入。
"""

from app.core.config import ConfigManager
from app.core.database.agent_runs import AgentRunsRepository
from app.services.matching import llm_assist


def test_requeue_failed_removed_from_agent_runs_repository():
    """requeue_failed 已删除（enqueue_match_run 已覆盖其语义）。"""
    assert not hasattr(AgentRunsRepository, "requeue_failed"), (
        "AgentRunsRepository.requeue_failed 应已删除（由 enqueue_match_run 的 requeued 决策替代）"
    )


def test_find_failed_by_sync_record_removed_from_agent_runs_repository():
    """find_failed_by_sync_record 已删除（orchestrator 不再调用）。"""
    assert not hasattr(AgentRunsRepository, "find_failed_by_sync_record"), (
        "AgentRunsRepository.find_failed_by_sync_record 应已删除（无调用方）"
    )


def test_ensure_llm_columns_removed_from_llm_assist():
    """ensure_llm_columns / _ensure_llm_columns 已删除（connection.py 建库迁移已覆盖）。"""
    assert not hasattr(llm_assist, "ensure_llm_columns"), (
        "llm_assist.ensure_llm_columns 应已删除（connection.py 建库期迁移已覆盖）"
    )
    assert not hasattr(llm_assist, "_ensure_llm_columns"), (
        "llm_assist._ensure_llm_columns 应已删除（connection.py 建库期迁移已覆盖）"
    )


def test_span_recorder_removed_from_llm_assist():
    """旧 _SpanRecorder 实现已删除（统一为 TraceRecorder），不应回归。"""
    assert not hasattr(llm_assist, "_SpanRecorder"), (
        "llm_assist._SpanRecorder 应已删除（统一为 TraceRecorder）"
    )


def test_wrap_stream_fn_removed_from_trace_recorder():
    """TraceRecorder.wrap_stream_fn 已删除（双聚合来源），不得回归。

    聚合与落库统一由 ``recorder.chat_span()`` 的 ``async with`` 承担：
    ``feed`` 单点聚合、``__aexit__`` 唯一一次 finalize。
    """
    from app.services.agent.observe.recorder import TraceRecorder

    assert not hasattr(TraceRecorder, "wrap_stream_fn"), (
        "TraceRecorder.wrap_stream_fn 应已删除（改由 chat_span() 单点聚合）"
    )


def test_trace_recorder_reexport_removed_from_llm_assist():
    """llm_assist 不再 re-export TraceRecorder（``as`` 同义别名会遮蔽导航）。

    测试与其它调用方直接从定义处（``agent.observe.recorder``）导入。
    """
    assert not hasattr(llm_assist, "TraceRecorder"), (
        "llm_assist.TraceRecorder 应已删除（re-export 别名；改从 observe.recorder 导入）"
    )


def test_llm_match_cross_call_cache_removed_from_config():
    """llm_match_cross_call_cache 已删除（enqueue_match_run 的 reused 决策无条件实现）。"""
    import inspect

    source = inspect.getsource(ConfigManager.get_sync_llm_match_config)
    assert "cross_call_cache" not in source, (
        "get_sync_llm_match_config 不应再含 cross_call_cache"
    )
