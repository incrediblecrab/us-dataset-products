# us-crs-service-products

This repository builds and updates three public Hugging Face datasets of work by the Congressional Research Service (CRS), the research service of the United States Congress. Each dataset card shows how much it holds.

- [us-crs-research-papers](https://huggingface.co/datasets/incrediblecrab/us-crs-research-papers): every CRS product the Congress.gov API lists, active and archived, with full text.
- [us-crs-bill-summaries](https://huggingface.co/datasets/incrediblecrab/us-crs-bill-summaries): every CRS summary of a bill or resolution of the US Congress that the API lists, from 1973 on.
- [us-crs-constitution](https://huggingface.co/datasets/incrediblecrab/us-crs-constitution): the printed editions and supplements of the Constitution Annotated, CRS's analysis of the US Constitution, on GovInfo, with text.

**Objective:** keep each dataset complete and current with no person, personal device or local copy. The workflows run on GitHub Actions once a day, scheduled at 12:37 UTC because GitHub starts them about four hours late, near 17:00 UTC (12:00 EST, 13:00 EDT), and write through Hugging Face Trusted Publishing, so no token is stored. GitHub disables schedules after 60 days without repository activity, such as a commit.

**Inputs:** the [Congress.gov API](https://api.congress.gov) and the [GovInfo API](https://api.govinfo.gov), both with a free [api.data.gov](https://api.data.gov/signup/) key in `DATA_GOV_API_KEY`, and product PDFs and HTML on www.congress.gov.

**Files:**

- [`crs_products/`](crs_products/README.md): the pipeline package
- [`tests/`](tests/README.md): offline tests
- [`.github/workflows/`](.github/workflows/README.md): the schedules
- [`pyproject.toml`](pyproject.toml): pinned dependencies; PDF text also needs poppler's `pdftotext`

**Try it:** `pip install .`, then `python -m crs_products run --local /tmp/out --partitions TE10 --max-units 3`, or `python -m crs_products run --dataset summaries --local /tmp/sum --partitions 119-sconres`.

**Incomplete API listings:** the products adapter makes bounded independent passes when pagination does not reconcile with the advertised count, page counts change, or the first page changes during the read. Passes are never unioned. If no pass reconciles, a products run waits 15 minutes and reads the listing again, up to six times (90 minutes, within its time budget), because the one on October 15, 2026 still failed at 18:16 UTC but reconciled at 19:45 UTC, and one scheduled run a day otherwise leaves the dataset a day behind. If no read reconciles, the sync records a degraded source check, retains all product rows and the last complete listing, and warns on Actions; it does not start a continuation run. The next scheduled probe follows the existing source-failure backoff. Live verification reports the comparison as unavailable rather than declaring stored rows "extra," and confirms apparently extra IDs through the detail endpoint before reporting removals. Hash, row and manifest failures still fail verification. A reconciled count and stable head are consistency checks, not a guarantee that the API supplied an atomic snapshot.

## License

MIT. See [`LICENSE`](LICENSE).
