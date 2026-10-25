#!/usr/bin/env python3
"""Bounded existing regression suites, each with its own Motor client/process."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "test_core_openai_configuration.py",
    "test_public_surcharges.py",
    "test_demo_gmail_provider.py",
    "test_demo_public_requester.py",
    "test_demo_reply_provenance.py",
)


def main():
    try:
        with socket.create_connection(("127.0.0.1", 27027), timeout=3):
            pass
    except OSError:
        sys.exit("Local MongoDB is unavailable. Run: docker compose up -d --wait mongo")
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT"}}
    env.update(MONGO_URL="mongodb://127.0.0.1:27027",
               DB_NAME="huo_submission_test_" + uuid.uuid4().hex[:12],
               PYTHONPATH=str(ROOT / "backend"), EMAIL_PROVIDER="log", SMS_PROVIDER="log",
               SEED_DEMO="false", JOBS_WORKER_ENABLED="false", DIGITRAFFIC_ENABLED="false")
    failed = []
    for module in MODULES:
        print(f"\nChecking {module}", flush=True)
        try:
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                     str(ROOT / "backend/tests" / module)],
                                    cwd=ROOT, env=env, timeout=120)
            if result.returncode:
                failed.append(module)
        except subprocess.TimeoutExpired:
            failed.append(module)
            print(f"Timed out after 120 seconds: {module}", file=sys.stderr)
    if failed:
        sys.exit("Failed modules: " + ", ".join(failed))
    print(f"\nAll {len(MODULES)} selected modules passed.")


if __name__ == "__main__":
    main()
