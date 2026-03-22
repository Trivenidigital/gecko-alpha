"""Dashboard entrypoint.

Run with:
    uv run python -m dashboard.main
    uvicorn dashboard.main:app --reload --port 8000
"""

import os
import subprocess
import sys

import uvicorn

from dashboard.api import create_app

# Build frontend if dist/ doesn't exist
frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
dist_dir = os.path.join(frontend_dir, "dist")

if not os.path.isdir(dist_dir) and os.path.isfile(os.path.join(frontend_dir, "package.json")):
    print("Building frontend...")
    subprocess.run(["npm", "install"], cwd=frontend_dir, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True)

app = create_app()

if __name__ == "__main__":
    uvicorn.run("dashboard.main:app", host="0.0.0.0", port=8000, reload=True)
