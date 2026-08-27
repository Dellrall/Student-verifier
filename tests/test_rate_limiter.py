import time
from tarveri.rate_limiter import RateLimiter


def test_rate_limiter_allows_under_limit():
    limiter = RateLimiter(max_attempts=3, window_seconds=60)
    user_id = 12345

    assert not limiter.is_rate_limited(user_id)
    limiter.record_attempt(user_id)
    assert not limiter.is_rate_limited(user_id)
    limiter.record_attempt(user_id)
    assert not limiter.is_rate_limited(user_id)
    limiter.record_attempt(user_id)

    # Reached limit (3 attempts recorded)
    assert limiter.is_rate_limited(user_id)


def test_rate_limiter_reset():
    limiter = RateLimiter(max_attempts=2, window_seconds=60)
    user_id = 999

    limiter.record_attempt(user_id)
    limiter.record_attempt(user_id)
    assert limiter.is_rate_limited(user_id)

    limiter.reset(user_id)
    assert not limiter.is_rate_limited(user_id)


def test_rate_limiter_window_expiry():
    # 0.1 second window for fast expiry testing
    limiter = RateLimiter(max_attempts=1, window_seconds=0.1)
    user_id = 456

    limiter.record_attempt(user_id)
    assert limiter.is_rate_limited(user_id)

    time.sleep(0.15)
    assert not limiter.is_rate_limited(user_id)
