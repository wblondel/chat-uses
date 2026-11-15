from chatuses.ratelimit import RateLimiter


def test_user_burst_then_one_every_two_seconds() -> None:
    limiter = RateLimiter()
    assert [limiter.allow("yt:a", 0.0) for _ in range(4)] == ["ok", "ok", "ok", "user"]
    assert limiter.allow("yt:a", 1.0) == "user"
    assert limiter.allow("yt:a", 2.0) == "ok"
    assert limiter.allow("yt:a", 2.5) == "user"


def test_global_limit_across_users() -> None:
    limiter = RateLimiter()
    verdicts = [limiter.allow(f"yt:{i}", 0.0) for i in range(9)]
    assert verdicts == ["ok"] * 8 + ["global"]


def test_global_refusal_costs_the_user_nothing() -> None:
    limiter = RateLimiter()
    for i in range(8):
        limiter.allow(f"yt:{i}", 0.0)
    for _ in range(3):
        assert limiter.allow("yt:late", 0.0) == "global"
    # A moment later the global bucket has a token; the user still has all 3.
    assert [limiter.allow("yt:late", 0.125) for _ in range(1)] == ["ok"]


def test_long_run_rate_holds_with_many_users() -> None:
    limiter = RateLimiter()
    ok = 0
    t = 0.0
    while t < 10.0:
        for i in range(100):
            ok += limiter.allow(f"yt:{i}", t) == "ok"
        t += 0.05
    assert ok <= 8 * 10 + 8  # 8/s plus the initial burst


def test_idle_users_are_pruned() -> None:
    limiter = RateLimiter(max_users=10)
    for i in range(10):
        limiter.allow(f"yt:{i}", 0.0)
    limiter.allow("yt:new", 30.0)  # the others have refilled: forgotten
    assert len(limiter._users) == 1  # pyright: ignore[reportPrivateUsage]


def test_pruning_never_changes_a_verdict() -> None:
    # A pruned bucket was full, and a new one starts full: same state. So a
    # raid that keeps the limiter pruning gets exactly the same answers.
    pruning, keeping = RateLimiter(max_users=10), RateLimiter(max_users=1_000_000)
    for step in range(3000):
        user, now = f"yt:{step * 7 % 60}", step * 0.01
        assert pruning.allow(user, now) == keeping.allow(user, now), f"step {step}"
