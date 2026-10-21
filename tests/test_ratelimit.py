"""Tests for tools/ratelimit.py."""

from tools.ratelimit import RateLimiter


def test_allows_up_to_limit_then_blocks_with_retry_time():
    rl = RateLimiter(2, window=60)
    assert rl.check("a") == 0 and rl.check("a") == 0
    assert 0 < rl.check("a") <= 60
    assert rl.check("b") == 0  # independent key


def test_window_resets(monkeypatch):
    import tools.ratelimit as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    rl = RateLimiter(1, window=60)
    assert rl.check("a") == 0 and rl.check("a") > 0
    now[0] += 61
    assert rl.check("a") == 0


def test_zero_limit_disables():
    rl = RateLimiter(0)
    assert all(rl.check("a") == 0 for _ in range(1000))


def test_key_table_is_bounded():
    rl = RateLimiter(5, window=60, max_keys=3)
    for i in range(10):
        rl.check(f"k{i}")
    assert len(rl._counts) <= 3
