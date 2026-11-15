from chatuses.sim import simulate


def test_fifty_users_flooding_the_pipeline() -> None:
    # PLAN.md 7.7, red-team test 14 (the brain CPU part comes with brain).
    report = simulate(users=50, seconds=60.0, seed=1)
    assert report.messages > 1000
    assert set(report.outcomes) <= {"chat", "accepted", "invalid", "rate_limited", "filtered", "banned"}
    assert report.outcomes["invalid"] > 0 and report.outcomes["rate_limited"] > 0
    assert report.accepted_inputs <= 8 * 60 + 8  # the global limit holds
    assert report.busiest_second <= 16  # at most a full burst plus a second of refill
    assert report.percentile(99) < 0.01  # well under 10 ms per message


def test_simulation_is_deterministic() -> None:
    a = simulate(users=20, seconds=30.0, seed=7)
    b = simulate(users=20, seconds=30.0, seed=7)
    assert (a.messages, a.outcomes) == (b.messages, b.outcomes)
