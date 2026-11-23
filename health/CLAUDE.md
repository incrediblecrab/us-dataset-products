# Health check

Run these commands from the repository root with Python 3.14.

```bash
python health/health.py    # live, read-only; needs GITHUB_TOKEN, GITHUB_REPOSITORY and full git history
python -m pytest -q health # offline; uses fake HTTP and temporary git repositories
```
