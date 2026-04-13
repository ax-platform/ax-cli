# Contributing

Thanks for improving `axctl`. This repository is public-facing, so changes
should be easy for operators to understand, test, and release.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v --tb=short
ruff check ax_cli/
ruff format --check ax_cli/
python -m build
```

Use `pipx install axctl` for normal CLI use. Use editable installs only for
local development.

## Branches

- `dev/staging` is the fast integration branch.
- `main` is the public release branch.
- Promotion to `main` should happen through a reviewed PR.

## Commit Style

Use Conventional Commits so Release Please can generate the changelog and
version bump correctly:

- `fix:` for compatible bug fixes.
- `feat:` for user-visible CLI capability.
- `docs:`, `test:`, `ci:`, `chore:`, and `style:` for non-release metadata.
- Use `!` or a `BREAKING CHANGE:` footer only when the operator-facing contract
  changes incompatibly.

## Auth And Credentials

`axctl` handles user PATs, agent PATs, exchanged JWTs, and profile metadata.
Treat identity boundaries as part of the product contract:

- Do not log raw tokens.
- Do not use user PATs as long-running agent credentials.
- Agent-authored sends should use agent-bound credentials.
- User PATs are bootstrap credentials used to establish trust and mint scoped
  credentials.
- Update tests and docs for any token, profile, JWT, or identity behavior
  change.

## Release Process

See [docs/release-process.md](docs/release-process.md).

The short version:

1. Land work in `dev/staging`.
2. Promote `dev/staging` to `main`.
3. Release Please opens a release PR.
4. Merge the release PR after reviewing the version and changelog.
5. GitHub Release publication triggers PyPI publishing.
