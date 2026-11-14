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
    return {(row["locus_state"], row["locus_jurisdiction_type"], row["locus_name"]): row for row in out}


def test_every_rule_matches_the_government_it_is_written_for(tmp_path):
    rows = crosswalk(tmp_path)
    assert {name: (row["census_id"], row["match"], row["text_fits"]) for name, row in rows.items()} == EXPECTED
    assert set(locus.MATCHES) == {match for _, match, _ in EXPECTED.values()}, "the fixture reaches every rule"


def test_candidates_are_kept_for_every_choice_among_same_name_governments(tmp_path):
    rows = crosswalk(tmp_path)
    candidates = {f"{state}/{name}": row["candidates"] for (state, _, name), row in rows.items() if row["candidates"]}
    assert candidates == {
        "vt/barre": ["114404", "135964"],
        "il/belvidere": ["125103", "162180"],
        "nj/franklin_township": ["109173", "170293", "170302", "186570"],
        "ks/howard": ["127039", "127088"],
        "sd/bison": ["175544", "187490"],
    }
    assert all((row["match"] in locus.POOLED) == bool(row["candidates"]) for row in rows.values())


def test_text_counts(tmp_path):
    rows = crosswalk(tmp_path)
    belvidere = rows[("il", "cities", "belvidere")]
    assert (belvidere["text_type"], belvidere["text_type_mentions"], belvidere["text_mentions"]) == ("city", 3, 4), "3 of 4 mentions is lopsided enough"
    howard = rows[("ks", "cities", "howard")]
    assert (howard["text_type"], howard["text_mentions"]) == ("city", 1), "text is counted even when it settles nothing"
    bison = rows[("sd", "cities", "bison")]
    assert (bison["text_type"], bison["text_type_mentions"], bison["text_mentions"]) == (None, 0, 0)
    assert rows[("al", "cities", "prattville")]["locus_rows"] == 4, "a row without content still counts as a row"
    nowhere = rows[("oh", "cities", "nowhere_village")]
    assert (nowhere["text_type"], nowhere["text_mentions"], nowhere["text_fits"]) == (None, None, None), "no government, nothing to look for"


def test_the_text_must_be_lopsided_to_choose(tmp_path):
    two = LOCUS | {("vt", "cities", "barre"): ["Town of Barre."] * 2}
    assert crosswalk(tmp_path / "two", two)[("vt", "cities", "barre")]["match"] == "municipal_preferred", "2 mentions are too few"
    split = LOCUS | {("vt", "cities", "barre"): ["Town of Barre."] * 3 + ["City of Barre."] * 2}
    assert crosswalk(tmp_path / "split", split)[("vt", "cities", "barre")]["match"] == "municipal_preferred", "3 of 5 is under two-thirds"
    county = LOCUS | {("nj", "cities", "franklin_township"): ["Township of Franklin, in Gloucester County."] * 3 + ["Warren County."] * 2}
    assert crosswalk(tmp_path / "county", county)[("nj", "cities", "franklin_township")]["match"] == "ambiguous"


def test_an_alias_whose_government_was_renamed_stops_the_build(tmp_path):
    governments = [dict(row, name="CITY OF AURELIA HEIGHTS") if row["census_id"] == "164335" else row for row in units_rows()[0]]
    with pytest.raises(SourceChanged, match="alias ia/cities/aurel expects"):
        crosswalk(tmp_path, governments=governments)


def test_an_alias_for_a_jurisdiction_locus_dropped_stops_the_build(tmp_path):
    fewer = {name: texts for name, texts in LOCUS.items() if name != ("in", "cities", "aust")}
    with pytest.raises(SourceChanged, match="aliases for jurisdictions LOCUS no longer has"):
        crosswalk(tmp_path, fewer)


def test_two_jurisdictions_matched_to_one_government_stop_the_build(tmp_path):
    with pytest.raises(SourceChanged, match="matched to the same government.*100019"):
        crosswalk(tmp_path, LOCUS | {("al", "cities", "prattville_city"): ["City of Prattville."]})


def test_a_row_without_a_jurisdiction_stops_the_build(tmp_path):
    with pytest.raises(SourceChanged, match="without a jurisdiction"):
        crosswalk(tmp_path, LOCUS | {("al", "cities", ""): ["text"]})
    with pytest.raises(SourceChanged, match="without a jurisdiction"):
        crosswalk(tmp_path, LOCUS | {("al", "townships", "autauga"): ["text"]})


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


def test_the_license_on_locus_s_card_is_checked():
    def api(license):
        card = {"license": license, "dataset_info": {"splits": [{"num_examples": 5}, {"num_examples": 7}]}}
        return SimpleNamespace(dataset_info=lambda repo_id, revision: SimpleNamespace(card_data=SimpleNamespace(to_dict=lambda: card)))

    assert locus.stated_rows(api(locus.LICENSE)) == 12
    with pytest.raises(SourceChanged, match="LOCUS's license is 'cc-by-4.0'"):
        locus.stated_rows(api("cc-by-4.0"))


def hub_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", f"https://huggingface.co/api/datasets/{locus.REPO_ID}/revision/{locus.REVISION}"))
    return HfHubHTTPError(f"{status} from the fake Hub", response=response)


class BusyLocus:
    """LOCUS's repo on a Hub that answers each read once with a planted failure, then as it should."""

    def __init__(self):
        self.calls = []
        self.planted = {"dataset_info": hub_error(429), "list_repo_files": httpx.ConnectError("planted"), "hf_hub_download": hub_error(503)}

    def answer(self, name):
        self.calls.append(name)
        if name in self.planted:
            raise self.planted.pop(name)

    def dataset_info(self, repo_id, revision=None):
        self.answer("dataset_info")
        card = {"license": locus.LICENSE, "dataset_info": {"splits": [{"num_examples": 5}, {"num_examples": 7}]}}
        return SimpleNamespace(sha=revision or "head", last_modified=None, card_data=SimpleNamespace(to_dict=lambda: card))

    def list_repo_files(self, repo_id, repo_type, revision):
        self.answer("list_repo_files")
        return ["README.md", "data/a.parquet"]

    def hf_hub_download(self, repo_id, path, repo_type, revision, local_dir):
        self.answer("hf_hub_download")
        return f"{local_dir}/{path}"


def test_busy_reads_of_locus_are_retried(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(store_module.time, "sleep", waits.append)
    api = BusyLocus()
    assert locus.download(str(tmp_path), api=api) == ([f"{tmp_path}/data/a.parquet"], 12)
    assert api.calls == ["dataset_info"] * 2 + ["list_repo_files"] * 2 + ["hf_hub_download"] * 2 and waits == [60, 60, 60]
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token: BusyLocus())
    assert locus.latest()["sha"] == "head" and waits == [60] * 4, "the stale-pin check's read is retried too"


def test_a_refused_read_of_locus_is_raised_at_once(monkeypatch):
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: pytest.fail("a refusal is not retried"))
    api = BusyLocus()
    api.planted["dataset_info"] = hub_error(404)
    with pytest.raises(HfHubHTTPError):
        locus.stated_rows(api)
    assert api.calls == ["dataset_info"]
