"""A crosswalk from LOCUS v1.1 to the governments in the Census file, by GEOID and then by name. It records which jurisdictions LOCUS covers, how many sections each has, and how its text names each government. It holds none of the text, which LOCUS licenses CC BY-NC 4.0."""

import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from .census import SourceChanged
from .store import hub_read

REPO_ID = "LocalLaws/LOCUS-v1"
# LOCUS v1.1, the head on November 16, 2026, reviewed November 22, 2026.
REVISION = "99354c417ed5cda88bf3f4bf36a3336fd24f101f"
LICENSE = "cc-by-nc-4.0"
COLUMNS = ["document_id", "state", "jurisdiction_type", "jurisdiction_name", "jurisdiction_geoid"]
# Measured November 22, 2026 by comparing the last v1.0 build's crosswalk with the first v1.1 build.
PREVIOUS = {"revision": "4cee954ca8ad8e31cb0502dff6682c87b74b4302", "until": "2026-11-22", "jurisdictions": 2287, "matched": 2286,
            "still_matched": 1812, "no_longer_matched": 474, "newly_matched": 548, "dataset_commit": "9f306bb249e7ca29fcf36f332b49e34e1081b527"}

# Census name titles, longest first so that CITY AND COUNTY OF wins over CITY OF.
PREFIXES = ("CITY AND COUNTY OF", "CITY AND BOROUGH OF", "CONSOLIDATED GOVERNMENT OF", "METROPOLITAN GOVERNMENT OF", "METRO GOVERNMENT OF", "UNIFIED GOVERNMENT OF",
            "URBAN COUNTY GOVERNMENT OF", "METRO TOWNSHIP OF", "CHARTER TOWNSHIP OF", "CIVIL TOWNSHIP OF", "CITY-PARISH OF", "CITY OF", "TOWN OF", "VILLAGE OF",
            "BOROUGH OF", "TOWNSHIP OF", "PLANTATION OF", "COUNTY OF", "PARISH OF", "MUNICIPALITY OF", "CORPORATION OF")
# Titles of municipal governments that are also their county: the Census counts these as municipal, with no county government beside them.
CONSOLIDATED = {"CITY AND COUNTY OF", "CITY AND BOROUGH OF", "CONSOLIDATED GOVERNMENT OF", "METROPOLITAN GOVERNMENT OF", "METRO GOVERNMENT OF", "UNIFIED GOVERNMENT OF",
                "URBAN COUNTY GOVERNMENT OF", "CITY-PARISH OF", "COUNTY OF", "MUNICIPALITY OF"}
# The Census titles each type word in a LOCUS name stands for.
KINDS = {
    "city": {"CITY OF", "CITY AND COUNTY OF", "CITY AND BOROUGH OF", "CITY-PARISH OF"},
    "town": {"TOWN OF"},
    "village": {"VILLAGE OF"},
    "borough": {"BOROUGH OF", "CITY AND BOROUGH OF"},
    "township": {"TOWNSHIP OF", "CHARTER TOWNSHIP OF", "CIVIL TOWNSHIP OF", "METRO TOWNSHIP OF"},
    "plantation": {"PLANTATION OF"},
}
MUNICIPAL_KINDS = ("city", "town", "village", "borough")
CITY_SUFFIXES = (("chrtr_township", "township"), ("charter_township", "township"), ("chartertownship", "township"), ("township", "township"), ("twp", "township"),
                 ("town", "town"), ("city", "city"), ("village", "village"), ("borough", "borough"), ("plantation", "plantation"))
# "town" is left out: too many names end in it (Georgetown).
GLUED_SUFFIXES = (("township", "township"), ("twp", "township"), ("city", "city"), ("borough", "borough"), ("bor", "borough"))
COUNTY_PREFIXES = ("unified_government_of_", "city_and_county_of_", "city_and_borough_of_", "city_borough_of_", "borough_of_", "county_of_", "parish_of_", "municipality_of_")
COUNTY_SUFFIXES = ("_parish_police_jury", "_parish_council", "_parish_government", "_county_commission", "_county_consolidated_government", "_county_-_unified_government",
                   "_county_unified_government", "_consolidated_government", "_county_government", "_county", "_parish", "_borough", "_census_area", "_municipality")
# Both sides are reduced to st, mt and ft.
SHORT = (("saint", "st"), ("mount", "mt"), ("fort", "ft"))
TRAILING_TYPES = ("CITY", "TOWN", "VILLAGE", "BOROUGH", "TOWNSHIP")
# A county after a comma qualifies the name.
COUNTY_HINT = re.compile(r",_?\(?([a-z_.' -]+?)_(?:co\.?|county)\)?$")
GENERAL_PURPOSE = ("county", "municipal", "township")

# The type words looked for in the text, and the ones each Census title accepts.
TEXT_TYPES = ("city", "town", "village", "borough", "township", "plantation", "county", "parish", "municipality")
TITLE_WORDS = {title: {kind for kind, titles in KINDS.items() if title in titles} for title in PREFIXES}
TITLE_WORDS.update({"COUNTY OF": {"county"}, "PARISH OF": {"parish"}, "MUNICIPALITY OF": {"municipality", "borough"}, "CORPORATION OF": {"city", "town"}})
CONSOLIDATED_WORDS = {"city", "county", "parish", "borough", "municipality", "town"}
# Text evidence picks one of several same-name governments only when it is lopsided: at least MIN_MENTIONS mentions, and at least MIN_SHARE of those that fit a candidate.
MIN_MENTIONS = 3
MIN_SHARE = 2 / 3
# What each match value means, in the order the card lists them.
MATCH_DOCS = {
    "geoid": "The Census GEOID fits one government: state and county FIPS for a county (5 digits), state and place for a municipality (7), or state, county and subdivision for a township (10)",
    "name": "The name alone fits one government in the state",
    "name_county": "The name and the county written after it fit one government",
    "name_type": "The name without its type word fits one government with that title",
    "name_type_county": "The name, its type word and the county after it fit one government",
    "name_type_differs": "No government of the name has the title LOCUS's type word gives, and exactly one municipality has the name under another title (Barnstable Town: CITY OF BARNSTABLE)",
    "name_variant": "Fits one government only once a second title or a trailing type word is dropped from the Census name (Creede: TOWN OF CITY OF CREEDE)",
    "consolidated": "A county or city-county name fits one consolidated government, which the Census counts as municipal (Honolulu: CITY AND COUNTY OF HONOLULU)",
    "text_type": "Several governments share the name, and the type word the text writes with it fits exactly one of them. The word needs at least 3 mentions and two-thirds of the mentions that fit any candidate",
    "text_county": "Several same-name governments lie in different counties, and the text names one of those counties in at least 3 mentions and two-thirds of all mentions of them",
    "municipal_preferred": "The name fits one municipality and one or more townships, the text does not settle which, and the municipality is taken: the least certain match",
    "ambiguous": "Several governments fit and neither the name nor the text settles which, so `census_id` is null and `candidates` lists them",
    "unmatched": "No government in the Census file fits",
}
MATCHES = tuple(MATCH_DOCS)
MATCHED = MATCHES[:-2]
# The matches that choose among several same-name governments, whose candidates are kept.
POOLED = ("text_type", "text_county", "municipal_preferred", "ambiguous")


def latest():
    """The Hub's current HEAD for LOCUS-v1, for the scheduled stale-pin check. The build still reads REVISION until a person reviews a newer release."""
    from huggingface_hub import HfApi

    info = hub_read(HfApi(token=False).dataset_info, REPO_ID)
    modified = getattr(info, "last_modified", None)
    return {"repo_id": REPO_ID, "sha": info.sha, "last_modified": modified.isoformat() if modified else None, "pinned": REVISION}


def stated_rows(api=None):
    """The row count LOCUS's card states at REVISION. Raises SourceChanged if the card's license is not the one the dataset card was written for."""
    from huggingface_hub import HfApi

    api = api or HfApi(token=False)
    card = hub_read(api.dataset_info, REPO_ID, revision=REVISION).card_data.to_dict()
    if card.get("license") != LICENSE:
        raise SourceChanged(f"LOCUS's license is {card.get('license')!r}, not {LICENSE}: the card's License section needs review")
    path = hub_read(api.hf_hub_download, REPO_ID, "README.md", repo_type="dataset", revision=REVISION)
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.startswith("| `sections` |")]
    count = re.fullmatch(r"\| `sections` \| ([0-9]{1,3}(?:,[0-9]{3})*) \| [^|]+ \|", lines[0]) if len(lines) == 1 else None
    if count is None:
        raise SourceChanged("expected one sections row with a count in LOCUS's README")
    return int(count[1].replace(",", ""))


def download(directory, api=None):
    """Download LOCUS's parquet files at REVISION into directory. Returns their paths and the row count LOCUS's card states."""
    from huggingface_hub import HfApi

    api = api or HfApi(token=False)
    stated = stated_rows(api)
    files = sorted(path for path in hub_read(api.list_repo_files, REPO_ID, repo_type="dataset", revision=REVISION) if path.startswith("sections/") and path.endswith(".parquet"))
    if not files:
        raise SourceChanged(f"no sections parquet files in {REPO_ID} at {REVISION}")
    paths = [hub_read(api.hf_hub_download, REPO_ID, path, repo_type="dataset", revision=REVISION, local_dir=directory) for path in files]
    return paths, stated


def jurisdictions(paths):
    """Count sections by document and check the document-to-jurisdiction mapping across files."""
    counts = Counter()
    documents, names = {}, {}
    for path in paths:
        table = pq.read_table(path, columns=COLUMNS)
        for row in table.group_by(COLUMNS).aggregate([([], "count_all")]).to_pylist():
            doc, state, kind, name, geoid = (row[column] for column in COLUMNS)
            if not all((doc, state, kind, name)):
                raise SourceChanged(f"LOCUS row without a document or jurisdiction: {doc!r} {state!r} {kind!r} {name!r}")
            if kind not in ("city", "county", "city-county", "other"):
                raise SourceChanged(f"unknown LOCUS jurisdiction type {kind!r}")
            jurisdiction = state, kind, name, geoid
            if documents.setdefault(doc, jurisdiction) != jurisdiction:
                raise SourceChanged(f"LOCUS document {doc} maps to multiple jurisdictions")
            if names.setdefault((state, kind, name), doc) != doc:
                raise SourceChanged(f"LOCUS jurisdiction {state}/{kind}/{name} maps to multiple documents")
            counts[doc] += row["count_all"]
    return [{"locus_document_id": doc, "locus_state": state, "locus_jurisdiction_type": kind, "locus_name": name, "locus_geoid": geoid, "locus_rows": counts[doc]}
            for doc, (state, kind, name, geoid) in sorted(documents.items())]


def key(text):
    words = re.findall(r"[a-z0-9]+", text.lower().replace("&", " and ").replace("'", "").replace("\u2019", ""))
    joined = "".join(words)
    for long, short in SHORT:
        joined = joined.replace(long, short)
    return joined


def split_title(name):
    """COUNTY OF ST TAMMANY -> ("COUNTY OF", "ST TAMMANY"); parenthetical notes such as (BRISCOE) are dropped."""
    upper = re.sub(r"\s*\([^)]*\)", "", " ".join(name.upper().split()))
    for prefix in PREFIXES:
        if upper.startswith(prefix + " "):
            return prefix, upper[len(prefix) + 1:]
    return "", upper


def bare_words(rest):
    """The name without a second title or a trailing type word, for irregular Census names: CITY OF CREEDE -> [CREEDE], MT JULIET CITY -> [MT, JULIET]."""
    for prefix in PREFIXES:
        if rest.startswith(prefix + " "):
            rest = rest[len(prefix) + 1:]
            break
    words = rest.split()
    if len(words) > 1 and words[-1] in TRAILING_TYPES:
        words = words[:-1]
    return words


def title_and_core(name):
    """COUNTY OF ST TAMMANY -> ("COUNTY OF", "sttammany", []). The list holds variant readings of irregular names, used only when nothing else fits:
    TOWN OF CITY OF CREEDE -> creede, CITY OF MT JULIET CITY -> mtjuliet."""
    title, rest = split_title(name)
    core = key(rest)
    bare = key(" ".join(bare_words(rest)))
    return title, core, [bare] if bare != core else []


def title_words(entry):
    """The type words that fit a government's Census title, such as {"town"} for TOWN OF BARRE."""
    if entry["type"] == "municipal" and entry["title"] in CONSOLIDATED:
        return CONSOLIDATED_WORDS
    return TITLE_WORDS.get(entry["title"], set())


class Index:
    """General-purpose governments by GEOID and (state, core name); counties and consolidated governments also by county area name."""

    def __init__(self, governments):
        self.by_geoid = defaultdict(list)
        self.by_core = defaultdict(list)
        self.by_variant = defaultdict(list)
        self.by_county_name = defaultdict(list)
        self.entries = {}
        for row in governments:
            if row["government_type"] not in GENERAL_PURPOSE:
                continue
            kind = row["government_type"]
            codes = [row["state_fips"]]
            if kind in ("county", "township"):
                codes.append(row["county_fips"])
            if kind in ("municipal", "township"):
                codes.append(row["fips_place"])
            if None not in codes:
                self.by_geoid["".join(codes)].append(row["census_id"])
            title, core, variants = title_and_core(row["name"])
            entry = {"census_id": row["census_id"], "type": row["government_type"], "title": title, "county": key(row["county_name"] or ""),
                     "name": row["name"], "county_name": row["county_name"]}
            self.entries[row["census_id"]] = entry
            state = row["state"].lower()
            self.by_core[(state, core)].append(entry)
            for variant in variants:
                self.by_variant[(state, variant)].append(entry)
            if row["government_type"] == "county" or title in CONSOLIDATED:
                self.by_county_name[(state, entry["county"])].append(entry)


def city_readings(name):
    """The ways to read a LOCUS city name, most literal first, as (core, type word or None), and the county it names, if any."""
    name = name.strip("_")
    hint = None
    found = COUNTY_HINT.search(name)
    if found:
        hint, name = key(found.group(1)), name[:found.start()]
    readings = [(key(name), None)]
    for suffix, kind in CITY_SUFFIXES:
        if name.endswith("_" + suffix) and len(name) > len(suffix) + 1:
            readings.append((key(name[:-len(suffix) - 1]), kind))
            break
    whole = key(name)
    for suffix, kind in GLUED_SUFFIXES:
        if whole.endswith(suffix) and len(whole) > len(suffix):
            readings.append((whole[:-len(suffix)], kind))
            break
    return readings, hint


def match_city(index, state, name):
    """(census_id or None, match, candidates). Candidates are the same-name governments a choice was made among, or that remain ambiguous."""
    readings, hint = city_readings(name)
    ambiguous = []
    for table, variant in ((index.by_core, False), (index.by_variant, True)):
        for core, kind in readings:
            found = [entry for entry in table.get((state, core), []) if entry["type"] in ("municipal", "township")]
            if hint:
                found = [entry for entry in found if entry["county"] == hint]
            pool = [entry for entry in found if entry["title"] in KINDS[kind]] if kind else found
            if len(pool) == 1:
                method = "name_variant" if variant else "name" + ("_type" if kind else "") + ("_county" if hint else "")
                return pool[0]["census_id"], method, []
            municipal = [entry for entry in pool if entry["type"] == "municipal"]
            if len(pool) > 1 and kind is None and len(municipal) == 1:
                return municipal[0]["census_id"], "municipal_preferred", sorted(entry["census_id"] for entry in pool)
            if not pool and kind in MUNICIPAL_KINDS:
                municipal = [entry for entry in found if entry["type"] == "municipal"]
                if len(municipal) == 1:
                    return municipal[0]["census_id"], "name_variant" if variant else "name_type_differs", []
            if len(pool) > 1 and not ambiguous:
                ambiguous = sorted(entry["census_id"] for entry in pool)
    return (None, "ambiguous", ambiguous) if ambiguous else (None, "unmatched", [])


def county_readings(name):
    stripped = name.strip("_")
    readings = [key(stripped)]
    for prefix in COUNTY_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    for suffix in COUNTY_SUFFIXES:
        if stripped.endswith(suffix):
            stripped = stripped[:-len(suffix)]
            break
    for reading in (key(stripped), key(stripped).removesuffix("county")):
        if reading and reading not in readings:
            readings.append(reading)
    return readings


def match_county(index, state, name):
    ambiguous = []
    for core in county_readings(name):
        pool = {}
        for entry in index.by_core.get((state, core), []) + index.by_county_name.get((state, core), []):
            if entry["type"] == "county" or (entry["type"] == "municipal" and entry["title"] in CONSOLIDATED):
                pool[entry["census_id"]] = entry
        if len(pool) == 1:
            entry = next(iter(pool.values()))
            return entry["census_id"], "name" if entry["type"] == "county" else "consolidated", []
        if len(pool) > 1 and not ambiguous:
            ambiguous = sorted(pool)
    return (None, "ambiguous", ambiguous) if ambiguous else (None, "unmatched", [])


def match_name(index, state, kind, name):
    name = "_".join(unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().split())
    if kind in ("city", "other"):
        return match_city(index, state, name)
    result = match_county(index, state, name)
    if result[1] == "unmatched":
        fallback = match_city(index, state, name)
        if kind == "city-county" or fallback[1] in ("name_type", "name_type_county"):
            return fallback
    return result


def match_names(index, jurisdictions):
    """Match GEOIDs and cross-check by name; keep the name result where no government has the GEOID."""
    rows = []
    for jurisdiction in jurisdictions:
        state, name, geoid = jurisdiction["locus_state"], jurisdiction["locus_name"], jurisdiction["locus_geoid"]
        if geoid is not None and len(geoid) not in (5, 7, 10):
            raise SourceChanged(f"unexpected LOCUS GEOID length: {state}/{name} {geoid!r}")
        ids = index.by_geoid.get(geoid, [])
        if len(ids) > 1:
            raise SourceChanged(f"LOCUS GEOID {geoid} fits multiple governments: {ids}")
        census_id, match, candidates = match_name(index, state.lower(), jurisdiction["locus_jurisdiction_type"], name)
        name_match = None
        if ids:
            uid = ids[0]
            if match in POOLED and uid not in candidates:
                raise SourceChanged(f"LOCUS's GEOID government is not among its name candidates: {state}/{name} {uid} not in {candidates}")
            if match not in POOLED and census_id is not None and census_id != uid:
                raise SourceChanged(f"LOCUS's GEOID and its name name different governments: {state}/{name} {uid} and {census_id}")
            name_match = match
            census_id, match, candidates = uid, "geoid", []
        rows.append(dict(jurisdiction, census_id=census_id, match=match, name_match=name_match, candidates=candidates))
    return rows


SAINT, SAINTE, MOUNT, FORT = r"(?:st\.?|saint)", r"(?:ste\.?|sainte)", r"(?:mt\.?|mount)", r"(?:ft\.?|fort)"
WORD_FORMS = {"ST": SAINT, "SAINT": SAINT, "STE": SAINTE, "SAINTE": SAINTE, "MT": MOUNT, "MOUNT": MOUNT, "FT": FORT, "FORT": FORT}
TYPES_PATTERN = "(" + "|".join(TEXT_TYPES) + ")"


# The Census writes names in plain capitals; prose adds accents, sometimes as combining marks, and possessive apostrophes.
ACCENTS = {"A": "aáàâäã", "C": "cç", "E": "eéèêë", "I": "iíìîï", "N": "nñ", "O": "oóòôöõ", "U": "uúùûü"}
COMBINING = "[\u0300-\u036f]*"


def piece_pattern(piece):
    if piece in WORD_FORMS:
        return WORD_FORMS[piece]
    pattern = "".join(f"[{ACCENTS[char]}]{COMBINING}" if char in ACCENTS else re.escape(char) for char in piece)
    if len(piece) > 2 and piece.endswith("S"):
        pattern = pattern[:-1] + "['\u2019]?S"
    return pattern


def words_pattern(words):
    """A case-insensitive pattern for a Census name as prose writes it: ST CHARLES matches St. Charles and Saint Charles, COEUR D ALENE matches Coeur d'Alene,
    EL DORADO matches ElDorado, SAN JOSE matches San José and ST MARYS matches St. Mary's."""
    return r"[\s\-'\u2019.]*".join(r"[\s\-]*".join(piece_pattern(piece) for piece in word.split("-")) for word in words)


class Target:
    """What to look for in one jurisdiction's text: its governments' shared name with a type word before or after it, and, when they sit in different counties, those counties' names."""

    def __init__(self, entries):
        # Same-name governments can spell the name differently (TOWNSHIP OF ELDORADO, CITY OF EL DORADO), so every spelling is looked for.
        spellings = sorted({words_pattern(bare_words(split_title(entry["name"])[1])) for entry in entries}, key=lambda pattern: (-len(pattern), pattern))
        name = "(?:" + "|".join(spellings) + ")"
        self.names = re.compile(rf"\b(?:{TYPES_PATTERN}\s+of\s+(?:the\s+)?{name}\b|{name}\s+{TYPES_PATTERN}\b)", re.I)
        counties = sorted({entry["county_name"] for entry in entries if entry["county_name"]})
        self.counties = None
        if len(counties) > 1:
            either = "(" + "|".join(words_pattern(county.split()) for county in counties) + ")"
            self.counties = re.compile(rf"\b(?:(?:county|parish)\s+of\s+{either}|{either}\s+(?:county|parish))\b", re.I)
        self.words = Counter()
        self.county_mentions = Counter()

    def read(self, content):
        for found in self.names.finditer(content):
            self.words[(found.group(1) or found.group(2)).lower()] += 1
        if self.counties is not None:
            for found in self.counties.finditer(content):
                self.county_mentions[key(found.group(1) or found.group(2))] += 1


def read_text(paths, targets):
    """One pass over LOCUS's text, feeding each row to its jurisdiction's Target."""
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=20_000, columns=["document_id", "content"]):
            data = batch.to_pydict()
            for doc, content in zip(data["document_id"], data["content"]):
                target = targets.get(doc)
                if target is not None and content:
                    target.read(content)


def lopsided(counts):
    """The key holding at least MIN_MENTIONS and at least MIN_SHARE of all counts, if one does."""
    total = sum(counts.values())
    if not total:
        return None
    top, n = counts.most_common(1)[0]
    return top if n >= MIN_MENTIONS and n >= MIN_SHARE * total else None


def choose(pool, target):
    """One government from several same-name ones, by the type word the text uses for the name and then by the county it names; (None, None) when the text does not settle it.
    A text that names the government only with words no candidate's title fits (a city where the Census lists only townships) settles nothing."""
    fitting = Counter({word: n for word, n in target.words.items() if any(word in title_words(entry) for entry in pool)})
    if target.words and not fitting:
        return None, None
    word = lopsided(fitting)
    if word:
        pool = [entry for entry in pool if word in title_words(entry)]
        if len(pool) == 1:
            return pool[0], "text_type"
    counties = {entry["county"] for entry in pool}
    if len(counties) > 1:
        county = lopsided(Counter({name: n for name, n in target.county_mentions.items() if name in counties}))
        chosen = [entry for entry in pool if entry["county"] == county]
        if len(chosen) == 1:
            return chosen[0], "text_county"
    return None, None


def crosswalk(paths, governments):
    """One row per LOCUS jurisdiction: the government it names when exactly one fits, and how its text names that government."""
    index = Index(governments)
    rows = match_names(index, jurisdictions(paths))
    targets = {}
    for row in rows:
        ids = [row["census_id"]] if row["candidates"] == [] and row["census_id"] else row["candidates"]
        if ids:
            targets[row["locus_document_id"]] = Target([index.entries[census_id] for census_id in ids])
    read_text(paths, targets)
    for row in rows:
        target = targets.get(row["locus_document_id"])
        if target is None:
            row.update(text_type=None, text_type_mentions=None, text_mentions=None, text_fits=None)
            continue
        if row["match"] in ("municipal_preferred", "ambiguous"):
            chosen, how = choose([index.entries[census_id] for census_id in row["candidates"]], target)
            if chosen is not None:
                row.update(census_id=chosen["census_id"], match=how)
        top = target.words.most_common(1)
        text_type, text_type_mentions = top[0] if top else (None, 0)
        mentions = sum(target.words.values())
        fits = None
        if row["census_id"] and mentions >= MIN_MENTIONS and title_words(index.entries[row["census_id"]]):
            fits = text_type in title_words(index.entries[row["census_id"]])
        row.update(text_type=text_type, text_type_mentions=text_type_mentions, text_mentions=mentions, text_fits=fits)
    duplicates = sorted(census_id for census_id, n in Counter(row["census_id"] for row in rows if row["census_id"]).items() if n > 1)
    if duplicates:
        raise SourceChanged(f"LOCUS jurisdictions matched to the same government, which the rules were never checked against: {duplicates[:10]}")
    return rows
