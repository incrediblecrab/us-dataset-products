"""The workflows' contract that needs no CLI: checkout paths and what the test workflow may do."""

from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("name", ["scotus-pipeline.yml", "scotus-test.yml"])
def test_workflows_use_the_folder_inside_the_shared_checkout(name):
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows" / name).read_text())
    assert workflow["name"] == Path(name).stem
    assert workflow["defaults"] == {"run": {"working-directory": "scotus"}}
    group = Path(name).stem + ("-${{ github.ref }}" if name.endswith("-test.yml") else "")
    assert workflow["concurrency"]["group"] == group
    for job in workflow["jobs"].values():
        if not job["steps"][0].get("uses", "").startswith("actions/checkout@"):
            # A job that checks nothing out has no folder to run in, so each of its run steps must say so.
            assert all(step.get("working-directory") == "." for step in job["steps"] if "run" in step)
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/setup-python@"):
                assert step["with"]["cache-dependency-path"] == "scotus/pyproject.toml"
            if "gh workflow run" in step.get("run", ""):
                assert f"gh workflow run {name} " in step["run"]


def test_the_test_workflow_runs_the_suite_and_cannot_publish():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/scotus-test.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads the key `on` as True
    paths = ["scotus/**", ".github/workflows/scotus-test.yml"]
    assert triggers == {"push": {"branches": ["main"], "paths": paths}, "pull_request": {"paths": paths}}
    assert workflow["concurrency"] == {"group": "scotus-test-${{ github.ref }}", "cancel-in-progress": True}
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values()), "no job widens the read-only token, and none asks for an OIDC token"
    runs = [step["run"] for job in workflow["jobs"].values() for step in job["steps"] if "run" in step]
    assert runs[-2:] == ['pip install -q ".[test]"', "python -m pytest -q"]
