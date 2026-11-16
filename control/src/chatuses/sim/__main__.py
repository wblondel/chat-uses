"""Run the chat simulator.

python -m chatuses.sim --users 50 --seconds 60   chat through the pipeline (rate limits, filters)
python -m chatuses.sim --scenarios               governance: votes, brigades, ties (sim/governance.py)
"""

import argparse
import sys

from chatuses.sim import simulate
from chatuses.sim.governance import run_all
from chatuses.store import Store


def scenarios(seed: int) -> None:
    results = run_all(seed)
    for result in results:
        print(f"{result.name}: {'ok' if result.ok else 'FAILED'}")
        for check in result.checks:
            detail = f" ({check.detail})" if check.detail and not check.ok else ""
            print(f"  {'✓' if check.ok else '✗'} {check.what}{detail}")
    failed = [r.name for r in results if not r.ok]
    print(
        f"{len(results) - len(failed)} of {len(results)} scenarios passed"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    sys.exit(1 if failed else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Feed simulated chat through brain's pipeline.")
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=60.0, help="simulated time")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--db", default=":memory:", help="SQLite file to keep the results in")
    parser.add_argument("--scenarios", action="store_true", help="run the governance scenarios instead")
    args = parser.parse_args()
    if args.scenarios:
        scenarios(args.seed)

    with Store(args.db) as store:
        report = simulate(args.users, args.seconds, args.seed, store=store)
    print(f"{report.messages} messages from {args.users} users over {report.seconds:.0f} simulated seconds")
    for kind, count in report.outcomes.most_common():
        print(f"  {kind:<13} {count}")
    print(
        f"accepted input: {report.accepted_inputs / report.seconds:.1f}/s on average, "
        f"busiest second {report.busiest_second}"
    )
    print(
        f"time per message: median {report.percentile(50) * 1e6:.0f} µs, "
        f"p99 {report.percentile(99) * 1e6:.0f} µs"
    )


if __name__ == "__main__":
    main()
