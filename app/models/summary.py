"""
Summary AI 观影报告数据模型。
"""

from typing import Literal, Optional

from pydantic import BaseModel


class LLMConfigResponse(BaseModel):
    """GET /llm 响应"""

    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""  # 掩码值（前端展示为 "***"）
    model: str = "gpt-4o-mini"
    max_tokens: int = 2000
    temperature: float = 0.7
    timeout: int = 60
    provider: str = "openai_compat"
    thinking_level: str = "off"


class LLMConfigUpdate(BaseModel):
    """PUT /llm 请求

    provider / thinking_level 为受控枚举：在 API 边界用 Literal 收口，
    非法值直接 422，避免脏值写入配置后在运行时才暴露（如未知 provider
    延迟到客户端构建才抛错、未知 thinking_level 被静默降级为 off）。
    """

    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    provider: Optional[Literal["openai_compat", "anthropic_compat"]] = None
    thinking_level: Optional[Literal["off", "low", "medium", "high"]] = None


class LLMTestResponse(BaseModel):
    """POST /llm/test 响应"""

    success: bool
    message: str
    model: Optional[str] = None
    latency_ms: Optional[int] = None


class SummaryJobCreate(BaseModel):
    """POST /api/summary/jobs 请求"""

    name: str = "New Summary"
    cron: str = "0 21 * * *"
    lookback_days: int = 1
    user_name: str = ""
    system_prompt: str = ""
    max_records: int = -1  # -1 表示不限制
    enabled: bool = True
    memory_enabled: bool = False  # 记忆开关（默认关闭）
    memory_limit: int = 5  # 注入记忆条数


class SummaryJobUpdate(BaseModel):
    """PUT /api/summary/jobs/{id} 请求"""

    name: Optional[str] = None
    cron: Optional[str] = None
    lookback_days: Optional[int] = None
    user_name: Optional[str] = None
    system_prompt: Optional[str] = None
    max_records: Optional[int] = None
    enabled: Optional[bool] = None
    memory_enabled: Optional[bool] = None
    memory_limit: Optional[int] = None


class SummaryJobResponse(BaseModel):
    """summary job CRUD 响应"""

    name: str
    cron: str
    lookback_days: int
    user_name: str
    system_prompt: str
    max_records: int
    enabled: bool
    memory_enabled: bool = False
    memory_limit: int = 5
    # 只读的 notification_type，供前端展示
    notification_type: str = ""

    @classmethod
    def from_config_dict(cls, data: dict) -> "SummaryJobResponse":
        """从 config_manager.get_summary_configs() 字典构建"""

        def _int(key: str, default: int) -> int:
            v = data.get(key, default)
            if v == "" or v is None:
                return default
            return int(v)

        def _memory_limit() -> int:
            try:
                return max(1, _int("memory_limit", 5))
            except ValueError:
                return 5

        name = str(data.get("name", ""))
        user_name = str(data.get("user_name", "") or "")
        notif_type = f"watching_summary_{name}"

        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = str(enabled).lower() in ("true", "1")

        memory_enabled = data.get("memory_enabled", False)
        if not isinstance(memory_enabled, bool):
            memory_enabled = str(memory_enabled).lower() in ("true", "1")

        return cls(
            name=str(data.get("name", "")),
            cron=str(data.get("cron", "0 21 * * *")),
            lookback_days=_int("lookback_days", 1),
            user_name=user_name,
            system_prompt=str(data.get("system_prompt", "")),
            max_records=_int("max_records", -1),
            enabled=enabled,
            memory_enabled=memory_enabled,
            memory_limit=_memory_limit(),
            notification_type=notif_type,
        )


class ClearMemoryRequest(BaseModel):
    """POST /api/summary/jobs/{name}/clear-memory 请求（二次确认）"""

    confirm: bool = False


class SummaryJobTestResponse(BaseModel):
    """POST /api/summary/jobs/{id}/test 响应"""

    success: bool
    job_name: str
    summary_text: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    record_count: int = 0
    error_message: str = ""


class LLMUsageStatsResponse(BaseModel):
    """GET /llm/stats 响应"""

    total_calls: int = 0
    total_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    error_count: int = 0
    avg_latency_ms: int = 0
    by_model: list[dict] = []
    by_job: list[dict] = []
    daily: list[dict] = []
