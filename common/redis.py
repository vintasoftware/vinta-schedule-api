"""Optional Redis connection with a shared circuit breaker.

Redis is treated as a best-effort dependency: if it is unconfigured or
unreachable the application keeps working. A process-wide circuit breaker stops
us from hammering a dead Redis on every request, and rate limiters transparently
fall back to an in-process bucket while the circuit is open.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from django.conf import settings

from pyrate_limiter import InMemoryBucket, Limiter, Rate, RedisBucket
from redis import Redis
from redis.exceptions import RedisError


logger = logging.getLogger(__name__)


class CircuitBreakerOpenError(RuntimeError):
    """Raised internally when the circuit is open and a call is short-circuited."""


class CircuitBreaker:
    """Thread-safe circuit breaker.

    States:
        closed     -> normal operation, calls pass through.
        open       -> calls are rejected immediately for ``reset_timeout`` seconds.
        half_open  -> a single trial call is allowed; success closes the circuit,
                      failure re-opens it.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        name: str = "redis",
    ):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.name = name
        self._lock = threading.Lock()
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def _current_state(self) -> str:
        # Must be called while holding the lock.
        if self._state == self.OPEN and (time.monotonic() - self._opened_at) >= self.reset_timeout:
            self._state = self.HALF_OPEN
            logger.info("Circuit '%s' half-open, allowing a trial call", self.name)
        return self._state

    def allows_request(self) -> bool:
        with self._lock:
            return self._current_state() != self.OPEN

    def record_success(self) -> None:
        with self._lock:
            if self._state != self.CLOSED:
                logger.info("Circuit '%s' closed", self.name)
            self._failures = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                if self._state != self.OPEN:
                    logger.warning(
                        "Circuit '%s' open after %d failure(s)", self.name, self._failures
                    )
                self._state = self.OPEN
                self._opened_at = time.monotonic()

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` through the breaker. Trips on ``RedisError``."""
        if not self.allows_request():
            raise CircuitBreakerOpenError(f"Circuit '{self.name}' is open")
        try:
            result = func(*args, **kwargs)
        except RedisError:
            self.record_failure()
            raise
        else:
            self.record_success()
            return result


# Shared, process-wide breaker for all Redis access.
redis_breaker = CircuitBreaker(
    failure_threshold=getattr(settings, "REDIS_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5),
    reset_timeout=getattr(settings, "REDIS_CIRCUIT_BREAKER_RESET_TIMEOUT", 30.0),
    name="redis",
)


def _connection_from_url(url: str) -> Redis | None:
    if not url:
        return None
    try:
        return Redis.from_url(url)
    except (RedisError, ValueError) as exc:
        logger.warning("Failed to build Redis connection: %s", exc)
        return None


def _build_connection() -> Redis | None:
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        logger.info("REDIS_URL is not configured; Redis features are disabled")
        return None
    return _connection_from_url(url)


# Kept as a module-level singleton for backwards compatibility with existing
# imports (`from common.redis import redis_connection`). May be ``None``.
redis_connection: Redis | None = _build_connection()


def get_redis_connection() -> Redis | None:
    """Return the shared Redis connection, rebuilding it lazily if needed."""
    global redis_connection
    if redis_connection is None:
        redis_connection = _build_connection()
    return redis_connection


def is_redis_available() -> bool:
    """Best-effort check: Redis is configured and the circuit is not open."""
    return get_redis_connection() is not None and redis_breaker.allows_request()


class ResilientLimiter:
    """A pyrate-limiter ``Limiter`` that tolerates Redis being absent or down.

    When Redis is healthy a ``RedisBucket`` is used so limits are shared across
    processes. When Redis is unconfigured, fails to initialize, or the circuit
    breaker is open, it transparently falls back to a per-process
    ``InMemoryBucket`` so rate limiting still applies and the app stays up.

    The public surface mirrors ``Limiter`` (``try_acquire`` plus attribute
    pass-through), so it is a drop-in replacement for the module-level limiters.
    """

    def __init__(
        self,
        rates: Iterable[Rate],
        bucket_key: str,
        *,
        max_delay: int | None = None,
        raise_when_fail: bool = False,
        name: str | None = None,
        breaker: CircuitBreaker | None = None,
        redis_url: str | None = None,
    ):
        self._rates = list(rates)
        self._bucket_key = bucket_key
        self._max_delay = max_delay
        self._raise_when_fail = raise_when_fail
        self._name = name or bucket_key
        self._breaker = breaker or redis_breaker
        self._redis_url = redis_url
        self._own_connection: Redis | None = None
        self._memory_limiter: Limiter | None = None
        self._redis_limiter: Limiter | None = self._build_redis_limiter()

    def _get_connection(self) -> Redis | None:
        if self._redis_url is not None:
            if self._own_connection is None:
                self._own_connection = _connection_from_url(self._redis_url)
            return self._own_connection
        return get_redis_connection()

    def _build_redis_limiter(self) -> Limiter | None:
        conn = self._get_connection()
        if conn is None or not self._breaker.allows_request():
            return None
        try:
            bucket = self._breaker.call(RedisBucket.init, self._rates, conn, self._bucket_key)
        except (RedisError, CircuitBreakerOpenError) as exc:
            logger.warning("Redis unavailable for limiter '%s': %s", self._name, exc)
            return None
        return Limiter(
            bucket,
            raise_when_fail=self._raise_when_fail,
            max_delay=self._max_delay,
        )

    def _get_memory_limiter(self) -> Limiter:
        if self._memory_limiter is None:
            self._memory_limiter = Limiter(
                InMemoryBucket(self._rates),
                raise_when_fail=self._raise_when_fail,
                max_delay=self._max_delay,
            )
        return self._memory_limiter

    def _active_limiter(self) -> tuple[Limiter, bool]:
        """Return ``(limiter, is_redis)`` for the current circuit state."""
        if self._breaker.allows_request():
            if self._redis_limiter is None:
                # Redis may have come back since the last attempt.
                self._redis_limiter = self._build_redis_limiter()
            if self._redis_limiter is not None:
                return self._redis_limiter, True
        return self._get_memory_limiter(), False

    def try_acquire(self, name: str, weight: int = 1) -> Any:
        limiter, is_redis = self._active_limiter()
        if not is_redis:
            return limiter.try_acquire(name, weight)
        try:
            return self._breaker.call(limiter.try_acquire, name, weight)
        except (RedisError, CircuitBreakerOpenError):
            logger.warning("Redis error in limiter '%s'; falling back to in-memory", self._name)
            return self._get_memory_limiter().try_acquire(name, weight)

    def __getattr__(self, item: str) -> Any:
        # Proxy any other Limiter attribute/method to the active limiter.
        limiter, _ = self._active_limiter()
        return getattr(limiter, item)


def build_resilient_limiter(
    rates: Iterable[Rate],
    bucket_key: str,
    *,
    max_delay: int | None = None,
    raise_when_fail: bool = False,
    name: str | None = None,
    redis_url: str | None = None,
) -> ResilientLimiter:
    """Convenience factory mirroring the old ``Limiter(RedisBucket.init(...))`` call."""
    return ResilientLimiter(
        rates,
        bucket_key,
        max_delay=max_delay,
        raise_when_fail=raise_when_fail,
        name=name,
        redis_url=redis_url,
    )
