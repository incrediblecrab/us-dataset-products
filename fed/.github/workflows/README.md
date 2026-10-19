# workflows

**Objective:** keep the dataset current with no personal device and no person, given a commit every 60 days. The workflow is scheduled at 12:37 UTC so that it starts near 17:00 UTC (12:00 EST, 13:00 EDT), because GitHub starts runs late or [drops them](https://huggingface.co/datasets/incrediblecrab/us-federal-reserve-beige-book#how-it-stays-current). From September 27 to October 15, 2026 GitHub started every one of the 208 scheduled runs of the four dataset pipelines in this account late, by 2.4 to 9.2 hours, and the 18 daily runs scheduled at 14:00 UTC from October 13 by 3.9 to 6.2 hours, so the cron asks for 12:37 UTC to have them start near 17:00 UTC. A run GitHub starts on time starts at 12:37 UTC (7:37 EST, 8:37 EDT).

**Inputs:** the job's OIDC token, which Hugging Face exchanges for a short-lived write token when the dataset lists this repository and workflow as a Trusted Publisher; otherwise the run fails. No secret is used.

**Files:**

- [`pipeline.yml`](pipeline.yml): `probe` reads the Board's landing page and the card's metadata. When needed, `run` syncs; `verify --live` and `squash` follow a commit. `continue` starts the next run when one ran out of budget mid-fetch. `inactivity` fails after 50 days without a commit, before GitHub disables every schedule here at 60. It makes no keepalive commit: GitHub [called](https://github.com/ddev/github-action-add-on-test/issues/46) that a Terms violation.
- [`test.yml`](test.yml): runs the test suite on every push to `main` and every pull request, with read-only contents and no OIDC token, so it cannot publish.

GitHub reports a failed scheduled run to whoever last changed its cron, by email if their settings allow. A dispatch with `args` runs only a bounded test. `.github/` has no README, which GitHub would show instead of the repository's.
