"""Read-only checks of the publishing workflows and their public datasets."""

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.client import HTTPException
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

SCHEDULE_HOURS = 6  # The publishing workflows use "37 */6 * * *".
LATE_HOURS = 11  # GitHub started daily runs 5.8 to 10.1 hours late on November 9-22, 2026.
MISSED_HOURS = SCHEDULE_HOURS + LATE_HOURS  # Allow one slot plus GitHub's observed delay.
STALE_HOURS = 2 * SCHEDULE_HOURS + LATE_HOURS  # Allow two slots plus the same delay for a complete listing.
# How often a dataset's manifest is rewritten when nothing changed: every run unless listed. The CRS papers' and the Beige Book's probes reread every list only once their last complete listing is 24 hours old; local laws writes only when its data changes, so it never shows a newer build or listing time on its own. The cases builder records its last complete run as last_complete_at.
REFRESH_HOURS = {"incrediblecrab/us-crs-research-papers": 24, "incrediblecrab/us-federal-reserve-beige-book": 24, "incrediblecrab/us-local-laws": None}

PIPELINES = ("crs-pipeline", "crs-summaries", "crs-constitution", "fed-pipeline", "local-laws-pipeline", "scotus-pipeline")
CASES = "incrediblecrab/us-scotus-cases"
DATASETS = {
    "incrediblecrab/us-crs-research-papers": "crs",
    "incrediblecrab/us-crs-bill-summaries": "crs",
    "incrediblecrab/us-crs-constitution": "crs",
    "incrediblecrab/us-federal-reserve-beige-book": "fed",
    "incrediblecrab/us-local-laws": "local-laws",
    "incrediblecrab/us-scotus-opinions": "scotus",
    "incrediblecrab/us-scotus-oral-arguments": "scotus",
    "incrediblecrab/us-scotus-case-documents": "scotus",
    "incrediblecrab/us-scotus-news-media": "scotus",
    "incrediblecrab/us-scotus-filing-and-rules": "scotus",
    "incrediblecrab/us-scotus-about": "scotus",
    CASES: "scotus",
}
HUB = "https://huggingface.co"


def allowance(dataset):
    """Hours after which a dataset's last complete listing, or its build from an older folder, is overdue; None when the dataset writes only when its data changes."""
    refresh = REFRESH_HOURS.get(dataset, SCHEDULE_HOURS)
    return None if refresh is None else refresh + SCHEDULE_HOURS + LATE_HOURS


class Row(NamedTuple):
    subject: str
    status: str
    detail: str


class CheckError(Exception):
    pass


def fetch(url, token=None):
    """Read a JSON page and its optional next link."""
    request = Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                link = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))
                return json.load(response), urljoin(url, link[1]) if link else None
        except HTTPError as exc:
            exc.close()
            if attempt == 3 or not (exc.code in (408, 429) or 500 <= exc.code < 600):
                raise CheckError(f"{url}: HTTP {exc.code}") from exc
        except (OSError, HTTPException) as exc:
            if attempt == 3:
                raise CheckError(f"{url}: {exc}") from exc
        except ValueError as exc:
            raise CheckError(f"{url}: invalid JSON: {exc}") from exc
        time.sleep(60)


def timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {value}")
    return stamp


def check_pipeline(name, repo, token, now):
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{name}.yml/runs?status=completed&per_page=1"
    rows = []
    try:
        data, _ = fetch(url, token)
        if runs := data["workflow_runs"]:
            run = runs[0]
            result = run["conclusion"]
            status = {"failure": "error", "timed_out": "error", "cancelled": "warning"}.get(result, "ok")
            rows.append(Row(name, status, f"{result}: {run['html_url']}"))
        else:
            rows.append(Row(name, "ok", "No completed run yet"))
        data, _ = fetch(f"{url}&event=schedule", token)
        runs = data["workflow_runs"]
        if not runs or now - timestamp(runs[0]["created_at"]) > timedelta(hours=MISSED_HOURS):
            rows.append(Row(name, "error", f"No completed scheduled run created in the last {MISSED_HOURS} hours"))
        else:
            rows.append(Row(name, "ok", f"Scheduled run created {runs[0]['created_at']}"))
    except (CheckError, KeyError, ValueError, TypeError) as exc:
        rows.append(Row(name, "error", f"Cannot check workflow: {exc}"))
    return rows


def manifest_paths(dataset):
    url = f"{HUB}/api/datasets/{dataset}/tree/main?recursive=true"
    paths = []
    while url:
        entries, url = fetch(url)
        paths.extend(e["path"] for e in entries if e["type"] == "file" and e["path"].endswith("manifest.json"))
        if url and (urlsplit(url).scheme, urlsplit(url).netloc) != ("https", "huggingface.co"):
            raise CheckError(f"Unexpected Hub pagination URL: {url}")
    if not paths:
        raise CheckError("No manifest.json found in the Hub tree")
    return paths


def recorded_build(dataset, folder, manifest):
    """Return the build commit and record time (to compare collections in one dataset)."""
    if folder == "local-laws":
        return (manifest.get("code") or {}).get("commit"), None
    runs = manifest.get("runs") or []
    if dataset == CASES:
        if commit := (manifest.get("last_complete_code") or {}).get("commit"):
            return commit, None
    else:
        runs = reversed(runs)  # Regular manifests append runs; cases prepends them.
    for run in runs:
        if commit := (run.get("code") or {}).get("commit"):
            return commit, run.get("ended") or run.get("at")
    if dataset == CASES:
        return None, None
    listing = manifest.get("listing") or {}
    return (listing.get("code") or {}).get("commit"), listing.get("at")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def alignment(dataset, folder, commit, now):
    if not commit:
        return Row(dataset, "warning", "No build commit recorded (records before 2026-11-23 lack it)")
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", commit) or git("cat-file", "-e", f"{commit}^{{commit}}").returncode:
        return Row(dataset, "error", f"Build commit {commit} is not in this repository")
    ancestor = git("merge-base", "--is-ancestor", commit, "HEAD")
    if ancestor.returncode == 1:
        return Row(dataset, "error", f"Build commit {commit} is not an ancestor of HEAD")
    ancestor.check_returncode()
    path = f"{folder}/"
    diff = git("diff", "--quiet", commit, "HEAD", "--", path)
    if diff.returncode == 1:
        log = git("log", "-1", "--format=%ct", "--", path)
        log.check_returncode()
        changed = datetime.fromtimestamp(int(log.stdout), UTC)
        limit = allowance(dataset)
        if limit is None:
            return Row(dataset, "warning", f"Built from {commit}; {path} changed at {changed.isoformat()}; this dataset is rewritten only when its data changes")
        overdue = now - changed > timedelta(hours=limit)
        return Row(dataset, "error" if overdue else "ok", f"Built from {commit}; {path} changed at {changed.isoformat()} (limit {limit} hours)")
    diff.check_returncode()
    return Row(dataset, "ok", f"{path} matches build {commit}")


def freshness(dataset, folder, path, listing, now):
    subject = f"{dataset}/{path}"
    limit = allowance(dataset)
    if limit is None:
        return Row(subject, "n/a", "Rewritten only when its data changes; its workflow runs are checked under A")
    at = (listing or {}).get("at")
    if not at:
        return Row(subject, "error", "No time recorded for the last complete run")
    stale = now - timestamp(at) > timedelta(hours=limit)
    return Row(subject, "error" if stale else "ok", f"Last complete listing: {at} (limit {limit} hours)")


def check_dataset(dataset, now):
    folder = DATASETS[dataset]
    builds = []
    listings = []
    try:
        for path in manifest_paths(dataset):
            manifest, _ = fetch(f"{HUB}/datasets/{dataset}/resolve/main/{quote(path)}")
            if not isinstance(manifest, dict):
                raise CheckError(f"{path}: expected a JSON object")
            commit, at = recorded_build(dataset, folder, manifest)
            if commit:
                builds.append((timestamp(at) if at else datetime.min.replace(tzinfo=UTC), commit))
            try:
                listing = {"at": manifest.get("last_complete_at")} if dataset == CASES else manifest.get("listing")
                listings.append(freshness(dataset, folder, path, listing, now))
            except (ValueError, TypeError) as exc:
                listings.append(Row(f"{dataset}/{path}", "error", f"Invalid listing.at: {exc}"))
    except (CheckError, KeyError, TypeError, ValueError, AttributeError) as exc:
        error = Row(dataset, "error", f"Cannot check all manifests: {exc}")
        return error, [*listings, Row(dataset, "unknown", error.detail)]
    try:
        commit = max(builds, key=lambda build: build[0])[1] if builds else None
        return alignment(dataset, folder, commit, now), listings
    except (OSError, subprocess.CalledProcessError, ValueError, TypeError) as exc:
        return Row(dataset, "error", f"Cannot check build commit: {exc}"), listings


def report(tables):
    lines = []
    failed = False
    for title, rows in tables:
        lines.extend([f"## {title}", "", "| Subject | Status | Detail |", "|---|---|---|"])
        for row in rows:
            cells = [s.replace("|", r"\|").replace("\r", " ").replace("\n", " ") for s in row]
            lines.append("| " + " | ".join(cells) + " |")
            failed |= row.status == "error"
            if row.status in ("error", "warning") and os.environ.get("GITHUB_ACTIONS") == "true":
                message = f"{row.subject}: {row.detail}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                print(f"::{row.status}::{message}")
        lines.append("")
    markdown = "\n".join(lines) + "\n"
    print(markdown, end="")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write(markdown)
    return int(failed)


def main():
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=6) as pool:
        workflows = [row for rows in pool.map(lambda name: check_pipeline(name, repo, token, now), PIPELINES) for row in rows]
        datasets = list(pool.map(lambda dataset: check_dataset(dataset, now), DATASETS))
    return report([
        ("A. Pipeline runs", workflows),
        ("B. Build commits", [build for build, _ in datasets]),
        ("C. Listing freshness", [row for _, listings in datasets for row in listings]),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
