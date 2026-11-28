import io
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

import health

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)
FED = "incrediblecrab/us-federal-reserve-beige-book"
OPINIONS = "incrediblecrab/us-scotus-opinions"
LAWS = "incrediblecrab/us-local-laws"


def test_readme_dataset_list():
    links = re.findall(r"https://huggingface\.co/datasets/([\w.-]+/[\w.-]+)", (ROOT / "README.md").read_text())
    assert set(links) == set(health.DATASETS)


def workflow_api(monkeypatch, runs):
    def fetch(url, token):
        assert token == "test-token"
        parts = urlsplit(url)
        assert parts.netloc == "api.github.com"
        assert parts.path == "/repos/test/repo/actions/workflows/fed-pipeline.yml/runs"
        query = parse_qs(parts.query)
        matches = [r for r in runs if all(r[k] in query[k] for k in ("status", "event") if k in query)]
        matches.sort(key=lambda r: r["created_at"], reverse=True)
        return {"workflow_runs": matches[:int(query["per_page"][0])]}, None

    monkeypatch.setattr(health, "fetch", fetch)


def run(at, event="schedule", conclusion="success"):
    return {"created_at": at.isoformat(), "event": event, "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "html_url": "https://example.org/runs/" + str(int(at.timestamp()))}


@pytest.mark.parametrize(("conclusion", "status"), [("failure", "error"), ("timed_out", "error"), ("cancelled", "warning"), ("success", "ok")])
def test_latest_completed_run(monkeypatch, conclusion, status):
    completed = run(NOW - timedelta(hours=1), "workflow_dispatch", conclusion)
    workflow_api(monkeypatch, [run(NOW, conclusion=None), completed, run(NOW - timedelta(hours=6), conclusion="failure")])
    outcome, schedule = health.check_pipeline("fed-pipeline", "test/repo", "test-token", NOW)
    assert outcome.status == status
    assert conclusion in outcome.detail and completed["html_url"] in outcome.detail
    assert schedule.status == "ok"


@pytest.mark.parametrize(("age", "status"), [(timedelta(hours=23), "ok"), (timedelta(hours=23, seconds=1), "error"), (None, "error")])
def test_manual_and_in_progress_runs_cannot_mask_missed_schedule(monkeypatch, age, status):
    runs = [run(NOW, "workflow_dispatch"), run(NOW, conclusion=None)]
    if age:
        runs.append(run(NOW - age))
    workflow_api(monkeypatch, runs)
    outcome, schedule = health.check_pipeline("fed-pipeline", "test/repo", "test-token", NOW)
    assert outcome.status == "ok"
    assert schedule.status == status


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.org")

    def git(*args):
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", *args], text=True).strip()

    git("init", "-q", "-b", "main")

    def commit(path, content, at):
        for role in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{role}_DATE", at.isoformat())
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        git("add", "--", path)
        git("commit", "-qm", content)
        return git("rev-parse", "HEAD")

    return git, commit


def test_unknown_and_non_ancestor_commits(repo):
    git, commit = repo
    commit("fed/code", "main", NOW)
    git("switch", "-qc", "side")
    side = commit("fed/code", "diverged", NOW)
    git("switch", "-q", "main")
    missing = health.alignment(FED, "fed", "f" * 40, NOW)
    assert missing.status == "error" and "not in this repository" in missing.detail
    diverged = health.alignment(FED, "fed", side, NOW)
    assert diverged.status == "error" and "not an ancestor" in diverged.detail


def test_folder_diff_and_push_grace_boundary(repo):
    _, commit = repo
    built = commit("fed/code", "published", NOW - timedelta(days=4))
    commit("fed/code", "changed", NOW - timedelta(hours=health.allowance(FED)))
    commit("crs/code", "unrelated", NOW)
    assert health.allowance(FED) == 24 + health.SCHEDULE_HOURS + health.LATE_HOURS
    assert health.alignment(FED, "fed", built, NOW).status == "ok"
    overdue = health.alignment(FED, "fed", built, NOW + timedelta(seconds=1))
    assert overdue.status == "error" and built in overdue.detail
    commit("fed/code", "published", NOW)
    assert health.alignment(FED, "fed", built, NOW + timedelta(days=1)).status == "ok"


def test_a_dataset_rewritten_only_on_data_changes_warns_on_old_code(repo):
    _, commit = repo
    built = commit("local-laws/code", "published", NOW - timedelta(days=10))
    commit("local-laws/code", "pin alert", NOW - timedelta(days=5))
    row = health.alignment("incrediblecrab/us-local-laws", "local-laws", built, NOW)
    assert row.status == "warning" and "only when its data changes" in row.detail
    assert health.freshness("incrediblecrab/us-local-laws", "local-laws", "manifest.json", None, NOW).status == "n/a"


@pytest.mark.parametrize("dataset, hours", [("incrediblecrab/us-scotus-opinions", 35), ("incrediblecrab/us-crs-research-papers", 47), ("incrediblecrab/us-federal-reserve-beige-book", 47)])
def test_listing_freshness_follows_each_dataset_s_refresh(dataset, hours):
    at = (NOW - timedelta(hours=hours)).isoformat()
    assert health.freshness(dataset, health.DATASETS[dataset], "manifest.json", {"at": at}, NOW).status == "ok"
    assert health.freshness(dataset, health.DATASETS[dataset], "manifest.json", {"at": at}, NOW + timedelta(seconds=1)).status == "error"


def test_regular_and_cases_run_order_and_fallbacks():
    old = {"code": {"commit": "old"}}
    new = {"code": {"commit": "new"}}
    listing = {"code": {"commit": "listed"}}
    manifest = {"runs": [old, new, {"code": None}], "listing": listing}
    assert health.recorded_build(FED, "fed", manifest)[0] == "new"
    assert health.recorded_build(FED, "fed", {"runs": [{}], "listing": listing})[0] == "listed"
    cases = {"runs": [{}, new, old], "last_complete_code": {"commit": "complete"}}
    assert health.recorded_build(health.CASES, "scotus", cases)[0] == "complete"
    del cases["last_complete_code"]
    assert health.recorded_build(health.CASES, "scotus", cases)[0] == "new"
    assert health.recorded_build(LAWS, "local-laws", {"code": {"commit": "top"}, "runs": [new]})[0] == "top"


def hub_api(monkeypatch, pages):
    def fetch(url, token=None):
        assert token is None
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(health, "fetch", fetch)


def tree(dataset):
    return f"https://huggingface.co/api/datasets/{dataset}/tree/main?recursive=true"


def resolve(dataset, path):
    return f"https://huggingface.co/datasets/{dataset}/resolve/main/{path}"


def file(path):
    return {"type": "file", "path": path}


def test_paginated_collections_newest_build_but_each_listing_checked(monkeypatch, repo):
    _, commit = repo
    cutoff = NOW - timedelta(hours=35)
    old = commit("scotus/code", "old", cutoff - timedelta(days=2))
    head = commit("scotus/code", "current", cutoff - timedelta(days=1))
    page = tree(OPINIONS)
    before = (cutoff - timedelta(seconds=1)).isoformat()
    hub_api(monkeypatch, {
        page: ([file("data.parquet"), {"type": "directory", "path": "not-manifest.json"}, file("new/manifest.json")], page + "&cursor=2"),
        page + "&cursor=2": ([file("old collection/manifest.json")], None),
        resolve(OPINIONS, "new/manifest.json"): ({"listing": {"at": cutoff.isoformat()}, "runs": [{"ended": cutoff.isoformat(), "code": {"commit": head}}]}, None),
        resolve(OPINIONS, "old%20collection/manifest.json"): ({"listing": {"at": before, "code": {"commit": old}}}, None),
    })
    build, listings = health.check_dataset(OPINIONS, NOW)
    assert build.status == "ok" and head in build.detail
    assert [(r.subject, r.status) for r in listings] == [(OPINIONS + "/new/manifest.json", "ok"), (OPINIONS + "/old collection/manifest.json", "error")]


@pytest.mark.parametrize(("dataset", "manifest", "status"), [
    (FED, {"listing": {"at": NOW.isoformat()}}, "ok"),
    (FED, {"listing": None}, "error"),
    (FED, {"listing": {"at": "not a date"}}, "error"),
    (LAWS, {}, "n/a"),
    (health.CASES, {"generated_at": NOW.isoformat()}, "error"),
    (health.CASES, {"last_complete_at": NOW.isoformat()}, "ok"),
])
def test_no_recorded_commit_does_not_skip_freshness(monkeypatch, dataset, manifest, status):
    hub_api(monkeypatch, {tree(dataset): ([file("manifest.json")], None), resolve(dataset, "manifest.json"): (manifest, None)})
    build, [listing] = health.check_dataset(dataset, NOW)
    assert build.status == "warning" and "No build commit" in build.detail
    assert listing.status == status


@pytest.mark.parametrize("failure", ["tree", "empty", "resolve"])
def test_unreadable_dataset_is_a_problem(monkeypatch, failure):
    error = health.CheckError("service unavailable")
    pages = {tree(FED): ([file("manifest.json")], None), resolve(FED, "manifest.json"): error}
    if failure == "tree":
        pages[tree(FED)] = error
    if failure == "empty":
        pages[tree(FED)] = ([file("README.md")], None)
    hub_api(monkeypatch, pages)
    build, [listing] = health.check_dataset(FED, NOW)
    assert build.status == "error"
    assert ("No manifest.json" if failure == "empty" else str(error)) in build.detail
    assert listing.status == "unknown"


@pytest.mark.parametrize(("code", "waits"), [(408, 3), (429, 3), (503, 3), (None, 3), (404, 0)])
def test_fetch_retry_limit(monkeypatch, code, waits):
    calls, sleeps = [], []

    def unavailable(request, timeout):
        calls.append(request.full_url)
        if code is None:
            raise URLError("connection refused")
        raise HTTPError(request.full_url, code, "unavailable", {}, None)

    monkeypatch.setattr(health, "urlopen", unavailable)
    monkeypatch.setattr(health.time, "sleep", sleeps.append)
    url = tree(FED)
    with pytest.raises(health.CheckError, match=str(code) if code else "connection refused"):
        health.fetch(url)
    assert calls == [url] * (waits + 1)
    assert sleeps == [60] * waits


def test_fetch_recovers_and_reads_next_link(monkeypatch):
    page = io.BytesIO(b'[{"path":"manifest.json"}]')
    page.headers = {"Link": '<?cursor=2>; rel="next"'}
    responses = iter([TimeoutError(), page])
    sleeps = []

    def open_page(request, timeout):
        assert request.get_header("Authorization") is None
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(health, "urlopen", open_page)
    monkeypatch.setattr(health.time, "sleep", sleeps.append)
    assert health.fetch(tree(FED)) == ([{"path": "manifest.json"}], tree(FED).split("?")[0] + "?cursor=2")
    assert sleeps == [60]


@pytest.mark.parametrize(("actions", "unavailable"), [(True, True), (True, False), (False, True)])
def test_main_summary_annotations_and_exit(monkeypatch, tmp_path, capsys, actions, unavailable):
    summary = tmp_path / "summary"
    summary.write_text("Earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTIONS", str(actions).lower())
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(health, "PIPELINES", ["fed-pipeline"])
    monkeypatch.setattr(health, "DATASETS", {LAWS: "local-laws"})
    latest = run(datetime.now(UTC), conclusion="cancelled")

    def fetch(url, token=None):
        if url.startswith("https://api.github.com/"):
            assert token == "test-token"
            if unavailable:
                raise health.CheckError("GitHub unavailable")
            return {"workflow_runs": [latest]}, None
        assert token is None
        return {tree(LAWS): [file("manifest.json")], resolve(LAWS, "manifest.json"): {}}[url], None

    monkeypatch.setattr(health, "fetch", fetch)
    assert health.main() == int(unavailable)
    stdout = capsys.readouterr().out
    markdown = summary.read_text().removeprefix("Earlier step\n")
    assert stdout.endswith(markdown) and markdown.count("| Subject | Status | Detail |") == 3
    assert ("::error::fed-pipeline: Cannot check workflow: GitHub unavailable" in stdout) == (actions and unavailable)
    assert ("::warning::" + LAWS in stdout) == actions
    assert "::" not in markdown
