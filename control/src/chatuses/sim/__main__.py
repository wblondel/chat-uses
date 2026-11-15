"""Run the chat simulator: python -m chatuses.sim --users 50 --seconds 60"""

import argparse

from chatuses.sim import simulate
from chatuses.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Feed simulated chat through brain's pipeline.")
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=60.0, help="simulated time")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--db", default=":memory:", help="SQLite file to keep the results in")
    args = parser.parse_args()

    report = simulate(args.users, args.seconds, args.seed, store=Store(args.db))
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
