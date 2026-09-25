"""Measure the warm SQLite quota preflight on the target Linux filesystem.

Run from the repository root with ``python -m benchmarks.admission``.
This measures the read and remaining-budget comparison, not the durable ticket
reservation transaction. Keep the database on the filesystem used in release.
"""

import argparse
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from gh0st.db import StateLedger
from gh0st.models import QuotaRecord


def measure(db_path: Path, samples: int) -> list[float]:
    db = StateLedger(f"sqlite:///{db_path}")
    db.init_db()
    db.create_task("admission_benchmark", "Quota preflight benchmark")
    db.create_quota_record(QuotaRecord(
        task_id="admission_benchmark", max_tokens_budget=100000,
        max_cost_usd_budget=100,
    ))
    for _ in range(100):
        db.get_quota_record("admission_benchmark")
    durations = []
    for _ in range(samples):
        started = perf_counter_ns()
        quota = db.get_quota_record("admission_benchmark")
        assert quota.remaining_tokens >= 4096 and quota.remaining_usd >= 0.5
        durations.append((perf_counter_ns() - started) / 1e6)
    return sorted(durations)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--directory", type=Path, default=None,
                        help="Filesystem for the temporary benchmark database")
    args = parser.parse_args()
    if args.samples < 100:
        parser.error("At least 100 samples are required")
    with TemporaryDirectory(dir=args.directory) as root:
        durations = measure(Path(root) / "state.db", args.samples)
    p95 = durations[int(0.95 * len(durations)) - 1]
    print(f"median_ms={median(durations):.4f} p95_ms={p95:.4f} "
          f"p99_ms={durations[int(0.99 * len(durations)) - 1]:.4f} "
          f"max_ms={durations[-1]:.4f}")
    if p95 >= 1.0:
        raise SystemExit("Quota preflight missed the 1 ms p95 target")


if __name__ == "__main__":
    main()
