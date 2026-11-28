# workflows

**Objective:** keep the dataset current with no personal device and no person, given a commit every 60 days. The workflow is scheduled daily at 12:37 UTC. It uses minute 37 because GitHub names the start of every hour as busy. GitHub starts scheduled runs late and sometimes [drops them](https://huggingface.co/datasets/incrediblecrab/us-federal-reserve-beige-book#how-it-stays-current).

**Schedule history:** On October 12, 2026 the owner asked for one daily update. From September 27 to October 15, 2026 GitHub started every one of the 208 scheduled runs of the four dataset pipelines in this account late, by 2.4 to 9.2 hours, and the 18 daily runs scheduled at 14:00 UTC from October 13 by 3.9 to 6.2 hours. The daily cron requested 12:37 UTC to target a start near 17:00 UTC. From November 9 to 22, 2026 GitHub started this repository's daily runs 5.8 to 10.1 hours after 12:37 UTC. On November 23, 2026 the owner asked for a run every 6 hours; from then to November 27 GitHub started 9 of the 19 scheduled runs of each pipeline. On November 27, 2026 the owner asked for a run every 12 hours. On November 28, 2026 the owner asked for one daily update again, at 12:37 UTC.

**Inputs:** the job's OIDC token, which Hugging Face exchanges for a short-lived write token when the dataset lists this repository and workflow as a Trusted Publisher; otherwise the run fails. No secret is used.

**Files:**

- [`fed-pipeline.yml`](../.github/workflows/fed-pipeline.yml): `probe` reads the Board's landing page and the card's metadata. When needed, `run` syncs; `verify --live` and `squash` follow a commit. `continue` starts the next run when one ran out of budget mid-fetch. `inactivity` fails after 50 days without a commit, before GitHub disables every schedule here at 60. It makes no keepalive commit: GitHub [called](https://github.com/ddev/github-action-add-on-test/issues/46) that a Terms violation.
- [`fed-test.yml`](../.github/workflows/fed-test.yml): runs the test suite on pushes to `main` and pull requests that change `fed/` or this workflow, with read-only contents and no OIDC token, so it cannot publish.

GitHub reports a failed scheduled run to whoever last changed its cron, by email if their settings allow. A dispatch with `args` runs only a bounded test. The root's `.github/` has no README, which GitHub would show instead of the repository's.
