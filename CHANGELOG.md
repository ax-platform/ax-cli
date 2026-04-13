# Changelog

## [0.4.0](https://github.com/ax-platform/ax-cli/compare/v0.3.1...v0.4.0) (2026-04-13)


### Features

* **listen:** respect backend kill-switch in ax listen mention gate ([#29](https://github.com/ax-platform/ax-cli/issues/29)) ([ace5aa9](https://github.com/ax-platform/ax-cli/commit/ace5aa9caf2fcf6e86fb6739c23057349a2469e0))
* token mint, user guardrail, config cleanup + upload fixes ([#43](https://github.com/ax-platform/ax-cli/issues/43)) ([9df4182](https://github.com/ax-platform/ax-cli/commit/9df4182597f16b1d182f8135923d987326330f38))


### Bug Fixes

* **auth:** require agent-bound tokens for channel replies ([e98bb7d](https://github.com/ax-platform/ax-cli/commit/e98bb7d5246f4af5138e5ab47b57101c4ac269dd))
* **cli:** use exchanged JWT for SSE watch/events commands ([#18](https://github.com/ax-platform/ax-cli/issues/18)) ([f7e4750](https://github.com/ax-platform/ax-cli/commit/f7e4750a3714fcc40d4885cc239e10713c0684e7))
* credential audience option ([#37](https://github.com/ax-platform/ax-cli/issues/37)) ([2cad6f8](https://github.com/ax-platform/ax-cli/commit/2cad6f8b6da1bfe9eefbe607144224c98fba20f3))
* **listen:** kill switch gate drops mentions instead of deferring ([#28](https://github.com/ax-platform/ax-cli/issues/28)) ([f0bb9a2](https://github.com/ax-platform/ax-cli/commit/f0bb9a22643730915c685c032fe311815185f620))
* **listen:** trust backend mentions array instead of content regex ([#31](https://github.com/ax-platform/ax-cli/issues/31)) ([f1be831](https://github.com/ax-platform/ax-cli/commit/f1be8315eee6ea52053fbd35869e0b39d858b1be))
* make context uploads and downloads space-safe ([#36](https://github.com/ax-platform/ax-cli/issues/36)) ([7bc60c5](https://github.com/ax-platform/ax-cli/commit/7bc60c54dceb8cdcc4740d37d9cb2e7435aecb49))
* resolve overlapping elements in profile fingerprint SVG ([#33](https://github.com/ax-platform/ax-cli/issues/33)) ([629e61f](https://github.com/ax-platform/ax-cli/commit/629e61fc95b290dc7f34f66e2dc0d92af07b02cc))


### Documentation

* add MCP docs (headless PAT + remote OAuth 2.1); remove internal files ([#38](https://github.com/ax-platform/ax-cli/issues/38)) ([dfd1d99](https://github.com/ax-platform/ax-cli/commit/dfd1d9937b49a6e2f66a872cb81f5a965fdb576c))
* **examples:** add runnable hermes_sentinel integration example ([#27](https://github.com/ax-platform/ax-cli/issues/27)) ([914d9fe](https://github.com/ax-platform/ax-cli/commit/914d9fed5115a4ecba0bbc5506f6a440d7330911))
* land AX-SCHEDULE-001 spec + remove CIPHER_TEST cruft ([#26](https://github.com/ax-platform/ax-cli/issues/26)) ([0ab335b](https://github.com/ax-platform/ax-cli/commit/0ab335b0a31692a89f6f5f1cf2dc6b67b11f7ea7))
* scrub internal agent names from README ([#34](https://github.com/ax-platform/ax-cli/issues/34)) ([ba6f3b2](https://github.com/ax-platform/ax-cli/commit/ba6f3b20035e8795848f2bfd8f9e60405409ebdf))
* update README for new user onboarding ([#32](https://github.com/ax-platform/ax-cli/issues/32)) ([1d38e01](https://github.com/ax-platform/ax-cli/commit/1d38e01bab16e7baddbb7f69c2156e9be589fb6c))

## Changelog

All notable changes to `axctl` are tracked here.

This project uses [Conventional Commits](https://www.conventionalcommits.org/)
and Release Please to generate release PRs, version bumps, and changelog entries.
