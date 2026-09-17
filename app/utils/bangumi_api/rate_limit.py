"""进程级 Bangumi API 令牌桶限速

Bangumi 官方公开 API 已限流（社区实测约 300 请求/分钟/IP，超限封禁 1 小时），
社区客户端普遍以 1 req/s 实践。本模块对所有访问 ``api.bgm.tv`` 的南向请求
做**进程级统一限速**：无论由哪个 ``BangumiApi`` 实例、哪个会话（含直连回退的
临时 client）发出，都经过同一个令牌桶。

设计要点：
- 同步、线程安全（``threading.Lock`` 保护），基于 ``time.monotonic``；
  等待采用"锁内检查、锁外休眠"，休眠期间不持锁，不阻塞其他请求与 429 通知；
- 构造支持注入 ``clock`` / ``sleep``，便于测试不真实等待；
- 模块级惰性单例 :func:`get_bgm_rate_limiter`，从 ``[bangumi]`` 段读取
  ``api_rate_limit``（默认 1/s）与 ``api_rate_burst``（默认 3），解析失败回退默认；
- 收到 429 时由 :meth:`RateLimiter.notify_rate_limited` 冻结令牌桶：
  默认 60s，连续命中按 60→120→240 指数延长，上限 3600s；
  成功请求由 :meth:`RateLimiter.notify_success` 重置升级计数。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ...core.config import config_manager
from ...core.logging import logger

__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_COOLDOWN",
    "DEFAULT_RATE",
    "MAX_COOLDOWN",
    "RateLimiter",
    "get_bgm_rate_limiter",
    "reset_bgm_rate_limiter",
]

DEFAULT_RATE = 1.0
DEFAULT_BURST = 3
DEFAULT_COOLDOWN = 60.0
MAX_COOLDOWN = 3600.0

# 等待超过该阈值时记录 warning，便于线上定位限速拖慢
_WARN_WAIT_THRESHOLD = 1.0


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

    def acquire(self) -> float:
        """获取一个令牌，令牌不足或处于冷却期时阻塞等待

        实现要点：**锁内检查、锁外休眠**。等待期间不持有互斥锁，其他线程
        （含 429 通知路径 :meth:`notify_rate_limited` / :meth:`notify_success`）
        可立即更新冻结状态；本线程休眠结束后重新进入循环按最新状态复查，因此
        等待中收到的更长冻结同样生效（伪唤醒也由复查兜底）。

        Returns:
            本次实际等待的秒数（无需等待时为 0.0）。
        """
        total_wait = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._refill(now)

                if now < self._frozen_until:
                    wait = self._frozen_until - now
                    reason = "Bangumi API 429 冷却等待"
                elif self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return total_wait
                else:
                    # 距离下一个令牌可用还差的时间
                    wait = (1.0 - self._tokens) / self.rate
                    reason = "Bangumi API 限速等待"

            # 锁外休眠：避免长时间持锁阻塞其他请求/通知
            total_wait += wait
            self._wait(wait, reason)

    def _refill(self, now: float) -> None:
        """按经过时间补充令牌（上限为 burst）"""
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
        self._last_refill = now

    def _wait(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        if seconds > _WARN_WAIT_THRESHOLD:
            logger.warning(f"⏳ {reason}: 等待 {seconds:.2f} 秒后继续")
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
            # 注：acquire 采用"锁内检查、锁外休眠 + 醒来复查"，
            # 等待者无需唤醒即会按这里更新的 _frozen_until 重新计算等待时长。
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
