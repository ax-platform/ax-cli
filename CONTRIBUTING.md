# Contributing to ax-gateway

Thanks for improving ax-gateway / `axctl`. This repository is public-facing, so
changes should be easy for operators to understand, test, and release.

## Code of Conduct

This project is governed by our [Code of Conduct](./CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it. Report unacceptable behavior to
**support@ax-platform.com**.

## Getting Started

### Prerequisites

- **Python 3.13+**
- **Git**

### Fork & Clone

1. Fork this repository on GitHub
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ax-gateway.git
   cd ax-gateway
   ```
3. Add upstream remote:
   ```bash
   git remote add upstream https://github.com/ax-platform/ax-gateway.git
   ```

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

- `fix:` for compatible bug fixes
- `feat:` for user-visible CLI capability
- `docs:`, `test:`, `ci:`, `chore:`, and `style:` for non-release metadata
- Use `!` or a `BREAKING CHANGE:` footer only when the operator-facing contract
  changes incompatibly

## Security and Credentials

`axctl` handles user PATs, agent PATs, exchanged JWTs, and profile metadata.
Treat identity boundaries as part of the product contract:

- Do not log raw tokens.
- Do not use user PATs as long-running agent credentials.
- Agent-authored sends should use agent-bound credentials.
- User PATs are bootstrap credentials used to establish trust and mint scoped
  credentials.
- Update tests and docs for any token, profile, JWT, or identity behavior
  change.

## Contributor License Agreement (CLA)

Before we can accept your first pull request, you must sign our CLA. This is
handled automatically by the CLA Assistant bot on GitHub. When you open your
first PR, the bot will post a comment with a sign link. You only need to sign
once. Without a signed CLA, the PR cannot be merged.

## Pull Request Guidelines

Before submitting:

- Code runs without errors
- `pytest tests/ -v --tb=short` passes
- `ruff check ax_cli/` and `ruff format --check ax_cli/` pass
- `python -m build` succeeds
- No sensitive data committed
- Branch is up to date with target branch

## Release Process

See [docs/release-process.md](docs/release-process.md).

The short version:

1. Land work in `dev/staging`.
2. Promote `dev/staging` to `main`.
3. Release Please opens a release PR.
4. Merge the release PR after reviewing the version and changelog.
5. GitHub Release publication triggers PyPI publishing.

## Community & Support

- **GitHub Issues**: [Report bugs or request features](https://github.com/ax-platform/ax-gateway/issues)
- **Security Vulnerabilities**: See [SECURITY.md](./SECURITY.md) — do not open a public issue

## License

By contributing to ax-gateway, you agree that your contributions will be
licensed under the **MIT License**.
