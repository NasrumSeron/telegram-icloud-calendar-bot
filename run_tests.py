"""Run the offline test suites. No network, no API keys, no calendar writes.

    python run_tests.py

Exits non-zero if any suite fails. Live tests (tests/live/) are opt-in:
set RUN_LIVE_TESTS=1 and a filled-in .env, then run each file directly.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["tests/test_ical_offline.py", "tests/test_flow_offline.py", "tests/test_service_offline.py"]

failed = []
for suite in SUITES:
    print(f"\n### {suite}", flush=True)
    if subprocess.run([sys.executable, suite], cwd=HERE).returncode != 0:
        failed.append(suite)

print("\n" + "=" * 70)
print("FAILED: " + ", ".join(failed) if failed else f"All {len(SUITES)} offline suites passed.")
sys.exit(1 if failed else 0)
