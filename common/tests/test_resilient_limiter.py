from unittest.mock import Mock

from pyrate_limiter import Duration, Rate

from common.redis import CircuitBreaker, ResilientLimiter


def _in_memory_limiter(*, per_name: bool, limit: int = 2) -> ResilientLimiter:
    # An empty redis_url means no Redis connection, so the in-process bucket is used.
    return ResilientLimiter(
        [Rate(limit, Duration.MINUTE)],
        bucket_key="test",
        redis_url="",
        breaker=CircuitBreaker(name="test"),
        per_name=per_name,
    )


def test_per_name_limiter_gives_each_name_its_own_budget():
    limiter = _in_memory_limiter(per_name=True)

    org_a = [limiter.try_acquire("org-a") for _ in range(3)]
    org_b = [limiter.try_acquire("org-b") for _ in range(3)]

    assert org_a == [True, True, False]
    assert org_b == [True, True, False]


def test_default_limiter_charges_every_name_to_one_budget():
    """Third-party quotas (Google, Outlook) are one budget for the whole app."""
    limiter = _in_memory_limiter(per_name=False)

    results = [limiter.try_acquire(name) for name in ("org-a", "org-a", "org-b")]

    assert results == [True, True, False]


def test_per_name_in_memory_buckets_drop_the_least_recently_used_name():
    limiter = _in_memory_limiter(per_name=True, limit=1)
    buckets = limiter._get_memory_limiter().bucket_factory._make_bucket
    buckets._max_names = 2

    assert limiter.try_acquire("a") is True
    assert limiter.try_acquire("b") is True
    assert limiter.try_acquire("c") is True  # pushes "a" out
    assert limiter.try_acquire("b") is False
    assert limiter.try_acquire("a") is True  # fresh bucket for "a"


def test_per_name_limiter_stores_each_name_under_its_own_redis_key_with_a_ttl():
    redis = Mock()
    redis.script_load.return_value = "script-sha"
    redis.evalsha.return_value = -1  # the Lua script's "accepted"
    limiter = ResilientLimiter(
        [Rate(5, Duration.SECOND), Rate(100, Duration.HOUR)],
        bucket_key="public_api",
        breaker=CircuitBreaker(name="test"),
        per_name=True,
    )
    limiter._get_connection = Mock(return_value=redis)  # type: ignore[method-assign]
    limiter._redis_limiter = limiter._build_redis_limiter()

    assert limiter.try_acquire("org-a") is True
    assert limiter.try_acquire("anon:203.0.113.5") is True

    assert [call.args[2] for call in redis.evalsha.call_args_list] == [
        "public_api:org-a",
        "public_api:anon:203.0.113.5",
    ]
    # One second past the longest window (an hour), so an idle name's key expires.
    assert [call.args for call in redis.expire.call_args_list] == [
        ("public_api:org-a", 3601),
        ("public_api:anon:203.0.113.5", 3601),
    ]
