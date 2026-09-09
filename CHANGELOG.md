# Changelog

All notable public changes are documented here. This log covers only the sanitized public showcase and intentionally excludes private research history.

## [Unreleased]

### Changed

- The README now separates the stable showcase release from the latest experimental prerelease and states what the experimental one has not established, instead of naming a single current public version.
- The public release rules and security boundary now state that only `main` and `v*` tags reach the public repository.

## [0.2.0-experimental.1] - 2026-09-08

Opt-in experimental prerelease. Not stable, not generally available, not production-ready. Published on the degraded release path, meaning the release proceeded with parts of its planned validation unfinished and disclosed rather than with those parts passing.

### Added

- Candidate repair, the Vitest adapter and the Jest adapter ship disabled by default. They run only when explicitly enabled, against a repository you trust, in an isolated environment.

### Changed

- The README carries a current-status table sourced from the capability registry, states the verified Python and Node.js versions, and documents the Chromium and virtual-environment prerequisites of the end-to-end check.

### Not established by this release

- Third-party repository compatibility. Both public-matrix method batches were invalidated before the formal six-repository denominator was frozen, so no fixed six-repository sample and no formal product-on comparison exist. The release makes no compatibility claim for pytest, Vitest or Jest repositories, and does not read a precheck failure as product incompatibility.
- The UI comprehension probe. Its batch was environment-blocked and never executed. Zero executed sessions is not a zero-event result, and the ten planned AI-and-Skill synthetic sessions are not human participants and are not a human-factors finding.
- Independent hidden QA, an independent Linux host run, an authorized private-repository pilot, a real-developer study, and manual security sign-off. All five remain open and are required before any stable promotion.

### Security

- Report vulnerabilities through the repository's private vulnerability reporting channel. The experimental release carries no fixed remediation-time commitment; maintainers may withdraw the release or disable an affected capability before shipping a fix.

## [0.1.1] - 2026-09-02

### Security

- Timed-out public commands now terminate the whole child process group instead of the direct child only, so descendants are no longer left running after a timeout.

### Added

- Public Playwright flow covering approval, real fixture execution, named regression output, and repository path rejection.
- Public architecture overview documenting responsibilities, evidence lifecycle, and resource-limit boundaries.
- CI and release workflows now install Chromium and run the browser gate.
- Review bundles now carry a `context.resource_policy` block recording which execution limits are enforced and which are explicitly not enforced.

### Changed

- Downloaded review bundles are now named `shuimu-yanma-review-<review_id>.json` instead of `modou-review-<review_id>.json`. Scripts matching the old filename need updating.
- The local control-plane title, UI strings, and frontend package metadata now use the Shuimu Yanma name. The importable Python package is still `modou/`.

## [0.1.0] - 2026-09-01

### Added

- First formal public showcase release under Apache-2.0.
- Shuimu Yanma branding and version identity in the local UI.
- Runnable capability registry required by the local service.
- Public/private boundary, security policy, contribution guide, and release checks.
- GitHub Actions checks for the Python smoke test, privacy boundary, frontend tests, and production build.

### Fixed

- Documented commands now match files that exist in the public repository.
- The public service can start without relying on private configuration files.
- The frontend test command now has a maintained smoke suite.
