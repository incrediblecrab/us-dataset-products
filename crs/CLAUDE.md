# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and GitHub Copilot CLI when working in this repository.

This repository builds and keeps current three public Hugging Face datasets of Congressional Research Service work: `incrediblecrab/us-crs-research-papers` (every CRS product Congress.gov lists, with full text), `us-crs-bill-summaries` (every CRS bill summary from 1973) and `us-crs-constitution` (the Constitution Annotated editions on GovInfo). The repository is public. `README.md`, `crs_products/README.md`, `tests/README.md` and `.github/workflows/README.md` describe each part.

## Commands

```bash
.venv/bin/python -m pytest -q                          # offline suite, what test.yml runs on push
.venv/bin/python -m pytest -q tests/test_summaries.py -k <name>
.venv/bin/python -m crs_products {probe,run,card,verify,squash} --dataset {products,summaries,constitution} --help
```

`.venv` links to the shared `~/.venvs/main`. The Congress.gov and GovInfo APIs need `DATA_GOV_API_KEY` (from the keychain locally). Use `--local DIR` for any local build; without it `run` and `squash` write to the published datasets, so do not run them that way unless the owner asks.

## How it stays current

Three scheduled workflows run at 12:37 UTC, which GitHub starts about four hours late (`.github/workflows/README.md` has the measurement), and publish through Hugging Face Trusted Publishing with no stored token: `pipeline.yml` (products: a one-request `probe`, then `run`, `verify --live` and `squash`), `summaries.yml` and `constitution.yml`. A run that exhausts its time budget mid-fetch starts a `continue` run. GitHub disables schedules after 60 days without a commit, and the `inactivity` job fails at 50 days to warn first; it deliberately makes no keepalive commit, which GitHub treats as a Terms violation, so do not add one.

## Structure

`crs_products/pipeline.py` is the sync loop for products and the Constitution Annotated, including the single-writer lease and the probe's decision; `summaries.py` is the summaries sync. Source adapters are `source.py` (products API) and `constitution.py` (GovInfo). `store.py` holds the schema, partitions and the Hub store's parent-commit fence, so a concurrent writer's commit is refused rather than overwritten. `http.py` owns per-host pacing, bounded retries, bot-challenge detection and sending the API key only in a header; route every request through it. `text.py` turns HTML and PDF into text, each dataset's card is rendered from its manifest (`card.py`, `summaries_card.py`, `constitution_card.py`), and `verify.py` is the publication check. A test checks that the summaries schedule in `crs_products/summaries.py` agrees with `summaries.yml`.
