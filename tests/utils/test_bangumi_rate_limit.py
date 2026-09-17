"""进程级 Bangumi API 令牌桶限速测试

覆盖 BDD 场景：
1. 超出速率时等待（注入时钟/sleep，不真实等待）
2. 多实例共享同一令牌桶
3. 直连回退路径同样限速
4. 429 自适应冷却（Retry-After / 默认 / 指数延长 / 成功重置）
5. 配置解析（生效值与非法/缺失回退）
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.config import config_manager
from app.utils.bangumi_api import BangumiApi, rate_limit
from app.utils.bangumi_api.http_layer import _parse_retry_after
from app.utils.bangumi_api.rate_limit import (
    DEFAULT_BURST,
    DEFAULT_RATE,
    RateLimiter,
    get_bgm_rate_limiter,
    reset_bgm_rate_limiter,
)


class FakeClock:
    """可注入时钟：单调递增的秒计数"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSleeper:
    """可注入 sleep：记录等待并推进注入时钟（不真实等待）"""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


class RecordingLimiter:
    """记录调用的假限速器，用于验证 http_layer 的挂载点"""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.notified: list[float | None] = []
        self.acquire_calls = 0
        self.success_calls = 0

    def acquire(self) -> None:
        self.acquire_calls += 1
        self.events.append("acquire")

    def notify_rate_limited(self, retry_after: float | None = None) -> None:
        self.notified.append(retry_after)

    def notify_success(self) -> None:
        self.success_calls += 1


def _mock_response(status_code: int = 200, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers if headers is not None else {}
    resp.text = ""
    resp.json.return_value = {}
    resp.elapsed.total_seconds.return_value = 0.01
    resp.request = MagicMock()
    return resp


@pytest.fixture(autouse=True)
def _reset_limiter_after_test():
    """每个用例结束后恢复惰性单例，避免用例间相互污染"""
    yield
    reset_bgm_rate_limiter(None)


# ---------------------------------------------------------------------------
# 场景 1：超出速率时等待
# ---------------------------------------------------------------------------


def test_acquire_within_burst_does_not_wait():
    """burst 内连续请求无需等待"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(rate=1.0, burst=3, clock=clock, sleep=sleeper)

    for _ in range(3):
        limiter.acquire()

    assert sleeper.calls == []


def test_acquire_waits_when_tokens_exhausted():
    """速率=1/s、burst=3，令牌耗尽后第 4 个请求等待 1 秒"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(rate=1.0, burst=3, clock=clock, sleep=sleeper)
    for _ in range(3):
        limiter.acquire()

    limiter.acquire()

    assert len(sleeper.calls) == 1
    assert sleeper.calls[0] == pytest.approx(1.0)


def test_acquire_is_thread_safe_under_concurrency():
    """并发调用不丢令牌、不死锁"""
    limiter = RateLimiter(rate=10000.0, burst=100)
    results: list[bool] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(5):
                limiter.acquire()
            results.append(True)
        except Exception as e:  # noqa: BLE001  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(results) == 8


def test_acquire_waits_without_holding_lock():
    """等待期间不持锁休眠：另一线程能在 sleep 进行中立即 notify

    若 ``acquire`` 在锁内休眠，notify 会一直阻塞到等待结束（此处 5s 超时），
    断言随即失败，可稳定暴露"持锁休眠"问题。
    """
    clock = FakeClock()
    entered = threading.Event()
    release = threading.Event()

    def slow_sleep(seconds: float) -> None:
        entered.set()
        release.wait(timeout=5)
        clock.advance(seconds)

    limiter = RateLimiter(rate=1.0, burst=1, clock=clock, sleep=slow_sleep)
    limiter.acquire()  # 耗尽唯一令牌

    worker = threading.Thread(target=lambda: limiter.acquire(), daemon=True)
    worker.start()
    assert entered.wait(timeout=5), "acquire 未进入等待"

    notified = threading.Event()

    def notifier() -> None:
        limiter.notify_rate_limited(0.0)
        limiter.notify_success()
        notified.set()

    notifier_thread = threading.Thread(target=notifier, daemon=True)
    notifier_thread.start()
    assert notified.wait(timeout=2.0), (
        "notify 在 acquire 等待期间被阻塞（疑似持锁休眠）"
    )

    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_freeze_update_visible_during_wait():
    """等待期间 notify_rate_limited 生效：等待者按新冻结时间重新计算"""
    clock = FakeClock()
    entered = threading.Event()
    release = threading.Event()
    waits: list[float] = []

    def controlled_sleep(seconds: float) -> None:
        waits.append(seconds)
        entered.set()
        release.wait(timeout=5)
        clock.advance(seconds)

    limiter = RateLimiter(rate=1.0, burst=1, clock=clock, sleep=controlled_sleep)
    limiter.acquire()  # 耗尽唯一令牌

    total: list[float] = []
    worker = threading.Thread(
        target=lambda: total.append(limiter.acquire()), daemon=True
    )
    worker.start()
    assert entered.wait(timeout=5), "acquire 未进入等待"

    # 等待期间下发更长的 429 冻结（300s > 首轮 1s 的补令牌等待）
    notified = threading.Event()

    def notifier() -> None:
        limiter.notify_rate_limited(300.0)
        notified.set()

    notifier_thread = threading.Thread(target=notifier, daemon=True)
    notifier_thread.start()
    assert notified.wait(timeout=2.0), "notify 在 acquire 等待期间被阻塞"

    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    # 首轮按旧状态等 1s（补令牌），复查后按新冻结续等 299s，累计 300s
    assert waits[0] == pytest.approx(1.0)
    assert waits[1] == pytest.approx(299.0)
    assert total == [pytest.approx(300.0)]


# ---------------------------------------------------------------------------
# 场景 2：多实例共享同一令牌桶
# ---------------------------------------------------------------------------


def test_get_bgm_rate_limiter_returns_same_instance():
    reset_bgm_rate_limiter(None)

    assert get_bgm_rate_limiter() is get_bgm_rate_limiter()


def test_two_bangumi_api_instances_share_same_bucket():
    """两个 BangumiApi 实例共用同一令牌桶：burst=1 时第二个实例必须等待"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    shared = RateLimiter(rate=1.0, burst=1, clock=clock, sleep=sleeper)
    reset_bgm_rate_limiter(shared)

    mock_resp = _mock_response(200)
    with patch("app.utils.bangumi_api.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.request.return_value = mock_resp
        api1 = BangumiApi(access_token="t")
        api2 = BangumiApi(access_token="t")

        api1.get("me")
        assert sleeper.calls == []

        api2.get("me")

    assert len(sleeper.calls) == 1
    assert sleeper.calls[0] == pytest.approx(1.0)


def test_req_not_auth_session_shares_same_bucket():
    """同一实例的 self.req 与 _req_not_auth 共用同一令牌桶"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    shared = RateLimiter(rate=1.0, burst=1, clock=clock, sleep=sleeper)
    reset_bgm_rate_limiter(shared)

    mock_resp = _mock_response(200)
    with patch("app.utils.bangumi_api.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.request.return_value = mock_resp
        api = BangumiApi(access_token="t")

        api.get("me")  # 经 self.req 消耗唯一令牌
        assert sleeper.calls == []

        api._request_with_retry("GET", api._req_not_auth, "https://api.bgm.tv/v0/me")

    assert len(sleeper.calls) == 1
    assert sleeper.calls[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 场景 3：直连回退路径同样限速
# ---------------------------------------------------------------------------


def test_direct_connection_waits_when_tokens_exhausted():
    """直连路径同样先 acquire 令牌"""
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(rate=1.0, burst=1, clock=clock, sleep=sleeper)
    reset_bgm_rate_limiter(limiter)
    limiter.acquire()  # 耗尽唯一令牌

    api = BangumiApi(access_token="t")
    mock_resp = _mock_response(200)
    with patch("app.utils.bangumi_api.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.request.return_value = mock_resp
        result = api._try_direct_connection("GET", "https://api.bgm.tv/v0/me")

    assert result == mock_resp
    assert len(sleeper.calls) == 1
    assert sleeper.calls[0] == pytest.approx(1.0)


def test_direct_connection_acquires_before_sending_request():
    """acquire 必须发生在实际发送直连请求之前"""
    events: list[str] = []
    limiter = RecordingLimiter(events)
    reset_bgm_rate_limiter(limiter)

    api = BangumiApi(access_token="t")
    mock_resp = _mock_response(200)

    def _do_request(*args, **kwargs):
        events.append("request")
        return mock_resp

    with patch("app.utils.bangumi_api.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.request.side_effect = _do_request
        api._try_direct_connection("GET", "https://api.bgm.tv/v0/me")

    assert events == ["acquire", "request"]


def test_request_with_retry_acquires_before_sending_request():
    """主路径 acquire 必须发生在 session.request 之前"""
    events: list[str] = []
    limiter = RecordingLimiter(events)
    reset_bgm_rate_limiter(limiter)

    api = BangumiApi(access_token="t")
    mock_resp = _mock_response(200)
    mock_session = MagicMock()

    def _do_request(*args, **kwargs):
        events.append("request")
        return mock_resp

    mock_session.request.side_effect = _do_request
    api._request_with_retry("GET", mock_session, "https://api.bgm.tv/v0/me")

    assert events == ["acquire", "request"]


def test_direct_connection_error_warning_includes_status_code():
    """直连回退遇 >=400 时 warning 带 status_code，便于排查"""
    limiter = RecordingLimiter()
    reset_bgm_rate_limiter(limiter)

    api = BangumiApi(access_token="t")
    mock_resp = _mock_response(503)

    with (
        patch("app.utils.bangumi_api.httpx.Client") as mock_client_cls,
        patch("app.utils.bangumi_api.http_layer.logger") as mock_http_logger,
    ):
        mock_client_cls.return_value.request.return_value = mock_resp
        result = api._try_direct_connection("GET", "https://api.bgm.tv/v0/me")

    assert result is None
    warnings = [
        str(c.args[0]) for c in mock_http_logger.warning.call_args_list if c.args
    ]
    assert any("503" in msg for msg in warnings), warnings


# ---------------------------------------------------------------------------
# 场景 4：429 自适应冷却
# ---------------------------------------------------------------------------


def _make_limiter():
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(rate=1.0, burst=3, clock=clock, sleep=sleeper)
    return limiter, sleeper


def test_notify_rate_limited_with_retry_after_freezes_bucket():
    """收到 Retry-After: 60，令牌桶冻结至少 60 秒"""
    limiter, sleeper = _make_limiter()

    limiter.notify_rate_limited(60.0)
    limiter.acquire()

    assert sleeper.calls == [pytest.approx(60.0)]


def test_notify_rate_limited_freezes_at_least_default_even_if_hint_smaller():
    """Retry-After 小于默认冷却时，冻结仍不低于默认 60 秒"""
    limiter, sleeper = _make_limiter()

    limiter.notify_rate_limited(5.0)
    limiter.acquire()

    assert sleeper.calls == [pytest.approx(60.0)]


def test_notify_rate_limited_without_retry_after_defaults_to_60():
    """无 Retry-After 头时默认冻结 60 秒"""
    limiter, sleeper = _make_limiter()

    limiter.notify_rate_limited(None)
    limiter.acquire()

    assert sleeper.calls == [pytest.approx(60.0)]


def test_consecutive_rate_limits_escalate_exponentially():
    """连续命中 429 时按 60→120→240 指数延长"""
    limiter, sleeper = _make_limiter()

    for _ in range(3):
        limiter.notify_rate_limited(None)
        limiter.acquire()

    assert sleeper.calls == [
        pytest.approx(60.0),
        pytest.approx(120.0),
        pytest.approx(240.0),
    ]


def test_consecutive_rate_limits_capped_at_3600():
    """连续命中 429 的冷却时长上限为 3600 秒"""
    limiter, sleeper = _make_limiter()

    for _ in range(8):
        limiter.notify_rate_limited(None)
        limiter.acquire()

    assert max(sleeper.calls) == pytest.approx(3600.0)
    assert all(w <= 3600.0 for w in sleeper.calls)


def test_notify_success_resets_escalation():
    """成功请求后重置指数升级，再次 429 从 60 秒重新开始"""
    limiter, sleeper = _make_limiter()

    limiter.notify_rate_limited(None)
    limiter.acquire()
    limiter.notify_success()

    limiter.notify_rate_limited(None)
    limiter.acquire()

    assert sleeper.calls == [pytest.approx(60.0), pytest.approx(60.0)]


def test_rate_limit_hint_capped_at_max_cooldown():
    """Retry-After 超过上限时冻结封顶 3600s（不会无限冻结）"""
    limiter, sleeper = _make_limiter()

    limiter.notify_rate_limited(100000)
    limiter.acquire()

    assert sleeper.calls == [pytest.approx(3600.0)]


def test_notify_success_not_called_for_5xx():
    """500 响应不重置 429 升级计数：429→500→429 的冻结应为 120s 而非 60s"""
    limiter, sleeper = _make_limiter()
    reset_bgm_rate_limiter(limiter)
    api = BangumiApi(access_token="t")

    api._apply_rate_limit_notification(_mock_response(429, {}))
    limiter.acquire()

    api._apply_rate_limit_notification(_mock_response(500, {}))
    api._apply_rate_limit_notification(_mock_response(429, {}))

    limiter.acquire()

    assert sleeper.calls == [pytest.approx(60.0), pytest.approx(120.0)]


# ---------------------------------------------------------------------------
# 场景 4（http_layer 挂载）：429 通知与 Retry-After 解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("60", 60.0),
        ("  120  ", 120.0),
        ("0", 0.0),
    ],
)
def test_parse_retry_after_seconds(header, expected):
    res = _mock_response(429, {"Retry-After": header})

    assert _parse_retry_after(res) == pytest.approx(expected)


def test_parse_retry_after_http_date():
    res = _mock_response(
        429,
        {
            "Retry-After": format_datetime(
                datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True
            )
        },
    )

    parsed = _parse_retry_after(res)

    assert parsed is not None
    assert 80.0 <= parsed <= 91.0


def test_parse_retry_after_invalid_returns_none():
    res = _mock_response(429, {"Retry-After": "not-a-valid-value"})

    assert _parse_retry_after(res) is None


def test_parse_retry_after_absent_returns_none():
    res = _mock_response(429, {})

    assert _parse_retry_after(res) is None


def test_request_with_retry_notifies_limiter_on_429():
    """主路径收到 429 时把 Retry-After 交给令牌桶"""
    limiter = RecordingLimiter()
    reset_bgm_rate_limiter(limiter)

    api = BangumiApi(access_token="t")
    mock_session = MagicMock()
    mock_session._max_retries = 3
    mock_session.request.return_value = _mock_response(429, {"Retry-After": "60"})

    with (
        patch("app.services.notification_service.notification_service"),
        pytest.raises(httpx.HTTPStatusError),
    ):
        api._request_with_retry("GET", mock_session, "https://api.bgm.tv/v0/me")

    assert limiter.notified == [pytest.approx(60.0)]


def test_request_with_retry_notifies_limiter_on_429_without_header():
    """无 Retry-After 头时传 None，由令牌桶走默认冷却"""
    limiter = RecordingLimiter()
    reset_bgm_rate_limiter(limiter)

    api = BangumiApi(access_token="t")
    mock_session = MagicMock()
    mock_session._max_retries = 3
    mock_session.request.return_value = _mock_response(429, {})

    with (
        patch("app.services.notification_service.notification_service"),
        pytest.raises(httpx.HTTPStatusError),
    ):
        api._request_with_retry("GET", mock_session, "https://api.bgm.tv/v0/me")

    assert limiter.notified == [None]


def test_request_with_retry_notifies_success_on_ok():
    """非 429 响应通知令牌桶重置升级计数"""
    limiter = RecordingLimiter()
    reset_bgm_rate_limiter(limiter)

    api = BangumiApi(access_token="t")
    mock_session = MagicMock()
    mock_session.request.return_value = _mock_response(200)

    api._request_with_retry("GET", mock_session, "https://api.bgm.tv/v0/me")

    assert limiter.success_calls == 1
    assert limiter.notified == []


# ---------------------------------------------------------------------------
# 场景 5：配置解析
# ---------------------------------------------------------------------------


def _patch_config(monkeypatch, values: dict) -> None:
    def fake_get(section, key, fallback=None):
        assert section == "bangumi"
        return values.get(key, fallback)

    monkeypatch.setattr(config_manager, "get", fake_get)


def test_config_values_applied(monkeypatch):
    """[bangumi] api_rate_limit=2、api_rate_burst=5 时按 2/s、burst=5 生效"""
    reset_bgm_rate_limiter(None)
    _patch_config(monkeypatch, {"api_rate_limit": "2", "api_rate_burst": "5"})

    limiter = get_bgm_rate_limiter()

    assert limiter.rate == pytest.approx(2.0)
    assert limiter.burst == 5


@pytest.mark.parametrize(
    ("rate", "burst"),
    [
        ("abc", "xyz"),
        ("", ""),
        ("0", "0"),
        ("-1", "-3"),
        ("1", "-3"),
    ],
)
def test_invalid_config_falls_back_to_defaults(monkeypatch, rate, burst):
    """非法配置回退默认 1/s、burst=3"""
    reset_bgm_rate_limiter(None)
    _patch_config(monkeypatch, {"api_rate_limit": rate, "api_rate_burst": burst})

    limiter = get_bgm_rate_limiter()

    assert limiter.rate == pytest.approx(DEFAULT_RATE)
    assert limiter.burst == DEFAULT_BURST


def test_missing_config_falls_back_to_defaults(monkeypatch):
    """缺失配置回退默认 1/s、burst=3"""
    reset_bgm_rate_limiter(None)
    monkeypatch.setattr(
        config_manager, "get", lambda section, key, fallback=None: fallback
    )

    limiter = get_bgm_rate_limiter()

    assert limiter.rate == pytest.approx(DEFAULT_RATE)
    assert limiter.burst == DEFAULT_BURST


def test_reset_bgm_rate_limiter_installs_given_instance():
    """reset 接口可注入测试实例，供下游/测试加速使用"""
    custom = RateLimiter(rate=5.0, burst=7)

    reset_bgm_rate_limiter(custom)

    assert get_bgm_rate_limiter() is custom
    assert rate_limit.get_bgm_rate_limiter() is custom
