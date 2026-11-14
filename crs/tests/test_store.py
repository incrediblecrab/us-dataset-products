"""The store: partition keys, Parquet round trips, and the Hub commit fence against a fake Hub API."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pyarrow.parquet as pq
import pytest
from huggingface_hub.errors import HfHubHTTPError

from crs_products import store as store_module
from crs_products.cli import read_probe_state
from crs_products.pipeline import PROBE_KEY, new_manifest, probe_state
from crs_products.store import HubStore, Superseded, git_blob_sha1, partition_of, read_parquet, write_parquet


def http_error(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://huggingface.co/api/datasets/x/y/commit/main"))
    return HfHubHTTPError(f"{status} from the fake Hub", response=response)


class FakeApi:
    """The parts of HfApi that HubStore.commit uses. A commit on a parent that is not the head answers 412, as the Hub did when measured."""

    token = False

    def __init__(self):
        self.head, self.files, self.commits, self.attempts = "c0", {}, [], 0
        self.lose_next_response = False
        self.card_data = None

    def dataset_info(self, repo_id):
        return SimpleNamespace(sha=self.head, card_data=self.card_data)

    def create_commit(self, repo_id, operations, commit_message, repo_type, parent_commit):
        self.attempts += 1
        if parent_commit != self.head:
            raise http_error(412)
        for operation in operations:
            self.files[operation.path_in_repo] = Path(operation.path_or_fileobj).read_bytes()
        self.commits.append(commit_message)
        self.head = f"c{len(self.commits)}"
        if self.lose_next_response:
            self.lose_next_response = False
            raise http_error(502)
        return SimpleNamespace(oid=self.head)

    def get_paths_info(self, repo_id, paths, repo_type, revision):
        return [SimpleNamespace(path=path, blob_id=git_blob_sha1(self.files[path]), lfs=None) for path in paths if path in self.files]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: None)
    api = FakeApi()
    return HubStore("x/y", workdir=tmp_path, api=api, card=lambda manifest: f"card for {manifest}"), api


class UnreachableApi(FakeApi):
    def dataset_info(self, repo_id):
        raise httpx.ConnectError("planted: the Hub cannot be reached")


def test_a_hub_that_cannot_be_reached_leaves_no_scratch_directory(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(store_module.time, "sleep", waits.append)
    with pytest.raises(httpx.ConnectError):
        HubStore("x/y", workdir=tmp_path, api=UnreachableApi())
    assert waits == [60, 120, 180], "tried HUB_ATTEMPTS times before giving up"
    assert list(tmp_path.iterdir()) == []


class FlakyApi(FakeApi):
    """dataset_info fails with each planted error in turn, then answers."""

    def __init__(self, *errors):
        super().__init__()
        self.errors, self.calls = list(errors), 0

    def dataset_info(self, repo_id):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return super().dataset_info(repo_id)


def oidc_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://run-actions-2-azure-eastus.actions.githubusercontent.com/229//idtoken/x/y?audience=https%3A%2F%2Fhuggingface.co"))
    return HfHubHTTPError(f"Server error '{status}' for the id-token endpoint", response=response)


@pytest.mark.parametrize("errors", [
    # The scheduled summaries verify of November 11, 2026: dataset_info answered "429 Too Many Requests ... maximum time in concurrency queue reached".
    (http_error(429), http_error(429)),
    # A writer's first request trades the job's OIDC token; GitHub's id-token endpoint answered 503 to the scotus run of November 2, 2026.
    (oidc_error(503),),
    (httpx.ReadTimeout("planted"), http_error(502), http_error(504)),
], ids=["hub-429", "oidc-503", "timeout-502-504"])
def test_a_busy_hub_at_open_is_retried(tmp_path, monkeypatch, errors):
    waits = []
    monkeypatch.setattr(store_module.time, "sleep", waits.append)
    api = FlakyApi(*errors)
    store = HubStore("x/y", workdir=tmp_path, api=api)
    assert store.revision == "c0" and api.calls == len(errors) + 1
    assert waits == [60, 120, 180][:len(errors)]


@pytest.mark.parametrize("status", [401, 404])
def test_a_refusal_at_open_is_raised_at_once(tmp_path, monkeypatch, status):
    """A missing repo or a refused token trade (an unregistered Trusted Publisher) is not a busy Hub: retrying it would only delay the error the owner must see."""
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: pytest.fail("a refusal is not retried"))
    api = FlakyApi(http_error(status))
    with pytest.raises(HfHubHTTPError):
        HubStore("x/y", workdir=tmp_path, api=api)
    assert api.calls == 1


def test_a_busy_hub_during_a_download_is_retried_and_a_missing_file_is_not(hub, monkeypatch):
    store, api = hub
    from huggingface_hub.errors import EntryNotFoundError

    calls = []

    def download(repo_id, repo_path, **kwargs):
        calls.append(repo_path)
        if repo_path == "absent.json":
            raise EntryNotFoundError("planted 404")
        if calls.count(repo_path) < 3:
            raise http_error(429)
        target = Path(kwargs["local_dir"]) / repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"n": 1}')
        return str(target)

    monkeypatch.setattr(store_module, "hf_hub_download", download)
    assert store.read_manifest() == {"n": 1} and calls.count("manifest.json") == 3
    assert store.read_text("absent.json") is None and calls.count("absent.json") == 1


def test_busy_metadata_reads_are_retried(hub):
    store, api = hub
    api.files["data/a.parquet"] = b"x"
    real_paths = api.get_paths_info
    failures = {"paths": 1, "files": 2}

    def get_paths_info(*args, **kwargs):
        if failures["paths"]:
            failures["paths"] -= 1
            raise http_error(503)
        return real_paths(*args, **kwargs)

    def list_repo_files(repo_id, repo_type, revision):
        if failures["files"]:
            failures["files"] -= 1
            raise httpx.ConnectError("planted")
        return sorted(api.files)

    api.get_paths_info, api.list_repo_files = get_paths_info, list_repo_files
    assert store.list_files("data/") == ["data/a.parquet"]
    store.file_sha256s(["data/a.parquet"])
    assert failures == {"paths": 0, "files": 0}


def test_a_commit_on_a_stale_parent_is_superseded_and_the_store_writes_nothing_more(hub):
    store, api = hub
    store.stage_manifest({"n": 1})
    assert store.commit("first") == "c1" and set(api.files) == {"manifest.json", "README.md"}
    api.head = "someone-else"
    store.stage_manifest({"n": 2})
    with pytest.raises(Superseded):
        store.commit("second")
    attempts = api.attempts
    store.stage_manifest({"n": 3})
    with pytest.raises(Superseded):
        store.commit("third")
    assert api.attempts == attempts, "a superseded store does not ask the Hub again"
    assert api.commits == ["first"]


def test_a_retried_commit_that_had_landed_is_adopted_not_superseded(hub):
    store, api = hub
    api.lose_next_response = True
    store.stage_manifest({"n": 1})
    assert store.commit("first") == "c1"
    assert api.commits == ["first"] and store.revision == "c1" and store.superseded is None
    store.stage_manifest({"n": 2})
    assert store.commit("second") == "c2"


def test_a_412_whose_head_holds_a_different_manifest_is_superseded(hub):
    store, api = hub
    store.stage_manifest({"n": 1})
    real_create = api.create_commit

    def other_writer_first(*args, **kwargs):
        # Another writer's manifest lands between this store's attempts.
        api.create_commit = real_create
        api.files["manifest.json"] = b'{"n": "theirs"}\n'
        api.head = "theirs"
        raise http_error(502)

    api.create_commit = other_writer_first
    with pytest.raises(Superseded):
        store.commit("first")


def test_a_readme_check_answered_with_a_busy_page_is_retried(hub):
    """huggingface_hub parses the validate-yaml body before it checks the status, so a busy Hub raises JSONDecodeError before anything is uploaded; the Fed scheduled run of September 27, 2026 stopped on one."""
    store, api = hub
    real_create = api.create_commit

    def busy_first(*args, **kwargs):
        api.create_commit = real_create
        raise json.JSONDecodeError("Expecting value", "", 0)

    api.create_commit = busy_first
    store.stage_manifest({"n": 1})
    assert store.commit("first") == "c1" and api.commits == ["first"]


def test_a_dropped_connection_whose_commit_landed_is_adopted(hub):
    store, api = hub
    real_create = api.create_commit

    def lands_then_drops(*args, **kwargs):
        api.create_commit = real_create
        real_create(*args, **kwargs)
        raise httpx.ReadTimeout("planted: the response never arrived")

    api.create_commit = lands_then_drops
    store.stage_manifest({"n": 1})
    assert store.commit("first") == "c1"
    assert api.commits == ["first"] and store.superseded is None, "the retry adopted the landed commit rather than committing twice"


def test_invalid_card_metadata_is_raised_at_once(hub):
    store, api = hub

    def invalid(*args, **kwargs):
        api.attempts += 1
        raise ValueError("Invalid metadata in README.md.")

    api.create_commit = invalid
    store.stage_manifest({"n": 1})
    with pytest.raises(ValueError, match="Invalid metadata"):
        store.commit("first")
    assert api.attempts == 1, "JSONDecodeError is a ValueError, but a card the Hub rejects is not retried"


def test_git_blob_sha1_matches_git():
    assert git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_the_probe_reads_the_hub_cards_state_without_downloading_a_file(tmp_path, monkeypatch):
    state = probe_state(dict(new_manifest(), writer={"by": "local", "at": "2026-09-24T21:13:28Z"}))
    downloads = []
    monkeypatch.setattr(HubStore, "_download", lambda self, repo_path: downloads.append(repo_path))
    api = FakeApi()
    api.card_data = SimpleNamespace(to_dict=lambda: {"license": "other", PROBE_KEY: state})
    assert read_probe_state(HubStore("x/y", workdir=tmp_path, api=api)) == (state, "card") and downloads == []
    # A card rendered before it carried the state, or carrying another version of it: the probe falls back to the manifest, a file download.
    for data in ({"license": "other"}, {PROBE_KEY: dict(state, version=state["version"] + 1)}):
        downloads.clear()
        api.card_data = SimpleNamespace(to_dict=lambda: data)
        assert read_probe_state(HubStore("x/y", workdir=tmp_path, api=api)) == (None, "manifest") and downloads == ["manifest.json"]


@pytest.mark.parametrize("uid, key", [("R49359", "R49"), ("RL34480", "RL34"), ("IN12740", "IN12"), ("LSB11001", "LSB11"), ("R40", "R0"), ("98-684", "numeric"), ("", "other"), ("abc", "other")])
def test_partition_of(uid, key):
    assert partition_of(uid) == key


def test_rows_round_trip_with_list_columns_and_typed_version(tmp_path):
    path = tmp_path / "p.parquet"
    stats = write_parquet([{"id": "R40002", "version": "3", "authors": None, "metadata": {"a": 1}, "text": "t"}, {"id": "R40001", "topics": ["T"]}], path)
    assert stats["rows"] == 2 and stats["text_rows"] == 1 and len(stats["sha256"]) == 64
    rows = read_parquet(path)
    assert [row["id"] for row in rows] == ["R40001", "R40002"], "rows are sorted by id"
    assert rows[1]["version"] == 3 and rows[1]["authors"] == [] and rows[1]["metadata"] == '{"a": 1}' and rows[0]["topics"] == ["T"]
    assert str(pq.read_schema(path).field("version").type) == "int32"
    empty = write_parquet([], tmp_path / "empty.parquet")
    assert empty["rows"] == 0 and read_parquet(tmp_path / "empty.parquet") == []
    # The datasets library reads a file in batches the size of its first row group, and fails on one of 0 rows.
    assert pq.ParquetFile(tmp_path / "empty.parquet").metadata.num_row_groups == 0


def busy_lookup(status):
    """What hf_hub_download 1.32.0 raises when its HEAD request fails and its own retry gives up: LocalEntryNotFoundError, chained from the failure."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        raise http_error(status)
    except HfHubHTTPError as cause:
        try:
            raise LocalEntryNotFoundError("An error happened while trying to locate the file on the Hub") from cause
        except LocalEntryNotFoundError as error:
            return error


def test_a_busy_file_lookup_is_retried_and_never_read_as_a_missing_file(hub, monkeypatch):
    store, api = hub
    waits, calls = [], []
    monkeypatch.setattr(store_module.time, "sleep", waits.append)
    plan = {"manifest.json": [busy_lookup(429)], "README.md": [busy_lookup(503)] * 4, "other.json": [busy_lookup(403)]}

    def download(repo_id, repo_path, **kwargs):
        calls.append(repo_path)
        if plan[repo_path]:
            raise plan[repo_path].pop(0)
        target = Path(kwargs["local_dir"]) / repo_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"n": 1}')
        return str(target)

    monkeypatch.setattr(store_module, "hf_hub_download", download)
    assert store.read_manifest() == {"n": 1} and waits == [60]
    with pytest.raises(HfHubHTTPError) as raised:
        store.read_text("README.md")
    assert raised.value.response.status_code == 503 and calls.count("README.md") == 4 and waits == [60, 60, 120, 180]
    with pytest.raises(HfHubHTTPError):
        store.read_text("other.json")
    assert calls.count("other.json") == 1, "a refusal behind the lookup is raised at once"
