"""Summary job 配置数据类。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SummaryJobConfig:
    name: str = ""
    enabled: bool = True
    cron: str = "0 21 * * *"
    lookback_days: int = 1
    user_name: str = ""
    system_prompt: str = (
        "你是一个轻松有趣的追番助手。用户会给你一段指定时间范围内的观影记录，请你用亲切自然的中文生成追番总结。\n\n"
        "规则：\n"
        '1. 如果记录为 0 条，告知用户"这段时间还没有追番记录哦~"\n'
        '2. 按番剧分组，简要描述观看进度（如"《芙莉莲》追到 S1E10"）\n'
        "3. 如果涉及多用户（记录中 user_name 不同），按用户分开描述\n"
        "4. 加一两句轻松评论，语气像朋友聊天，不要太正式\n"
        "5. 限制在 300 字以内"
    )
    max_records: int = -1  # -1 表示不限制
    memory_enabled: bool = False  # 记忆开关（默认关闭，每任务显式声明）
    memory_limit: int = 5  # 注入记忆条数（最小值 1，仅 enabled=true 时生效）

    @classmethod
    def from_config_dict(cls, data: dict) -> SummaryJobConfig:
        """从 config_manager.get_summary_configs() 字典创建实例。"""

        def _memory_limit() -> int:
            try:
                # 对齐前端约定（1–50）：上限防呆——过大值注入 token 成本线性膨胀
                return min(50, max(1, int(data.get("memory_limit", 5))))
            except (TypeError, ValueError):
                return 5

        return cls(
            name=str(data.get("name", "")),
            enabled=data.get("enabled", True)
            if isinstance(data.get("enabled"), bool)
            else str(data.get("enabled", "true")).lower() in ("true", "1"),
            cron=str(data.get("cron", "0 21 * * *")),
            lookback_days=int(data.get("lookback_days", 1)),
            user_name=str(data.get("user_name", "")),
            system_prompt=str(data.get("system_prompt", cls.system_prompt)),
            max_records=int(data.get("max_records", -1)),
            memory_enabled=data.get("memory_enabled", False)
            if isinstance(data.get("memory_enabled"), bool)
            else str(data.get("memory_enabled", "false")).lower() in ("true", "1"),
            memory_limit=_memory_limit(),
        )


@dataclass
class SummaryRecord:
    """summary 链路内部观影记录载体（_query_records 返回类型）。"""

    id: int
    timestamp: str
    user_name: str
    title: str
    bgm_title: str
    season: int
    episode: int
    media_type: str
    source: str
    status: str
    consumed_run_id: str | None = None  # 消费标记（NULL=未消费）
