# Changelog

All notable public changes are documented here. This log covers only the sanitized public showcase and intentionally excludes private research history.

## [Unreleased]

### Changed

- README now carries a current-status table sourced from the capability registry, states the verified Python and Node.js versions, and documents the Chromium and virtual-environment prerequisites of the end-to-end check.
- The public release rules and security boundary now state that only `main` and `v*` tags reach the public repository.

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
