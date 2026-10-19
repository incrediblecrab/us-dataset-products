# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and GitHub Copilot CLI when working in this repository.

This repository builds and keeps current the public Hugging Face dataset `incrediblecrab/us-federal-reserve-beige-book`: every Federal Reserve Beige Book edition from May 1970, one row for the national summary and one per district, with text. The repository is public. `README.md`, `fed_products/README.md` and `tests/README.md` describe each part, and the dataset card states which sections no source serves.

## Commands

```bash
.venv/bin/python -m pytest -q                         # offline suite, what test.yml runs on push
.venv/bin/python -m pytest -q tests/test_board.py -k <name>
.venv/bin/python -m fed_products {probe,run,card,verify,squash} --help
```

`.venv` links to the shared `~/.venvs/main`. No API key is needed. Use `--local DIR` for any local build; without it `run` and `squash` write to the published dataset, so do not run them that way unless the owner asks.

## How it stays current

`.github/workflows/pipeline.yml` runs at 12:37 UTC, which GitHub starts about four hours late (`.github/workflows/README.md` has the measurement), and publishes through Hugging Face Trusted Publishing with no stored token. GitHub disables schedules after 60 days without a commit; the workflow's `inactivity` job fails at 50 days to warn first and deliberately makes no keepalive commit, which GitHub treats as a Terms violation, so do not add one.

## Structure

Editions from October 30, 1996 come from the Board's Beige Book and FOMC historical pages (`fed_products/board.py`, which handles the Board's four page layouts and the one page known to hold another edition's report); earlier editions come from the Minneapolis Fed archive (`minneapolis.py`). `beige_book.py` defines the schema, the rows and the units a fetch covers, `sections.py` the sections and their order, and `text.py` renders HTML to text as a browser lays it out. `pipeline.py` is the sync loop with the single-writer lease and the probe's decision; `store.py` writes one Parquet partition per year and guards Hub commits with a parent-commit fence. `http.py` owns pacing, retries and bot-challenge detection; route every request through it. `card.py` renders the card from the manifest and `verify.py` is the publication check.
