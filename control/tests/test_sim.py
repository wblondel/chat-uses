import asyncio

import pytest

from chatuses.sim import simulate
from chatuses.sim.governance import SCENARIOS, run


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


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_governance_scenarios(name: str, seed: int) -> None:
    result = asyncio.run(run(name, seed))
    failed = [f"{c.what} ({c.detail})" for c in result.checks if not c.ok]
    assert result.checks and not failed


def test_the_scenarios_catch_a_broken_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatuses import identity

    monkeypatch.setattr(identity, "SETTLE_TIME", 0.0)  # raiders could vote at once
    result = asyncio.run(run("brigade"))
    assert not result.ok
    assert "every raider's ballot is refused" in [c.what for c in result.checks if not c.ok]


def test_a_failed_scenario_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chatuses.sim import __main__ as cli
    from chatuses.sim.governance import Result

    broken = Result("broken")
    broken.check("something", False, "what happened")

    def run_all(seed: int) -> list[Result]:
        return [broken]

    monkeypatch.setattr(cli, "run_all", run_all)
    with pytest.raises(SystemExit) as stop:
        cli.scenarios(1)
    assert stop.value.code == 1
    assert "✗ something (what happened)" in capsys.readouterr().out
