"""Shared fixtures: the Census sample (real rows), a synthetic LOCUS (invented text; LOCUS's own is CC BY-NC), a sample of New York's local-law filings (real metadata) behind a fake of its API, a sample of New York's older index (real records, CC0), samples of the Bureau of Indian Affairs' two notices (real entries), samples of FEMA's Community Status Book and of OpenFEMA's copy of it (real records), a fake fetcher and a build published to a local store."""

import copy
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from local_laws import census, nfip, nyindex, nylaws, tribes
from local_laws.build import build
from local_laws.store import LocalStore

FIXTURES = Path(__file__).parent / "fixtures"
UNITS = (FIXTURES / "govt_units_sample.zip").read_bytes()
ORG02 = (FIXTURES / "org02_sample.zip").read_bytes()
NY_SAMPLE = json.loads((FIXTURES / "ny_snapshot_sample.json").read_text())
NY_INDEX = (FIXTURES / "ny_index_sample.zip").read_bytes()
TRIBES_NOTICE = (FIXTURES / "tribes_notice_sample.xml").read_bytes()
TRIBES_PREVIOUS = (FIXTURES / "tribes_previous_sample.xml").read_bytes()
NFIP_CSV = (FIXTURES / "nfip_nation_sample.csv").read_bytes()
NFIP_API = (FIXTURES / "nfip_api_sample.parquet").read_bytes()
BUILT_AT = "2026-09-25T00:00:00Z"
CODE = {"version": "test", "commit": "0" * 40, "dirty": False}


def three(text):
    return [text] * 3


# (document_id, state, type, name, GEOID) -> synthetic section text.
LOCUS = {
    ("doc_0001", "AL", "county", "Autauga County", "01001"): three("The governing body of Autauga County adopts this chapter."),
    ("doc_0002", "AL", "city", "Prattville", "0162328"): three("It is unlawful within the City of Prattville to park on a sidewalk.") + [None],
    ("doc_0003", "VT", "city", "Barre", "5002303250"): three("The Selectboard of the Town of Barre may appoint a constable."),
    ("doc_0004", "NY", "city", "Batavia City", None): three("The City of Batavia shall keep a register of dogs."),
    ("doc_0005", "IL", "city", "Belvidere", None): three("Belvidere City Council meetings are held monthly.") + ["Board of Belvidere Township."],
    ("doc_0006", "NJ", "city", "Franklin Township", None): three("The Township of Franklin, in Gloucester County, requires a permit."),
    ("doc_0007", "KS", "city", "Howard", None): ["The City of Howard levies a tax."],
    ("doc_0008", "SD", "city", "Bison", None): ["No person shall burn refuse."],
    ("doc_0009", "WI", "city", "Harrison, Calumet Co", None): three("The Village of Harrison board shall meet."),
    ("doc_0010", "MI", "county", "Bedford Township, (Monroe Co.)", None): three("Bedford Township zoning ordinance."),
    ("doc_0011", "CO", "city", "Creede", None): three("The City of Creede, a statutory town."),
    ("doc_0012", "MA", "city", "Barnstable Town", None): three("The Town of Barnstable shall license moorings."),
    ("doc_0013", "GA", "city-county", "Macon-Bibb County", "13021"): three("Macon-Bibb County code of ordinances."),
    ("doc_0014", "AK", "city", "Anchorage", None): three("The Municipality of Anchorage assembly."),
    ("doc_0015", "LA", "county", "Saint Tammany Parish", None): three("St. Tammany Parish council."),
    ("doc_0016", "OH", "city", "Nowhere Village", None): three("The Village of Nowhere."),
}
# (census_id, match, name_match, text_fits) for each document.
EXPECTED = {
    "doc_0001": ("100001", "geoid", "name", True),
    "doc_0002": ("100019", "geoid", "name", True),
    "doc_0003": ("135964", "geoid", "municipal_preferred", True),
    "doc_0004": ("109507", "name_type", None, True),
    "doc_0005": ("162180", "text_type", None, True),
    "doc_0006": ("170293", "text_county", None, True),
    "doc_0007": (None, "ambiguous", None, None),
    "doc_0008": ("187490", "municipal_preferred", None, None),
    "doc_0009": ("240470", "name_county", None, True),
    "doc_0010": ("201942", "name_type_county", None, True),
    "doc_0011": ("123750", "name_variant", None, False),
    "doc_0012": ("185811", "name_type_differs", None, False),
    "doc_0013": ("102163", "consolidated", None, True),
    "doc_0014": ("194463", "name", None, True),
    "doc_0015": ("166017", "name", None, True),
    "doc_0016": (None, "unmatched", None, None),
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_locus(directory, jurisdictions=None):
    """Two synthetic LOCUS sections files. Returns (paths, rows)."""
    rows = []
    for (doc, state, kind, name, geoid), texts in (LOCUS if jurisdictions is None else jurisdictions).items():
        for index, text in enumerate(texts):
            rows.append({"document_id": doc, "section_index": index, "state": state, "jurisdiction_name": name, "jurisdiction_type": kind,
                         "jurisdiction_geoid": geoid, "header": f"§ {index + 1}", "content": text})
    schema = pa.schema([("document_id", pa.string()), ("section_index", pa.int64())] + [(column, pa.string()) for column in ("state", "jurisdiction_name", "jurisdiction_type", "jurisdiction_geoid", "header", "content")])
    directory = Path(directory) / "sections"
    directory.mkdir(parents=True, exist_ok=True)
    half = len(rows) // 2
    paths = []
    for number, part in enumerate((rows[:half], rows[half:])):
        path = directory / f"train-{number:05d}-of-00002.parquet"
        pq.write_table(pa.Table.from_pylist(part, schema=schema), path)
        paths.append(str(path))
    return paths, len(rows)


def fake_locus_download(directory):
    return write_locus(directory)


def ny_snapshot():
    """A fresh copy of the sample snapshot, as nylaws.harvest returns one."""
    return copy.deepcopy(NY_SAMPLE)


def nfip_snapshot():
    """The NFIP samples as nfip.harvest returns them, read at fixed times."""
    return {"csv": NFIP_CSV, "retrieved_at": "2026-09-26T08:18:00Z", "api": NFIP_API, "api_retrieved_at": "2026-09-26T08:23:00Z"}


def rezipped(snapshot, **changes):
    """snapshot's files zipped again, deflated rather than stored as nfip.save stores them, with changes to its reading: the same files in different bytes when there are none."""
    buffer = io.BytesIO()
    read = {"csv_url": nfip.CSV_URL, "retrieved_at": snapshot["retrieved_at"], "api_url": nfip.API_URL, "api_retrieved_at": snapshot["api_retrieved_at"]} | changes
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nation.csv", snapshot["csv"])
        archive.writestr("NfipCommunityStatusBook.parquet", snapshot["api"])
        archive.writestr("read.json", json.dumps(read))
    return buffer.getvalue()


def api_item(kept):
    """An API search result carrying what nylaws.keep reads from one, and a signed download link it must leave out."""
    return {
        "id": kept["id"], "external_id": kept["external_id"], "filename": kept["filename"], "created_date": kept["created_date"], "last_update_date": kept["last_update_date"],
        "current_version": kept["current_version"], "deleted_date": kept["deleted_date"], "released_and_not_expired": kept["released_and_not_expired"],
        "file_properties": {"size_in_bytes": kept["size_in_bytes"], "format": "PDF"},
        "embeds": {"document_viewer": {"share": kept["share"], "url": "https://orders-bb.us-east-1.widencdn.net/x?Signature=s&Expires=1"}},
        "metadata": {"fields": copy.deepcopy(kept["fields"])},
        "_links": {"download": "https://orders-bb.us-east-1.widencdn.net/download?Signature=s&Expires=1&Key-Pair-Id=k"},
    }


class FakeNYApi:
    """The Department's search API over a list of kept items: its query syntax for the category and a dateFiled range, its sorts, and its limits.
    before_answer(api, params), if given, runs before each answer, so a test can change the filings while they are read. queries records each request's parameters."""

    RANGE = re.compile(r"cat:\(Local Laws\)(?: AND dateFiled:\[(\S+) TO (\S+)\])?")

    def __init__(self, items=None, before_answer=None):
        self.items = [copy.deepcopy(item) for item in (NY_SAMPLE["items"] if items is None else items)]
        self.before_answer = before_answer
        self.queries = []

    def answer(self, url):
        params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        self.queries.append(params)
        if self.before_answer:
            self.before_answer(self, params)
        found = self.RANGE.fullmatch(params["query"])
        if not found or params["search_document_text"] != "false":
            raise ValueError(f"the fake API does not answer {params}")
        start, end = found.groups()
        hits = [item for item in self.items if start is None or start <= item["fields"]["dateFiled"][0] <= end]
        order = params["sort"]
        field = order.lstrip("-")
        if field == "filename":
            hits.sort(key=lambda item: item["filename"], reverse=order.startswith("-"))
        elif field == "dateFiled":
            hits.sort(key=lambda item: item["fields"]["dateFiled"][0], reverse=order.startswith("-"))
        else:
            raise ValueError(f"the fake API does not sort by {order}")
        limit, offset = int(params["limit"]), int(params["offset"])
        if limit > 100 or offset + limit > nylaws.WINDOW:
            raise ValueError(f"HTTP 400: limit {limit} at offset {offset}")
        return {"total_count": len(hits), "items": [api_item(item) for item in hits[offset:offset + limit]]}


class FakeFetcher:
    """Answers the two Census URLs, the New York index's, the two notices' and FEMA's two with the fixtures and New York's API from a FakeNYApi; a value that is an exception is raised instead."""

    def __init__(self, responses=None, ny=None):
        self.responses = {census.GOVT_UNITS_URL: UNITS, census.ORG02_URL: ORG02, nyindex.URL: NY_INDEX, tribes.NOTICE["url"]: TRIBES_NOTICE, tribes.PREVIOUS["url"]: TRIBES_PREVIOUS,
                          nfip.CSV_URL: NFIP_CSV, nfip.API_URL: NFIP_API} | (responses or {})
        self.ny = ny or FakeNYApi()
        self.requests = 0

    def get(self, url):
        self.requests += 1
        if url.startswith(nylaws.API + "?"):
            return json.dumps(self.ny.answer(url)).encode()
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        pass


def units_rows():
    return census.parse_units(census.zip_member(UNITS, census.GOVT_UNITS_MEMBER))


def edited_units(edit):
    """The Census sample with edit(book) applied, zipped as the Census ships it."""
    book = openpyxl.load_workbook(io.BytesIO(census.zip_member(UNITS, census.GOVT_UNITS_MEMBER)))
    edit(book)
    buffer, out = io.BytesIO(), io.BytesIO()
    book.save(buffer)
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Govt_Units_2022_Final.xlsx", buffer.getvalue())
    return out.getvalue()


@pytest.fixture
def pins(monkeypatch):
    """Pin the fixtures' SHA-256 in place of the real files'."""
    monkeypatch.setattr(census, "GOVT_UNITS_SHA256", sha256(UNITS))
    monkeypatch.setattr(census, "ORG02_SHA256", sha256(ORG02))
    monkeypatch.setattr(nyindex, "SHA256", sha256(NY_INDEX))
    monkeypatch.setitem(tribes.NOTICE, "sha256", sha256(TRIBES_NOTICE))
    monkeypatch.setitem(tribes.PREVIOUS, "sha256", sha256(TRIBES_PREVIOUS))


@pytest.fixture
def published(tmp_path, pins):
    """A build of the fixtures committed to a local store: (store, manifest)."""
    manifest, files = build(FakeFetcher(), tmp_path / "work", locus_download=fake_locus_download, ny_snapshot=ny_snapshot(), nfip_snapshot=nfip_snapshot(), built_at=BUILT_AT, code=CODE)
    store = LocalStore(tmp_path / "hub")
    store.commit(files, "build")
    return store, manifest
