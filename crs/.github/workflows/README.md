# workflows

**Objective:** keep the three datasets current with no personal device and no person, given a commit every 60 days. Each workflow is scheduled at 12:37 UTC so that it starts near 17:00 UTC (12:00 EST, 13:00 EDT), because GitHub starts runs late or [drops them](https://huggingface.co/datasets/incrediblecrab/us-crs-research-papers#how-it-stays-current). From September 27 to October 15, 2026 GitHub started every one of the 208 scheduled runs of the four dataset pipelines in this account late, by 2.4 to 9.2 hours, and the 18 daily runs scheduled at 14:00 UTC from October 13 by 3.9 to 6.2 hours, so the cron asks for 12:37 UTC to have them start near 17:00 UTC. A run GitHub starts on time starts at 12:37 UTC (7:37 EST, 8:37 EDT).

**Inputs:** the secret `DATA_GOV_API_KEY`, and each job's OIDC token, which Hugging Face exchanges for a short-lived write token when the dataset lists this repository and workflow as a Trusted Publisher; otherwise the run fails.

**Files:**

- [`pipeline.yml`](pipeline.yml), products: `probe` makes one API request. When needed, `run` syncs; `verify --live` and `squash` follow a commit. `continue` starts the next run when one ran out of budget mid-fetch. `inactivity` fails after 50 days without a commit, before GitHub disables every schedule here at 60. It makes no keepalive commit: GitHub [called](https://github.com/ddev/github-action-add-on-test/issues/46) that a Terms violation.
- [`summaries.yml`](summaries.yml), bill summaries: `run`, then `verify` and `squash` after a commit; `continue` as above.
- [`constitution.yml`](constitution.yml), Constitution Annotated: `run`, then `verify --live` and `squash` after a commit.
- [`test.yml`](test.yml): runs the test suite on every push to `main` and every pull request, with read-only contents and no OIDC token, so it cannot publish.

GitHub reports a failed scheduled run to whoever last changed its cron, by email if their settings allow. A dispatch with `args` runs only a bounded test. `.github/` has no README, which GitHub would show instead of the repository's.
