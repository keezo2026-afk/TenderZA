# CI workflow (manual install required)

GitHub blocks this repo's app connection from writing workflow files
(`workflows` permission not granted). To enable CI, move the workflow into
place yourself:

```bash
mkdir -p .github/workflows
git mv ci/github-ci.yml .github/workflows/ci.yml
git commit -m "Enable CI workflow"
git push
```

Or paste `ci/github-ci.yml` into `.github/workflows/ci.yml` via the GitHub
web UI. The workflow runs ruff + pytest, then applies `db/schema.sql` and the
registry seeder against a real pgvector Postgres service.
