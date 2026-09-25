"""Measure the local Layla classifier on representative prompts.

Run from the repository root with ``python -m benchmarks.routing``. This
measures classifier latency only; model selection and durable ledger writes are
outside this interface benchmark.
"""

import argparse
from statistics import median
from time import perf_counter_ns

from gh0st.router import LaylaClassifier, TemplateRegistry


PROMPTS = (
    "Fix a bug in the parser and update its regression test",
    "Run pytest and lint the auth package",
    "Create a plan for migrating the database schema",
    "Inspect the repository and explain the release workflow",
    "Refactor the cache implementation across modules",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1000)
    args = parser.parse_args()
    if args.samples < 100:
        parser.error("At least 100 samples are required")

    classifier = LaylaClassifier()
    templates = TemplateRegistry().list_all()
    for prompt in PROMPTS:
        classifier.classify(prompt, templates)
    durations = []
    for index in range(args.samples):
        prompt = PROMPTS[index % len(PROMPTS)]
        started = perf_counter_ns()
        capability, confidence = classifier.classify(prompt, templates)
        durations.append((perf_counter_ns() - started) / 1e6)
        assert capability and 0 <= confidence <= 1

    durations.sort()
    p95 = durations[int(0.95 * len(durations)) - 1]
    p99 = durations[int(0.99 * len(durations)) - 1]
    print(f"median_ms={median(durations):.4f} p95_ms={p95:.4f} "
          f"p99_ms={p99:.4f} max_ms={durations[-1]:.4f}")
    if p95 >= 100:
        raise SystemExit("Layla classifier missed the 100 ms p95 target")


if __name__ == "__main__":
    main()
