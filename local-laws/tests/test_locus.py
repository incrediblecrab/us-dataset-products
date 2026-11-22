"""The crosswalk's rules, each reached by one jurisdiction of the synthetic LOCUS against real Census rows, and the guards that stop a build the rules were not checked against."""

import re
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest
from huggingface_hub.errors import HfHubHTTPError

from conftest import EXPECTED, LOCUS, units_rows, write_locus
from local_laws import locus
from local_laws import store as store_module
from local_laws.census import SourceChanged


def crosswalk(tmp_path, jurisdictions=None, governments=None):
    paths, rows = write_locus(tmp_path, jurisdictions)
    out = locus.crosswalk(paths, governments or units_rows()[0])
    assert sum(row["locus_rows"] for row in out) == rows
    return {row["locus_document_id"]: row for row in out}


def test_every_rule_matches_the_government_it_is_written_for(tmp_path):
    rows = crosswalk(tmp_path)
    assert {doc: (row["census_id"], row["match"], row["name_match"], row["text_fits"]) for doc, row in rows.items()} == EXPECTED
    assert set(locus.MATCHES) == {match for _, match, _, _ in EXPECTED.values()}, "the fixture reaches every rule"
    assert {(doc, row["locus_state"], row["locus_jurisdiction_type"], row["locus_name"], row["locus_geoid"]) for doc, row in rows.items()} == set(LOCUS)


def test_candidates_are_kept_for_every_choice_among_same_name_governments(tmp_path):
    rows = crosswalk(tmp_path)
    candidates = {row["locus_name"]: row["candidates"] for row in rows.values() if row["candidates"]}
    assert candidates == {
        "Belvidere": ["125103", "162180"],
        "Franklin Township": ["109173", "170293", "170302", "186570"],
        "Howard": ["127039", "127088"],
        "Bison": ["175544", "187490"],
    }
    assert all((row["match"] in locus.POOLED) == bool(row["candidates"]) for row in rows.values())


def test_text_counts(tmp_path):
    rows = crosswalk(tmp_path)
    belvidere = rows["doc_0005"]
    assert (belvidere["text_type"], belvidere["text_type_mentions"], belvidere["text_mentions"]) == ("city", 3, 4), "3 of 4 mentions is lopsided enough"
    howard = rows["doc_0007"]
    assert (howard["text_type"], howard["text_mentions"]) == ("city", 1), "text is counted even when it settles nothing"
    bison = rows["doc_0008"]
    assert (bison["text_type"], bison["text_type_mentions"], bison["text_mentions"]) == (None, 0, 0)
    assert rows["doc_0002"]["locus_rows"] == 4, "a row without content still counts as a row"
    nowhere = rows["doc_0016"]
    assert (nowhere["text_type"], nowhere["text_mentions"], nowhere["text_fits"]) == (None, None, None), "no government, nothing to look for"


def test_the_text_must_be_lopsided_to_choose(tmp_path):
    barre = ("doc_0003", "VT", "city", "Barre", None)
    two = crosswalk(tmp_path / "two", {barre: ["Town of Barre."] * 2})["doc_0003"]
    assert (two["match"], two["census_id"]) == ("municipal_preferred", "114404"), "2 mentions are too few"
    split = crosswalk(tmp_path / "split", {barre: ["Town of Barre."] * 3 + ["City of Barre."] * 2})
    assert split["doc_0003"]["match"] == "municipal_preferred", "3 of 5 is under two-thirds"
    county = {("doc_0006", "NJ", "city", "Franklin Township", None): ["Township of Franklin, in Gloucester County."] * 3 + ["Warren County."] * 2}
    assert crosswalk(tmp_path / "county", county)["doc_0006"]["match"] == "ambiguous"


@pytest.mark.parametrize("name, geoid, uid, method", [
    ("Elsewhere", "01001", "100001", "unmatched"),
    ("Elsewhere", "0162328", "100019", "unmatched"),
    ("Elsewhere", "5002303250", "135964", "unmatched"),
    ("Howard", "2004933275", "127039", "ambiguous"),
])
def test_geoid_length_selects_the_government_even_when_its_name_is_unsettled(tmp_path, name, geoid, uid, method):
    state = {"01": "AL", "50": "VT", "20": "KS"}[geoid[:2]]
    row = crosswalk(tmp_path, {("doc_0001", state, "city", name, geoid): [""]})["doc_0001"]
    assert (row["match"], row["census_id"], row["name_match"]) == ("geoid", uid, method)


@pytest.mark.parametrize("state, name, geoid, problem", [
    ("VT", "Barre", "5002", "GEOID length"),
    ("AL", "Prattville", "01001", "GEOID and its name name different governments"),
    ("VT", "Barre", "0162328", "not among its name candidates"),
])
def test_an_invalid_or_contradictory_geoid_stops_the_build(tmp_path, state, name, geoid, problem):
    with pytest.raises(SourceChanged, match=problem):
        crosswalk(tmp_path, {("doc_0001", state, "city", name, geoid): ["text"]})


@pytest.mark.parametrize("kind", ["city-county", "other"])
def test_a_name_fallback_reads_accents_and_spaces(tmp_path, kind):
    row = crosswalk(tmp_path, {("doc_0001", "AL", kind, "Práttville   City", "0199999"): ["text"]})["doc_0001"]
    assert (row["census_id"], row["match"], row["name_match"]) == ("100019", "name_type", None)


def test_two_jurisdictions_matched_to_one_government_stop_the_build(tmp_path):
    with pytest.raises(SourceChanged, match="matched to the same government.*100019"):
        crosswalk(tmp_path, LOCUS | {("doc_0017", "AL", "city", "Prattville City", None): ["City of Prattville."]})


@pytest.mark.parametrize("other, problem", [
    (("doc_0001", "AL", "city", "Prattville", None), "maps to multiple jurisdictions"),
    (("doc_0001", "AL", "county", "Autauga County", None), "maps to multiple jurisdictions"),
    (("doc_0002", "AL", "county", "Autauga County", "01001"), "maps to multiple documents"),
])
def test_document_and_jurisdiction_identify_each_other_across_files(tmp_path, other, problem):
    with pytest.raises(SourceChanged, match=problem):
        crosswalk(tmp_path, {("doc_0001", "AL", "county", "Autauga County", "01001"): ["a"], other: ["b"]})


@pytest.mark.parametrize("column", range(4))
def test_null_identifiers_stop_the_build(tmp_path, column):
    key = ["doc_0001", "AL", "city", "Prattville", None]
    key[column] = None
    with pytest.raises(SourceChanged, match="without a document or jurisdiction"):
        crosswalk(tmp_path, {tuple(key): ["text"]})


def test_unknown_jurisdiction_type_stops_the_build(tmp_path):
    with pytest.raises(SourceChanged, match="unknown LOCUS jurisdiction type"):
        crosswalk(tmp_path, {("doc_0001", "AL", "district", "Prattville", "0162328"): ["text"]})


@pytest.mark.parametrize("words, prose", [
    (["ST", "CHARLES"], ["St. Charles", "Saint Charles", "St Charles"]),
    (["COEUR", "D", "ALENE"], ["Coeur d'Alene", "Coeur d\u2019Alene", "Coeur D Alene"]),
    (["EL", "DORADO"], ["ElDorado", "El Dorado"]),
    (["SAN", "JOSE"], ["San José", "San Jose\u0301", "San Jose"]),
    (["ST", "MARYS"], ["St. Mary's", "Saint Marys"]),
    (["MACON-BIBB"], ["Macon-Bibb", "Macon Bibb"]),
])
def test_census_names_match_as_prose_writes_them(words, prose):
    pattern = re.compile(rf"^{locus.words_pattern(words)}$", re.I)
    assert [text for text in prose if not pattern.match(text)] == []


def test_names_do_not_match_a_longer_word():
    target = locus.Target([{"name": "CITY OF BARRE", "county_name": "WASHINGTON"}])
    target.read("The City of Barrett and Barretown City are not Barre. The City of the Barre is.")
    assert target.words == Counter({"city": 1})


def hub_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", f"https://huggingface.co/api/datasets/{locus.REPO_ID}/revision/{locus.REVISION}"))
    return HfHubHTTPError(f"{status} from the fake Hub", response=response)


class BusyLocus:
    """LOCUS's repo on a Hub that answers each read once with a planted failure, then as it should."""

    def __init__(self, directory):
        self.calls = []
        self.planted = {"dataset_info": hub_error(429), "list_repo_files": httpx.ConnectError("planted"), "README.md": hub_error(503), "sections/a.parquet": hub_error(503)}
        self.license = locus.LICENSE
        self.readme = directory / "README.md"
        self.readme.write_text("## Subsets and coverage\n\n| `sections` | 1,234 | Default: synthetic sections |\n")
        self.files = ["README.md", "data/a.parquet", "sections/a.parquet", "sections/notes.txt"]

    def answer(self, name):
        self.calls.append(name)
        if name in self.planted:
            raise self.planted.pop(name)

    def dataset_info(self, repo_id, revision=None):
        assert repo_id == locus.REPO_ID and revision in (None, locus.REVISION)
        self.answer("dataset_info")
        card = {"license": self.license}
        return SimpleNamespace(sha=revision or "head", last_modified=None, card_data=SimpleNamespace(to_dict=lambda: card))

    def list_repo_files(self, repo_id, repo_type, revision):
        assert (repo_id, repo_type, revision) == (locus.REPO_ID, "dataset", locus.REVISION)
        self.answer("list_repo_files")
        return self.files

    def hf_hub_download(self, repo_id, path, repo_type, revision, local_dir=None):
        assert (repo_id, repo_type, revision) == (locus.REPO_ID, "dataset", locus.REVISION)
        self.answer(path)
        if path == "README.md":
            return self.readme
        return f"{local_dir}/{path}"


def test_the_license_is_checked_before_reading_sections(tmp_path):
    api = BusyLocus(tmp_path)
    api.planted.clear()
    api.license = "cc-by-4.0"
    with pytest.raises(SourceChanged, match="LOCUS's license is 'cc-by-4.0'"):
        locus.stated_rows(api)
    assert api.calls == ["dataset_info"]


@pytest.mark.parametrize("body", ["| `documents` | 12 | Documents |", "| `sections` | 1,234 | Default |\n" * 2, "| `sections` | 1,23 | Default |"])
def test_missing_duplicate_or_malformed_sections_count_is_refused(tmp_path, body):
    api = BusyLocus(tmp_path)
    api.planted.clear()
    api.readme.write_text(body)
    with pytest.raises(SourceChanged, match="expected one sections row"):
        locus.stated_rows(api)


def test_no_sections_files_is_not_a_build_of_the_old_subset(tmp_path):
    api = BusyLocus(tmp_path)
    api.planted.clear()
    api.files = ["data/a.parquet"]
    with pytest.raises(SourceChanged, match="no sections parquet"):
        locus.download(tmp_path, api=api)


def test_busy_reads_of_locus_are_retried(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(store_module.time, "sleep", waits.append)
    api = BusyLocus(tmp_path)
    assert locus.download(str(tmp_path), api=api) == ([f"{tmp_path}/sections/a.parquet"], 1234)
    assert api.calls == ["dataset_info"] * 2 + ["README.md"] * 2 + ["list_repo_files"] * 2 + ["sections/a.parquet"] * 2 and waits == [60] * 4
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token: BusyLocus(tmp_path))
    assert locus.latest()["sha"] == "head" and waits == [60] * 5, "the stale-pin check's read is retried too"


@pytest.mark.parametrize("refused", ["dataset_info", "README.md"])
def test_a_refused_read_of_locus_is_raised_at_once(monkeypatch, tmp_path, refused):
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: pytest.fail("a refusal is not retried"))
    api = BusyLocus(tmp_path)
    api.planted = {refused: hub_error(404)}
    with pytest.raises(HfHubHTTPError):
        locus.stated_rows(api)
    assert api.calls == (["dataset_info"] if refused == "dataset_info" else ["dataset_info", "README.md"])
