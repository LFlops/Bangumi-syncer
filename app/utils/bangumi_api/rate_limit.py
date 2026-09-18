"""进程级 Bangumi API 令牌桶限速

Bangumi 官方公开 API 已限流（社区实测约 300 请求/分钟/IP，超限封禁 1 小时），
社区客户端普遍以 1 req/s 实践。本模块对所有访问 ``api.bgm.tv`` 的南向请求
做**进程级统一限速**：无论由哪个 ``BangumiApi`` 实例、哪个会话（含直连回退的
临时 client）发出，都经过同一个令牌桶。

设计要点：
- 同步、线程安全（``threading.Lock`` 保护），基于 ``time.monotonic``；
  等待采用"锁内检查、锁外休眠"，休眠期间不持锁，不阻塞其他请求与 429 通知；
- 采用 Go ``golang.org/x/time/rate`` 的**预扣（reserve）**语义：锁内一次性
  占用令牌（允许 ``_tokens`` 透支为负），后续请求按 ``-tokens/rate`` 排队，
  先到先得、等待时长单调不减，保证公平；超出 ``timeout`` 的预约直接放弃并抛
  :class:`RateLimitTimeoutError`（默认 30s）；
- :meth:`RateLimiter.acquire_async` 提供 async 兼容层：复用同一预约计算，
  等待改用 ``asyncio.sleep``，与同步 :meth:`RateLimiter.acquire` 共享令牌桶状态；
- 构造支持注入 ``clock`` / ``sleep``，便于测试不真实等待；
- 模块级惰性单例 :func:`get_bgm_rate_limiter`，从 ``[bangumi]`` 段读取
  ``api_rate_limit``（默认 1/s）与 ``api_rate_burst``（默认 3），解析失败回退默认；
- 收到 429 时由 :meth:`RateLimiter.notify_rate_limited` 冻结令牌桶：
  默认 60s，连续命中按 60→120→240 指数延长，上限 3600s；
  成功请求由 :meth:`RateLimiter.notify_success` 重置升级计数。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

from ...core.config import config_manager
from ...core.logging import logger

__all__ = [
    "DEFAULT_ACQUIRE_TIMEOUT",
    "DEFAULT_BURST",
    "DEFAULT_COOLDOWN",
    "DEFAULT_RATE",
    "MAX_COOLDOWN",
    "RateLimitTimeoutError",
    "RateLimiter",
    "get_bgm_rate_limiter",
    "reset_bgm_rate_limiter",
]

DEFAULT_RATE = 1.0
DEFAULT_BURST = 3
DEFAULT_COOLDOWN = 60.0
MAX_COOLDOWN = 3600.0

# 单次 acquire 允许预约（排队）的最长等待秒数，超过则放弃并抛
# RateLimitTimeoutError，避免请求被无上限地挂起。
DEFAULT_ACQUIRE_TIMEOUT = 30.0

# 等待超过该阈值时记录 warning，便于线上定位限速拖慢
_WARN_WAIT_THRESHOLD = 1.0


class RateLimitTimeoutError(Exception):
    """预约的放行时刻超过调用方给定的 ``timeout``

    刻意不继承任何 httpx 异常，确保不会被 ``retry.RETRY_EXCEPTIONS``
    当作网络/服务端错误重复重试（重试只会加剧限速）。
    """


class RateLimiter:
    """同步、线程安全的令牌桶限速器

    Args:
        rate: 令牌补充速率（个/秒），必须 > 0
        burst: 桶容量（允许的突发请求数），必须 >= 1
        clock: 单调时钟注入点（默认 ``time.monotonic``）
        sleep: 等待注入点（默认 ``time.sleep``），测试可注入假实现
    """

    def __init__(
        self,
        rate: float = DEFAULT_RATE,
        burst: int = DEFAULT_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # 防御：非法参数回落默认值（配置层已归一化，此处兜底并记日志）
        if not isinstance(rate, (int, float)) or rate <= 0:
            logger.warning(
                f"⏳ RateLimiter 非法 rate={rate!r}，回退默认 {DEFAULT_RATE}/s"
            )
            rate = DEFAULT_RATE
        if not isinstance(burst, int) or burst < 1:
            logger.warning(
                f"⏳ RateLimiter 非法 burst={burst!r}，回退默认 {DEFAULT_BURST}"
            )
            burst = DEFAULT_BURST

        self.rate = float(rate)
        self.burst = int(burst)
        self._clock = clock
        self._sleep = sleep

        self._lock = threading.Lock()
        self._tokens = float(self.burst)
        self._last_refill = clock()
        # 429 自适应冷却
        self._frozen_until = 0.0
        self._consecutive_429 = 0

    # ------------------------------------------------------------------
    # 令牌获取
    # ------------------------------------------------------------------

    def acquire(self, timeout: float = DEFAULT_ACQUIRE_TIMEOUT) -> float:
        """获取一个令牌，令牌不足或处于冷却期时阻塞等待

        采用 Go ``rate.Limiter`` 的**预扣（reserve）**语义：先在锁内一次性
        完成预约（占用令牌、允许透支为负），再在锁外按预约的放行时刻休眠。
        等待期间不持有互斥锁，其他线程（含 :meth:`notify_rate_limited` /
        :meth:`notify_success`）可立即更新冻结状态；休眠醒来后复查，若等待
        期间收到更长的 429 冻结，则继续等待到新的放行时刻（并同步增加令牌
        债务，令后来者排在其后，保持 FIFO）。

        Args:
            timeout: 允许预约的最大等待秒数；预约所需等待超过该值时放弃并抛
                :class:`RateLimitTimeoutError`（不影响已排队等待者的预约）。
                默认 :data:`DEFAULT_ACQUIRE_TIMEOUT`。

        Returns:
            本次实际等待的秒数（无需等待时为 0.0）。

        Raises:
            RateLimitTimeoutError: 预约所需等待超过 ``timeout``。
        """
        reserved_act = self._reserve(timeout)
        total_wait = 0.0
        while True:
            remaining, reserved_act, reason = self._remaining_wait(reserved_act)
            if remaining <= 0:
                return total_wait
            total_wait += remaining
            # 锁外休眠：避免长时间持锁阻塞其他请求/通知
            self._wait(remaining, reason)

    async def acquire_async(self, timeout: float = DEFAULT_ACQUIRE_TIMEOUT) -> float:
        """``acquire`` 的 async 兼容层，供 agent 的异步循环使用

        预约计算复用 :meth:`_reserve` / :meth:`_remaining_wait`（仅短暂持有
        ``threading.Lock``，绝不跨 ``await`` 持锁），等待改用
        ``asyncio.sleep``，因此不会阻塞事件循环。与同步 :meth:`acquire`
        共享同一令牌桶状态，同一实例上混用安全。

        Args:
            timeout: 同 :meth:`acquire`。

        Returns:
            本次实际等待的秒数。

        Raises:
            RateLimitTimeoutError: 预约所需等待超过 ``timeout``。
        """
        reserved_act = self._reserve(timeout)
        total_wait = 0.0
        while True:
            remaining, reserved_act, reason = self._remaining_wait(reserved_act)
            if remaining <= 0:
                return total_wait
            total_wait += remaining
            self._log_wait(remaining, reason)
            await asyncio.sleep(remaining)

    def _reserve(self, timeout: float) -> float:
        """锁内完成令牌预约，返回预约的放行时刻（单调时钟）

        预扣算法（参考 Go ``reserveN``）：
        - 令牌可用时刻 ``bucket_act``：足够则为 ``now``，否则为
          ``now + (1 - _tokens) / rate``（``_tokens`` 透支为负）；
        - 放行时刻 ``act = max(bucket_act, _frozen_until)``，429 冻结作为下界；
        - 提交时 ``_tokens -= 1``，并把冻结造成的额外延时
          ``(act - bucket_act) * rate`` 一并折算为令牌债务，使冻结解除后
          排队的请求之间仍保持 ``1/rate`` 间隔（不超发）。

        若 ``act - now > timeout``，则不修改任何状态并抛
        :class:`RateLimitTimeoutError`（等价于回滚预约，不影响其他等待者）。
        """
        with self._lock:
            now = self._clock()
            self._refill(now)
            bucket_act = (
                now if self._tokens >= 1.0 else now + (1.0 - self._tokens) / self.rate
            )
            act = max(bucket_act, self._frozen_until)
            required = act - now
            if required > timeout:
                # 放弃预约：未扣减令牌，已排队等待者的放行时刻不受影响
                logger.warning(
                    f"⏳ Bangumi API 预约等待 {required:.2f}s 超过 timeout="
                    f"{timeout:.2f}s，放弃本次令牌获取"
                )
                raise RateLimitTimeoutError(
                    f"限速排队需等待 {required:.2f}s，超过 timeout={timeout:.2f}s"
                )

            # 预扣一个令牌；冻结额外延时折算为令牌债务，令后来者排在其后
            self._tokens -= 1.0
            freeze_extra = act - bucket_act
            if freeze_extra > 0:
                self._tokens -= freeze_extra * self.rate
            return act

    def _remaining_wait(self, reserved_act: float) -> tuple[float, float, str]:
        """锁内复查预约状态，返回 ``(还需等待秒数, 最新放行时刻, 等待原因)``

        等待期间可能收到更长的 429 冻结（:meth:`notify_rate_limited`）。此处
        把超出原预约时刻的部分折算为令牌债务，保证冻结解除后仍严格执行 FIFO：
        后到但尚未预约的请求会排在当前等待者之后，不会抢先放行。
        """
        with self._lock:
            now = self._clock()
            self._refill(now)
            if self._frozen_until > reserved_act:
                extra = self._frozen_until - reserved_act
                self._tokens -= extra * self.rate
                reserved_act = self._frozen_until
            remaining = reserved_act - now
            reason = (
                "Bangumi API 429 冷却等待"
                if self._frozen_until > now
                else "Bangumi API 限速等待"
            )
            return remaining, reserved_act, reason

    def _refill(self, now: float) -> None:
        """按经过时间补充令牌（上限为 burst）"""
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
        self._last_refill = now

    def _log_wait(self, seconds: float, reason: str) -> None:
        if seconds > _WARN_WAIT_THRESHOLD:
            logger.warning(f"⏳ {reason}: 等待 {seconds:.2f} 秒后继续")

    def _wait(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        self._log_wait(seconds, reason)
        self._sleep(seconds)

    # ------------------------------------------------------------------
    # 429 自适应冷却
    # ------------------------------------------------------------------

    def notify_rate_limited(self, retry_after: float | None = None) -> float:
        """收到 429 时冻结令牌桶

        Args:
            retry_after: 服务端 ``Retry-After`` 解析出的秒数；None 表示无头或解析失败。

        Returns:
            本次生效的冷却秒数。
        """
        with self._lock:
            now = self._clock()
            self._consecutive_429 += 1
            # 指数升级：60 → 120 → 240 …（上限 3600）
            backoff = min(
                DEFAULT_COOLDOWN * (2 ** (self._consecutive_429 - 1)), MAX_COOLDOWN
            )

            hint = 0.0
            if retry_after is not None:
                try:
                    hint = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    logger.warning(
                        f"⏳ 无法解析 Retry-After={retry_after!r}，使用默认冷却"
                    )

            # 冻结不低于默认冷却；服务端给的更长时尊重服务端
            cooldown = min(max(backoff, hint), MAX_COOLDOWN)
            self._frozen_until = max(self._frozen_until, now + cooldown)
            logger.warning(
                f"🚦 Bangumi API 返回 429，令牌桶冷却 {cooldown:.0f}s"
                f"（连续第 {self._consecutive_429} 次）"
            )
            # 注：acquire/acquire_async 采用"锁内预约、锁外休眠 + 醒来复查"，
            # 等待者无需唤醒即会按这里更新的 _frozen_until 重新计算放行时刻，
            # 并把延长部分折算为令牌债务，保证冻结解除后仍按 FIFO 排队。
            return cooldown

    def notify_success(self) -> None:
        """请求成功时重置 429 指数升级计数"""
        with self._lock:
            self._consecutive_429 = 0


# ----------------------------------------------------------------------
# 进程级单例
# ----------------------------------------------------------------------

_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def _read_positive_float(raw: object, default: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"⏳ 非法 api_rate_limit={raw!r}，回退默认 {default}/s")
        return default
    if value <= 0:
        logger.warning(f"⏳ 非法 api_rate_limit={raw!r}，回退默认 {default}/s")
        return default
    return value


def _read_positive_int(raw: object, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"⏳ 非法 api_rate_burst={raw!r}，回退默认 {default}")
        return default
    if value < 1:
        logger.warning(f"⏳ 非法 api_rate_burst={raw!r}，回退默认 {default}")
        return default
    return value


def get_bgm_rate_limiter() -> RateLimiter:
    """获取进程级令牌桶单例（首次调用时从配置惰性构建）"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                rate = _read_positive_float(
                    config_manager.get("bangumi", "api_rate_limit", fallback=1),
                    DEFAULT_RATE,
                )
                burst = _read_positive_int(
                    config_manager.get("bangumi", "api_rate_burst", fallback=3),
                    DEFAULT_BURST,
                )
                _limiter = RateLimiter(rate=rate, burst=burst)
                logger.info(f"🚦 Bangumi API 限速器已初始化：{rate}/s，burst={burst}")
    return _limiter


def reset_bgm_rate_limiter(limiter: RateLimiter | None = None) -> None:
    """替换/清空进程级令牌桶单例

    供测试注入假时钟实例或"测试加速"使用；传 None 表示清空，下次调用重新加载配置。
    """
    global _limiter
    with _limiter_lock:
        _limiter = limiter
