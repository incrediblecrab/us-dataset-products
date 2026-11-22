"""Identify the checkout and, on Actions, the run that published a manifest record."""

import os
import re
import subprocess
from pathlib import Path


def run_code():
    commit = os.environ.get("GITHUB_SHA")
    if not commit:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent, stderr=subprocess.DEVNULL, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            commit = None
    server, repo, run = (os.environ.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return {"commit": commit if re.fullmatch(r"[0-9a-fA-F]{40}", commit or "") else None, "run_url": f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None}


def code_links(code):
    links = []
    if code and (sha := code.get("commit")):
        links.append(f"commit [{sha[:12]}](https://github.com/incrediblecrab/us-dataset-products/commit/{sha})")
    if code and code.get("run_url"):
        links.append(f"[Actions run]({code['run_url']})")
    return ", ".join(links)
