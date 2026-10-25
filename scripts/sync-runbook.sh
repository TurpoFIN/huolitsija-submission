#!/bin/bash
# Copy the owner runbook (shared/s4) into the backend so the deployed app serves it at /ops/runbook.
# Run after any change to shared/s4; test_s4_runbook.py fails while the copies differ.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$ROOT/backend/data/runbook"
for f in RUNBOOK.md OWNER-ACTIVATION.md LEGAL-HUMAN-POINTS.md FINAL-RUN-PLAN.md PROCESSORS.md CONFIG-INVENTORY.md HUOLITSIJA-RUNBOOK.pdf; do
  cp "$ROOT/shared/s4/$f" "$ROOT/backend/data/runbook/$f"
done
echo "runbook synced from shared/s4"
