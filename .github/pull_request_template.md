## Summary

What changed and why?

## Validation

- [ ] `pytest tests/ -v --tb=short`
- [ ] `ruff check ax_cli/`
- [ ] `ruff format --check ax_cli/`
- [ ] `python -m build && twine check dist/*`
- [ ] Live aX smoke test, if this changes auth, messages, uploads, listeners, or release behavior

## Release Notes

- [ ] This should appear in the changelog (`feat:`, `fix:`, or breaking change)
- [ ] This is internal/docs/test-only and does not need a package release

## Credential / Auth Impact

- [ ] No token, profile, PAT, JWT, or agent identity behavior changed
- [ ] Auth behavior changed and the docs/tests were updated
