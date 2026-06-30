#!/usr/bin/env python3
"""Run only partial-return multi-layer FIFO scenario repeatedly."""

from __future__ import annotations

import os
import sys
import uuid

import httpx

# Reuse helpers from main FIFO suite
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.test_fifo_consumption import (  # noqa: E402
    RESULTS,
    record,
    test_partial_return_multi_layer,
)

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000/api/v1")


def main() -> int:
    runs = int(os.environ.get("RUNS", "5"))
    print(f"=== Partial return scenario x{runs} on {BASE} ===\n")
    outcomes: list[bool] = []
    with httpx.Client(timeout=60.0) as client:
        health = client.get(f"{BASE.replace('/api/v1', '')}/health")
        if health.status_code != 200:
            print(f"API unhealthy: {health.status_code}")
            return 1
        for i in range(runs):
            RESULTS.clear()
            suffix = uuid.uuid4().int % 10_000_000 + i * 97
            test_partial_return_multi_layer(client, suffix)
            failed = [name for name, ok, _ in RESULTS if not ok]
            passed = not failed
            outcomes.append(passed)
            mark = "PASS" if passed else "FAIL"
            print(f"Run {i + 1}: [{mark}] failures={failed or 'none'}")
            for name, ok, detail in RESULTS:
                if not ok:
                    print(f"  - {name}: {detail}")

    passed_count = sum(outcomes)
    print(
        f"\nSummary: {passed_count}/{runs} full passes "
        f"({runs - passed_count} runs with failures)"
    )
    return 0 if passed_count == runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
