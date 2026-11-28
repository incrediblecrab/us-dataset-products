# us-dataset-products

This public repository holds the pipelines that keep the owner's US government datasets on Hugging Face current, one folder per source. It was assembled on October 19, 2026 by merging `incrediblecrab/us-crs-service-products`, `incrediblecrab/us-fed-research-service-products`, `incrediblecrab/us-local-laws` and `incrediblecrab/us-scotus-research-service-products`, with their histories; those repositories were deleted the same day.

| Folder | Source | Hugging Face datasets | Workflows |
|---|---|---|---|
| [`crs/`](crs/README.md) | Congressional Research Service, via Congress.gov and GovInfo | [us-crs-research-papers](https://huggingface.co/datasets/incrediblecrab/us-crs-research-papers), [us-crs-bill-summaries](https://huggingface.co/datasets/incrediblecrab/us-crs-bill-summaries), [us-crs-constitution](https://huggingface.co/datasets/incrediblecrab/us-crs-constitution) | `.github/workflows/crs-*.yml` |
| [`fed/`](fed/README.md) | Federal Reserve Board and Federal Reserve Bank of Minneapolis | [us-federal-reserve-beige-book](https://huggingface.co/datasets/incrediblecrab/us-federal-reserve-beige-book) | `.github/workflows/fed-*.yml` |
| [`local-laws/`](local-laws/README.md) | Census Bureau, LOCUS-v1, New York, Bureau of Indian Affairs and FEMA | [incrediblecrab/us-local-laws](https://huggingface.co/datasets/incrediblecrab/us-local-laws) | `.github/workflows/local-laws-*.yml` |
| [`scotus/`](scotus/README.md) | Supreme Court of the United States | [us-scotus-opinions](https://huggingface.co/datasets/incrediblecrab/us-scotus-opinions), [us-scotus-oral-arguments](https://huggingface.co/datasets/incrediblecrab/us-scotus-oral-arguments), [us-scotus-case-documents](https://huggingface.co/datasets/incrediblecrab/us-scotus-case-documents), [us-scotus-news-media](https://huggingface.co/datasets/incrediblecrab/us-scotus-news-media), [us-scotus-filing-and-rules](https://huggingface.co/datasets/incrediblecrab/us-scotus-filing-and-rules), [us-scotus-about](https://huggingface.co/datasets/incrediblecrab/us-scotus-about), [us-scotus-cases](https://huggingface.co/datasets/incrediblecrab/us-scotus-cases) | `.github/workflows/scotus-*.yml` |

The Federal Reserve work begins with the Beige Book. The local-law map is intended as a public, reproducible base for helping people understand the laws that govern them. Each folder's README gives its objectives, inputs, terms and local commands; run those commands from that folder. Each folder's `WORKFLOWS.md` describes its workflows, which live only in the root [`.github/workflows/`](.github/workflows/).

CRS uses Congress.gov API v3 and GovInfo with an api.data.gov key held locally in the macOS keychain as `DATA_GOV_API_KEY`, read with `keychain-env`. The other pipelines need no source API key.

The Supreme Court pipeline was first published on GitHub on September 26, 2026, and its six document datasets on September 27, 2026. The local build that seeded `us-scotus-opinions`, `us-scotus-case-documents` and `us-scotus-oral-arguments` was deleted on September 27, 2026 after every one of its rows was found on the Hub. The Hub holds the data.

## Health check

At 03:07, 09:07, 15:07 and 21:07 UTC, [`health-check`](.github/workflows/health-check.yml) checks the six publishing workflows and twelve datasets. It alerts through GitHub's failure email if the latest completed run failed or timed out, or no completed scheduled run was created within 23 hours (one 12-hour slot plus 11 hours for observed delays).

Each dataset's newest recorded build commit must be in `HEAD`'s history and match its pipeline folder, and each last complete listing must be recent. The limit is how often the dataset is rewritten when nothing changed, plus one 12-hour slot and 11 hours of delay: 35 hours for datasets rewritten every run, 47 hours for the CRS research papers and the Beige Book, whose probes reread every list once a day. `us-local-laws` is rewritten only when its data changes, so an older build is a warning and its freshness is its workflow runs. A cancelled run or a dataset without a recorded commit gives a warning; stale data, old code past its limit, or an unreadable API or manifest fails the check. Thresholds are in `health/health.py`.

## Publishing

Each dataset must trust repository `incrediblecrab/us-dataset-products`, branch `main` and its publishing workflow file under Hugging Face **Settings > Trusted Publishers**. Register `crs-pipeline.yml` for CRS research papers, `crs-summaries.yml` for bill summaries, `crs-constitution.yml` for the Constitution Annotated, `fed-pipeline.yml` for the Beige Book, `local-laws-pipeline.yml` for local laws and `scotus-pipeline.yml` for all seven Supreme Court datasets. These files live in the root `.github/workflows/`; a rename requires updating the registration.

GitHub Actions exchanges each job's OIDC token for a short-lived dataset-scoped write token, so no Hugging Face token is stored for scheduled publishing. The only GitHub Actions secret is `DATA_GOV_API_KEY`, used by `crs/`. The Hugging Face dataset names are unchanged by the repository merger.
