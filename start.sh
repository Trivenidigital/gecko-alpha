#!/bin/bash
set -e
echo "Starting gecko-alpha..."
uv run python -m scout.main &
PIPELINE_PID=$!
sleep 3
uv run uvicorn dashboard.main:app --port 8000 &
DASHBOARD_PID=$!
echo "Pipeline PID: $PIPELINE_PID"
echo "Dashboard PID: $DASHBOARD_PID"
echo "Dashboard: http://localhost:8000"
wait
